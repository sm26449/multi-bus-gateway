# Serial-to-TCP bridge: ser2net exposes RS485/USB adapters over TCP so MBG talks
# RTU-over-TCP. A python supervisor manages ser2net dynamically (hotplug), keeps
# stable per-adapter TCP ports, and exposes GET /adapters for MBG to scan.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        ser2net python3 python3-pyudev tini \
    && rm -rf /var/lib/apt/lists/*
COPY supervisor.py /app/supervisor.py
# Only the control API is declared here. The adapter data ports (7001-7099) are
# deliberately NOT EXPOSEd: they carry raw Modbus to live buses and must stay
# reachable ONLY on the internal Docker network — never publish them to the LAN
# (no `-p 7001-7099`, no `-P`). MBG reaches them by container name internally.
EXPOSE 7000
# tini as PID 1 so ser2net (child of the supervisor) is reaped cleanly on stop
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/app/supervisor.py"]
