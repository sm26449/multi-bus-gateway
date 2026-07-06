"""Frame-level Modbus bus monitor — the REAL bytes on the wire, per transaction.

Captures every ADU exactly as pymodbus sends/receives it by shadowing the
sync client's ``send``/``recv`` on the instance (the transaction manager calls
``client.send(full_adu)`` once per attempt and ``client.recv(size)`` one or
more times for the reply), so each RETRY shows up as its own entry — which is
exactly what a commissioning engineer wants to see.

Off by default and runtime-only (never persisted): when disabled the wrappers
cost one boolean check per call. Entries live in a bounded ring in memory and
are decoded at capture time (unit, FC, address, exception code, RTU CRC check)
so reads from the API are free.
"""
from __future__ import annotations

import struct
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional

_FC_NAMES = {
    1: "Read Coils", 2: "Read Discrete Inputs",
    3: "Read Holding Registers", 4: "Read Input Registers",
    5: "Write Single Coil", 6: "Write Single Register",
    7: "Read Exception Status", 8: "Diagnostics",
    15: "Write Multiple Coils", 16: "Write Multiple Registers",
    17: "Report Server ID", 22: "Mask Write Register",
    23: "Read/Write Multiple Registers", 43: "Encapsulated Interface",
}

_EXC_NAMES = {
    1: "ILLEGAL FUNCTION", 2: "ILLEGAL DATA ADDRESS", 3: "ILLEGAL DATA VALUE",
    4: "SERVER DEVICE FAILURE", 5: "ACKNOWLEDGE", 6: "SERVER DEVICE BUSY",
    8: "MEMORY PARITY ERROR", 10: "GATEWAY PATH UNAVAILABLE",
    11: "GATEWAY TARGET FAILED TO RESPOND",
}

# request PDUs where bytes 1-4 are (address, count) / (address, value)
_ADDR_COUNT_FCS = {1, 2, 3, 4, 15, 16, 23}
_ADDR_VALUE_FCS = {5, 6}


