# Serial bridge — control API

The serial bridge (`serial-bridge/`, image
`ghcr.io/sm26449/multi-bus-gateway-serial-bridge`) exposes every USB serial
adapter (ttyUSB/ttyACM) and the board's own UARTs (RS-485 HAT channels
`ttySC*`, `ttyAMA*`, real `ttyS*`, `ttymxc*`, `ttyO*`) as stable TCP
endpoints through ser2net, **one bus per port**. Besides those raw-Modbus data ports it serves a small HTTP
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
# an RS-485 HAT on this host (ttySC*, a real ttyS…): allow exactly its device
# majors in .env first (USB adapters and ttyAMA need nothing)
sh ../scripts/serial-access.sh --compose docker-compose.bridge.yml
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

Each port has a stable key (`stable_id`):

- a USB adapter: its USB serial number when it has one (FTDI, CP210x), e.g.
  `B0045K08`;
- otherwise `vendor:product@usb-port-path`, e.g. `1a86:7523@1-1`. Cheap
  CH340 adapters use this form. The key stays the same as long as the cable
  stays in the same USB port;
- a board UART: the device behind it, from `/sys/class/tty/<name>/device` —
  its bus address, e.g. `fe201000.serial` for a SoC UART — plus `:<channel>`
  when that device carries several ports, e.g. `spi0.0:0` and `spi0.0:1` for
  the two channels of an SC16IS752 HAT. The channel is the port's rank among
  its siblings, so the key survives the kernel naming the same channel
  differently on the next boot. The tty name alone is never the key.

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

- `adapters` is the number of ports the bridge lists, excluded, reserved,
  not enabled and inaccessible ones included.
