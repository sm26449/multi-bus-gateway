#!/usr/bin/env python3
# Serial-bridge supervisor (Faza B).
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Manages ser2net dynamically so serial adapters appear/disappear on hotplug.

- Enumerates USB serial adapters straight from sysfs (robust — no reliance on
  udev-enriched properties, which may be absent inside a container), and the
  board's own UARTs (SC16IS7xx HAT channels ttySC*, ttyAMA*, real ttyS*,
  ttymxc*, ttyO*) keyed by the device behind them. Every port carries a
  status: ok, busy, no-access (the container may not open it — the device
  rule it needs is reported), reserved (kernel console, Bluetooth) or
  excluded. Only `ok` ports are ever handed to ser2net.
- Assigns each a STABLE TCP port from a persistent map, keyed by USB serial
  number (FTDI/CP210x) or, as a fallback for serial-less adapters (cheap CH340),
  by physical USB port-path. Same adapter → same port across replug/restart.
- Regenerates the ser2net config and reloads ser2net (SIGHUP — only changed
  ports restart), one TCP client per adapter (whether ser2net's open is
  exclusive on real hardware is checked on the bench: FINDINGS BR-E1b-2).
- Reacts to hotplug via kernel uevents (pyudev, debounced) with a periodic
  reconcile as a robust safety net.
- Exposes a small control API (docs/serial-bridge-api.md): GET /adapters for
  MBG to scan the live inventory, GET /health, and POST /adapters/<key>/serial
  for per-adapter serial parameters; per served port, line and transport
  health (GET /adapters/<key>/stats, GET /events — ADR-0017). With
  BRIDGE_TOKEN set, every endpoint but
  /health requires `Authorization: Bearer <token>` — the bridge can then run on
  another host (a Pi next to the RS-485 bus) and be added to a gateway over
  the LAN.
"""
from __future__ import annotations

import errno
import fcntl
import glob
import hmac
import json
import os
import re
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit


def _load_serial_ports():
    """The shared port enumerator, from beside this file (/app in the image,
    serial-bridge/ in the repo) whatever the importer's sys.path is."""
    import importlib.util
    import sys
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "serial_ports.py")
    mod = sys.modules.get("mbg_bridge_serial_ports")
    if mod is None or getattr(mod, "__file__", "") != path:
        spec = importlib.util.spec_from_file_location("mbg_bridge_serial_ports", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["mbg_bridge_serial_ports"] = mod
        spec.loader.exec_module(mod)
    return mod


_sp = _load_serial_ports()

# Bumped by the build (Dockerfile ARG BRIDGE_VERSION → env); "dev" for a
# local build without the arg.
VERSION = os.environ.get("BRIDGE_VERSION", "").strip() or "dev"
STATE_FILE = os.environ.get("BRIDGE_STATE", "/data/portmap.json")
# Lives in its own directory (owned by the non-root user) because the atomic
# rewrite (tmp + os.replace) needs write permission on the DIRECTORY.
SER2NET_CFG = os.environ.get("SER2NET_CFG", "/etc/ser2net/ser2net.yaml")
# Data-port range. A remote bridge publishes exactly this range on its host,
# so the two must agree (docker-compose.bridge.yml wires both from one var).
PORT_LOW = int(os.environ.get("BRIDGE_PORT_LOW", "7001"))
PORT_HIGH = int(os.environ.get("BRIDGE_PORT_HIGH", "7099"))
CONTROL_PORT = int(os.environ.get("BRIDGE_CONTROL_PORT", "7000"))
# Shared secret for the control API. Unset/empty = no auth (same-host
# deployments, where compose binds the API to 127.0.0.1 + a private network).
TOKEN = os.environ.get("BRIDGE_TOKEN", "").strip()
# Adapters to NEVER expose — claimed by another service (e.g. a BMS on
# its own container). Comma-separated; each token matches a stable_id, dev path,
# or USB port-path. Excluded adapters are LISTED (available=false) but never get
# a ser2net connection, so their serial line is never opened by the bridge.
EXCLUDE = {t.strip() for t in os.environ.get("BRIDGE_EXCLUDE", "").split(",") if t.strip()}
# The SoC's own UARTs (ttyAMA, real ttyS, ttymxc, ttyO) are served only when
# opted in — here (same tokens as BRIDGE_EXCLUDE) or per port through
# POST /adapters/<key>/enable. A program holding such a port without an
# exclusive lock (gpsd, a getty, a direct mapping) cannot be detected, so the
# bridge never takes one on its own. RS-485 HAT channels (ttySC*) need no opt-in.
INCLUDE = {t.strip() for t in os.environ.get("BRIDGE_INCLUDE", "").split(",") if t.strip()}
# Where /sys, /dev and /proc are read from. "/" in a container; the tests point
# it at a fake tree so enumeration runs with no hardware.
SYSROOT = os.environ.get("BRIDGE_SYSROOT", "/") or "/"
# What to tell an operator whose port the container may not open (status
# no-access). Each compose file sets its own form (scripts/serial-access.sh);
# unset — a plain `docker run` — the hint is the exact --device-cgroup-rule.
ACCESS_FIX = os.environ.get("BRIDGE_ACCESS_FIX", "").strip()
# The compose files' four device-rule slots (BRIDGE_TTY_1..4, filled by
# scripts/serial-access.sh) and the driver each was written for, so a slot left
# stale by a dynamic-major change is reported, not silently wrong.
TTY_SLOTS = os.environ.get("BRIDGE_TTY_SLOTS", "").split()
TTY_DRIVERS = dict(x.split(":", 1) for x in os.environ.get("BRIDGE_TTY_DRIVERS", "").split(",")
                   if ":" in x)

# ---- serial parameters -----------------------------------------------------
BAUD_RATES = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)
PARITIES = ("N", "E", "O")
DATABITS = (7, 8)
STOPBITS = (1, 2)
SERIAL_FIELDS = ("baud", "parity", "databits", "stopbits")
# Optional per-adapter flag: drive the transceiver's direction from RTS in the
# kernel (TIOCSRS485), for boards whose RS-485 chip does not switch by itself
# (some SC16IS7xx HATs). Off by default; board UARTs only. Held in memory with
# the serial params, but stored under its OWN key (RS485_STATE_KEY): a bridge
# older than it validates `__serial__` entries strictly and would drop the
# whole entry — baud and parity with it — on meeting an unknown field.
RS485_FIELD = "rs485"
# The gensio serialdev rs485 option it renders to:
# <delay before send ms>:<delay after send ms>[:rts_on_send][:rts_after_send]
# [:rx_during_tx][:terminate_bus]. RTS high while sending is what most boards
# wire; BRIDGE_RS485 changes it for one that wires it the other way.
_RS485_RE = re.compile(r"\d{1,4}:\d{1,4}(:(rts_on_send|rts_after_send|rx_during_tx|terminate_bus))*")
# Reserved portmap.json key holding per-adapter serial overrides. Ports stay
# flat top-level `stable_id: port` entries, exactly as older versions wrote
# them — so an older bridge (downgrade) still reads its ports and only drops
# this one entry as "invalid".
SERIAL_STATE_KEY = "__serial__"
RS485_STATE_KEY = "__rs485__"                    # [stable_id, ...] with rs485 on
ENABLED_STATE_KEY = "__enabled__"                # [stable_id, ...] SoC UARTs opted in
_STATE_KEYS = {SERIAL_STATE_KEY, RS485_STATE_KEY, ENABLED_STATE_KEY}


def _as_int(v, field: str) -> int:
    if isinstance(v, bool):
        raise ValueError(f"{field}: expected a number")
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    raise ValueError(f"{field}: expected a number, got {v!r}")


