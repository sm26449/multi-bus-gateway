# Serial-to-TCP bridge (Faza A prototype). ser2net exposes RS485/USB adapters
# over TCP so MBG talks RTU-over-TCP — no /dev mapping or container recreation
# on the app side.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ser2net \
    && rm -rf /var/lib/apt/lists/*
COPY ser2net.yaml /etc/ser2net.yaml
EXPOSE 7001
CMD ["ser2net", "-n", "-c", "/etc/ser2net.yaml"]