- `warnings` appears when the bridge found a stale device-rule slot (see
  [Warnings](#warnings)): a count for an anonymous caller, the messages for an
  authorized one. The gateway then shows the bridge as degraded.
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
      "serial_custom": false,
      "rs485": false,
      "kind": "usb", "driver": "ch341-uart",
      "label": "USB Serial (USB 1-1)", "dev_num": "188:0",
      "status": "ok", "reason": "", "fix": "", "needs_major": null
    },
    {
      "stable_id": "spi0.0:1",
      "dev": "/dev/ttySC1",
      "vendor_id": "", "product_id": "", "serial": "", "manufacturer": "", "model": "",
      "port_path": "spi0.0",
      "tcp_port": null,
      "connected": true, "available": false, "excluded": false,
      "serial_params": {"baud": 9600, "parity": "N", "databits": 8, "stopbits": 1},
      "serial_text": "9600 8N1",
      "serial_custom": false,
      "rs485": false,
      "kind": "board", "driver": "sc16is7xx",
      "label": "SC16IS752 ch1 (spi0.0)", "dev_num": "509:1",
      "status": "no-access",
      "reason": "the container may not open device major 509",
      "fix": "sh scripts/serial-access.sh", "needs_major": 509
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
| `tcp_port` | The data port to connect to (Modbus RTU over TCP). `null` for every port that is not served (any `status` but `ok`). |
| `available` | `true` only for a port the bridge serves (`status` `ok`). |
| `excluded` | `true` when the port is listed in `BRIDGE_EXCLUDE`. The bridge never opens it. |
| `rs485` | `true` when the kernel's RS-485 mode is on for this port (see below). |
| `kind` | `usb` or `board` (a UART of the board itself, a HAT channel included). |
| `driver` | The kernel driver behind the port (`ftdi_sio`, `ch341-uart`, `sc16is7xx`, `uart-pl011`…), when sysfs names one. |
| `label` | What the port is, in words: `SC16IS752 ch0 (spi0.0)`, `FT232R (B0045K08)`. |
| `dev_num` | The node's `major:minor`, e.g. `509:0`. |
| `status` | Whether the bridge can serve the port, see the table below. |
| `reason` | Why the port is not served (empty for `ok`). |
| `fix` | For `no-access`: the command that fixes it, when there is one. |
| `needs_major` | For `no-access` from the container's device rules: the major to allow. |
| `needs_enable` | `true` for a UART of the SoC itself (`ttyAMA*`, real `ttyS*`, `ttymxc*`, `ttyO*`): served only once enabled. |
| `enabled` | `true` when such a port is enabled (`POST /adapters/<key>/enable` or `BRIDGE_INCLUDE`). |
| `stats` | The port's line and transport health (see [Diagnostics](#diagnostics)); `null` for a port that is not served, and until its first sample. |

The answer also carries a top-level `warnings` list (empty when all is well),
see [Warnings](#warnings), and `diagnostics`: how this bridge measures
(`interval_s`, `boot`, `started`, `transport`, `line`), see
[Diagnostics](#diagnostics). A bridge older than diagnostics sends neither
`stats` nor `diagnostics`.

**Statuses.** Only `ok` ports are ever handed to ser2net. Every other port is
still listed, so the gateway can show it and say why.

| `status` | Meaning | What to do |
|----------|---------|------------|
| `ok` | Served on `tcp_port`. | — |
| `no-access` | The container cannot open the port. With `needs_major`: its device rules do not allow that major (an RS-485 HAT's dynamic major, a real `ttyS`). Otherwise: the node's permissions (`reason` says which), or the node is missing. | Do what `fix` says on the bridge's host. With the compose files: `scripts/serial-access.sh`, then the `docker compose … up -d` line it prints ([install.md §D](install.md#d-the-rtu-serial-bridge)). With a plain `docker run`: recreate the container with the `--device-cgroup-rule` that `fix` names. For a permission problem: a udev rule making the node group `dialout`, mode 0660. |
| `not-enabled` | A UART of the SoC itself. It may already be used by something the bridge cannot see (a GPS, a login console, a modem), so it is never served, nor even opened, until you opt in. | `POST /adapters/<key>/enable` (the gateway's Buses dialog has an **Enable** button), or list it in `BRIDGE_INCLUDE`. |
| `busy` | Another program holds the port **exclusively** (`TIOCEXCL` or a `flock`). | Stop that program, or leave the port to it. Recreating the bridge looks again. |
| `reserved` | The kernel console (`console=` in `/proc/cmdline`, `/sys/class/tty/console/active`) or the board's Bluetooth UART. | Nothing: it is never served, and never even opened to look. Free the console in the boot configuration first if you really mean to use that UART. |
| `excluded` | Listed in `BRIDGE_EXCLUDE`. | — |

A board UART the kernel registered with no hardware behind it (the 8250
driver's `ttyS0..31` placeholders on a PC: sysfs `type` 0, or neither an I/O
port nor a memory address) is not listed at all.

To tell whether it can open a board port, the bridge opens it non-blocking,
tries an exclusive `flock`, clears `HUPCL` so the close does not drop DTR, and
lets go at once. It does this **once per port per bridge lifetime**: never
again on a re-scan, nor while ser2net restarts, since each open raises
DTR/RTS (on a transceiver driven by RTS, a glitch on the bus). A USB adapter
is never probed: it is served as before. A reserved, excluded or not-enabled
port is never opened.

**Only exclusive holders are seen.** A program that opened a port plainly
(gpsd, a getty, pyserial's default, a device mapped straight into the
gateway) does not show as `busy`; the bridge would open the same line beside
it. This is why the SoC's own UARTs wait for an opt-in. For USB adapters and
HAT channels, list a port another service owns in `BRIDGE_EXCLUDE`.

### Warnings

At start the bridge checks the four device-rule slots the compose files pass
it (`BRIDGE_TTY_SLOTS`, filled by `scripts/serial-access.sh`). A slot whose
major no longer belongs to a tty device is reported: no device has that
number any more (a kernel update gave the RS-485 HAT another one), or the
number now belongs to something else, which the container can still open.
Each warning is `{slot, rule, driver, message, fix}`, logged at start and
listed in `/adapters` and `/health`. The fix is to run the script again and
recreate the container.

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
| `rs485` | `true` / `false` (a JSON boolean). Off by default. Board UARTs only. |

Numbers may also be given as digit strings. Unknown fields are rejected.

**`rs485`** turns on the kernel's RS-485 mode for the port (`TIOCSRS485`):
the UART drives the transceiver's direction from RTS. USB RS-485 adapters
switch direction themselves and do not need it. Some SC16IS7xx HATs do.
The bridge renders it into the port's ser2net connector as gensio's
`rs485=<BRIDGE_RS485>`, by default `rs485=0:0:rts_on_send` (no delay
before or after sending, RTS high while sending). A port whose driver does
not support RS-485 mode (a pty, many USB chips) **fails to open** with it on,
so leave it off unless the board needs it. The bridge refuses it (`400`) for a
USB adapter. `serial_text` then ends in `RS-485`.

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
Ports that are not served (excluded, reserved, not enabled, no access, busy)
can be given settings too. These are stored, and reach ser2net once the port
is served.

### `POST /adapters/<key>/enable`

Serves (or stops serving) a UART of the SoC itself — a port with
`needs_enable: true`. The body is exactly `{"enabled": true}` or
`{"enabled": false}`. The opt-in is stored in `portmap.json` and survives
restarts. Make sure nothing else uses the line first: once served, anyone who
can reach its data port talks to whatever is on it.

```bash
curl -s -X POST -H "Authorization: Bearer $BRIDGE_TOKEN" \
     -H 'Content-Type: application/json' -d '{"enabled": true}' \
     'http://pi-garage:7000/adapters/fe201000.serial/enable'
```

| Status | Meaning |
|--------|---------|
| `200` | The port, as in `/adapters` (`status` `ok` once served). |
| `400` | Not that body; a port that needs no enabling (USB, HAT channels); or a reserved port, which is never served. |
| `404` | No port with that key now. |

### `GET /adapters/<key>/stats`

One port's health and its last 50 events. Behind the token.

```json
{
  "stable_id": "B0045K08", "tcp_port": 7001, "available": true,
  "stats": {
    "sampled_ts": 1761400000.1, "interval_s": 5,
    "source": {"transport": "ser2net-admin", "line": "TIOCGICOUNT"},
    "transport_error": "",
    "client": {"address": "192.168.1.10:40312", "since": 1761390000.0},
    "connects": 3, "disconnects": 2, "connects_10min": 0,
    "bytes": {"from_device": 918233, "to_device": 402112, "from_net": 402112, "to_net": 918233},
    "last_rx_ts": 1761400000.1, "last_tx_ts": 1761400000.1, "silent_s": 0.0,
    "line": {"frame": 41, "parity": 3, "overrun": 0, "brk": 0, "buf_overrun": 0,
             "rx": 918233, "tx": 402112},
    "line_as_of": 1761400000.1, "line_note": "", "line_verified": true
  },
  "events": [{"seq": 12, "ts": 1761399970.0, "kind": "line_errors", "key": "B0045K08",
              "tcp_port": 7001, "message": "line errors: frame +12, parity +1",
              "data": {"frame": 12, "parity": 1}}],
  "diagnostics": {"interval_s": 5, "boot": "9f3c1a20", "started": 1761300000.0,
                  "transport": "ser2net-admin", "line": "TIOCGICOUNT via pidfd_getfd"}
}
```

`404` for a key no port has now.

### `GET /events?since=<seq>`

The bridge's events after `seq` (oldest first), from a ring of the last 500.
Behind the token.

```json
{"boot": "9f3c1a20", "events": [...], "next": 14, "oldest": 1, "lost": false}
```

- Ask again with `since=<next>`. `lost: true` means the ring dropped events
  you had not seen.
- `boot` changes when the bridge restarts: the numbers start again from 1, so
  a client that sees a new `boot` asks from `since=0`.
- Kinds: `started`, `slot_warning`, `adapter_new`, `adapter_gone`,
  `adapter_back`, `port_moved`, `dev_renamed`, `status` (a port's status
  changed), `ser2net_restart`, `ser2net_exited`, `client_connected`,
  `client_left`, `line_errors` (at most one per port per minute, with what
  grew in `data`).

## Diagnostics

For every served port the bridge samples, every `BRIDGE_STATS_S` seconds
(default 5), the TCP side and the line. What each number is, and where it
comes from:

| Field | Source | Notes |
|-------|--------|-------|
| `client`, `connects`, `disconnects`, `connects_10min` | ser2net's admin interface (`showport`) | A new client is a new address:port. A client that connects and leaves between two samples is not seen, so `connects` is a lower bound. |
| `bytes.from_device` / `to_device` (and `from_net` / `to_net`) | ser2net's admin interface | ser2net counts per connection; the bridge sums them across connections since it started. A counter that goes down is a new base, never growth. |
| `last_rx_ts`, `last_tx_ts`, `silent_s` | from the byte counters | To the sample interval. `silent_s` is the time since the last byte from the bus, or since the bridge started sampling the port. |
| `line` (`frame`, `parity`, `overrun`, `brk`, `buf_overrun`, `rx`, `tx`) | the kernel's `TIOCGICOUNT` | Cumulative since the adapter appeared (a replug starts again from 0). A counter the driver does not keep is `null`, never `0`. |

**ser2net's admin interface** listens on `127.0.0.1:<BRIDGE_ADMIN_PORT>`
(default 6999) **inside the container only**. It is never published, and it
is off when the port would collide with the data or control ports, or with
`BRIDGE_ADMIN_PORT=0`. When it does not answer, `transport_error` says so and
the last values are kept.

**The line counters are read on ser2net's own open file.** The supervisor
duplicates it with `pidfd_getfd(2)` (ser2net is its child, same uid), asks
`TIOCGICOUNT` on the duplicate and closes it. The tty is never opened a second
time and no termios or modem line is touched. The close is not the last one
while ser2net holds the file; if ser2net closes the port in the microseconds
between the lookup and the ioctl (a client leaving), the duplicate's close is
the last close — it does what ser2net's own close would have done a moment
earlier — and that reading is dropped. This needs:

- the container capability **`SYS_PTRACE`**: Docker's default seccomp profile
  refuses `pidfd_getfd` without it. Both compose files and the gateway's
  `docker run` line add it. The process still runs as uid 10002 with no
  effective capability, so ptrace rules apply to it as to any user: it can
  reach processes of its own uid, which in this container are only itself
  and ser2net (with Yama `ptrace_scope` 1, only its child; on a host where it
  is 0 — Pi OS may ship that — the same set). **Never run the bridge with
  `pid: host`.**
- a kernel 5.6 or newer.

Without them the bridge works as before and `line` is `null`, with the reason
in `line_note`. ser2net opens a port only while a client is connected, so
with no client the last values are kept (`line_as_of` says when).

What each driver keeps (from the kernel's sources; `line_verified: false`
for any other driver, whose values are passed on as the kernel gives them):

| Driver | Line errors | rx / tx | Notes |
|--------|-------------|---------|-------|
| board UARTs (`sc16is7xx`, `uart-pl011` on the Pi 5's RP1, 8250, i.MX, OMAP) | yes | yes | serial_core keeps every counter. |
| `ftdi_sio` (FT232R, FT232H…) | yes | yes | Frame, parity, overrun and break from the chip's status bytes. |
| `cp210x` | only with parity checking on (`INPCK`) | no (`null`) | The driver turns on the chip's event mode only then; with parity N the error counters are `null`. |
| `ch341-uart` (CH340, CH341) | no (`null`) | no (`null`) | The driver has no `get_icount`: `TIOCGICOUNT` fails. |
| `cdc_acm` | as the device reports them | no (`null`) | Only if the device sends `SERIAL_STATE` notifications. |

## Persistent state

The state file is `/data/portmap.json` (in the `serial-bridge-data`
volume).

```json
{
 "B0045K08": 7002,
 "1a86:7523@1-1": 7001,
 "__serial__": {
  "1a86:7523@1-1": {"baud": 19200, "parity": "E", "databits": 8, "stopbits": 1},
  "spi0.0:0": {"baud": 9600, "parity": "N", "databits": 8, "stopbits": 1}
 },
 "__rs485__": ["spi0.0:0"],
 "__enabled__": ["fe201000.serial"]
}
```

- **Ports** stay flat `key: port` entries, the format older versions wrote.
  Older files load unchanged: adapters without a `__serial__` entry use the
  default settings.
- **Downgrade:** an older bridge reads the ports and drops only the
  `__serial__` entry. It logs this as an invalid entry.
- **Invalid entries:** a broken serial entry is dropped and logged, and the
  adapter falls back to the default.
- **`__rs485__`** lists the ports with RS-485 mode on, and **`__enabled__`**
  the SoC UARTs opted in. Both are keys of their own, so `__serial__` stays
  exactly what older bridges read: a bridge older than board UARTs keeps
  every port and every line setting, and only logs the two keys it does not
  know.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `BRIDGE_TOKEN` | *(unset)* | Bearer token for the control API. Unset means no auth. Required by `docker-compose.bridge.yml`. |
| `BRIDGE_SERIAL_PARAMS` | `9600n81` | Default settings for adapters without their own, in ser2net form `<baud><n\|e\|o><databits><stopbits>`. An invalid value falls back to `9600n81`. |
| `BRIDGE_EXCLUDE` | *(empty)* | Comma-separated ports to never expose. Each entry can be a stable id, a `/dev` path, a USB port-path or bus address, or a tty name. |
| `BRIDGE_INCLUDE` | *(empty)* | Comma-separated SoC UARTs to serve without a per-port opt-in. Same tokens as `BRIDGE_EXCLUDE`. |
| `BRIDGE_ACCESS_FIX` | *(unset)* | The command a `no-access` port reports as its `fix`. Each compose file sets its `scripts/serial-access.sh` form. Unset (a plain `docker run`), the fix names the exact `--device-cgroup-rule='c <major>:* rmw'` to recreate the container with. |
| `BRIDGE_RS485` | `0:0:rts_on_send` | The gensio RS-485 setting used for ports with `rs485` on: `<delay before ms>:<delay after ms>` followed by any of `:rts_on_send`, `:rts_after_send`, `:rx_during_tx`, `:terminate_bus`. An invalid value falls back to the default (logged). |
| `BRIDGE_TTY_1` … `BRIDGE_TTY_4` | `188:0` … `188:3` | Read by the compose files: four device rules for board UARTs, `<major>:*` or `4:<minor>`, written by `scripts/serial-access.sh`. **Trusted operator input:** whatever is written there is granted, and a hand-written wildcard (`*:*`) removes the container's device isolation. |
| `BRIDGE_TTY_SLOTS` / `BRIDGE_TTY_DRIVERS` | set by the compose files | The four slots as the bridge sees them, and the driver each was written for (`1:sc16is7xx`), so a stale slot is reported by name ([Warnings](#warnings)). |
| `BRIDGE_PORT_LOW` / `BRIDGE_PORT_HIGH` | `7001` / `7099` | Range of data ports to allocate from. The remote compose file sets 7001–7016 and publishes the same range. |
| `BRIDGE_CONTROL_PORT` | `7000` | Port of the control API inside the container. The image healthcheck assumes 7000. |
| `BRIDGE_HOSTNAME` | *(unset)* | Name reported as `hostname`. If unset, the bridge uses the host's `/etc/hostname` when it is mounted at `/etc/host-hostname` (the remote compose file mounts it), and otherwise the container hostname. |
| `BRIDGE_VERSION` | `dev` | Version string that is reported. Set at build time with `--build-arg BRIDGE_VERSION=<tag>`. |
| `BRIDGE_STATE` | `/data/portmap.json` | Path of the state file. |
| `BRIDGE_RECONCILE_S` | `10` | Seconds between periodic re-scans. These also cover hotplug. |
| `SER2NET_CFG` | `/etc/ser2net/ser2net.yaml` | Path of the generated ser2net config. |
| `BRIDGE_ADMIN_PORT` | `6999` | ser2net's admin interface, on `127.0.0.1` inside the container (the transport statistics). `0` turns it off; a value inside the data range or equal to the control port is refused (off, logged). |
| `BRIDGE_STATS_S` | `5` | Seconds between two samples of every served port (1–300). |
| `BRIDGE_SYSROOT` | `/` | Where `/sys`, `/dev` and `/proc` are read from. Only the tests change it (a fake tree). |