def validate_serial(body, base: dict) -> dict:
    """Merge `body` (any subset of baud/parity/databits/stopbits/rs485) onto
    `base` and validate the result. Raises ValueError with an
    operator-readable message; returns a normalised dict with the four line
    fields, plus `rs485: True` only while the RS-485 mode is on."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    unknown = set(body) - set(SERIAL_FIELDS) - {RS485_FIELD}
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    out = dict(base)
    if RS485_FIELD in body:
        if not isinstance(body[RS485_FIELD], bool):
            raise ValueError("rs485: expected true or false")
        out[RS485_FIELD] = body[RS485_FIELD]
    if "baud" in body:
        out["baud"] = _as_int(body["baud"], "baud")
    if "parity" in body:
        par = body["parity"]
        if not isinstance(par, str):
            raise ValueError("parity: expected one of N, E, O")
        out["parity"] = par.strip().upper()[:1] if par.strip() else ""
    if "databits" in body:
        out["databits"] = _as_int(body["databits"], "databits")
    if "stopbits" in body:
        out["stopbits"] = _as_int(body["stopbits"], "stopbits")
    if out.get("baud") not in BAUD_RATES:
        raise ValueError(f"baud: must be one of {', '.join(map(str, BAUD_RATES))}")
    if out.get("parity") not in PARITIES:
        raise ValueError("parity: must be N, E or O")
    if out.get("databits") not in DATABITS:
        raise ValueError("databits: must be 7 or 8")
    if out.get("stopbits") not in STOPBITS:
        raise ValueError("stopbits: must be 1 or 2")
    if not isinstance(out.get(RS485_FIELD, False), bool):
        raise ValueError("rs485: expected true or false")
    norm = {k: out[k] for k in SERIAL_FIELDS}
    if out.get(RS485_FIELD):
        norm[RS485_FIELD] = True
    return norm


def parse_serial_params(text: str) -> dict:
    """ser2net-style "9600n81" → {baud, parity, databits, stopbits}."""
    t = (text or "").strip().lower()
    if len(t) < 4 or not t[:-3].isdigit():
        raise ValueError(f"bad serial params {text!r} (expected e.g. 9600n81)")
    return validate_serial({"baud": t[:-3], "parity": t[-3],
                            "databits": t[-2], "stopbits": t[-1]},
                           {})


def ser2net_params(p: dict) -> str:
    """{9600, N, 8, 1} → "9600n81" (the ser2net serialdev option)."""
    return f"{p['baud']}{p['parity'].lower()}{p['databits']}{p['stopbits']}"


def serial_text(p: dict) -> str:
    """Short human form: "9600 8N1" ("9600 8N1 RS-485" with the kernel's
    RS-485 mode on)."""
    text = f"{p['baud']} {p['databits']}{p['parity']}{p['stopbits']}"
    return text + " RS-485" if p.get(RS485_FIELD) else text


def _rs485_spec() -> str:
    raw = os.environ.get("BRIDGE_RS485", "").strip() or "0:0:rts_on_send"
    if not _RS485_RE.fullmatch(raw):
        print(f"[supervisor] BRIDGE_RS485 invalid ({raw!r}) — using 0:0:rts_on_send",
              flush=True)
        return "0:0:rts_on_send"
    return raw


RS485_SPEC = _rs485_spec()


def _default_serial() -> dict:
    raw = os.environ.get("BRIDGE_SERIAL_PARAMS", "9600n81")
    try:
        return parse_serial_params(raw)
    except ValueError as e:
        print(f"[supervisor] BRIDGE_SERIAL_PARAMS invalid ({e}) — using 9600n81",
              flush=True)
        return parse_serial_params("9600n81")


# Default for every adapter without a stored override.
DEFAULT_SERIAL = _default_serial()


def check_token(auth_header: str | None, token: str) -> bool:
    """Bearer-token check, constant-time. No configured token = open."""
    if not token:
        return True
    if not auth_header:
        return False
    scheme, _, value = auth_header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(value.strip().encode(), token.encode())


def _hostname() -> str:
    """Which machine this bridge is — BRIDGE_HOSTNAME, else the host's
    /etc/hostname when mounted (docker-compose.bridge.yml), else the
    container's own hostname."""
    name = os.environ.get("BRIDGE_HOSTNAME", "").strip()
    if name:
        return name
    host_file = _read(os.environ.get("BRIDGE_HOST_HOSTNAME_FILE", "/etc/host-hostname"))
    return host_file or socket.gethostname()


def _is_excluded(a: dict) -> bool:
    return bool(EXCLUDE & {a["stable_id"], a["dev"], a["port_path"], a["name"]})


_lock = threading.RLock()     # POST /serial reconciles while holding it
_adapters: list[dict] = []
_unserved: set = set()                           # last logged (name, status, reason)
_ser2net: subprocess.Popen | None = None


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def _sys(path: str) -> str:
    """A host path (/sys/..., /dev/..., /proc/...) under BRIDGE_SYSROOT."""
    return os.path.join(SYSROOT, path.lstrip("/"))


# The enumeration itself is the gateway's (multibus/serial_ports.py, ADR-0028),
# shipped beside this file as serial_ports.py so the bridge and the gateway
# list the same ports; this keeps the bridge's adapter shape and ids.
BOARD_TTY = _sp.BOARD_TTY


def console_ttys() -> dict:
    """tty name → why it is a console: `console=` on the kernel command line,
    and what /sys/class/tty/console/active lists."""
    return _sp.console_ttys(SYSROOT)


def _adapter(p: dict) -> dict:
    """A port from the shared enumerator, in the bridge's adapter shape."""
    if p["kind"] == "usb":
        # stable key: unique serial if the adapter has one, else USB port-path
        # — exactly what earlier versions keyed the port map with
        stable = p["serial"] if p["serial"] else f"{p['vendor_id']}:{p['product_id']}@{p['port_path']}"
        return {
            "dev": p["path"], "name": p["name"], "vendor_id": p["vendor_id"],
            "product_id": p["product_id"], "serial": p["serial"],
            "manufacturer": p["manufacturer"], "model": p["model"], "port_path": p["port_path"],
            "stable_id": stable, "kind": "usb", "driver": p["driver"],
            "label": p["label"], "dev_num": p["dev_num"],
        }
    # a board UART is keyed by the device behind it (its bus address) plus the
    # channel when that device carries several — never by the tty name alone,
    # which the kernel may hand out differently on the next boot
    a = {
        "dev": p["path"], "name": p["name"], "vendor_id": "", "product_id": "",
        "serial": "", "manufacturer": "", "model": "", "port_path": p["port_path"],
        "stable_id": (f"{p['port_path']}:{p['channel']}" if p["channel"] is not None
                      else p["port_path"]),
        "kind": "board", "driver": p["driver"], "label": p["label"], "dev_num": p["dev_num"],
        # an add-on RS-485 chip (SC16IS7xx) is there for a bus; a SoC UART
        # may be anything's (GPS, a getty, a modem): opt-in only
        "needs_enable": p["kind"] != "hat",
    }
    if p["bluetooth"]:
        a.update(status="reserved",
                 reason="Bluetooth UART (the board's Bluetooth controller is on it)")
    return a


def enumerate_adapters() -> list[dict]:
    """Every serial port of this host the bridge can serve: USB adapters
    (ttyUSB/ttyACM with a node in /dev) first, unchanged ids, then the
    board's own UARTs. Phantoms are left out; Bluetooth UARTs come back
    `reserved`, as does any kernel console — a USB one too (console=ttyUSB0);
    the SoC's own UARTs carry `needs_enable` (served only when opted in)."""
    consoles = console_ttys()
    ports = _sp.enumerate_ports(SYSROOT)
    usb = [p for p in ports if p["kind"] == "usb" and _sp.USB_TTY.match(p["name"]) and p["in_dev"]]
    board = [p for p in ports if p["kind"] in ("hat", "board_uart")]
    out = [_adapter(p) for p in usb + board]
    for a in out:
        if a["name"] in consoles:
            a.update(status="reserved", reason=consoles[a["name"]])
    return out


_open = os.open                                 # the tests swap it for a fake
# stable_id → the probe's verdict: a board port is opened to look ONCE per
# bridge lifetime (each open raises DTR/RTS — on an RS-485 transceiver wired
# to RTS that is a glitch on the bus), never again per reconcile nor while
# ser2net restarts. Recreating the container (as the fixes do) re-checks.
_probed: dict = {}


def _access_fix(a: dict) -> str:
    """What fixes a no-access port: the compose files' script, or — a plain
    `docker run` (BRIDGE_ACCESS_FIX unset) — the exact device rule to add."""
    if ACCESS_FIX:
        return ACCESS_FIX
    major, _, minor = (a.get("dev_num") or "?:?").partition(":")
    rule = f"c 4:{minor} rmw" if major == "4" else f"c {major}:* rmw"
    return f"recreate the container with --device-cgroup-rule='{rule}'"