def _crc16(data: bytes) -> int:
    """Modbus RTU CRC-16 (poly 0xA001, init 0xFFFF)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _split_adu(proto: str, adu: bytes):
    """→ (tid, unit, pdu, crc_ok) — tid/crc_ok are None where not applicable."""
    if proto == "rtu":
        if len(adu) < 4:
            return None, None, b"", None
        crc_ok = struct.unpack("<H", adu[-2:])[0] == _crc16(adu[:-2])
        return None, adu[0], adu[1:-2], crc_ok
    if len(adu) < 8:  # MBAP header (7) + at least the function code
        return None, None, b"", None
    tid = struct.unpack(">H", adu[0:2])[0]
    return tid, adu[6], adu[7:], None


def decode_transaction(proto: str, tx: bytes, rx: bytes) -> Dict[str, Any]:
    """Decode a request/response ADU pair into display metadata.

    ``result``: ok | exception | no_response | crc_error | mismatch | malformed.
    Never raises — garbage on the wire is precisely what this tool is for.
    """
    out: Dict[str, Any] = {"unit": None, "tid": None, "fc": None, "fc_name": None,
                           "addr": None, "count": None, "value": None,
                           "result": "no_response", "exc": None, "exc_name": None}
    tid, unit, pdu, _ = _split_adu(proto, tx)
    if not pdu:
        out["result"] = "malformed"
        return out
    out["tid"], out["unit"] = tid, unit
    fc = pdu[0]
    out["fc"] = fc
    out["fc_name"] = _FC_NAMES.get(fc, f"FC{fc}")
    if len(pdu) >= 5:
        a, b = struct.unpack(">HH", pdu[1:5])
        if fc in _ADDR_COUNT_FCS:
            out["addr"], out["count"] = a, b
        elif fc in _ADDR_VALUE_FCS:
            out["addr"], out["value"] = a, b

    if not rx:
        return out  # no_response
    rtid, runit, rpdu, crc_ok = _split_adu(proto, rx)
    if not rpdu:
        out["result"] = "malformed"
        return out
    if crc_ok is False:
        out["result"] = "crc_error"
        return out
    rfc = rpdu[0]
    if rfc == fc | 0x80:
        out["result"] = "exception"
        out["exc"] = rpdu[1] if len(rpdu) > 1 else None
        out["exc_name"] = _EXC_NAMES.get(out["exc"], f"exception {out['exc']}")
    elif rfc == fc and (proto == "rtu" or rtid == tid):
        out["result"] = "ok"
    else:
        out["result"] = "mismatch"  # wrong FC echo or a stale/foreign TID
    return out


class BusTrace:
    """Bounded, thread-safe transaction ring shared by every Modbus connection."""

    def __init__(self, capacity: int = 1000):
        self.enabled = False
        self._lock = threading.Lock()
        self._buf: deque = deque(maxlen=capacity)
        self._seq = 0
        self.captured_total = 0
        self.enabled_since: Optional[float] = None

    @property
    def capacity(self) -> int:
        return self._buf.maxlen or 0

    # ── control ─────────────────────────────────────────────────────────────

    def configure(self, *, enabled: Optional[bool] = None,
                  capacity: Optional[int] = None, clear: bool = False) -> None:
        with self._lock:
            if capacity is not None:
                cap = max(100, min(20000, int(capacity)))
                if cap != self._buf.maxlen:
                    self._buf = deque(self._buf, maxlen=cap)
            if clear:
                self._buf.clear()
            if enabled is not None and enabled != self.enabled:
                self.enabled = bool(enabled)
                self.enabled_since = time.time() if self.enabled else None

    # ── capture (called from the wrapped pymodbus client) ───────────────────

    def instrument(self, client, *, label: str, proto: str) -> None:
        """Shadow ``send``/``recv`` on this client instance. Idempotent per
        instance (clients are rebuilt on every reconnect — each new instance
        gets its own wrap; the old one is garbage together with its socket)."""
        if getattr(client, "_bus_trace_state", None) is not None:
            return
        state: Dict[str, Any] = {"cur": None, "label": label, "proto": proto,
                                 "lock": threading.Lock()}
        client._bus_trace_state = state
        orig_send = getattr(client, "send", None)
        orig_recv = getattr(client, "recv", None)
        if orig_send is None or orig_recv is None:
            return  # not a sync pymodbus client — nothing to shadow

        def send(request, _orig=orig_send):
            if self.enabled and request:
                with state["lock"]:
                    if state["cur"] is not None:
                        self._commit_locked_state(state)
                    state["cur"] = {"ts": time.time(), "t0": time.perf_counter(),
                                    "tx": bytes(request), "rx": b""}
            return _orig(request)

        def recv(size, _orig=orig_recv):
            data = _orig(size)
            if self.enabled and data:
                with state["lock"]:
                    cur = state["cur"]
                    if cur is not None:
                        cur["rx"] += bytes(data)
                        cur["lat"] = (time.perf_counter() - cur["t0"]) * 1000
            return data

        client.send, client.recv = send, recv

    def commit(self, client) -> None:
        """Close the in-flight transaction (call after each read/write attempt).
        Safe on uninstrumented clients and with tracing off."""
        state = getattr(client, "_bus_trace_state", None)
        if state is None or state["cur"] is None:
            return
        with state["lock"]:
            self._commit_locked_state(state)

    def _commit_locked_state(self, state: Dict[str, Any]) -> None:
        cur, state["cur"] = state["cur"], None
        if cur is None:
            return
        meta = decode_transaction(state["proto"], cur["tx"], cur["rx"])
        entry = {"ts": round(cur["ts"], 3), "device": state["label"],
                 "proto": state["proto"],
                 "tx": cur["tx"].hex(), "rx": cur["rx"].hex(),
                 "latency_ms": round(cur["lat"], 1) if "lat" in cur else None,
                 **meta}
        with self._lock:
            self._seq += 1
            entry["seq"] = self._seq
            self._buf.append(entry)
            self.captured_total += 1

    # ── read side ────────────────────────────────────────────────────────────

    def snapshot(self, *, after: int = 0, limit: int = 200,
                 device: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            entries: List[Dict] = [e for e in self._buf if e["seq"] > after
                                   and (device is None or e["device"] == device)]
        if len(entries) > limit:
            entries = entries[-limit:]  # newest win; client paginates via `after`
        return {"enabled": self.enabled, "capacity": self.capacity,
                "captured_total": self.captured_total,
                "enabled_since": self.enabled_since,
                "last_seq": self._seq, "entries": entries}


trace = BusTrace()
