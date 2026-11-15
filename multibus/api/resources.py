# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""This process's own resource footprint, read from /proc/self so the Status
page needs no psutil (Linux/container). CPU% is a delta between polls; all
fields are best-effort."""
import os
import time

# Process start (approx = module import) for uptime, and last CPU sample for the
# %-delta. Read from /proc/self so no psutil dependency is needed (Linux/container).
_APP_START_TS = time.time()
_CPU_SAMPLE = {"wall": None, "cpu_s": None}


def _read_self_resources() -> dict:
    """Host/process resource footprint from /proc/self (no external deps).

    CPU% is the delta since the previous call, so the first call after start
    returns null and subsequent polls (the Status page refreshes) report a real
    figure. All fields are best-effort: missing /proc entries just omit a key."""
    out: dict = {"num_cpus": os.cpu_count(), "uptime_s": int(time.time() - _APP_START_TS)}
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    out["rss_mb"] = round(int(line.split()[1]) / 1024, 1)
                elif line.startswith("Threads:"):
                    out["threads"] = int(line.split()[1])
    except OSError:
        pass
    try:
        out["open_fds"] = len(os.listdir("/proc/self/fd"))
    except OSError:
        pass
    try:
        est = 0
        for fn in ("/proc/self/net/tcp", "/proc/self/net/tcp6"):
            try:
                with open(fn) as f:
                    next(f, None)  # header row
                    for line in f:
                        parts = line.split()
                        if len(parts) > 3 and parts[3] == "01":  # 01 = ESTABLISHED
                            est += 1
            except OSError:
                continue
        out["tcp_established"] = est
    except OSError:
        pass
    try:
        with open("/proc/self/stat") as f:
            fields = f.read().split()
        cpu_s = (int(fields[13]) + int(fields[14])) / os.sysconf("SC_CLK_TCK")
        now = time.time()
        prev_w, prev_c = _CPU_SAMPLE["wall"], _CPU_SAMPLE["cpu_s"]
        _CPU_SAMPLE["wall"], _CPU_SAMPLE["cpu_s"] = now, cpu_s
        out["cpu_pct"] = (round(100.0 * (cpu_s - prev_c) / (now - prev_w), 1)
                          if prev_w is not None and now > prev_w else None)
    except (OSError, IndexError, ValueError):
        pass
    return out