def probe_access(a: dict) -> dict:
    """Can the bridge take this port? Opens it non-blocking and tries the
    exclusive lock, then lets go at once, with HUPCL cleared so the close
    does not drop DTR. → {status, reason, fix, needs_major}. Never called for
    a USB adapter (served as before), a reserved, excluded or not-enabled
    port (a console is never opened), nor for one ser2net already holds.

    Only an EXCLUSIVE holder (TIOCEXCL, flock) is seen as busy: a program
    that opened the port plainly (gpsd, a getty, pyserial's default) is not
    detectable — which is why the SoC's UARTs need an explicit opt-in."""
    major = (a.get("dev_num") or "").split(":")[0]
    res = {"status": "ok", "reason": "", "fix": "", "needs_major": None}
    try:
        fd = _open(_sys(a["dev"]), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as e:
        if e.errno == errno.EBUSY:
            return {**res, "status": "busy",
                    "reason": "held by another program (exclusive open)"}
        if e.errno == errno.EPERM:
            # the device cgroup: the container has no rule for this major
            return {**res, "status": "no-access",
                    "reason": f"the container may not open device major {major or '?'}",
                    "fix": _access_fix(a),
                    "needs_major": int(major) if major.isdigit() else None}
        if e.errno == errno.EACCES:
            return {**res, "status": "no-access",
                    "reason": f"permission denied on {a['dev']}: the node must be "
                              "group dialout, mode 0660",
                    "fix": f'a udev rule on the host: KERNEL=="{a["name"]}", '
                           'GROUP="dialout", MODE="0660"'}
        if e.errno == errno.ENOENT:
            return {**res, "status": "no-access",
                    "reason": f"{a['dev']} is not in the container (is /dev mounted?)"}
        return {**res, "status": "no-access",
                "reason": f"cannot open {a['dev']}: {os.strerror(e.errno or 0)}"}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {**res, "status": "busy", "reason": "locked by another program"}
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        _keep_lines(fd)
        os.close(fd)
    return res


def _keep_lines(fd: int) -> None:
    """Clear HUPCL so closing the probe does not drop DTR/RTS (not a tty —
    the tests' fake nodes — is fine)."""
    try:
        import termios
        attrs = termios.tcgetattr(fd)
        if attrs[2] & termios.HUPCL:
            attrs[2] &= ~termios.HUPCL
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:  # noqa: BLE001
        pass


def parse_state(raw) -> tuple[dict, dict]:
    """portmap.json content → (ports, serial overrides). Pure; migrates files
    written by older versions (a flat `stable_id: port` map, no serial
    section): missing serial entries simply mean "use the default".

    Port entries are validated as unique ints in the data-port range; serial
    overrides are re-validated, and a broken one is dropped LOUDLY (the
    adapter falls back to the default rather than ser2net getting a garbage
    config line)."""
    if not isinstance(raw, dict):
        raise ValueError("portmap is not a JSON object")
    ports: dict = {}
    for k, v in raw.items():
        if k in _STATE_KEYS:
            continue
        if (isinstance(v, int) and not isinstance(v, bool)
                and PORT_LOW <= v <= PORT_HIGH and v not in ports.values()):
            ports[str(k)] = v
        else:
            print(f"[supervisor] portmap entry dropped (invalid): {k}={v!r}", flush=True)
    serial: dict = {}
    sraw = raw.get(SERIAL_STATE_KEY) or {}
    if not isinstance(sraw, dict):
        print(f"[supervisor] serial overrides dropped (not an object): {sraw!r}", flush=True)
        sraw = {}
    for k, v in sraw.items():
        try:
            serial[str(k)] = validate_serial(v, DEFAULT_SERIAL)
        except ValueError as e:
            print(f"[supervisor] serial override dropped for {k} ({e}) — "
                  f"using {serial_text(DEFAULT_SERIAL)}", flush=True)
    for k in _key_list(raw, RS485_STATE_KEY):
        serial[k] = {**serial.get(k, DEFAULT_SERIAL), RS485_FIELD: True}
    return ports, serial


def _key_list(raw: dict, key: str) -> list:
    val = raw.get(key) or []
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        print(f"[supervisor] {key} dropped (not a list of keys): {val!r}", flush=True)
        return []
    return val


def parse_enabled(raw) -> set:
    """The SoC UARTs an operator opted in (POST /adapters/<key>/enable)."""
    return set(_key_list(raw, ENABLED_STATE_KEY)) if isinstance(raw, dict) else set()


def dump_state(ports: dict, serial: dict, enabled=()) -> dict:
    """Inverse of parse_state (+ parse_enabled). Ports stay top-level (older
    versions can still read them); the line settings go under `__serial__`
    exactly as older versions read them, rs485 and the opt-ins under keys of
    their own, each omitted when empty."""
    out = dict(ports)
    if serial:
        out[SERIAL_STATE_KEY] = {k: {f: v[f] for f in SERIAL_FIELDS}
                                 for k, v in sorted(serial.items())}
        rs = sorted(k for k, v in serial.items() if v.get(RS485_FIELD))
        if rs:
            out[RS485_STATE_KEY] = rs
    if enabled:
        out[ENABLED_STATE_KEY] = sorted(enabled)
    return out


def serial_for(stable_id: str, serial: dict) -> dict:
    """Effective serial params of an adapter: its override, else the default."""
    return dict(serial.get(stable_id) or DEFAULT_SERIAL)


def _load_state() -> tuple[dict, dict]:
    """Port map + serial overrides from disk. A corrupt/unreadable map is LOUD
    (audit DP-30): silently starting from {} re-allocates ports in enumeration
    order, which can hand a well-known port to the wrong adapter — MBG would
    then poll a different physical bus."""
    if not os.path.exists(STATE_FILE):
        return {}, {}
    try:
        with open(STATE_FILE) as f:
            return parse_state(json.load(f))
    except Exception as e:  # noqa: BLE001
        print(f"[supervisor] PORTMAP UNREADABLE ({e}) — starting empty; "
              f"port assignments may change, check adapter/port pairing!", flush=True)
        return {}, {}


def _load_enabled() -> set:
    try:
        with open(STATE_FILE) as f:
            return parse_enabled(json.load(f))
    except Exception:  # noqa: BLE001 — absent/unreadable: _load_state says so
        return set()


def _save_state(ports: dict, serial: dict, enabled=None) -> None:
    """Write the map atomically. `enabled` None keeps the stored opt-ins."""
    if enabled is None:
        enabled = _load_enabled()
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(dump_state(ports, serial, enabled), f, indent=1)
        f.flush()
        os.fsync(f.fileno())      # durable before the rename (audit DP-30:
    os.replace(tmp, STATE_FILE)   # a host freeze left a 0-byte map once)


def assign_port(stable_id: str, pmap: dict) -> int:
    if stable_id in pmap:
        return pmap[stable_id]
    used = set(pmap.values())
    for p in range(PORT_LOW, PORT_HIGH + 1):
        if p not in used:
            pmap[stable_id] = p
            return p
    raise RuntimeError("no free bridge ports left")


def render_ser2net_config(adapters: list[dict], admin_port: int | None = None) -> str:
    """ser2net YAML for the MANAGED adapters, each with its own serial
    params (`a["serial"]`, default when absent). Pure. The kernel's RS-485
    mode is added only to the adapters that have it on. ser2net's admin
    interface always listens on 127.0.0.1:<admin_port> (ADMIN_PORT unless
    given; 0 = none) — inside the container only, for the diagnostics
    (connection, bytes per direction), never published. Always, so a config
    that goes from no adapter to one needs no ser2net restart for it."""
    parts = ["%YAML 1.1", "---"]
    admin = ADMIN_PORT if admin_port is None else admin_port
    if admin:
        parts += ["admin:", f"  accepter: tcp,127.0.0.1,{admin}", ""]
    for a in adapters:
        serial = a.get("serial") or DEFAULT_SERIAL
        params = ser2net_params(serial)
        if serial.get(RS485_FIELD):
            params += f",local,rs485={RS485_SPEC}"
        else:
            params += ",local"
        parts += [
            f"connection: &{a['name']}",
            f"  accepter: tcp,{a['tcp_port']}",
            f"  connector: serialdev,{a['dev']},{params}",
            "  options:",
            "    kickolduser: true",
            "    max-connections: 1",
            "",
        ]
    return "\n".join(parts) + "\n"


def _write_ser2net_config(adapters: list[dict]) -> None:
    tmp = SER2NET_CFG + ".tmp"
    with open(tmp, "w") as f:
        f.write(render_ser2net_config(adapters))
    os.replace(tmp, SER2NET_CFG)


def _clear_stale_locks() -> None:
    """Remove UUCP lockfiles (LCK..ttyUSB*) left behind by an unclean ser2net
    shutdown (host freeze, container kill). Container PIDs restart from low
    numbers, so a stale lock's PID can match a live process and gensio then
    refuses every open with GE_INUSE ("Object was already in use") — surviving
    even a host reboot, since the container layer persists. Only safe to call
    while ser2net is NOT running: then any lock in this container is stale by
    definition (the bridge is the sole serial user here)."""
    seen = set()
    for d in ("/run/lock", "/var/lock"):
        for f in glob.glob(os.path.join(d, "LCK..*")):
            real = os.path.realpath(f)
            if real in seen:
                continue
            seen.add(real)
            try:
                os.unlink(f)
                print(f"[supervisor] removed stale serial lock {f}", flush=True)
            except OSError:
                pass


def _clear_dead_pid_locks() -> None:
    """Remove UUCP lockfiles whose OWNER PID is dead — safe even while ser2net
    is ALIVE (its own live locks carry a live PID). Audit DP-18: an orphan
    lock with ser2net still running (the /dev node vanished under it at
    replug) used to kill the bus indefinitely, because the full sweep above
    only ever ran on respawn. UUCP lock format: the PID in the first line."""
    for d in ("/run/lock", "/var/lock"):
        for f in glob.glob(os.path.join(d, "LCK..*")):
            try:
                with open(f) as fh:
                    pid = int(fh.read().split()[0])
                os.kill(pid, 0)                  # raises if the PID is gone
            except (ValueError, IndexError, OSError, ProcessLookupError):
                try:
                    os.unlink(f)
                    print(f"[supervisor] removed dead-owner serial lock {f}", flush=True)
                except OSError:
                    pass


def _reload_ser2net() -> None:
    global _ser2net
    _clear_dead_pid_locks()                      # safe alongside a live ser2net
    if _ser2net and _ser2net.poll() is None:
        _ser2net.send_signal(signal.SIGHUP)      # non-disruptive: only changed ports restart
    else:
        if _ser2net is not None:
            # audit DP-18/32: a dying ser2net used to respawn silently —
            # the exit code is the first diagnostic an operator needs
            print(f"[supervisor] ser2net not running (exit code "
                  f"{_ser2net.poll()}) — respawning", flush=True)
            add_event("ser2net_restart", f"ser2net was not running (exit code "
                      f"{_ser2net.poll()}) and was started again")
        _clear_stale_locks()                     # ser2net not running → locks are leftovers
        _ser2net = subprocess.Popen(["ser2net", "-n", "-c", SER2NET_CFG])
        _diag_reset_connections()                # every client of the old one is gone


def _is_included(a: dict) -> bool:
    return bool(INCLUDE & {a["stable_id"], a["dev"], a["port_path"], a["name"]})


def _set_status(a: dict, enabled: set) -> None:
    """Decide one port's status. Excluded, reserved and not-enabled ports are
    never opened. A USB adapter is served as it always was (no probe). A
    board port is probed once per bridge lifetime (_probed)."""
    a.setdefault("reason", "")
    a.update(fix="", needs_major=None)
    a["enabled"] = a.get("needs_enable", False) and (
        a["stable_id"] in enabled or _is_included(a))
    if _is_excluded(a):
        a.update(status="excluded", reason="listed in BRIDGE_EXCLUDE")
    elif a.get("status") == "reserved":
        pass
    elif a.get("needs_enable") and not a["enabled"]:
        a.update(status="not-enabled",
                 reason="a UART of the board itself: served only once enabled, since "
                        "a program using it without a lock (GPS, a login console) "
                        "cannot be seen")
    elif a.get("kind", "usb") == "usb":
        a.update(status="ok", reason="")
    else:
        if a["stable_id"] not in _probed:
            _probed[a["stable_id"]] = probe_access(a)
        a.update(_probed[a["stable_id"]])


def reconcile() -> None:
    """Idempotent: enumerate, and if the managed adapter set (or any managed
    adapter's serial params) changed, regen + reload. Only ports with status
    ok are managed: excluded ones (claimed elsewhere, e.g. a BMS), reserved
    ones (console, Bluetooth), SoC UARTs not enabled, ports the container may
    not open and ports another program holds exclusively are listed with the
    reason but NEVER exposed."""
    global _adapters, _unserved, _seen_ids
    with _lock:
        new = enumerate_adapters()
        pmap, serial = _load_state()
        enabled = _load_enabled()
        alive = bool(_ser2net and _ser2net.poll() is None)
        for a in new:
            _set_status(a, enabled)
            a["excluded"] = a["status"] == "excluded"
            a["available"] = a["status"] == "ok"
            a["serial_params"] = serial_for(a["stable_id"], serial)
            a["serial_custom"] = a["stable_id"] in serial
            if a["available"]:
                # assign from the persistent map on EVERY pass, so tcp_port is
                # always populated (incl. the unchanged/early-return path)
                a["tcp_port"] = assign_port(a["stable_id"], pmap)
        managed = [a for a in new if a["available"]]
        if _seen_ids is not None:                # the first scan is no change
            for kind, message, a in inventory_changes(_adapters, new, _seen_ids):
                add_event(kind, message, key=a["stable_id"], tcp_port=a.get("tcp_port"))
        else:
            _seen_ids = set()
        _seen_ids.update(a["stable_id"] for a in new)
        unserved = {(a["name"], a["status"], a["reason"]) for a in new if not a["available"]}
        if unserved != _unserved:                # said once, not every 10 s
            _unserved = unserved
            if unserved:
                print("[supervisor] not served: " + "; ".join(
                    f"{n} {st} ({why})" for n, st, why in sorted(unserved)), flush=True)
        # only the MANAGED set drives ser2net; changes to it trigger a reload.
        # An unchanged set only short-circuits while ser2net is actually alive —
        # otherwise a dead ser2net stayed dead until the next adapter change.
        # The signature includes the DEV PATH (audit DP-4): an unplug+replug
        # inside one reconcile window keeps the stable_id set identical while
        # the kernel renumbers the node — matching on ids alone left ser2net
        # opening a dead /dev (or, with a recycled node, the WRONG bus)
        # forever. It also includes the serial params, so a POST
        # /adapters/<key>/serial reaches ser2net through this same path.
        sig = _signature(managed)
        prev = _signature([a for a in _adapters if a.get("available")])
        if sig == prev and _adapters and alive:
            _adapters = new                      # no set/path change → no reload
            return
        if not managed and not prev and _adapters is not None:
            # zero adapters, still zero adapters (audit DP-32): rewriting the
            # config + SIGHUP every 10s was pure noise exactly when the
            # operator wants clean logs
            _adapters = new
            return
        _save_state(pmap, serial)
        _write_ser2net_config([{**a, "serial": a["serial_params"]} for a in managed])
        _reload_ser2net()                        # unserved ports NOT in the config
        _adapters = new
        expo = ", ".join(f"{a['name']}({a['stable_id']})->:{a['tcp_port']} "
                         f"{serial_text(a['serial_params'])}" for a in managed) or "none"
        excl = ", ".join(a["name"] for a in new if a["excluded"])
        print(f"[supervisor] reconciled: exposed[{expo}]"
              + (f" excluded[{excl}]" if excl else ""), flush=True)


def _signature(adapters: list[dict]) -> set:
    def line(a):
        p = a.get("serial_params") or DEFAULT_SERIAL
        return ser2net_params(p) + (",rs485" if p.get(RS485_FIELD) else "")
    return {(a["stable_id"], a["dev"], line(a)) for a in adapters}


def set_adapter_serial(key: str, body) -> dict | None:
    """Validate + persist an adapter's serial params, then reconcile (which
    rewrites that adapter's ser2net line and SIGHUPs — only the changed port
    restarts). Returns the adapter's API view, None when the key is not a
    present adapter. Raises ValueError on invalid params."""
    with _lock:                                  # RLock: reconcile() re-enters
        port = next((a for a in _adapters if a["stable_id"] == key), None)
        if port is None:
            return None
        pmap, serial = _load_state()
        params = validate_serial(body, serial_for(key, serial))
        if params.get(RS485_FIELD) and port.get("kind", "usb") != "board":
            # a USB chip switches direction itself; most refuse TIOCSRS485,
            # and ser2net would then fail to open the port at all
            raise ValueError("rs485: only for a board UART (a USB adapter switches "
                             "direction by itself)")
        serial[key] = params
        _save_state(pmap, serial)
        reconcile()
        return _view_of(key)


def _view_of(key: str) -> dict | None:
    for a in _adapters:
        if a["stable_id"] == key:
            return adapter_view(a)
    return None                                  # unplugged in between


def set_adapter_enabled(key: str, body) -> dict | None:
    """Opt a SoC UART in (or out) of being served. Raises ValueError for a
    body that is not {"enabled": true|false}, for a port that needs no opt-in
    and for a reserved one (a console or Bluetooth UART is never served)."""
    if not isinstance(body, dict) or set(body) != {"enabled"} \
            or not isinstance(body["enabled"], bool):
        raise ValueError('body must be {"enabled": true} or {"enabled": false}')
    with _lock:
        port = next((a for a in _adapters if a["stable_id"] == key), None)
        if port is None:
            return None
        if not port.get("needs_enable"):
            raise ValueError("this port needs no enabling: USB adapters and RS-485 HAT "
                             "channels are served as they are")
        if port.get("status") == "reserved" and body["enabled"]:
            raise ValueError(f"reserved ({port.get('reason')}): never served")
        pmap, serial = _load_state()
        enabled = _load_enabled()
        (enabled.add if body["enabled"] else enabled.discard)(key)
        _save_state(pmap, serial, enabled)
        reconcile()
        return _view_of(key)


def adapter_view(a: dict) -> dict:
    """The /adapters JSON shape for one adapter. Every field MBG's
    commissioning route + wizard read (stable_id, tcp_port, available, ...)
    keeps its pre-existing name and meaning."""
    params = a.get("serial_params") or DEFAULT_SERIAL
    return {
        "stable_id": a["stable_id"], "dev": a["dev"],
        "vendor_id": a["vendor_id"], "product_id": a["product_id"],
        "serial": a["serial"], "manufacturer": a["manufacturer"],
        "model": a["model"], "port_path": a["port_path"],
        "tcp_port": a.get("tcp_port"), "connected": True,
        "available": a.get("available", True),
        "excluded": a.get("excluded", False),
        "serial_params": {k: params[k] for k in SERIAL_FIELDS},
        "serial_text": serial_text(params),
        "serial_custom": a.get("serial_custom", False),
        "rs485": bool(params.get(RS485_FIELD)),
        # what the port is, and whether the bridge can serve it
        "kind": a.get("kind", "usb"), "driver": a.get("driver", ""),
        "label": a.get("label", ""), "dev_num": a.get("dev_num", ""),
        "status": a.get("status", "ok"), "reason": a.get("reason", ""),
        "fix": a.get("fix", ""), "needs_major": a.get("needs_major"),
        "needs_enable": bool(a.get("needs_enable")), "enabled": bool(a.get("enabled")),
        # line + transport health of a served port (ADR-0017); null otherwise
        "stats": port_stats(a["stable_id"]) if a.get("available") else None,
    }


# ---- diagnostics: line and transport health (ADR-0017) --------------------
#
# What a served port's health is made of, and where each number comes from:
#
# - the TCP side and the bytes per direction: ser2net's own admin interface
#   (`showport`), on 127.0.0.1 inside the container only. It counts per
#   CONNECTION (a new client starts from 0), so the totals here are summed
#   sample by sample; a client that connects and leaves between two samples
#   is not seen.
# - the line counters (frame, parity, overrun, break, buffer overrun, rx, tx):
#   the kernel's TIOCGICOUNT, asked on ser2net's OWN open file — duplicated
#   with pidfd_getfd(2), never a second open of the tty: nothing is opened,
#   no termios or modem line is touched. Closing the duplicate is the last
#   close only if ser2net closed the port in the microseconds between the
#   lookup and the ioctl (that reading is then dropped); its HUPCL is what
#   ser2net's own close would have done. Needs the container
#   capability SYS_PTRACE (Docker's default seccomp profile refuses
#   pidfd_getfd without it); kernel 5.6+. A counter the driver does not keep
#   is reported null, never 0.

def _int_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        val = int(raw) if raw else default
    except ValueError:
        val = default
    return val if low <= val <= high else default


def _admin_port() -> int:
    """ser2net's admin port: BRIDGE_ADMIN_PORT (default 6999), 0 = off. Never
    one of the data ports nor the control port (it would collide)."""
    port = _int_env("BRIDGE_ADMIN_PORT", 6999, 0, 65535)
    if port and (PORT_LOW <= port <= PORT_HIGH or port == CONTROL_PORT):
        print(f"[supervisor] BRIDGE_ADMIN_PORT {port} collides with the data or control "
              f"ports — the transport statistics are off", flush=True)
        return 0
    return port


ADMIN_PORT = _admin_port()
STATS_S = _int_env("BRIDGE_STATS_S", 5, 1, 300)  # how often a served port is sampled
EVENTS_MAX = 500                                 # the event ring
LINE_EVENT_S = 60                                # line-error events: at most one per port per minute
WINDOW_S = 600                                   # "connects in the last 10 minutes"
BOOT = os.urandom(4).hex()                       # tells a gateway the event numbers restarted
STARTED = time.time()
LINE_FIELDS = ("frame", "parity", "overrun", "brk", "buf_overrun", "rx", "tx")
LINE_ERRORS = ("frame", "parity", "overrun", "brk", "buf_overrun")
# struct serial_icounter_struct: cts dsr rng dcd rx tx frame overrun parity
# brk buf_overrun reserved[9] — 20 ints
_ICOUNT_ORDER = ("cts", "dsr", "rng", "dcd", "rx", "tx", "frame", "overrun", "parity",
                 "brk", "buf_overrun")

# What each driver keeps, from its kernel source (and the bench). A board UART
# goes through serial_core, which keeps every counter. Unknown USB drivers
# report what the ioctl returns, flagged unverified.
DRIVER_COUNTS = {
    "ftdi_sio": {"fields": LINE_FIELDS, "note": ""},
    # error counts only in its event mode, which the driver enables only with
    # input parity checking on; it never counts rx/tx
    "cp210x": {"fields": LINE_ERRORS, "needs_inpck": True,
               "note": "CP210x counts line errors only with parity checking on"},
    # no get_icount: the ioctl fails, everything is null
    "ch341-uart": {"fields": (), "note": "the CH340/CH341 driver keeps no line counters"},
    # errors as the device reports them in SERIAL_STATE; no rx/tx
    "cdc_acm": {"fields": LINE_ERRORS, "note": "a CDC-ACM device reports line errors only if it sends them"},
}

_events: list = []                               # bounded ring, oldest first
_event_seq = 0
_ev_lock = threading.Lock()
_diag: dict = {}                                 # stable_id → per-port state
_diag_lock = threading.Lock()
_seen_ids: set | None = None                     # stable ids listed since start (None: no scan yet)
_ser2net_dead_said = False


def add_event(kind: str, message: str, key: str = "", tcp_port=None, now=None, **data) -> dict:
    """Append one event to the bounded ring. Every event has a number that
    only grows within one bridge run (`BOOT` changes on restart)."""
    global _event_seq
    with _ev_lock:
        _event_seq += 1
        ev = {"seq": _event_seq, "ts": round(now if now is not None else time.time(), 1),
              "kind": kind, "key": key, "tcp_port": tcp_port, "message": message}
        if data:
            ev["data"] = data
        _events.append(ev)
        del _events[:-EVENTS_MAX]
    return ev


def events_since(since: int = 0, key: str | None = None, limit: int = EVENTS_MAX) -> dict:
    """{boot, events (seq > since, oldest first), next, oldest, lost}. `lost`
    says the ring dropped events the caller had not seen yet."""
    with _ev_lock:
        evs = list(_events)
        last = _event_seq
    oldest = evs[0]["seq"] if evs else last + 1
    out = [e for e in evs if e["seq"] > since and (key is None or e["key"] == key)]
    return {"boot": BOOT, "events": out[-limit:], "next": last, "oldest": oldest,
            "lost": since + 1 < oldest and since < last}


def inventory_changes(prev: list, new: list, seen: set) -> list:
    """(kind, message, adapter) for what changed between two scans: an
    adapter gone, back (seen before in this run), new, its data port moved,
    its /dev node renamed, its status changed. Pure."""
    def name(a):
        return a.get("label") or a.get("dev") or a["stable_id"]
    before = {a["stable_id"]: a for a in prev}
    after = {a["stable_id"]: a for a in new}
    out = []
    for sid, a in after.items():
        b = before.get(sid)
        if b is None:
            kind = "adapter_back" if sid in seen else "adapter_new"
            out.append((kind, f"{name(a)} {'is back' if sid in seen else 'appeared'} "
                              f"({a.get('dev')})", a))
            continue
        if b.get("tcp_port") and a.get("tcp_port") and b["tcp_port"] != a["tcp_port"]:
            out.append(("port_moved", f"{name(a)} moved from :{b['tcp_port']} "
                                      f"to :{a['tcp_port']}", a))
        if b.get("dev") != a.get("dev"):
            out.append(("dev_renamed", f"{name(a)} is now {a.get('dev')} (was {b.get('dev')})", a))
        if b.get("status") != a.get("status"):
            out.append(("status", f"{name(a)}: {b.get('status')} → {a.get('status')}"
                                  + (f" ({a['reason']})" if a.get("reason") else ""), a))
    for sid, b in before.items():
        if sid not in after:
            out.append(("adapter_gone", f"{name(b)} is gone ({b.get('dev')})", b))
    return out


# -- ser2net's admin interface -------------------------------------------------

_SHOWPORT_KEYS = {
    "bytes read from tcp": "net_in", "bytes written to tcp": "net_out",
    "bytes read from device": "dev_in", "bytes written to device": "dev_out",
}


def parse_showport(text: str) -> dict:
    """ser2net 4 admin `showport` text → {tcp_port: {name, remote, net_in,
    net_out, dev_in, dev_out, enabled}}. Pure. `remote` is "ip:port" of the
    connected client, "" when none. A port block starts at `port: <name>` in
    the first column; the counters are per connection."""
    out: dict = {}
    cur = None
    for raw in (text or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if line.startswith("->"):                # the prompt (and an echoed command)
            line = line[2:].strip()
            if not line.startswith("port:"):
                continue
        if not line or ":" not in line:
            continue
        name, _, val = line.partition(":")
        name, val = name.strip().lower(), val.strip()
        if name == "port" and not raw.startswith(" "):
            cur = {"name": val, "remote": "", "net_in": 0, "net_out": 0,
                   "dev_in": 0, "dev_out": 0, "enabled": True, "_port": None}
            continue
        if cur is None:
            continue
        if name == "accepter":
            last = val.rsplit(",", 1)[-1].strip()
            if last.isdigit():
                cur["_port"] = int(last)
                out[int(last)] = cur
        elif name == "connected to":
            parts = val.split(",")
            cur["remote"] = (",".join(parts[1:-1]) + ":" + parts[-1]) if len(parts) >= 3 else val
        elif name == "enable state":
            cur["enabled"] = val.lower() != "off"
        elif name in _SHOWPORT_KEYS and val.isdigit():
            cur[_SHOWPORT_KEYS[name]] = int(val)
    for v in out.values():
        v.pop("_port", None)
    return out


def _recv_until(sock, buf: bytes, marker: bytes, deadline: float) -> bytes:
    """Read until the buffer ends with `marker`, EOF or the deadline."""
    while not buf.endswith(marker) and time.monotonic() < deadline and len(buf) < 1 << 20:
        sock.settimeout(max(0.05, deadline - time.monotonic()))
        try:
            data = sock.recv(65536)
        except socket.timeout:
            break
        if not data:
            break
        buf += data
    return buf


def admin_query(cmd: str = "showport", port: int | None = None, timeout: float = 2.0) -> str:
    """One command to ser2net's admin interface on localhost; the reply text.
    ser2net drops what arrives before its first prompt, so: wait for the
    prompt, send the command, read up to the next prompt, then `exit`."""
    port = ADMIN_PORT if port is None else port
    if not port:
        raise OSError(errno.ENOTCONN, "the ser2net admin interface is off (BRIDGE_ADMIN_PORT=0)")
    deadline = time.monotonic() + timeout
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        banner = _recv_until(s, b"", b"-> ", deadline)
        if not banner.endswith(b"-> "):
            raise OSError(errno.ETIMEDOUT, "the ser2net admin interface gave no prompt")
        s.sendall(f"{cmd}\r\n".encode())
        reply = _recv_until(s, b"", b"\n-> ", deadline)
        try:
            s.sendall(b"exit\r\n")
        except OSError:
            pass
    return reply.decode(errors="replace")


# -- the kernel's line counters, on ser2net's own file -------------------------

PROC = "/proc"                                   # the tests point it at a fake tree


def ser2net_fd(pid: int, dev: str) -> int | None:
    """Which of ser2net's file descriptors is this tty (None: not open — ser2net
    opens a port only while a client is connected)."""
    want = {dev, os.path.realpath(dev)}
    base = os.path.join(PROC, str(pid), "fd")
    try:
        names = os.listdir(base)
    except OSError:
        return None
    for n in names:
        try:
            if os.readlink(os.path.join(base, n)) in want:
                return int(n)
        except (OSError, ValueError):
            continue
    return None


def _grab_fd(pid: int, target: int) -> int:
    """A duplicate of another process's fd (pidfd_getfd, syscall 438 on every
    architecture). Raises OSError."""
    import ctypes
    pidfd = os.pidfd_open(pid)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fd = libc.syscall(438, pidfd, target, 0)
        if fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))
        return fd
    finally:
        os.close(pidfd)


