# Multi-Bus Gateway
FROM python:3.11-slim

WORKDIR /app

# Install dependencies from the LOCK file (exact pins → reproducible image;
# requirements.txt stays the human-edited intent file, see requirements.lock
# header for the regenerate command).
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock

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
# Port follows UI_PORT (audit MEDIUM-1: the 8080 hardcode made any
# UI_PORT!=8080 deploy permanently 'unhealthy').
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request, sys; p = os.environ.get('UI_PORT', '8080'); sys.exit(0 if urllib.request.urlopen(f'http://localhost:{p}/health', timeout=5).status == 200 else 1)" || exit 1

# Run application
CMD ["python", "main.py", "-c", "config/config.yaml"]
