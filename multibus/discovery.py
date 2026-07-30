# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
"""Modbus device auto-discovery (onboarding helper).

Two read-only probes to find devices without knowing their address up front:
  * scan_tcp   — sweep a private CIDR on a port (default 502) for hosts that
                 answer a Modbus read.
  * sweep_units — sweep unit/slave IDs on one endpoint (TCP host or RTU serial
                 line) for the ones that respond.

A "response" is a connect PLUS any Modbus reply to a holding-register read —
including an exception reply (e.g. illegal address), which still proves the peer
speaks Modbus. Probes never write. Network scanning is restricted to PRIVATE LAN
ranges so the appliance can't be used to scan the internet.
"""
import ipaddress
import logging
import socket
import time
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)


def lan_host_error(host: str, allow_nonlan: bool = False):
    """Return an error string if ``host`` (a literal IP OR a hostname) does not
    resolve to ONLY private LAN addresses, else None. Lets the unit-sweep accept
    LAN names (meter.local) like normal device config, not just literal IPs."""
    host = str(host or "").strip()
    if not host:
        return "host required"
    if allow_nonlan:
        return None
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        try:
            ips = list({ai[4][0] for ai in socket.getaddrinfo(host, None)})
        except OSError as e:
            return f"could not resolve {host}: {e}"
    if not ips:
        return f"could not resolve {host}"
    for a in ips:
        ip = ipaddress.ip_address(a)
        if (not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast):
            return f"{host} ({a}) is not a private LAN address — scanning is restricted to the LAN"
    return None

MAX_HOSTS = 256          # cap a scan at a /24 so it stays quick and bounded
MAX_UNITS = 247          # Modbus unit-id range is 1..247


def hosts_from_cidr(cidr: str, allow_nonlan: bool = False, max_hosts: int = MAX_HOSTS):
    """Expand a CIDR (e.g. 192.168.1.0/24) to host IPs, refusing non-LAN ranges
    and anything larger than ``max_hosts``. Raises ValueError with a clear message."""
    try:
        net = ipaddress.ip_network(str(cidr).strip(), strict=False)
    except ValueError as e:
        raise ValueError(f"invalid network '{cidr}': {e}")
    hosts = [str(h) for h in net.hosts()] or [str(net.network_address)]
    if len(hosts) > max_hosts:
        raise ValueError(f"range too large ({len(hosts)} hosts) — use /{32 - (max_hosts).bit_length() + 1} or smaller (max {max_hosts})")
    if not allow_nonlan:
        for h in hosts:
            ip = ipaddress.ip_address(h)
            if (not ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast):
                raise ValueError(f"{h} is not a private LAN address — scanning is restricted to the LAN")
    return hosts


def _probe(client, unit_id: int) -> bool:
    """True if the connected client gets ANY Modbus reply (data or exception)."""
    from pymodbus.pdu import ExceptionResponse
    try:
        r = client.read_holding_registers(address=0, count=1, slave=int(unit_id))
    except Exception:  # noqa: BLE001
        return False
    if r is None:
        return False
    if isinstance(r, ExceptionResponse):
        return True                      # device answered with a Modbus exception
    return not r.isError()


def probe_tcp(host: str, port: int, unit_id: int, timeout: float):
    """Returns (tcp_open, modbus_ok) for one host."""
    from pymodbus.client import ModbusTcpClient
    c = ModbusTcpClient(host=host, port=int(port), timeout=timeout)
    tcp_open = modbus = False
    try:
        tcp_open = bool(c.connect())
        if tcp_open:
            modbus = _probe(c, unit_id)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    return tcp_open, modbus


