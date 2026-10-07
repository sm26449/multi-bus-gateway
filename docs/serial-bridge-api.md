# Serial bridge — control API

The serial bridge (`serial-bridge/`, image
`ghcr.io/sm26449/multi-bus-gateway-serial-bridge`) exposes every USB serial
adapter (ttyUSB/ttyACM) as a stable TCP endpoint through ser2net, **one bus
per adapter**. Besides those raw-Modbus data ports it serves a small HTTP
control API, by default on port **7000**. This page describes that API and
the bridge's environment variables. How the gateway uses the bridge is in
[rtu-serial.md](rtu-serial.md).

## Deployment modes

| Mode | How | Control API auth |
|------|-----|------------------|
| Same host as the gateway | `docker compose --profile rtu-bridge up -d` (main `docker-compose.yml`) | none needed: API on `127.0.0.1` + a private Docker network |
| Remote host (a Raspberry Pi next to the bus, a server) | `serial-bridge/docker-compose.bridge.yml` | `BRIDGE_TOKEN` **required** |

Remote quick start:

```bash
cd serial-bridge
echo "BRIDGE_TOKEN=$(openssl rand -hex 24)" > .env
docker compose -f docker-compose.bridge.yml up -d
# firewall 7000 + the data ports to the gateway's IP only, e.g.:
sudo ufw allow from 192.168.1.10 to any port 7000:7016 proto tcp
```

**Security model.** The token protects only the control API, and it travels
as plain HTTP. The data ports carry raw Modbus RTU and have **no
authentication**, because RTU has none. Anyone who can open a data port
controls that bus. Firewall all published ports to the gateway's IP. When
the network path is not one you trust, tunnel the traffic (WireGuard, SSH).

## Authentication

When `BRIDGE_TOKEN` is set, every endpoint except `GET /health` requires:

```
Authorization: Bearer <token>
```

The comparison is constant-time. A missing or wrong token gets
`401 {"error": "unauthorized"}` with `WWW-Authenticate: Bearer`. Unknown
paths also get 401 rather than 404, so nobody can probe the API
anonymously. When `BRIDGE_TOKEN` is unset or empty, no endpoint needs a
token. Same-host deployments behave as before.

## Adapter keys

Each adapter has a stable key (`stable_id`):

- its USB serial number when it has one (FTDI, CP210x), e.g. `B0045K08`;
- otherwise `vendor:product@usb-port-path`, e.g. `1a86:7523@1-1`. Cheap
  CH340 adapters use this form. The key stays the same as long as the cable
  stays in the same USB port.

Percent-encode the key in URLs. For example, `1a86:7523@1-1` becomes
`1a86%3A7523%401-1` (in JavaScript, `encodeURIComponent`). The bridge splits
the path first and decodes each part afterwards, so an encoded `/` (`%2F`)
inside a key also works.

## Endpoints

### `GET /health`

Needs no token. Returns no device details.

```json
{"status": "ok", "adapters": 2, "version": "3.89.0"}
```

- `adapters` is the number of adapters the bridge sees, excluded ones
  included.
- `hostname` is added only when the caller is authorized: either a valid
  token was sent or no token is configured.
- The response is `500` with `{"status": "down", "reason": "ser2net not
  running", ...}` when there are adapters to serve but ser2net is dead.
  The container healthcheck uses this endpoint.

### `GET /adapters`

Returns the live inventory.

```json
{
  "version": "3.89.0",
  "hostname": "pi-garage",
  "auth": true,
  "adapters": [
    {
      "stable_id": "1a86:7523@1-1",
      "dev": "/dev/ttyUSB0",
      "vendor_id": "1a86", "product_id": "7523",
      "serial": "",
      "manufacturer": "QinHeng", "model": "USB Serial",
      "port_path": "1-1",
      "tcp_port": 7001,
      "connected": true, "available": true, "excluded": false,
      "serial_params": {"baud": 9600, "parity": "N", "databits": 8, "stopbits": 1},
      "serial_text": "9600 8N1",
      "serial_custom": false
    }
  ]
}
```

