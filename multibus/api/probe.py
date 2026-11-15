# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Device probe / connection-test, extracted whole from ``create_api`` (P0.8b squeeze).

``ProbeService`` holds the twelve probe/test closures that used to live nested in
``create_api``: the per-source probe, the ad-hoc Modbus probe, the virtual-map
probe/hearing, the tap/MQTT/CAN hearings, the one-test-at-a-time guard and the
``test_saved_device`` dispatcher. They are kept as closures built in ``__init__``
so the bodies are **verbatim** — the collaborators they captured from
``create_api`` arrive as constructor arguments under the same names.

``config`` is the only runtime store. Four checks that belong to the device
validator (``refuse_if_listened``, ``serial_missing``, ``test_on_shared_bus``,
``listening_template``) are injected as the same ``_dv`` methods ``create_api``
already facade-aliases. This stays in the ``api`` layer (it raises
``HTTPException`` like the sibling services), mapping to ADR-0038's end state
later. The public surface is the methods exposed at the end of ``__init__``.
"""
from __future__ import annotations

import dataclasses
import threading
import time
from typing import Dict, Set

from fastapi import HTTPException

from multibus.devices.device_validation import _vmap_mode
from multibus.api.validation import _PROTO_WORDS
from multibus.core.config import (
    BUS_OVER_TCP_PROTOCOLS, CAN_PROTOCOLS, MODBUS_PROTOCOLS, NETWORK_PROTOCOLS,
    SERIAL_PROTOCOLS, VIRTUAL_MAP_OVER_TCP, VIRTUAL_MAP_PROTOCOLS, VIRTUAL_MAP_SERIAL)

# every Modbus way but listening (mirrors create_api's module-level const)
_POLLED_MODBUS = NETWORK_PROTOCOLS + SERIAL_PROTOCOLS


class ProbeService:
    """The device probe/test seam (ADR-0038 service, api-layer for now).

    Collaborators are injected explicitly, making visible what used to be
    captured implicitly from ``create_api``'s closure:

      config                 — the device store it reads
      refuse_if_listened     — ADR-0025 guard (``_dv`` method)
      serial_missing         — serial-port diagnosis (``_dv`` method)
      test_on_shared_bus     — probe through a live device's transport (``_dv``)
      listening_template     — whether a template is listen-only (``_dv`` method)
    """

    def __init__(self, config, *, refuse_if_listened, serial_missing,
                 test_on_shared_bus, listening_template):
        # Rebind the injected collaborators under the exact names the verbatim
        # bodies use, as they resolved in create_api (config param + _dv facades).
        _refuse_if_listened = refuse_if_listened
        _serial_missing = serial_missing
        _test_on_shared_bus = test_on_shared_bus
        _listening_template = listening_template

        def _source_probe(dev, src, address_of, client=None) -> Dict:
            """Probe ONE unit over ONE of its sources, the way that source reads it.

            An HTTP source is asked for its document and judged by how many of the
            unit's selected paths it carries; a Modbus source gets one FC3 read on
            its own connection; a pushed input (MQTT) cannot be probed — we say so
            rather than pretend."""
            proto = str(src.protocol or 'tcp').lower()
            if proto == 'http':
                from multibus.transports.http.http_client import HttpClient
                from multibus.core.redact import redact_url
                regs, _g = config.load_device_registers(dev, source=src)
                hc = HttpClient(dict(src.http or {}), regs, {},
                                allow_nonlan=config.security.allow_nonlan_http_devices)
                t0 = time.perf_counter()
                res = hc.test_read()
                res.setdefault('latency_ms', round((time.perf_counter() - t0) * 1000, 1))
                res['where'] = redact_url(hc.url)
                if hc.url and isinstance(res.get('message'), str):
                    # an error text may quote the URL, token and all
                    res['message'] = res['message'].replace(hc.url, res['where'])
                return res
            if proto in _POLLED_MODBUS and _listening_template(src.template or dev.template):
                # a map written for listening: the runtime taps it (ADR-0025) —
                # what it hears, never a question on the bus
                drv = _driver_of(client, src) if client is not None else None
                res = _tap_hearing(dev, drv) if drv is not None else {
                    'ok': None, 'message': "listen-only — this source taps its bus; nothing to ask"}
                res['where'] = src.connection.bus
                return res
            if proto in _POLLED_MODBUS:
                conn = dataclasses.asdict(src.connection)
                conn['protocol'] = proto
                timeout = float(conn.get('timeout', 3) or 3)
                res = _modbus_probe(conn, dev.connection.unit_id, timeout,
                                    address_of(src))
                res['where'] = (conn.get('serial_port') if proto in SERIAL_PROTOCOLS
                                else f"{conn.get('host', '')}:{conn.get('port', 502)}")
                return res
            if proto == 'mqtt':
                return _mqtt_hearing(_driver_of(client, src))
            return {'ok': None, 'message': 'not probed — this source is pushed to '
                                           'the gateway, it cannot be asked',
                    'where': str((src.mqtt_in or {}).get('topic', ''))}

        def _modbus_probe(conn: Dict, unit_id: int, timeout: float,
                          address: int = 0) -> Dict:
            """One ad-hoc Modbus probe (TCP or RTU): connect + FC3 read. ANY
            protocol-level answer (even a Modbus exception) proves a live device;
            only silence/timeouts fail. Used by the wizard's Test connection button."""
            from pymodbus.pdu import ExceptionResponse
            from multibus.transports.serial.bus_claims import canonical_serial
            from multibus.core.config import ModbusConfig
            from multibus.transports.modbus.modbus_client import _build_client
            proto = str(conn.get('protocol', 'tcp')).lower()
            rtu = proto in SERIAL_PROTOCOLS              # a serial port here (RTU or ASCII)
            # a bus a device only listens to is never asked (ADR-0025)
            _refuse_if_listened("A Test", proto, conn)
            if not rtu:
                # same LAN-egress policy as every /api/discover/* route — without
                # it the probe doubles as an internal TCP port scanner (audit L1)
                from multibus.commissioning.discovery import lan_host_error
                _e = lan_host_error(conn.get('host', ''),
                                    config.security.allow_nonlan_http_devices)
                if _e:
                    return {"ok": False, "message": f"blocked: {_e}"}
            t0 = time.perf_counter()
            sp = str(conn.get('serial_port', '') or '')
            where = (f"{sp}@{conn.get('baudrate', 9600)}" if rtu
                     else f"{conn.get('host', '')}:{conn.get('port', 502)}")
            if proto in BUS_OVER_TCP_PROTOCOLS + SERIAL_PROTOCOLS:
                # A bus already read by a running device: ask THROUGH its
                # connection (the shared transport), in turn with the slaves on
                # it — a private socket would make a single-client bridge drop
                # the live one (audit DP-19), and a tty opens once. Only the unit
                # id is the test's own.
                _th = str(conn.get('host', '')).strip().lower()
                _tp = int(conn.get('port', 502) or 502)
                _busy = next((_d for _d in config.devices
                              if getattr(_d, 'enabled', False) and getattr(_d, 'protocol', '') == proto
                              and ((rtu and canonical_serial(getattr(_d.connection, 'serial_port', ''))
                                    == canonical_serial(sp))
                                   or (not rtu and str(getattr(_d.connection, 'host', '')).strip().lower() == _th
                                       and int(getattr(_d.connection, 'port', 0) or 0) == _tp))), None)
                if _busy is not None:
                    return _test_on_shared_bus(conn, unit_id, address, timeout, where, _busy.id)
            c = _build_client(ModbusConfig(
                host=str(conn.get('host', '') or ''), port=int(conn.get('port', 502) or 502),
                unit_id=unit_id, timeout=timeout, protocol=proto, serial_port=sp,
                baudrate=int(conn.get('baudrate', 9600) or 9600), parity=str(conn.get('parity', 'N') or 'N'),
                stopbits=int(conn.get('stopbits', 1) or 1), bytesize=int(conn.get('bytesize', 8) or 8)))
            try:
                if not c.connect():
                    return {"ok": False,
                            "message": ((_serial_missing(sp) or f"Serial open of {where} failed — check the port/permissions")
                                        if rtu else
                                        f"{_PROTO_WORDS.get(proto, 'TCP')} connect to {where} failed — check IP/port/firewall")}
                rr = c.read_holding_registers(address=address, count=2, device_id=unit_id)
                lat = round((time.perf_counter() - t0) * 1000, 1)
                if not rr.isError():
                    return {"ok": True, "latency_ms": lat,
                            "message": f"Device answered in {lat} ms (unit {unit_id}, FC3 @ {address})"}
                if isinstance(rr, ExceptionResponse):
                    return {"ok": True, "latency_ms": lat,
                            "message": f"Device is alive (answered with Modbus exception "
                                       f"code {getattr(rr, 'exception_code', '?')} @ {address} "
                                       f"— try another register address)"}
                return {"ok": False,
                        "message": f"TCP connected but no Modbus response from unit {unit_id} "
                                   f"(timeout) — check unit ID"}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "message": f"probe failed: {e}"}
            finally:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass

        def _virtual_map_bus_refusal(protocol: str, conn: Dict) -> str:
            """Why a poll Test must not transmit on this bus, or '' when it may.
            A bus another device holds is asked only when that device POLLS it
            with the same protocol (the Test then takes turns on its transport);
            a bus listened to (a master already polls it), a tap, or another
            protocol (Modbus) is never written to."""
            from multibus.transports.serial.bus_claims import canonical_serial
            serial = protocol in VIRTUAL_MAP_SERIAL
            sp = canonical_serial(conn.get('serial_port', ''))
            host = str(conn.get('host', '') or '').strip().lower()
            try:
                port = int(conn.get('port', 0) or 0)
            except (TypeError, ValueError):
                port = 0
            for d in config.devices:
                dproto = str(getattr(d, 'protocol', '') or '').lower()
                c = d.connection
                if serial:
                    same = (dproto in SERIAL_PROTOCOLS + ('rtu_tap',) + VIRTUAL_MAP_SERIAL
                            and canonical_serial(getattr(c, 'serial_port', '')) == sp)
                    where = f"'{sp}'"
                else:
                    same = (dproto not in ('http', 'mqtt', 'none') and dproto not in SERIAL_PROTOCOLS + VIRTUAL_MAP_SERIAL
                            and str(getattr(c, 'host', '') or '').strip().lower() == host
                            and int(getattr(c, 'port', 0) or 0) == port)
                    where = f"{host}:{port}"
                if not same:
                    continue
                if dproto != protocol:
                    return (f"{where} is used by device '{d.id}' as {dproto} — a Seplos question there would "
                            f"land on another protocol's bus; nothing sent")
                if _vmap_mode(c) == 'listen':
                    return (f"{where} is listened to by device '{d.id}' — a master pack polls that bus, and a "
                            f"question from here would collide with it; nothing sent")
            return ''

        def _virtual_map_probe(protocol: str, conn: Dict) -> Dict:
            """One read request to an unsaved virtual-map device (listen mode asks
            nothing), through the bus's shared transport."""
            import importlib
            from multibus.core.config import ModbusConfig
            from multibus.devices.device_runtime import VIRTUAL_MAP_DRIVERS
            if protocol in VIRTUAL_MAP_SERIAL and not str(conn.get('serial_port', '')).strip():
                raise HTTPException(status_code=422, detail={"errors": ["connection.serial_port required"]})
            if protocol in VIRTUAL_MAP_OVER_TCP and not str(conn.get('host', '')).strip():
                raise HTTPException(status_code=422, detail={"errors": ["connection.host required"]})
            if _vmap_mode(conn) == 'poll':
                if protocol in VIRTUAL_MAP_OVER_TCP:
                    # the LAN-egress policy of every probe (audit L1)
                    from multibus.commissioning.discovery import lan_host_error
                    _e = lan_host_error(conn.get('host', ''), config.security.allow_nonlan_http_devices)
                    if _e:
                        return {"ok": False, "message": f"blocked: {_e}"}
                _refuse_if_listened("A Test", protocol, conn)       # ADR-0025
                _busy = _virtual_map_bus_refusal(protocol, conn)
                if _busy:
                    return {"ok": False, "message": _busy}
            try:
                mc = ModbusConfig(host=str(conn.get('host', '') or ''), port=int(conn.get('port', 0) or 0),
                                  unit_id=int(conn.get('unit_id', 0) or 0), protocol=protocol,
                                  serial_port=str(conn.get('serial_port', '') or ''),
                                  baudrate=int(conn.get('baudrate', 9600) or 9600),
                                  parity=str(conn.get('parity', 'N') or 'N'),
                                  stopbits=int(conn.get('stopbits', 1) or 1),
                                  bytesize=int(conn.get('bytesize', 8) or 8), mode=_vmap_mode(conn))
                timeout = float(conn.get('timeout', 3) or 3)
            except (TypeError, ValueError) as e:
                raise HTTPException(status_code=422, detail={"errors": [f"connection: {e}"]})
            mod = importlib.import_module(VIRTUAL_MAP_DRIVERS[protocol][0])
            return mod.probe(mc, timeout=timeout)

        def _virtual_map_hearing(dev_cfg, client) -> Dict:
            """A saved virtual-map device's test: what its running client has got
            (polled: answers and errors; listening: what it heard) — never a
            second master on the bus."""
            c = dev_cfg.connection
            srcs = dev_cfg.sources or []
            drv = _driver_of(client, srcs[0]) if (client is not None and srcs) else None
            if drv is None or not hasattr(drv, 'get_stats'):
                return {"ok": False, "message": f"not reading {c.bus} (device disabled or not started)"}
            st = drv.get_stats()
            bus = st.get('bus') or {}
            if bus.get('open_error'):
                return {"ok": False, "message": f"{c.bus}: {bus['open_error']}"}
            ec = st.get('error_counts') or {}
            good, age = int(st.get('successful_reads') or 0), st.get('staleness_age_s')
            errs = ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in ec.items() if v)
            listen = _vmap_mode(c) == 'listen'
            if not good:
                if listen:
                    frames = int(bus.get('frames') or 0)
                    why = (f"the bus is live ({frames} frames) but no answer from pack {c.unit_id} yet"
                           if frames else f"{c.bus} is open but silent (master pack off? A/B swapped? "
                                          f"line not {c.baudrate} {c.parity})")
                    if bus.get('warning'):
                        why += f" — {bus['warning']}"
                else:
                    why = f"no answer from pack {c.unit_id} yet" + (f" — {errs}" if errs else "")
                return {"ok": False, "message": why}
            said = (f"{'hearing' if listen else 'reading'} pack {c.unit_id} — {good} answers, "
                    f"last {age} s ago" + (f" · {errs}" if errs else ""))
            if st.get('last_latency_ms') is not None:
                said += f" · {st['last_latency_ms']} ms"
            return {"ok": True, "message": said}

        def _tap_hearing(dev_cfg, client) -> Dict:
            """A tap's "test": not a probe (it never transmits) but what it has
            HEARD — the line open, the bus alive, this unit's windows arriving.
            Each failure names the layer, so "wrong A/B" reads differently from
            "the master never asks unit 7"."""
            c = dev_cfg.connection
            if client is None or not hasattr(client, 'get_stats'):
                return {"ok": False, "message": f"not listening on {c.bus} "
                                                "(device disabled or not started)"}
            st = client.get_stats()
            bus = st.get('bus') or {}
            if bus.get('open_error'):
                return {"ok": False, "message": f"{c.bus}: {bus['open_error']}"}
            frames = int(bus.get('frames') or 0)
            if not frames:
                return {"ok": False, "message": f"{c.bus} is open but silent — no "
                                                "valid frame yet (master off? A/B swapped? "
                                                f"line not {c.baudrate} {c.parity})"}
            wins, age = st.get('successful_reads') or 0, st.get('staleness_age_s')
            mm = st.get('table_mismatch')
            if mm and not st.get('updates'):
                fc = {'holding': 'FC3', 'input': 'FC4'}
                return {"ok": False, "message": f"the master reads unit {c.unit_id} @{mm['address']} as "
                                                f"{mm['heard']} registers ({fc.get(mm['heard'], '')}) but the "
                                                f"map declares them {mm['map']} — set those rows' register "
                                                f"type to {mm['heard']} in the template"}
            if not wins:
                return {"ok": False, "message": f"the bus is live ({frames} frames) but "
                                                f"nothing for unit {c.unit_id} yet"}
            return {"ok": True, "message": f"hearing unit {c.unit_id} — {wins} windows, "
                                           f"last {age} s ago · {frames} frames on "
                                           f"{c.bus}"}

        # one Test at a time per device / installation: a probe opens another
        # client on hardware that serves few (a Fronius datalogger falls over
        # past ~26 transactions a minute) — a second click waits its turn
        _tests_running: Set[str] = set()
        _tests_lock = threading.Lock()

        def _one_test_at_a_time(key: str):
            with _tests_lock:
                if key in _tests_running:
                    raise HTTPException(status_code=409, detail="a test of this is already running — wait for its answer")
                _tests_running.add(key)

        def _test_done(key: str):
            with _tests_lock:
                _tests_running.discard(key)

        def _mqtt_hearing(drv) -> Dict:
            """A pushed MQTT source's "test": what its running input hears — never
            a second connection to the broker (a GX allows few). ``drv`` is the
            source's own input (MqttInput), or None when nothing runs."""
            if drv is None or not hasattr(drv, 'get_stats'):
                # nothing runs to ask: said plainly, not counted as a failed source
                return {"ok": None, "message": "not listening (device disabled or not started) — "
                                               "a pushed source cannot be asked, only heard"}
            where = f"{getattr(drv, 'broker', '')}:{getattr(drv, 'port', '')}"
            st = drv.get_stats() or {}
            try:
                dh = drv.data_health() or {}
            except Exception:  # noqa: BLE001
                dh = {}
            if not st.get('connected'):
                return {"ok": False, "message": f"not connected to {where}", "where": where}
            # the device's own word first: an availability message is no value
            if dh.get('available') is False:
                return {"ok": False, "message": f"connected to {where} — the device says it is offline",
                        "where": where}
            if dh.get('status') == 'idle':
                return {"ok": None, "message": f"connected to {where} — no value selected to read",
                        "where": where}
            n = int(st.get('messages') or 0)
            if not n:
                dropped = int(getattr(drv, 'retained_dropped', 0) or 0)
                why = (f" — {dropped} retained message(s) ignored (accept_retained is off)" if dropped
                       else " — nothing on its topics yet")
                return {"ok": None, "message": f"connected to {where}{why}", "where": where}
            age = dh.get('staleness_age_s')
            said = f"connected to {where} · {n} messages" + (f", last {age} s ago" if age is not None else "")
            if dh.get('stale') or dh.get('status') == 'down':
                return {"ok": False, "message": said + " — older than its stale limit", "where": where}
            return {"ok": True, "message": said, "where": where}

        def _driver_of(client, src):
            """The running input of one source of a device (MultiSourceClient.parts)."""
            for s, drv in (getattr(client, 'parts', None) or []):
                if getattr(s, 'id', None) == getattr(src, 'id', None):
                    return drv
            return None


        def _can_test(can_cfg: Dict, client=None) -> Dict:
            """A CAN device's Test: the interface (exists, up, listen-only) and
            what it carries for ~2 s, heard on a receive-only socket; for a saved
            device, what its running driver has counted too. One at a time per
            interface."""
            from multibus.transports.can.can_client import hear, hear_verdict, stats_summary
            key = f"can:{can_cfg.get('channel') or 'can0'}"
            _one_test_at_a_time(key)
            try:
                h = hear(can_cfg, seconds=2.0)
            finally:
                _test_done(key)
            ok, msg = hear_verdict(h)
            out = {"ok": ok, "message": msg, "can": h}
            drv = stats_summary(client.get_stats() or {}) if client is not None and hasattr(client, 'get_stats') else None
            if drv:
                out["driver"] = drv
                out["message"] += (f" · the device has counted {drv.get('frames') or 0} frames, "
                                   f"{drv.get('unknown_frames') or 0} on unknown ids, "
                                   f"{drv.get('decode_errors') or 0} decode errors")
            return out

        def _test_saved_device(dev_cfg, _client) -> Dict:
            if dev_cfg.protocol in CAN_PROTOCOLS:
                srcs = dev_cfg.sources or []
                return _can_test(dict(getattr(srcs[0], 'can', None) or {}) if srcs else {}, _client)
            if dev_cfg.protocol == 'rtu_tap' or (dev_cfg.protocol in MODBUS_PROTOCOLS
                                                 and _listening_template(dev_cfg.template)):
                # a tap, or a stored device the runtime starts as one (ADR-0025)
                return _tap_hearing(dev_cfg, _client)
            if dev_cfg.protocol in VIRTUAL_MAP_PROTOCOLS:
                return _virtual_map_hearing(dev_cfg, _client)
            # every source it is read through, each the way that source reads it
            # (a pushed MQTT source: what its running input hears)
            srcs = [x for x in (dev_cfg.sources or []) if getattr(x, 'enabled', True)]
            if srcs and (len(srcs) > 1 or str(srcs[0].protocol).lower() in ('mqtt', 'http')):
                def address_of(src=None):
                    rr, _g = config.load_device_registers(dev_cfg, source=src)
                    return rr[0].address if rr else 0
                rows = [{'source': x.id, **_source_probe(dev_cfg, x, address_of, _client)} for x in srcs]
                if len(rows) == 1:
                    return {k: v for k, v in rows[0].items() if k != 'source'}
                oks = [r.get('ok') for r in rows]
                return {"ok": all(o is True for o in oks) if None not in oks else (False if False in oks else None),
                        "message": " · ".join(f"{r['source']}: {r.get('message', '')}" for r in rows),
                        "sources": rows}
            regs, _g = config.load_device_registers(dev_cfg)
            address = regs[0].address if regs else 0
            c = dev_cfg.connection
            conn = {"protocol": dev_cfg.protocol, "host": c.host, "port": c.port,
                    "serial_port": c.serial_port, "baudrate": c.baudrate,
                    "parity": c.parity, "stopbits": c.stopbits, "bytesize": c.bytesize}
            return _modbus_probe(conn, c.unit_id, float(c.timeout), address)

        # --- public surface (what create_api / the routes call) -------------
        self.source_probe = _source_probe
        self.modbus_probe = _modbus_probe
        self.virtual_map_bus_refusal = _virtual_map_bus_refusal
        self.virtual_map_probe = _virtual_map_probe
        self.virtual_map_hearing = _virtual_map_hearing
        self.tap_hearing = _tap_hearing
        self.one_test_at_a_time = _one_test_at_a_time
        self.test_done = _test_done
        self.mqtt_hearing = _mqtt_hearing
        self.driver_of = _driver_of
        self.can_test = _can_test
        self.test_saved_device = _test_saved_device