def scan_tcp(hosts, port=502, unit_id=1, timeout=0.5, workers=64):
    """Probe every host concurrently; return the responders (TCP open OR Modbus)."""
    results = []

    def one(h):
        tcp_open, modbus = probe_tcp(h, port, unit_id, timeout)
        if tcp_open or modbus:
            return {"host": h, "port": int(port), "unit_id": int(unit_id),
                    "tcp_open": tcp_open, "modbus": modbus}
        return None

    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(hosts)))) as ex:
        for r in ex.map(one, hosts):
            if r:
                results.append(r)
    results.sort(key=lambda r: ipaddress.ip_address(r["host"]))
    return results


def sweep_units_tcp(host: str, port: int, unit_start: int, unit_end: int, timeout: float):
    """Reuse one TCP connection to sweep unit ids; return the responding ones."""
    from pymodbus.client import ModbusTcpClient
    found = []
    c = ModbusTcpClient(host=host, port=int(port), timeout=timeout)
    try:
        if not c.connect():
            return found
        for uid in range(max(1, unit_start), min(MAX_UNITS, unit_end) + 1):
            if _probe(c, uid):
                found.append(uid)
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    return found


def sweep_units_rtu(serial_port: str, unit_start: int, unit_end: int, timeout: float,
                    baudrate=9600, parity='N', stopbits=1, bytesize=8):
    """Sweep unit ids on a serial (RTU) line. Needs the adapter present."""
    from pymodbus.client import ModbusSerialClient
    found = []
    c = ModbusSerialClient(port=serial_port, baudrate=int(baudrate), parity=parity,
                           stopbits=int(stopbits), bytesize=int(bytesize), timeout=timeout)
    try:
        if not c.connect():
            return found
        for uid in range(max(1, unit_start), min(MAX_UNITS, unit_end) + 1):
            if _probe(c, uid):
                found.append(uid)
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass
    return found


# ── SunSpec model walk ──────────────────────────────────────────────────────
# The SunSpec information model self-describes: a "SunS" marker at one of the
# well-known bases, then a chain of (model id, length) headers to walk. The
# walk READS what the device declares — nothing here is fabricated, which is
# exactly why it beats shipping guessed register maps for inverters.

SUNSPEC_BASES = (40000, 50000, 0)
_SUNS = 0x53756E53  # "SunS"

SUNSPEC_MODEL_NAMES = {
    1: "Common (identity)",
    101: "Inverter — single phase (int+SF)",
    102: "Inverter — split phase (int+SF)",
    103: "Inverter — three phase (int+SF)",
    111: "Inverter — single phase (float)",
    112: "Inverter — split phase (float)",
    113: "Inverter — three phase (float)",
    120: "Nameplate ratings",
    121: "Basic settings",
    122: "Extended measurements & status",
    123: "Immediate controls",
    124: "Storage",
    126: "Volt-VAR",
    160: "MPPT extension",
    201: "Meter — single phase (int+SF)",
    202: "Meter — split phase (int+SF)",
    203: "Meter — wye three phase (int+SF)",
    204: "Meter — delta three phase (int+SF)",
    211: "Meter — single phase (float)",
    212: "Meter — split phase (float)",
    213: "Meter — wye three phase (float)",
    214: "Meter — delta three phase (float)",
    302: "Irradiance",
    307: "Base meteorological",
    401: "String combiner (basic)",
    402: "String combiner (advanced)",
    802: "Battery base",
    803: "Lithium-ion battery bank",
}

_MAX_MODELS = 60  # sanity cap against a corrupt chain


def _regs_to_str(regs):
    """Registers → ASCII (two chars per register, big-endian), NUL-trimmed."""
    b = b"".join(bytes([(r >> 8) & 0xFF, r & 0xFF]) for r in regs)
    return b.split(b"\x00")[0].decode("ascii", errors="replace").strip()


