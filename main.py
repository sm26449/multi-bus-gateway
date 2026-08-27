#!/usr/bin/env python3
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
"""Multi-Bus Gateway — main application."""

import os
import time
import logging
import argparse
import threading
from pathlib import Path

import uvicorn

from multibus.config import Config
from multibus.modbus_client import ModbusClient
from multibus.mqtt_publisher import MQTTPublisher
from multibus.influxdb_publisher import InfluxDBPublisher
from multibus.api import create_api
from multibus.virtual_meter_manager import VirtualMeterManager

# Shrink the per-thread stack reservation from the 8 MB default to 512 KB
# BEFORE any thread is spawned. The gateway's threads (pollers, vmeter servers,
# reconnect loops) have shallow call stacks, so 512 KB is ample. This only
# changes ADDRESS-SPACE reservation (VmData) — resident memory is unaffected —
# but it keeps the virtual footprint sane at high thread counts (e.g. 59
# threads: ~464 MB -> ~30 MB reserved), which matters under 32-bit userland or
# strict overcommit. Wrapped: a platform that rejects the size just keeps 8 MB.
try:
    threading.stack_size(512 * 1024)
except (ValueError, RuntimeError):
    pass

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class GatewayApp:
    """Main application class."""

    def __init__(self, config_path: str = "config/config.yaml"):
        self.config_path = config_path
        self.config = None
        self.modbus_client = None
        self.mqtt_publisher = None
        self.influxdb_publisher = None
        self.app = None
        self.ws_manager = None
        self.vmeter_manager = None
        self.running = False

    def _first_run_provision(self):
        """Fresh install (no config.yaml at all): generate an admin password,
        store it HASHED with auth enabled, and print it ONCE — the standard
        appliance pattern (external audit B5: the gateway used to ship ~120
        routes open to anyone who could reach the port). Never touches an
        existing config; a wiped password stays wipeable by editing the file."""
        import os as _os
        if _os.path.exists(self.config_path):
            return
        import secrets
        from multibus import auth as _auth
        password = secrets.token_urlsafe(12)
        cfg = Config(self.config_path)          # defaults (file absent)
        cfg.ui.auth_enabled = True
        cfg.ui.auth_username = "admin"
        cfg.ui.auth_password = _auth.hash_password(password)
        cfg.save_yaml_config()
        banner = (
            "\n" + "=" * 62 +
            "\n  FIRST RUN — generated admin credentials (shown ONLY once):" +
            "\n      username: admin" +
            f"\n      password: {password}" +
            "\n  Change it in Config -> Security after logging in." +
            "\n" + "=" * 62)
        print(banner, flush=True)
        logger.warning("first run: auth enabled with a generated admin "
                       "password (printed to stdout once)")

    def setup(self):
        """Initialize all components."""
        logger.info("Multi-Bus Gateway starting...")

        self._first_run_provision()

        # Load configuration — through the last-known-good seatbelt: a
        # config.yaml that fails to parse (bad edit, torn write) is restored
        # from the LKG snapshot automatically so an unattended box still boots.
        from multibus.snapshots import boot_seatbelt
        self.config = boot_seatbelt(self.config_path,
                                    lambda: Config(self.config_path))
        logger.info(f"Loaded config from {self.config_path}")
        logger.info(f"Selected registers: {len(self.config.selected_registers)}")

        # Initialize MQTT publisher (connection will be done in background)
        if self.config.mqtt.enabled:
            self.mqtt_publisher = MQTTPublisher(
                config=self.config.mqtt,
                registers=self.config.selected_registers,
                publish_mode=self.config.mqtt.publish_mode,
                heartbeat_interval=getattr(self.config.mqtt, 'heartbeat_interval', 0),
            )
            logger.info("MQTT publisher initialized (connecting in background)")

        # Initialize InfluxDB publisher
        if self.config.influxdb.enabled:
            self.influxdb_publisher = InfluxDBPublisher(
                config=self.config.influxdb,
                registers=self.config.selected_registers,
                publish_mode=self.config.influxdb.publish_mode,
                buffer_dir=self.config.config_path.parent
            )
            logger.info("InfluxDB publisher initialized")

        # Initialize Modbus clients — one per configured device (Tier 2).
        # Device #1 is the legacy-synthesized primary: same config objects,
        # same registers file, same routing — behavior identical to before.
        self.devices = []                      # list of (DeviceConfig, ModbusClient|None)
        self.modbus_client = None              # primary client (back-compat)
        # Template registry so the boot path resolves each device's byte_order
        # EXACTLY like the runtime create/apply path (device_template.byte_order_for)
        # — a non-big device (EM24, Fronius TS) must not decode word-swapped
        # garbage after a restart. create_api builds its own from the same files.
        from multibus.device_template import TemplateRegistry
        self.template_registry = TemplateRegistry(
            user_dir=self.config.config_path.parent / 'device_templates')

        for device in self.config.devices:
            if device.primary:
                client = ModbusClient(
                    config=self.config.modbus,
                    registers=self.config.selected_registers,
                    poll_groups=self.config.poll_groups,
                    device_id=device.id,
                )
                self.modbus_client = client
            elif not device.enabled:
                logger.info(f"Device '{device.id}' disabled — skipping")
                client = None
            elif device.protocol == 'http':
                from multibus.http_client import HttpClient
                regs, groups = self.config.load_device_registers(device)
                client = HttpClient(
                    http_cfg=device.http, registers=regs, poll_groups=groups,
                    # external audit: the boot path was the ONE of three
                    # constructors not passing this — an allowed non-LAN
                    # device worked until the first restart, then died at
                    # debug level
                    allow_nonlan=self.config.security.allow_nonlan_http_devices)
                logger.info(f"Device '{device.id}': HTTP/JSON, {len(regs)} registers, "
                            f"{device.http.get('url', '')}")
            elif device.protocol == 'mqtt':
                from multibus.mqtt_input import MqttInputClient
                regs, groups = self.config.load_device_registers(device)
                client = MqttInputClient(mqtt_cfg=device.mqtt_in, registers=regs, poll_groups=groups)
                logger.info(f"Device '{device.id}': MQTT input, {len(regs)} registers, "
                            f"broker {device.mqtt_in.get('broker', '')}:{device.mqtt_in.get('port', 1883)} "
                            f"topic {device.mqtt_in.get('topic', '')}")
            elif device.protocol not in ('tcp', 'rtu', 'rtu-tcp'):
                logger.warning(f"Device '{device.id}': unknown protocol "
                               f"'{device.protocol}' — idle")
                client = None
            else:
                # tcp / rtu / rtu-tcp all build a ModbusClient — its _build_client
                # picks the transport (rtu-tcp = RTU frames over the bridge TCP
                # socket). Without rtu-tcp here the device would go idle on every
                # restart even though the runtime add path started it fine.
                regs, groups = self.config.load_device_registers(device)
                _bo = self.template_registry.byte_order_for(device.template)
                client = ModbusClient(config=device.connection,
                                      registers=regs, poll_groups=groups,
                                      byte_order=_bo, device_id=device.id)
                _where = (f"{device.connection.serial_port}" if device.protocol == 'rtu'
                          else f"{device.connection.host}:{device.connection.port}")
                logger.info(f"Device '{device.id}': {len(regs)} registers, "
                            f"{device.protocol} {_where} ({_bo})")
            self.devices.append((device, client))

        # Create API
        self.app, self.ws_manager = create_api(
            config=self.config,
            modbus_client=self.modbus_client,
            mqtt_publisher=self.mqtt_publisher,
            influxdb_publisher=self.influxdb_publisher,
            devices=self.devices,
            template_registry=self.template_registry,   # reuse the boot registry (no 2nd load)
        )

        logger.info("API server initialized")

        # Virtual meters (config-driven Modbus servers — emulate EM24/etc. from
        # the live Janitza values). Reads the API's live value cache. Disabled
        # by default in config/virtual_meters.yaml — enable an instance only
        # when ready to validate it (control-critical: it can feed an ESS).
        # bounds_for: a composite row sourced from another device is judged
        # against THAT device's staleness threshold (closure over the live
        # config → devices added/edited at runtime resolve correctly).
        def _device_stale_bound(did, _cfg=self.config):
            for d in _cfg.devices:
                if d.id == did:
                    return float(getattr(d.connection, 'stale_after_s', 30) or 30)
            return None

        # PQ event recorder (Jasic/Janitza) — per-device pollers that archive
        # the meter's on-device PQ ring (events + waveforms) through the
        # gateway sinks. Publishers resolved via app.state.ctx at use time
        # (config/apply can rebind them). Opt-in per device (pq_recorder:).
        from multibus.pq_recorder import PqRecorderManager
        _api_ctx = self.app.state.ctx
        self.pq_manager = PqRecorderManager(
            config=self.config,
            get_influx=lambda: getattr(_api_ctx, 'influxdb_publisher', None),
            get_mqtt=lambda: getattr(_api_ctx, 'mqtt_publisher', None),
            get_alerts=lambda: getattr(_api_ctx, 'alert_mgr', None),
            get_template=self.template_registry.get,
            event_log=self.app.state.event_log,
            state_dir=self.config.config_path.parent,
        )
        self.app.state.pq_manager = self.pq_manager   # for the /api/pq routes

        _cfg_dir = self.config.config_path.parent
        self.vmeter_manager = VirtualMeterManager(self.app.state.current_values,
                                                  device_values=self.app.state.device_values,
                                                  primary_device_id=self.config.primary_device.id,
                                                  mqtt_publisher=self.mqtt_publisher,
                                                  modbus_client=self.modbus_client,
                                                  bounds_for=_device_stale_bound,
                                                  config_path=str(_cfg_dir / 'virtual_meters.yaml'),
                                                  templates_dir=str(_cfg_dir / 'templates'))
        self.app.state.vmeter_manager = self.vmeter_manager   # for the /api/virtual-meters routes

        # Ensure each non-primary device's InfluxDB bucket exists (off-thread;
        # waits for the publisher to connect). New devices get their history/
        # energy without manual bucket setup.
        if self.influxdb_publisher:
            def _ensure_device_buckets():
                import time
                for _ in range(60):
                    if getattr(self.influxdb_publisher, 'connected', False):
                        break
                    time.sleep(1)
                for dev in self.config.devices:
                    if not dev.primary and dev.influxdb_enabled and dev.influxdb_bucket:
                        self.influxdb_publisher.ensure_bucket(dev.influxdb_bucket)
            threading.Thread(target=_ensure_device_buckets, daemon=True,
                             name="DeviceBuckets").start()

        # HA autodiscovery hooks for NON-primary devices are registered by
        # create_api's _sync_device_discovery() — the ONE owner, and the only
        # builder that computes write_rules. This module used to append its
        # own write-blind hooks here, which won the boot reconnect and
        # downgraded every HA number/select to a plain sensor after a restart
        # (external audit). Do not re-add hook registration here.

    def _connect_mqtt_background(self):
        """Connect to MQTT in background thread."""
        if self.mqtt_publisher:
            # Wait for network to be ready (Docker networking delay)
            import time
            import socket
            broker = self.config.mqtt.broker
            port = self.config.mqtt.port
            for i in range(30):  # Wait up to 30 seconds for network
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(1)
                    s.connect((broker, port))
                    s.close()
                    logger.info(f"Network ready, MQTT broker reachable at {broker}:{port}")
                    break
                except Exception:
                    if i < 29:
                        time.sleep(1)
                    else:
                        logger.warning(f"MQTT broker {broker}:{port} not reachable after 30s")
            logger.info("Attempting MQTT connection in background...")
            if self.mqtt_publisher.connect():
                logger.info("MQTT connected successfully")
                # Publish Home Assistant discovery (Janitza sensors + virtual meters)
                if self.config.mqtt.ha_discovery_enabled:
                    self.mqtt_publisher.publish_ha_discovery()
                    if self.vmeter_manager:
                        self.vmeter_manager.publish_ha_discovery()
            else:
                logger.warning("MQTT connection failed - will retry automatically")

    def _connect_modbus_background(self, client, host, port, label="Modbus", tcp=True):
        """Connect one Modbus client in a background thread. For TCP we first
        wait for the host:port to be reachable (Docker networking delay); RTU
        (serial) has no network to probe — connect straight away."""
        if client:
            import time
            import socket
            if tcp:
                for i in range(30):  # Wait up to 30 seconds for network
                    try:
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.settimeout(1)
                        s.connect((host, port))
                        s.close()
                        logger.info(f"Network ready, {label} device reachable at {host}:{port}")
                        break
                    except Exception:
                        if i < 29:
                            time.sleep(1)
                        else:
                            logger.warning(f"{label} device {host}:{port} not reachable after 30s")
            logger.info(f"Attempting {label} connection...")
            if client.connect():
                logger.info(f"{label} connected")
            else:
                logger.warning(f"{label} connection failed - pollers will keep retrying")
            # Start polling regardless of the first connect outcome: each read
            # reconnects on demand, so a meter that is down at boot (or comes
            # up later) is picked up automatically — no dead-on-arrival boot.
            client.start_polling()
            logger.info(f"{label} polling started")

    def start(self):
        """Start all components."""
        self.running = True

        # Mark the config last-known-good once the app has PROVEN healthy:
        # 5 minutes up AND at least one successful device read (or no pollable
        # devices at all). Re-checks each minute until it succeeds once per boot.
        def _lkg_when_healthy():
            deadline_first = time.time() + 300
            while self.running:
                time.sleep(max(1.0, deadline_first - time.time()) if time.time() < deadline_first else 60.0)
                if not self.running:
                    return
                clients = [c for _d, c in self.devices if c is not None]
                healthy = (not clients) or any(
                    getattr(getattr(c, 'connection', None), 'successful_reads', 0) > 0
                    for c in clients)
                if healthy:
                    try:
                        store = getattr(self.app.state, 'snapshot_store', None)
                        if store:
                            store.mark_lkg()
                            self.app.state.event_log.add(
                                "info", "snapshots",
                                "config marked last-known-good (healthy boot)")
                    except Exception:  # noqa: BLE001
                        logger.exception("LKG mark failed")
                    return
        threading.Thread(target=_lkg_when_healthy, daemon=True,
                         name="LKG-Health").start()

        # Connect MQTT in background thread (non-blocking)
        if self.mqtt_publisher:
            mqtt_thread = threading.Thread(
                target=self._connect_mqtt_background,
                name="MQTT-Init",
                daemon=True
            )
            mqtt_thread.start()

        # Connect each device's Modbus client in a background thread (non-blocking)
        for device, client in self.devices:
            if not client:
                continue
            threading.Thread(
                target=self._connect_modbus_background,
                args=(client, device.connection.host, device.connection.port,
                      f"Modbus[{device.id}]", device.protocol in ('tcp', 'rtu-tcp')),
                name=f"Modbus-Init-{device.id}",
                daemon=True
            ).start()

        # Virtual meters (each runs its own isolated server thread).
        if self.vmeter_manager:
            self.vmeter_manager.start_all()
            self.vmeter_manager.start_state_publisher()   # publish health to MQTT for alertd

        # PQ event recorders (opt-in per device)
        if getattr(self, 'pq_manager', None):
            self.pq_manager.start_all()

        logger.info(f"Starting web server on {self.config.ui.host}:{self.config.ui.port}")

    def stop(self):
        """Stop all components."""
        self.running = False
        logger.info("Shutting down...")

        if self.vmeter_manager:
            self.vmeter_manager.stop_all()

        if getattr(self, 'pq_manager', None):
            self.pq_manager.stop_all()

        # Disconnect devices in PARALLEL (external audit: sequential joins of
        # up to ~5 s per device could outlast docker's stop grace period —
        # SIGKILL then loses the InfluxDB replay buffer flushed further down).
        # Bounded join: a wedged disconnect must not hold the flush hostage.
        _threads = []
        for _device, client in getattr(self, 'devices', []):
            if client:
                t = threading.Thread(target=client.disconnect, daemon=True,
                                     name=f"Shutdown-{getattr(_device, 'id', '?')}")
                t.start()
                _threads.append(t)
        for t in _threads:
            t.join(timeout=8)
            if t.is_alive():
                logger.warning("%s still disconnecting — proceeding with shutdown", t.name)

        # Resolve the LIVE publishers from the API context: /api/config/apply
        # can rebind them (a sink enabled after boot creates a new one), so the
        # boot-time self.* references may be stale — closing those would leave
        # the live InfluxDB publisher's replay buffer unflushed on shutdown.
        _ctx = getattr(getattr(self.app, 'state', None), 'ctx', None)
        mqtt_pub = getattr(_ctx, 'mqtt_publisher', None) or self.mqtt_publisher
        influx_pub = getattr(_ctx, 'influxdb_publisher', None) or self.influxdb_publisher

        if mqtt_pub:
            mqtt_pub.disconnect()

        if influx_pub:
            influx_pub.close()

        logger.info("Shutdown complete")