def _icount(fd: int) -> tuple[dict, bool]:
    """(TIOCGICOUNT counters, input parity checking on). Raises OSError."""
    import struct
    import termios
    buf = bytearray(80)
    fcntl.ioctl(fd, getattr(termios, "TIOCGICOUNT", 0x545D), buf)
    vals = struct.unpack("20i", bytes(buf))
    try:
        inpck = bool(termios.tcgetattr(fd)[0] & termios.INPCK)   # read only
    except termios.error:
        inpck = False
    return dict(zip(_ICOUNT_ORDER, vals)), inpck


def shape_line(raw: dict, driver: str, kind: str, inpck: bool) -> tuple[dict, str, bool]:
    """The kernel's counters → what this driver really keeps: (counters with
    null for what it does not count, note, verified). Pure."""
    if kind == "board":                          # serial_core keeps them all
        spec = {"fields": LINE_FIELDS, "note": ""}
        verified = True
    else:
        spec = DRIVER_COUNTS.get(driver)
        verified = spec is not None
        spec = spec or {"fields": LINE_FIELDS, "note": f"driver {driver or '?'} not verified"}
    fields = set(spec["fields"])
    note = spec["note"]
    if spec.get("needs_inpck") and not inpck:
        fields -= set(LINE_ERRORS)
    else:
        note = "" if spec.get("needs_inpck") else note
    return ({f: (int(raw[f]) & 0xFFFFFFFF if f in fields else None) for f in LINE_FIELDS},
            note, verified)