def sunspec_walk(host: str, port: int = 502, unit_id: int = 1, timeout: float = 2.0):
    """Walk the SunSpec model chain on one endpoint. Read-only (FC3).

    Returns {ok, base, identity?, models: [{id, name, length, addr}]} or
    {ok: False, error} when no SunS marker answers at any known base.
    """
    from pymodbus.client import ModbusTcpClient

    def read(c, addr, count):
        # one quiet retry — a single dropped reply mid-chain must not silently
        # truncate the model list (Datamanagers hiccup under connection churn)
        for attempt in range(2):
            r = c.read_holding_registers(address=addr, count=count, slave=unit_id)
            if not r.isError() and getattr(r, "registers", None):
                return r.registers
            if attempt == 0:
                time.sleep(0.3)
        return None

    c = ModbusTcpClient(host=host, port=int(port), timeout=timeout)
    try:
        if not c.connect():
            return {"ok": False, "error": f"could not connect to {host}:{port}"}
        # Some gateways (Fronius Datamanager) serve a limited pool of Modbus
        # connections and answer a fresh one with pure silence until an old
        # slot is released. Silence at every base therefore gets a full
        # close-wait-reconnect cycle (twice) before declaring non-SunSpec.
        base = None
        for attempt in range(3):
            base = next((b for b in SUNSPEC_BASES
                         if (m := read(c, b, 2)) and ((m[0] << 16) | m[1]) == _SUNS), None)
            if base is not None:
                break
            if attempt < 2:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(1.2)
                if not c.connect():
                    return {"ok": False, "error": f"could not connect to {host}:{port}"}
        if base is None:
            return {"ok": False,
                    "error": "no SunS marker at 40000/50000/0 — not a SunSpec device "
                             "(or a different unit id)"}

        models, identity = [], None
        addr = base + 2
        for _ in range(_MAX_MODELS):
            hdr = read(c, addr, 2)
            if hdr is None:
                break
            mid, length = hdr
            if mid == 0xFFFF:
                break
            name = SUNSPEC_MODEL_NAMES.get(
                mid, "Vendor extension" if mid >= 64000 else f"Model {mid}")
            models.append({"id": mid, "name": name, "length": length, "addr": addr})
            if mid == 1 and length >= 64:
                block = read(c, addr + 2, 66)
                if block:
                    identity = {
                        "manufacturer": _regs_to_str(block[0:16]),
                        "model": _regs_to_str(block[16:32]),
                        "options": _regs_to_str(block[32:40]),
                        "version": _regs_to_str(block[40:48]),
                        "serial": _regs_to_str(block[48:64]),
                    }
            addr += 2 + length
        return {"ok": True, "base": base, "unit_id": int(unit_id),
                "identity": identity, "models": models}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    finally:
        try:
            c.close()
        except Exception:  # noqa: BLE001
            pass


# ── MQTT topic browse ───────────────────────────────────────────────────────

def mqtt_browse(broker: str, port: int = 1883, username: str = "",
                password: str = "", tls: bool = False, topic_filter: str = "#",
                duration_s: float = 3.0, max_topics: int = 500):
    """Connect to a broker and collect the topics that speak, with payload
    previews. Retained messages arrive immediately on subscribe, so a broker
    carrying retained telemetry yields its topic tree in the first instant;
    the rest of the window catches live publishers. Read-only, bounded.

    Returns {ok, topics: [{topic, payload, retained, count}], count, truncated}
    or {ok: False, error}.
    """
    import threading as _th
    import paho.mqtt.client as mqtt

    duration_s = min(10.0, max(1.0, float(duration_s)))
    max_topics = min(2000, max(10, int(max_topics)))
    topic_filter = (topic_filter or "#").strip() or "#"

    topics: dict = {}
    lock = _th.Lock()
    connected = _th.Event()
    fail = {"error": None}

    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if username:
        cli.username_pw_set(username, password or "")
    if tls:
        try:
            cli.tls_set()
        except Exception:  # noqa: BLE001
            pass

    def _oc(c, u, f, rc, props=None):
        if getattr(rc, "is_failure", False):
            fail["error"] = f"broker refused the connection: {rc}"
            connected.set()
            return
        connected.set()
        c.subscribe(topic_filter, qos=0)

    def _om(c, u, m):
        with lock:
            e = topics.get(m.topic)
            if e is None:
                if len(topics) >= max_topics:
                    return
                e = topics[m.topic] = {"topic": m.topic, "count": 0,
                                       "retained": False, "payload": ""}
            e["count"] += 1
            e["retained"] = e["retained"] or bool(m.retain)
            e["payload"] = m.payload.decode("utf-8", "replace")[:300]

    cli.on_connect = _oc
    cli.on_message = _om
    try:
        cli.connect(str(broker).strip(), int(port), keepalive=15)
        cli.loop_start()
        if not connected.wait(timeout=4.0):
            fail["error"] = f"could not connect to {broker}:{port}"
        elif fail["error"] is None:
            time.sleep(duration_s)
    except Exception as e:  # noqa: BLE001
        fail["error"] = f"connect failed: {e}"
    finally:
        try:
            cli.loop_stop()
            cli.disconnect()
        except Exception:  # noqa: BLE001
            pass
    if fail["error"]:
        return {"ok": False, "error": fail["error"]}
    with lock:
        out = sorted(topics.values(), key=lambda t: t["topic"])
    return {"ok": True, "topics": out, "count": len(out),
            "truncated": len(out) >= max_topics}