| Field | Meaning |
|-------|---------|
| `serial` | The adapter's **USB serial number** (string, may be empty). Its meaning is unchanged from earlier versions. |
| `serial_params` | The adapter's line settings. |
| `serial_text` | The same settings in short form. |
| `serial_custom` | `true` when the adapter has its own stored settings, `false` when it uses `BRIDGE_SERIAL_PARAMS`. |
| `tcp_port` | The data port to connect to (Modbus RTU over TCP). Absent for excluded adapters. |
| `available: false` / `excluded: true` | The adapter is listed in `BRIDGE_EXCLUDE`. The bridge never opens it. |

```bash
curl -s -H "Authorization: Bearer $BRIDGE_TOKEN" http://pi-garage:7000/adapters
```

### `POST /adapters/<key>/serial`

Sets the serial parameters of one adapter. The body is a JSON object with
any subset of these fields. Fields you leave out keep their current values.

| Field | Allowed |
|-------|---------|
| `baud` | 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200 |
| `parity` | `"N"`, `"E"`, `"O"` (case-insensitive) |
| `databits` | 7, 8 |
| `stopbits` | 1, 2 |

Numbers may also be given as digit strings. Unknown fields are rejected.

```bash
curl -s -X POST -H "Authorization: Bearer $BRIDGE_TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"baud": 19200, "parity": "E", "databits": 8, "stopbits": 1}' \
     'http://pi-garage:7000/adapters/1a86%3A7523%401-1/serial'
```

When the request is valid, the bridge:

1. stores the settings in `portmap.json` under the adapter's key;
2. rewrites that adapter's ser2net line;
3. reloads ser2net with SIGHUP. Only the changed port restarts, so the
   other buses are not interrupted.

The response is `200` with the adapter, in the same shape as in
`/adapters`.

| Status | Meaning |
|--------|---------|
| `400 {"error": "..."}` | Invalid or missing JSON, or a value out of range. The message names the field. |
| `404` | No adapter with that key is plugged in now. |
| `405` | Wrong method. |
| `500` | The settings could not be applied, for example `/data` is not writable. |

The settings follow the adapter key, so they survive replug and restart.
Excluded adapters can be given settings too. These are stored, but they
never reach ser2net.

## Persistent state

The state file is `/data/portmap.json` (in the `serial-bridge-data`
volume).

```json
{
 "B0045K08": 7002,
 "1a86:7523@1-1": 7001,
 "__serial__": {
  "1a86:7523@1-1": {"baud": 19200, "parity": "E", "databits": 8, "stopbits": 1}
 }
}
```

- **Ports** stay flat `key: port` entries, the format older versions wrote.
  Older files load unchanged: adapters without a `__serial__` entry use the
  default settings.
- **Downgrade:** an older bridge reads the ports and drops only the
  `__serial__` entry. It logs this as an invalid entry.
- **Invalid entries:** a broken serial entry is dropped and logged, and the
  adapter falls back to the default.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `BRIDGE_TOKEN` | *(unset)* | Bearer token for the control API. Unset means no auth. Required by `docker-compose.bridge.yml`. |
| `BRIDGE_SERIAL_PARAMS` | `9600n81` | Default settings for adapters without their own, in ser2net form `<baud><n\|e\|o><databits><stopbits>`. An invalid value falls back to `9600n81`. |
| `BRIDGE_EXCLUDE` | *(empty)* | Comma-separated adapters to never expose. Each entry can be a stable id, a `/dev` path, a USB port-path or a tty name. |
| `BRIDGE_PORT_LOW` / `BRIDGE_PORT_HIGH` | `7001` / `7099` | Range of data ports to allocate from. The remote compose file sets 7001–7016 and publishes the same range. |
| `BRIDGE_CONTROL_PORT` | `7000` | Port of the control API inside the container. The image healthcheck assumes 7000. |
| `BRIDGE_HOSTNAME` | *(unset)* | Name reported as `hostname`. If unset, the bridge uses the host's `/etc/hostname` when it is mounted at `/etc/host-hostname` (the remote compose file mounts it), and otherwise the container hostname. |
| `BRIDGE_VERSION` | `dev` | Version string that is reported. Set at build time with `--build-arg BRIDGE_VERSION=<tag>`. |
| `BRIDGE_STATE` | `/data/portmap.json` | Path of the state file. |
| `BRIDGE_RECONCILE_S` | `10` | Seconds between periodic re-scans. These also cover hotplug. |
| `SER2NET_CFG` | `/etc/ser2net/ser2net.yaml` | Path of the generated ser2net config. |