def line_counters(dev: str, driver: str, kind: str, pid: int | None) -> tuple[dict | None, str, bool]:
    """(counters | None, why/note, verified) for one served port."""
    if pid is None:
        return None, "ser2net is not running", False
    target = ser2net_fd(pid, dev)
    if target is None:
        return None, "the port is not open (no client connected)", False
    try:
        fd = _grab_fd(pid, target)
    except OSError as e:
        if e.errno == errno.EPERM:
            return None, ("the container may not read ser2net's file (add the capability "
                          "SYS_PTRACE — the compose files do)"), False
        if e.errno == errno.ENOSYS:
            return None, "the kernel is older than 5.6 (no pidfd_getfd)", False
        return None, f"cannot read ser2net's file: {os.strerror(e.errno or 0)}", False
    except AttributeError:                       # Python without os.pidfd_open
        return None, "pidfd_open unavailable", False
    try:
        raw, inpck = _icount(fd)
        # ser2net may have closed the port between finding its fd and the
        # ioctl (a client left): then our duplicate held the last reference
        # and its close below is the last close. Rare (a few µs), and what
        # ser2net's own close would have done a moment earlier; a reading
        # taken across it is not trusted.
        if ser2net_fd(pid, dev) != target:
            return None, "the port closed while it was being read", False
    except OSError as e:
        if e.errno in (errno.ENOTTY, errno.EINVAL):    # the driver has no get_icount
            known = DRIVER_COUNTS.get(driver) or {}
            note = known.get("note") or f"the {driver or 'port'} driver keeps no line counters"
            # verified only for a driver known to keep none (CH340)
            return None, note, known.get("fields") == ()
        return None, f"TIOCGICOUNT failed: {os.strerror(e.errno or 0)}", False
    finally:
        os.close(fd)
    counts, note, verified = shape_line(raw, driver, kind, inpck)
    return counts, note, verified


