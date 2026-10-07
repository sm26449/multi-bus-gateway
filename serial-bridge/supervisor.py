#!/usr/bin/env python3
# Serial-bridge supervisor (Faza B).
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Manages ser2net dynamically so serial adapters appear/disappear on hotplug.

- Enumerates USB serial adapters straight from sysfs (robust — no reliance on
  udev-enriched properties, which may be absent inside a container).
- Assigns each a STABLE TCP port from a persistent map, keyed by USB serial
  number (FTDI/CP210x) or, as a fallback for serial-less adapters (cheap CH340),
  by physical USB port-path. Same adapter → same port across replug/restart.
- Regenerates the ser2net config and reloads ser2net (SIGHUP — only changed
  ports restart), holding each adapter EXCLUSIVE (local flag → TIOCEXCL), single
  TCP client.
- Reacts to hotplug via kernel uevents (pyudev, debounced) with a periodic
  reconcile as a robust safety net.
- Exposes a small control API (docs/serial-bridge-api.md): GET /adapters for
  MBG to scan the live inventory, GET /health, and POST /adapters/<key>/serial
  for per-adapter serial parameters. With BRIDGE_TOKEN set, every endpoint but
  /health requires `Authorization: Bearer <token>` — the bridge can then run on
  another host (a Pi next to the RS-485 bus) and be added to a gateway over
  the LAN.
"""
from __future__ import annotations

import glob
import hmac
import json
import os
import signal
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

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

# ---- serial parameters -----------------------------------------------------
BAUD_RATES = (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200)
PARITIES = ("N", "E", "O")
DATABITS = (7, 8)
STOPBITS = (1, 2)
SERIAL_FIELDS = ("baud", "parity", "databits", "stopbits")
# Reserved portmap.json key holding per-adapter serial overrides. Ports stay
# flat top-level `stable_id: port` entries, exactly as older versions wrote
# them — so an older bridge (downgrade) still reads its ports and only drops
# this one entry as "invalid".
SERIAL_STATE_KEY = "__serial__"


def _as_int(v, field: str) -> int:
    if isinstance(v, bool):
        raise ValueError(f"{field}: expected a number")
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    raise ValueError(f"{field}: expected a number, got {v!r}")


def validate_serial(body, base: dict) -> dict:
    """Merge `body` (any subset of baud/parity/databits/stopbits) onto `base`
    and validate the result. Raises ValueError with an operator-readable
    message; returns a normalised dict with all four fields."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    unknown = set(body) - set(SERIAL_FIELDS)
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")
    out = dict(base)
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
    return {k: out[k] for k in SERIAL_FIELDS}


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
    """Short human form: "9600 8N1"."""
    return f"{p['baud']} {p['databits']}{p['parity']}{p['stopbits']}"


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
_ser2net: subprocess.Popen | None = None


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def enumerate_adapters() -> list[dict]:
    """Current USB serial adapters, with stable identity, read from sysfs."""
    out = []
    for dev in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
        name = os.path.basename(dev)
        usb = os.path.realpath(f"/sys/class/tty/{name}/device")
        for _ in range(8):                       # walk up to the USB device dir
            if os.path.exists(os.path.join(usb, "idVendor")):
                break
            usb = os.path.dirname(usb)
        vendor = _read(f"{usb}/idVendor")
        product = _read(f"{usb}/idProduct")
        serial = _read(f"{usb}/serial")
        port_path = os.path.basename(usb)        # e.g. 1-5
        # stable key: unique serial if the adapter has one, else USB port-path
        stable = serial if serial else f"{vendor}:{product}@{port_path}"
        out.append({
            "dev": dev, "name": name, "vendor_id": vendor, "product_id": product,
            "serial": serial, "manufacturer": _read(f"{usb}/manufacturer"),
            "model": _read(f"{usb}/product"), "port_path": port_path,
            "stable_id": stable,
        })
    return out


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
        if k == SERIAL_STATE_KEY:
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
    return ports, serial


def dump_state(ports: dict, serial: dict) -> dict:
    """Inverse of parse_state. Ports stay top-level (older versions can still
    read them); overrides go under the reserved key, omitted when empty."""
    out = dict(ports)
    if serial:
        out[SERIAL_STATE_KEY] = {k: dict(v) for k, v in sorted(serial.items())}
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