def mqtt_sample(broker: str, port: int = 1883, username: str = "",
                password: str = "", tls: bool = False, topic: str = "",
                timeout_s: float = 4.0, max_bytes: int = 16384):
    """Grab ONE full message from a topic (retained → instant) so the user can
    pick a json_path from the real payload instead of typing it blind.
    Returns {ok, topic, payload, retained} or {ok: False, error}."""
    import threading as _th
    import paho.mqtt.client as mqtt

    topic = (topic or "").strip()
    if not topic:
        return {"ok": False, "error": "topic required"}
    timeout_s = min(15.0, max(1.0, float(timeout_s)))

    got = {}
    done = _th.Event()
    fail = {"error": None}

    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if username:
        cli.username_pw_set(username, password or "")
    if tls:
        try:
            cli.tls_set()
        except Exception:  # noqa: BLE001
            pass

    def _oc(c, u, f, rc, props=None):
        if getattr(rc, "is_failure", False):
            fail["error"] = f"broker refused the connection: {rc}"
            done.set()
            return
        c.subscribe(topic, qos=0)

    def _om(c, u, m):
        if done.is_set():
            return  # keep the FIRST sample; later arrivals must not overwrite it
        got["topic"] = m.topic
        got["retained"] = bool(m.retain)
        got["payload"] = m.payload[:max_bytes].decode("utf-8", "replace")
        done.set()

    cli.on_connect = _oc
    cli.on_message = _om
    try:
        cli.connect(str(broker).strip(), int(port), keepalive=15)
        cli.loop_start()
        done.wait(timeout=timeout_s)
    except Exception as e:  # noqa: BLE001
        fail["error"] = f"connect failed: {e}"
    finally:
        try:
            cli.loop_stop()
            cli.disconnect()
        except Exception:  # noqa: BLE001
            pass
    if fail["error"]:
        return {"ok": False, "error": fail["error"]}
    if not got:
        return {"ok": False,
                "error": f"no message on {topic!r} within {timeout_s:g}s — "
                         "nothing retained and nobody published; try while the device is sending"}
    return {"ok": True, **got}


# ---------------------------------------------------------------------------
# ESPHome node discovery (native API, TCP 6053)
#
# mDNS does not cross a Docker bridge network, so the reliable way to find
# ESPHome devices from inside the container is an active unicast sweep of the
# native-API port — the same LAN-restricted CIDR mechanics as scan_tcp. For
# identity we speak the plaintext framing of the native API: a HelloRequest
# is answered with name + server info BEFORE any authentication, and a
# noise-encrypted device reveals itself by the 0x01 frame indicator.
# ---------------------------------------------------------------------------

ESPHOME_API_PORT = 6053