# -- per-port state ------------------------------------------------------------

def new_port_state(now: float) -> dict:
    return {"client": "", "client_since": None, "connects": 0, "disconnects": 0,
            "connect_ts": [], "bytes": {"from_device": 0, "to_device": 0, "from_net": 0, "to_net": 0},
            "last_rx_ts": None, "last_tx_ts": None, "since": now, "sampled_ts": None,
            "admin": "", "_last": None,
            "line": None, "line_note": "", "line_verified": False, "line_as_of": None,
            "_line_prev": None, "_line_pending": {}, "_line_event_ts": 0.0}


def apply_sample(st: dict, sample: dict | None, now: float) -> list:
    """Fold one `showport` sample of a port into its state; the events it
    makes (client connected / left). Pure but for `st`. ser2net counts per
    connection, so a new client (another address) or a counter that went
    down starts a new base."""
    events = []
    st["sampled_ts"] = now
    remote = (sample or {}).get("remote", "")
    prev = st["_last"]
    if remote != st["client"]:
        if st["client"]:
            st["disconnects"] += 1
            events.append(("client_left", f"client {st['client']} left", {"client": st["client"]}))
        if remote:
            st["connects"] += 1
            st["connect_ts"] = [t for t in st["connect_ts"] if now - t <= WINDOW_S] + [now]
            events.append(("client_connected", f"client {remote} connected", {"client": remote}))
        st["client"], st["client_since"] = remote, (now if remote else None)
        prev = None                              # a new connection counts from 0
    if not remote or sample is None:
        st["_last"] = None
        return events
    cur = (sample["dev_in"], sample["dev_out"], sample["net_in"], sample["net_out"])
    if prev is None or any(c < p for c, p in zip(cur, prev)):
        prev = (0, 0, 0, 0)                      # the same address reconnected in between
    d = [c - p for c, p in zip(cur, prev)]
    b = st["bytes"]
    b["from_device"] += d[0]
    b["to_device"] += d[1]
    b["from_net"] += d[2]
    b["to_net"] += d[3]
    if d[0] > 0:
        st["last_rx_ts"] = now
    if d[1] > 0:
        st["last_tx_ts"] = now
    st["_last"] = cur
    return events


