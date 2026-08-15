# Serial-to-TCP bridge: ser2net exposes RS485/USB adapters over TCP so MBG talks
# RTU-over-TCP. A python supervisor manages ser2net dynamically (hotplug), keeps
# stable per-adapter TCP ports, and exposes GET /adapters for MBG to scan.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        ser2net python3 python3-pyudev tini \
    && rm -rf /var/lib/apt/lists/*
COPY supervisor.py /app/supervisor.py
# Non-root: a dedicated user in `dialout` (gid 20 matches the host's group on
# the bound /dev tty nodes, root:dialout 0660). The supervisor's writable
# paths get explicit ownership: /etc/ser2net/ (config rewritten per reconcile),
# the UUCP lock dirs (ser2net lockfiles + the stale-lock sweep), /data is the
# state volume (chown'd host-side). Device access stays doubly bounded by the
# compose device_cgroup_rules (tty majors 188/166 only).
RUN useradd --uid 10002 --user-group --no-create-home --shell /usr/sbin/nologin bridge \
    && usermod -aG dialout bridge \
    && mkdir -p /etc/ser2net /var/lock /run/lock /data \
    && chown bridge:bridge /etc/ser2net /var/lock /run/lock /data
# Only the control API is declared here. The adapter data ports (7001-7099) are
# deliberately NOT EXPOSEd: they carry raw Modbus to live buses and must stay
# reachable ONLY on the internal Docker network — never publish them to the LAN
# (no `-p 7001-7099`, no `-P`). MBG reaches them by container name internally.
EXPOSE 7000
# Health: the control API answering implies the supervisor loop is alive (it
# also owns ser2net respawn). stdlib urllib — no curl in the slim image.
# Baked here so a standalone `docker run` gets monitoring, not just the stack.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://localhost:7000/health', timeout=4).status == 200 else 1)" || exit 1
# tini as PID 1 so ser2net (child of the supervisor) is reaped cleanly on
# stop; the entrypoint then chowns the mounted /data and drops to `bridge`
# (setpriv) — the long-lived supervisor+ser2net never run as root.
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["python3", "/app/supervisor.py"]