def _pb_fields(buf: bytes) -> dict:
    """Minimal protobuf walk: {field_no: int|bytes}. Only what a HelloResponse
    needs (varint + length-delimited, single-byte lengths)."""
    out, i = {}, 0
    try:
        while i < len(buf):
            tag = buf[i]; i += 1
            field, wt = tag >> 3, tag & 7
            if wt == 0:                        # varint
                v = sh = 0
                while True:
                    b = buf[i]; i += 1
                    v |= (b & 0x7F) << sh; sh += 7
                    if not b & 0x80:
                        break
                out[field] = v
            elif wt == 2:                      # length-delimited (varint length)
                ln = sh = 0
                while True:
                    b = buf[i]; i += 1
                    ln |= (b & 0x7F) << sh; sh += 7
                    if not b & 0x80:
                        break
                out[field] = bytes(buf[i:i + ln]); i += ln
            else:                              # unexpected wire type — stop
                break
    except IndexError:
        pass                                   # truncated → keep what parsed
    return out


def _esphome_hello(host: str, port: int, timeout: float):
    """Probe one host. None = port closed; else a result dict. A device with
    API encryption answers the plaintext hello with a 0x01 indicator (noise)
    or drops the connection — still a positive detection."""
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return None
    res = {"host": host, "port": port, "name": "", "server_info": "",
           "api_version": "", "encrypted": False}
    try:
        s.settimeout(timeout)
        client = b"multi-bus-gateway"
        payload = b"\x0a" + bytes([len(client)]) + client   # HelloRequest.client_info
        s.sendall(b"\x00" + bytes([len(payload)]) + b"\x01" + payload)
        head = b""                             # TCP may fragment: read all 3 bytes
        while len(head) < 3:
            chunk = s.recv(3 - len(head))
            if not chunk:
                break
            head += chunk
        if not head:
            res["encrypted"] = True            # closed on plaintext → noise-only
            return res
        if len(head) < 3:
            return res                          # partial header → not ESPHome-ish
        if head[0] == 0x01:
            res["encrypted"] = True            # noise frame indicator
            return res
        if head[0] != 0x00:
            return res                          # something else on this port
        size, mtype = head[1], head[2]
        body = b""
        while len(body) < size:
            chunk = s.recv(size - len(body))
            if not chunk:
                break
            body += chunk
        if mtype == 2 and len(body) == size:    # a COMPLETE HelloResponse
            f = _pb_fields(body)
            res["server_info"] = f.get(3, b"").decode("utf-8", "replace") if isinstance(f.get(3), bytes) else ""
            res["name"] = f.get(4, b"").decode("utf-8", "replace") if isinstance(f.get(4), bytes) else ""
            if isinstance(f.get(1), int):
                res["api_version"] = f"{f.get(1)}.{f.get(2, 0)}"
    except (socket.timeout, TimeoutError):
        # a silent service that accepted the connection but never answered the
        # hello is NOT identifiable as ESPHome — don't report it as an encrypted
        # device (a reset, below, is the "refused plaintext → protected" signal)
        return None
    except OSError:
        res["encrypted"] = True                 # reset mid-hello → treat as protected
    finally:
        try:
            s.close()
        except OSError:
            pass
    return res


def scan_esphome(hosts, port: int = ESPHOME_API_PORT, timeout: float = 0.5,
                 workers: int = 64):
    """Probe every host concurrently; return ESPHome responders."""
    t0 = time.time()

    def one(h):
        # never let one misbehaving peer abort the whole sweep
        try:
            return _esphome_hello(h, port, timeout)
        except Exception:  # noqa: BLE001
            return None

    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(hosts)))) as ex:
        results = [r for r in ex.map(one, hosts) if r is not None]

    def _sort_key(r):
        try:
            return (0, int(ipaddress.ip_address(r["host"])))
        except ValueError:
            return (1, r["host"])
    results.sort(key=_sort_key)
    return {"scanned": len(hosts), "results": results,
            "elapsed_s": round(time.time() - t0, 2)}