def apply_line(st: dict, line: dict | None, note: str, verified: bool, now: float) -> list:
    """Fold one read of the line counters; at most one `line_errors` event per
    port per LINE_EVENT_S with what grew since the last one. A counter that
    went down (adapter replugged) is a new base with no growth."""
    st["line_note"], st["line_verified"] = note, verified
    if line is None:
        return []                                # keep the last values, with their time
    prev = st["_line_prev"]
    st["line"], st["line_as_of"], st["_line_prev"] = line, now, line
    if prev is None:
        return []
    for f in LINE_ERRORS:
        c, p = line.get(f), prev.get(f)
        if c is None or p is None:
            continue
        grew = c - p if c >= p else 0         # went down: a new base, never a spike
        if grew > 0:
            st["_line_pending"][f] = st["_line_pending"].get(f, 0) + grew
    if st["_line_pending"] and now - st["_line_event_ts"] >= LINE_EVENT_S:
        got, st["_line_pending"] = st["_line_pending"], {}
        st["_line_event_ts"] = now
        text = ", ".join(f"{f} +{n}" for f, n in sorted(got.items()))
        return [("line_errors", f"line errors: {text}", got)]
    return []


def port_stats(key: str, now: float | None = None) -> dict | None:
    """The API view of one port's health (None before its first sample)."""
    now = time.time() if now is None else now
    with _diag_lock:
        st = _diag.get(key)
        if st is None:
            return None
        rx = st["last_rx_ts"]
        return {
            "sampled_ts": st["sampled_ts"], "interval_s": STATS_S,
            "source": {"transport": "ser2net-admin" if not st["admin"] else "unavailable",
                       "line": "TIOCGICOUNT" if st["line"] is not None else "unavailable"},
            "transport_error": st["admin"],
            "client": ({"address": st["client"], "since": st["client_since"]}
                       if st["client"] else None),
            "connects": st["connects"], "disconnects": st["disconnects"],
            "connects_10min": sum(1 for t in st["connect_ts"] if now - t <= WINDOW_S),
            "bytes": dict(st["bytes"]),
            "last_rx_ts": rx, "last_tx_ts": st["last_tx_ts"],
            # since the last byte from the bus (or since the bridge started)
            "silent_s": round(now - (rx if rx is not None else st["since"]), 1),
            "line": dict(st["line"]) if st["line"] is not None else None,
            "line_as_of": st["line_as_of"], "line_note": st["line_note"],
            "line_verified": st["line_verified"],
        }


def _diag_reset_connections() -> None:
    """ser2net restarted: every client it had is gone."""
    with _diag_lock:
        for st in _diag.values():
            st["_last"] = None


def sample_once(now: float | None = None, query=None) -> None:
    """Sample every served port once: ser2net's view of the TCP side, then
    the kernel's line counters. Events go to the ring."""
    global _ser2net_dead_said
    now = time.time() if now is None else now
    with _lock:
        served = [(a["stable_id"], a["tcp_port"], a["dev"], a.get("driver", ""),
                   a.get("kind", "usb")) for a in _adapters if a.get("available")]
        proc = _ser2net
    alive = proc is not None and proc.poll() is None
    if proc is not None and not alive and not _ser2net_dead_said:
        _ser2net_dead_said = True
        add_event("ser2net_exited", f"ser2net exited (code {proc.poll()})", now=now)
    elif alive:
        _ser2net_dead_said = False
    if not served:
        return
    ports, err = None, ""
    if alive:
        try:
            ports = parse_showport((query or admin_query)())
        except OSError as e:
            err = f"ser2net admin interface: {e}"
    else:
        err = "ser2net is not running"
    pid = getattr(proc, "pid", None) if alive else None
    for sid, tcp, dev, driver, kind in served:
        line, note, verified = line_counters(dev, driver, kind, pid)
        with _diag_lock:
            st = _diag.setdefault(sid, new_port_state(now))
            st["admin"] = err
            evs = apply_sample(st, ports.get(tcp) if ports is not None else None, now) \
                if ports is not None or not alive else []
            evs += apply_line(st, line, note, verified, now)
        for kind_, msg, data in evs:
            add_event(kind_, msg, key=sid, tcp_port=tcp, now=now, **data)


def diagnostics_info() -> dict:
    """How this bridge measures (for a gateway to say what it can trust)."""
    return {"interval_s": STATS_S, "boot": BOOT, "started": round(STARTED, 1),
            "transport": "ser2net-admin" if ADMIN_PORT else "off",
            "line": "TIOCGICOUNT via pidfd_getfd"}


def _stats_loop():
    while True:
        time.sleep(STATS_S)
        try:
            sample_once()
        except Exception as e:  # noqa: BLE001 — diagnostics must never stop the bridge
            print(f"[supervisor] statistics sample failed: {e}", flush=True)


def route(method: str, raw_path: str) -> tuple[str, str | None]:
    """(endpoint, adapter key) for a request. Pure. The key is split on the
    RAW path and then percent-decoded, so keys containing ':' '@' or even an
    encoded '/' address the right adapter. endpoint is one of: health,
    adapters, serial, enable, stats, events, not_found, method_not_allowed."""
    path = urlsplit(raw_path).path.rstrip("/") or "/"
    parts = path.split("/")[1:]
    if parts == ["health"]:
        return ("health", None) if method == "GET" else ("method_not_allowed", None)
    if parts == ["adapters"]:
        return ("adapters", None) if method == "GET" else ("method_not_allowed", None)
    if parts == ["events"]:
        return ("events", None) if method == "GET" else ("method_not_allowed", None)
    if len(parts) == 3 and parts[0] == "adapters" and parts[2] in ("serial", "enable") \
            and parts[1]:
        key = unquote(parts[1])
        return (parts[2], key) if method == "POST" else ("method_not_allowed", key)
    if len(parts) == 3 and parts[0] == "adapters" and parts[2] == "stats" and parts[1]:
        key = unquote(parts[1])
        return ("stats", key) if method == "GET" else ("method_not_allowed", key)
    return "not_found", None


MAX_BODY = 4096


