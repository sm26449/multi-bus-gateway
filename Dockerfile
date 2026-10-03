# Multi-Bus Gateway
# Digest-pinned base image (Dependabot's docker ecosystem refreshes the pin);
# the tag is kept for humans, the digest is what is built.
FROM python:3.11-slim@sha256:bab1b7ef4b450c81002278d035eff85ebe394ae94df904f7a3ba14f7e16e487b
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"

# Debian security fixes newer than the base image's last rebuild: the digest
# pin keeps the build reproducible, but a fixed CVE (pcre2/openssl class) can
# sit published for days before the official image rebuilds — Trivy rightly
# fails the gate on it. A dist-upgrade of the ~120 base packages closes that
# window; Dependabot's digest bumps keep this layer close to a no-op.
RUN apt-get update \
    && apt-get dist-upgrade -y --no-install-recommends \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Dedicated non-root user. The entrypoint starts as root ONLY to chown the
# mounted config volume (bind mounts arrive with host ownership — root on a
# fresh install), then drops to this user via setpriv; the long-lived process
# never runs as root. The app image stays root-owned/read-only to this user;
# the only writable path is the mounted /app/config. Binding :502
# (privileged) as non-root needs the per-container sysctl
# net.ipv4.ip_unprivileged_port_start=0 — scoped to the container's own
# network namespace, no capabilities involved (see docker-compose.yml).
# --groups dialout: the documented DIRECT serial mode (docs/rtu-serial.md §1)
# maps /dev/ttyUSBx into the container, whose node keeps the host's
# root:dialout 660 — without this membership the unprivileged user could
# never open it (the entrypoint's setpriv --init-groups also drops any
# docker --group-add, so the image is the only place this can live).
RUN useradd --uid 10001 --user-group --groups dialout --no-create-home --shell /usr/sbin/nologin mbg

WORKDIR /app

# Install dependencies from the LOCK file (exact pins → reproducible image;
# requirements.txt stays the human-edited intent file, see requirements.lock
# header for the regenerate command).
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock \
    # the runtime needs neither build tool; their vendored copies carried CVEs the
    # image scanner flags (setuptools/_vendor jaraco.context + wheel)
    && pip uninstall -y setuptools wheel

# Copy application
COPY multibus/ ./multibus/
COPY ui/ ./ui/
COPY docs/modbus_data.json ./docs/
COPY main.py .
# Ship the default config + meter templates so the prebuilt image is
# self-contained (a host ./config volume still overlays user edits).
COPY config/ ./config/

# Expose ports: Web UI, the published virtual-meter range, and standard Modbus.
EXPOSE 8080 1502-1512 502

# Health check — use python stdlib (no curl in the slim image, was the
# root cause of 40k+ failing health checks since image build). Probes /health:
# 200 for ok/degraded (a stale source is a correct fail-safe, not a fault),
# 503 (→ HTTPError → unhealthy) only when an enabled virtual meter is down.
# In-container bind: every interface of the container's OWN namespace —
# exposure to the LAN is governed solely by the compose port mapping.
# (The bare-metal code default is loopback; see UIConfig.host.)
ENV UI_HOST=0.0.0.0

# Port follows UI_PORT (audit MEDIUM-1: the 8080 hardcode made any
# UI_PORT!=8080 deploy permanently 'unhealthy').
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request, sys; p = os.environ.get('UI_PORT', '8080'); sys.exit(0 if urllib.request.urlopen(f'http://localhost:{p}/health', timeout=5).status == 200 else 1)" || exit 1

# chown-then-drop entrypoint (see the user comment above)
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "main.py", "-c", "config/config.yaml"]
