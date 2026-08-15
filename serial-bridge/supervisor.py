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
- Exposes GET /adapters on the control port so MBG can scan the live inventory.
"""
from __future__ import annotations

import glob
import json
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE_FILE = os.environ.get("BRIDGE_STATE", "/data/portmap.json")
# Lives in its own directory (owned by the non-root user) because the atomic
# rewrite (tmp + os.replace) needs write permission on the DIRECTORY.
SER2NET_CFG = os.environ.get("SER2NET_CFG", "/etc/ser2net/ser2net.yaml")
PORT_LOW, PORT_HIGH = 7001, 7099
CONTROL_PORT = int(os.environ.get("BRIDGE_CONTROL_PORT", "7000"))
# ser2net serial params applied to every adapter. Per-adapter baud is a later
# refinement (RFC2217 or a control API); today's meters are 9600 8N1.
SERIAL_PARAMS = os.environ.get("BRIDGE_SERIAL_PARAMS", "9600n81")
# Adapters to NEVER expose — claimed by another service (e.g. the Seplos BMS on
# its own container). Comma-separated; each token matches a stable_id, dev path,
# or USB port-path. Excluded adapters are LISTED (available=false) but never get
# a ser2net connection, so their serial line is never opened by the bridge.
EXCLUDE = {t.strip() for t in os.environ.get("BRIDGE_EXCLUDE", "").split(",") if t.strip()}


def _is_excluded(a: dict) -> bool:
    return bool(EXCLUDE & {a["stable_id"], a["dev"], a["port_path"], a["name"]})

_lock = threading.Lock()
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


def _load_map() -> dict:
    """Port map from disk. A corrupt/unreadable map is LOUD (audit DP-30):
    silently starting from {} re-allocates ports in enumeration order, which
    can hand a well-known port to the wrong adapter — MBG would then poll a
    different physical bus. Entries are validated as int ports in range."""
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            raw = json.load(f)
        out = {}
        for k, v in raw.items():
            if isinstance(v, int) and PORT_LOW <= v <= PORT_HIGH and v not in out.values():
                out[str(k)] = v
            else:
                print(f"[supervisor] portmap entry dropped (invalid): {k}={v!r}", flush=True)
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[supervisor] PORTMAP UNREADABLE ({e}) — starting empty; "
              f"port assignments may change, check adapter/port pairing!", flush=True)
        return {}


def _save_map(m: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(m, f, indent=1)
        f.flush()
        os.fsync(f.fileno())      # durable before the rename (audit DP-30:
    os.replace(tmp, STATE_FILE)   # a host freeze left a 0-byte map once)


def _assign_port(stable_id: str, pmap: dict) -> int:
    if stable_id in pmap:
        return pmap[stable_id]
    used = set(pmap.values())
    for p in range(PORT_LOW, PORT_HIGH + 1):
        if p not in used:
            pmap[stable_id] = p
            return p
    raise RuntimeError("no free bridge ports left")


def _write_ser2net_config(adapters: list[dict]) -> None:
    parts = ["%YAML 1.1", "---"]
    for a in adapters:
        parts += [
            f"connection: &{a['name']}",
            f"  accepter: tcp,{a['tcp_port']}",
            f"  connector: serialdev,{a['dev']},{SERIAL_PARAMS},local",
            "  options:",
            "    kickolduser: true",
            "    max-connections: 1",
            "",
        ]
    tmp = SER2NET_CFG + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(parts) + "\n")
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
    """Idempotent: enumerate, and if the managed adapter set changed, regen +
    reload. Excluded adapters (claimed elsewhere, e.g. Seplos) are listed but
    NEVER exposed — the bridge never opens their serial line."""
    global _adapters
    with _lock:
        new = enumerate_adapters()
        pmap = _load_map()
        for a in new:
            a["excluded"] = _is_excluded(a)
            a["available"] = not a["excluded"]
            if a["available"]:
                # assign from the persistent map on EVERY pass, so tcp_port is
                # always populated (incl. the unchanged/early-return path)
                a["tcp_port"] = _assign_port(a["stable_id"], pmap)
        managed = [a for a in new if a["available"]]
        # only the MANAGED set drives ser2net; changes to it trigger a reload.
        # An unchanged set only short-circuits while ser2net is actually alive —
        # otherwise a dead ser2net stayed dead until the next adapter change.
        # The signature includes the DEV PATH (audit DP-4): an unplug+replug
        # inside one reconcile window keeps the stable_id set identical while
        # the kernel renumbers the node — matching on ids alone left ser2net
        # opening a dead /dev (or, with a recycled node, the WRONG bus)
        # forever.
        sig = {(a["stable_id"], a["dev"]) for a in managed}
        prev = {(a["stable_id"], a["dev"]) for a in _adapters if a.get("available")}
        if sig == prev and _adapters and _ser2net and _ser2net.poll() is None:
            _adapters = new                      # no set/path change → no reload
            return
        if not managed and not prev and _adapters is not None:
            # zero adapters, still zero adapters (audit DP-32): rewriting the
            # config + SIGHUP every 10s was pure noise exactly when the
            # operator wants clean logs
            _adapters = new
            return
        _save_map(pmap)
        _write_ser2net_config(managed)           # excluded adapters NOT in the config
        _reload_ser2net()
        _adapters = new
        expo = ", ".join(f"{a['name']}({a['stable_id']})->:{a['tcp_port']}" for a in managed) or "none"
        excl = ", ".join(a["name"] for a in new if a["excluded"])
        print(f"[supervisor] reconciled: exposed[{expo}]"
              + (f" excluded[{excl}]" if excl else ""), flush=True)


class _Handler(BaseHTTPRequestHandler):
    timeout = 10                                 # a mute client can't hold a thread forever

    def do_GET(self):  # noqa: N802
        path = self.path.rstrip("/") or "/"
        if path == "/adapters":
            with _lock:
                data = [{
                    "stable_id": a["stable_id"], "dev": a["dev"],
                    "vendor_id": a["vendor_id"], "product_id": a["product_id"],
                    "serial": a["serial"], "manufacturer": a["manufacturer"],
                    "model": a["model"], "port_path": a["port_path"],
                    "tcp_port": a.get("tcp_port"), "connected": True,
                    "available": a.get("available", True),
                    "excluded": a.get("excluded", False),
                } for a in _adapters]
            self._json({"adapters": data})
        elif path == "/health":
            # the DATA path matters, not just this API (audit DP-18): a dead
            # ser2net with adapters to serve = the bus is down — report it so
            # the container healthcheck stops declaring a dead bridge healthy
            with _lock:
                n_managed = sum(1 for a in _adapters
                                if a.get("available") and not a.get("excluded"))
            ser2net_ok = _ser2net is not None and _ser2net.poll() is None
            if n_managed and not ser2net_ok:
                self._json({"status": "down", "reason": "ser2net not running",
                            "adapters": len(_adapters)}, code=500)
            else:
                self._json({"status": "ok", "adapters": len(_adapters)})
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
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
    print(f"[supervisor] control API on :{CONTROL_PORT}/adapters", flush=True)
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