class _Handler(BaseHTTPRequestHandler):
    timeout = 10                                 # a mute client can't hold a thread forever

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def _authorized(self) -> bool:
        return check_token(self.headers.get("Authorization"), TOKEN)

    def _dispatch(self, method: str):
        endpoint, key = route(method, self.path)
        if endpoint == "health":
            return self._health()
        # everything but /health is behind the token (when one is set) —
        # checked before routing errors so the API can't be probed anonymously
        if not self._authorized():
            return self._json({"error": "unauthorized"}, code=401,
                              extra={"WWW-Authenticate": "Bearer"})
        if endpoint == "adapters":
            with _lock:
                data = [adapter_view(a) for a in _adapters]
            self._json({"version": VERSION, "hostname": _hostname(),
                        "auth": bool(TOKEN), "adapters": data,
                        "warnings": list(_warnings),
                        "diagnostics": diagnostics_info()})
        elif endpoint == "stats":
            with _lock:
                a = next((x for x in _adapters if x["stable_id"] == key), None)
            if a is None:
                return self._json({"error": f"no adapter {key!r}"}, code=404)
            self._json({"stable_id": key, "tcp_port": a.get("tcp_port"),
                        "available": bool(a.get("available")),
                        "stats": port_stats(key) if a.get("available") else None,
                        "events": events_since(0, key=key, limit=50)["events"],
                        "diagnostics": diagnostics_info()})
        elif endpoint == "events":
            q = parse_qs(urlsplit(self.path).query)
            try:
                since = max(0, int((q.get("since") or ["0"])[0]))
            except ValueError:
                return self._json({"error": "since: a number"}, code=400)
            self._json(events_since(since))
        elif endpoint == "serial":
            self._post(key, set_adapter_serial, "serial update",
                       lambda v: f"serial params for {key}: {v['serial_text']}")
        elif endpoint == "enable":
            self._post(key, set_adapter_enabled, "enable",
                       lambda v: f"{key} {'enabled' if v['enabled'] else 'disabled'}: "
                                 f"{v['status']}")
        elif endpoint == "method_not_allowed":
            self._json({"error": "method not allowed"}, code=405)
        else:
            self._json({"error": "not found"}, code=404)

    def _health(self):
        # the DATA path matters, not just this API (audit DP-18): a dead
        # ser2net with adapters to serve = the bus is down — report it so
        # the container healthcheck stops declaring a dead bridge healthy.
        # Unauthenticated, so it carries NO device details; the hostname is
        # added only for a caller that could read /adapters anyway.
        with _lock:
            n_all = len(_adapters)
            n_managed = sum(1 for a in _adapters
                            if a.get("available") and not a.get("excluded"))
        ser2net_ok = _ser2net is not None and _ser2net.poll() is None
        body = {"status": "ok", "adapters": n_all, "version": VERSION}
        code = 200
        if n_managed and not ser2net_ok:
            body.update(status="down", reason="ser2net not running")
            code = 500
        if _warnings:                            # a stale device-rule slot
            body["warnings"] = len(_warnings)
        if self._authorized():
            body["hostname"] = _hostname()
            if _warnings:
                body["warnings"] = [w["message"] for w in _warnings]
        self._json(body, code=code)

    def _post(self, key: str, apply, what: str, said):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > MAX_BODY:
            return self._json({"error": f"JSON body required (max {MAX_BODY} bytes)"}, code=400)
        try:
            body = json.loads(self.rfile.read(length).decode())
        except (ValueError, UnicodeDecodeError):
            return self._json({"error": "invalid JSON"}, code=400)
        try:
            view = apply(key, body)
        except ValueError as e:
            return self._json({"error": str(e)}, code=400)
        except Exception as e:  # noqa: BLE001
            print(f"[supervisor] {what} for {key} failed: {e}", flush=True)
            return self._json({"error": f"could not apply the {what}"}, code=500)
        if view is None:
            return self._json({"error": f"no adapter {key!r}"}, code=404)
        print(f"[supervisor] {said(view)}", flush=True)
        self._json(view)

    def _json(self, obj, code: int = 200, extra: dict | None = None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence access logging
        pass


def _http_server():
    # threading + per-connection timeout (audit DP-31): the old single-
    # threaded server let one silent client wedge /adapters — and with it the
    # container healthcheck, which then restarted a healthy ser2net
    srv = ThreadingHTTPServer(("0.0.0.0", CONTROL_PORT), _Handler)
    srv.serve_forever()


def _udev_watch():
    """Kernel-uevent trigger for instant hotplug; debounced. NOTE: on a
    bridge-network container the kernel's uevent netlink usually delivers
    NOTHING (uevents broadcast in the initial netns only) — this thread then
    blocks forever in poll() and the 10s periodic reconcile is the real
    hotplug mechanism. Kept for host-network deployments where it does fire."""
    try:
        import pyudev
        mon = pyudev.Monitor.from_netlink(pyudev.Context(), source="kernel")
        mon.filter_by(subsystem="tty")
        mon.start()
    except Exception as e:  # noqa: BLE001
        print(f"[supervisor] udev watch unavailable ({e}); "
              f"periodic reconcile covers hotplug", flush=True)
        return
    for _dev in iter(lambda: mon.poll(timeout=None), None):
        time.sleep(1.0)                          # debounce: let enumeration settle
        try:
            reconcile()
        except Exception as e:  # noqa: BLE001
            # a reconcile hiccup must not kill the watch (audit DP-17 — and
            # the old message blamed udev for what was a reconcile error)
            print(f"[supervisor] reconcile from udev event failed: {e}", flush=True)


_warnings: list = []                             # stale device-rule slots (check_slots)


def check_slots(slots=None, drivers=None) -> list:
    """The compose files' BRIDGE_TTY_n slots still name tty devices? A slot
    written for an RS-485 HAT's dynamic major goes stale when a kernel update
    hands the HAT another major: the port then reads no-access, and the old
    major may now belong to a different driver — which the container can
    still open. Default slots (USB 188) are skipped; no /sys/dev/char (an
    unusual host) skips the check. → [{slot, rule, driver, message, fix}]."""
    slots = TTY_SLOTS if slots is None else slots
    drivers = TTY_DRIVERS if drivers is None else drivers
    char = _sys("/sys/dev/char")
    if not os.path.isdir(char):
        return []
    out = []
    for n, rule in enumerate(slots, start=1):
        major, _, minor = rule.partition(":")
        if major == "188" or not major.isdigit():
            continue
        nodes = glob.glob(os.path.join(char, f"{major}:*")) if minor == "*" \
            else glob.glob(os.path.join(char, f"{major}:{minor}"))
        subs = {os.path.basename(os.path.realpath(os.path.join(x, "subsystem"))) for x in nodes}
        drv = drivers.get(str(n), "")
        was = f" (written for {drv})" if drv else ""
        if not nodes:
            msg = (f"device rule slot BRIDGE_TTY_{n}={rule}{was}: no device has that "
                   f"number now — the HAT's major changed (kernel update) or it is gone")
        elif subs != {"tty"}:
            msg = (f"device rule slot BRIDGE_TTY_{n}={rule}{was}: that number now belongs "
                   f"to {', '.join(sorted(subs - {'tty'}))}, not a serial port — the "
                   f"container can open a device it should not")
        else:
            continue
        out.append({"slot": n, "rule": rule, "driver": drv, "message": msg,
                    "fix": ACCESS_FIX or "sh scripts/serial-access.sh"})
    return out


def main():
    _warnings[:] = check_slots()
    for w in _warnings:
        print(f"[supervisor] WARNING {w['message']}. Re-run {w['fix']} and recreate "
              f"the container.", flush=True)
    add_event("started", f"bridge {VERSION} started on {_hostname()}")
    for w in _warnings:
        add_event("slot_warning", w["message"], slot=w["slot"], rule=w["rule"])
    reconcile()                                   # initial: enumerate + start ser2net
    threading.Thread(target=_http_server, daemon=True).start()
    threading.Thread(target=_stats_loop, daemon=True).start()
    threading.Thread(target=_udev_watch, daemon=True).start()
    print(f"[supervisor] {VERSION} on {_hostname()}: control API on :{CONTROL_PORT} "
          f"(auth {'on' if TOKEN else 'OFF'}), data ports {PORT_LOW}-{PORT_HIGH}, "
          f"default serial {serial_text(DEFAULT_SERIAL)}", flush=True)
    while True:
        time.sleep(int(os.environ.get("BRIDGE_RECONCILE_S", "10")))
        try:
            reconcile()                           # safety net
        except Exception as e:  # noqa: BLE001
            # a transient OSError (full /data, EBUSY on os.replace, no free
            # ports) used to kill the supervisor — and the container restart
            # took a healthy ser2net down with it (audit DP-17). Log and let
            # the next tick retry instead.
            print(f"[supervisor] reconcile failed (will retry): {e}", flush=True)


if __name__ == "__main__":
    main()
