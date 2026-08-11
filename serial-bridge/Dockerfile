# Serial-to-TCP bridge: ser2net exposes RS485/USB adapters over TCP so MBG talks
# RTU-over-TCP. A python supervisor manages ser2net dynamically (hotplug), keeps
# stable per-adapter TCP ports, and exposes GET /adapters for MBG to scan.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        ser2net python3 python3-pyudev tini \
    && rm -rf /var/lib/apt/lists/*
COPY supervisor.py /app/supervisor.py
# control API port; adapter TCP ports are 7001-7099
EXPOSE 7000 7001-7099
# tini as PID 1 so ser2net (child of the supervisor) is reaped cleanly on stop
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/app/supervisor.py"]