def main():
    parser = argparse.ArgumentParser(description="Multi-Bus Gateway")
    parser.add_argument(
        "-c", "--config",
        default="config/config.yaml",
        help="Path to configuration file"
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Override UI host"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override UI port"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Create application
    monitor = GatewayApp(args.config)
    monitor.setup()

    # Override host/port if specified
    host = args.host or monitor.config.ui.host
    port = args.port or monitor.config.ui.port

    # Start components
    monitor.start()

    # HTTPS (optional): serve the UI over TLS when ui.tls.enabled. Paths come
    # from config; if missing, auto-generate a self-signed cert so HTTPS works
    # out of the box (the operator can drop in a real cert later).
    ssl_kwargs = {}
    ui_tls = getattr(monitor.config.ui, "tls_enabled", False)
    if ui_tls:
        _certs_dir = monitor.config.config_path.parent / "certs"
        cert = monitor.config.ui.tls_cert or str(_certs_dir / "ui.crt")
        key = monitor.config.ui.tls_key or str(_certs_dir / "ui.key")
        try:
            _ensure_self_signed(cert, key)
            ssl_kwargs = {"ssl_certfile": cert, "ssl_keyfile": key}
            logger.info(f"HTTPS enabled — serving TLS with {cert}")
        except Exception as e:
            logger.error(f"HTTPS requested but cert setup failed ({e}); "
                         f"falling back to HTTP")

    # Run uvicorn via an explicit Server so we regain the shutdown hook: uvicorn
    # installs its OWN signal handlers (overriding any we'd set), so cleanup must
    # run AFTER server.run() returns on SIGTERM/SIGINT. Without this, monitor.stop()
    # never fires and up to `influxdb.buffer_minutes` of buffered points are lost,
    # pollers/pushers/leases die abruptly — the graceful path was dead code.
    server = uvicorn.Server(uvicorn.Config(
        monitor.app,
        host=host,
        port=port,
        log_level="info" if not args.debug else "debug",
        # X-Forwarded-* is honored ONLY from proxies the operator explicitly
        # listed in ui.trusted_proxies (e.g. the Traefik container that
        # terminates TLS for gateway.example.com). With the list empty — the
        # default — proxy headers stay OFF entirely: a client co-located with
        # some proxy cannot spoof client.host and defeat the login lockout,
        # the IP allowlist or the audit trail.
        proxy_headers=bool(monitor.config.ui.trusted_proxies),
        forwarded_allow_ips=(",".join(monitor.config.ui.trusted_proxies)
                             if monitor.config.ui.trusted_proxies else None),
        **ssl_kwargs,
    ))
    try:
        server.run()
    finally:
        monitor.stop()


def _ensure_self_signed(cert_path: str, key_path: str):
    """Create a self-signed cert/key pair at the given paths if absent.
    Uses the `cryptography` lib if present, else falls back to openssl."""
    cp, kp = Path(cert_path), Path(key_path)
    if cp.exists() and kp.exists():
        return
    cp.parent.mkdir(parents=True, exist_ok=True)
    kp.parent.mkdir(parents=True, exist_ok=True)
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime as _dt
        keyobj = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "multi-bus-gateway")])
        cert = (x509.CertificateBuilder()
                .subject_name(name).issuer_name(name)
                .public_key(keyobj.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(_dt.datetime.utcnow() - _dt.timedelta(days=1))
                .not_valid_after(_dt.datetime.utcnow() + _dt.timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), False)
                .sign(keyobj, hashes.SHA256()))
        # 0600 from creation, like every other secret-bearing writer
        # (external audit: this was the one exception — the key landed
        # world-readable on the bind-mounted config volume)
        _kfd = os.open(str(kp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(_kfd, 'wb') as _kf:
            _kf.write(keyobj.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
        cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        logger.info(f"Generated self-signed certificate at {cert_path}")
    except ImportError:
        import subprocess
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", key_path, "-out", cert_path, "-days", "3650",
            "-subj", "/CN=multi-bus-gateway",
        ], check=True)
        try:
            os.chmod(str(kp), 0o600)      # same guarantee on the fallback path
        except OSError:
            pass
        logger.info(f"Generated self-signed certificate (openssl) at {cert_path}")


if __name__ == "__main__":
    main()