def _save_state(ports: dict, serial: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(dump_state(ports, serial), f, indent=1)
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


def render_ser2net_config(adapters: list[dict]) -> str:
    """ser2net YAML for the MANAGED adapters, each with its own serial
    params (`a["serial"]`, default when absent). Pure."""
    parts = ["%YAML 1.1", "---"]
    for a in adapters:
        params = ser2net_params(a.get("serial") or DEFAULT_SERIAL)
        parts += [
            f"connection: &{a['name']}",
            f"  accepter: tcp,{a['tcp_port']}",
            f"  connector: serialdev,{a['dev']},{params},local",
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
        _clear_stale_locks()                     # ser2net not running → locks are leftovers
        _ser2net = subprocess.Popen(["ser2net", "-n", "-c", SER2NET_CFG])


def reconcile() -> None:
    """Idempotent: enumerate, and if the managed adapter set (or any managed
    adapter's serial params) changed, regen + reload. Excluded adapters
    (claimed elsewhere, e.g. a BMS) are listed but NEVER exposed — the bridge
    never opens their serial line."""
    global _adapters
    with _lock:
        new = enumerate_adapters()
        pmap, serial = _load_state()
        for a in new:
            a["excluded"] = _is_excluded(a)
            a["available"] = not a["excluded"]
            a["serial_params"] = serial_for(a["stable_id"], serial)
            a["serial_custom"] = a["stable_id"] in serial
            if a["available"]:
                # assign from the persistent map on EVERY pass, so tcp_port is
                # always populated (incl. the unchanged/early-return path)
                a["tcp_port"] = assign_port(a["stable_id"], pmap)
        managed = [a for a in new if a["available"]]
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
        if sig == prev and _adapters and _ser2net and _ser2net.poll() is None:
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
        _reload_ser2net()                        # excluded adapters NOT in the config
        _adapters = new
        expo = ", ".join(f"{a['name']}({a['stable_id']})->:{a['tcp_port']} "
                         f"{serial_text(a['serial_params'])}" for a in managed) or "none"
        excl = ", ".join(a["name"] for a in new if a["excluded"])
        print(f"[supervisor] reconciled: exposed[{expo}]"
              + (f" excluded[{excl}]" if excl else ""), flush=True)


def _signature(adapters: list[dict]) -> set:
    return {(a["stable_id"], a["dev"], ser2net_params(a.get("serial_params") or DEFAULT_SERIAL))
            for a in adapters}


def set_adapter_serial(key: str, body) -> dict | None:
    """Validate + persist an adapter's serial params, then reconcile (which
    rewrites that adapter's ser2net line and SIGHUPs — only the changed port
    restarts). Returns the adapter's API view, None when the key is not a
    present adapter. Raises ValueError on invalid params."""
    with _lock:                                  # RLock: reconcile() re-enters
        if not any(a["stable_id"] == key for a in _adapters):
            return None
        pmap, serial = _load_state()
        params = validate_serial(body, serial_for(key, serial))
        serial[key] = params
        _save_state(pmap, serial)
        reconcile()
        for a in _adapters:
            if a["stable_id"] == key:
                return adapter_view(a)
    return None                                  # unplugged in between


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
        "serial_params": dict(params),
        "serial_text": serial_text(params),
        "serial_custom": a.get("serial_custom", False),
    }


def route(method: str, raw_path: str) -> tuple[str, str | None]:
    """(endpoint, adapter key) for a request. Pure. The key is split on the
    RAW path and then percent-decoded, so keys containing ':' '@' or even an
    encoded '/' address the right adapter. endpoint is one of: health,
    adapters, serial, not_found, method_not_allowed."""
    path = urlsplit(raw_path).path.rstrip("/") or "/"
    parts = path.split("/")[1:]
    if parts == ["health"]:
        return ("health", None) if method == "GET" else ("method_not_allowed", None)
    if parts == ["adapters"]:
        return ("adapters", None) if method == "GET" else ("method_not_allowed", None)
    if len(parts) == 3 and parts[0] == "adapters" and parts[2] == "serial" and parts[1]:
        key = unquote(parts[1])
        return ("serial", key) if method == "POST" else ("method_not_allowed", key)
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
                        "auth": bool(TOKEN), "adapters": data})
        elif endpoint == "serial":
            self._set_serial(key)
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
        if self._authorized():
            body["hostname"] = _hostname()
        self._json(body, code=code)

    def _set_serial(self, key: str):
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
            view = set_adapter_serial(key, body)
        except ValueError as e:
            return self._json({"error": str(e)}, code=400)
        except Exception as e:  # noqa: BLE001
            print(f"[supervisor] serial update for {key} failed: {e}", flush=True)
            return self._json({"error": "could not apply serial parameters"}, code=500)
        if view is None:
            return self._json({"error": f"no adapter {key!r}"}, code=404)
        print(f"[supervisor] serial params for {key}: {view['serial_text']}", flush=True)
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


def main():
    reconcile()                                   # initial: enumerate + start ser2net
    threading.Thread(target=_http_server, daemon=True).start()
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
