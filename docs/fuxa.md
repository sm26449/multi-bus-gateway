# FUXA: a web SCADA/HMI on the gateway's OPC UA server

[FUXA](https://github.com/frangoteam/FUXA) is an open-source web SCADA/HMI.
Here it is the gateway's reference OPC UA client:
- it shows that a SCADA reads the gateway with no glue;
- it gives a screen to test with, from any browser.

What this page covers:
1. run FUXA;
2. provision it from the gateway, in one command;
3. give it a screen;
4. check that FUXA shows exactly what the gateway serves.

Every step is scripted. The scripts send FUXA the same requests its own
editor sends; those were recorded with Playwright and are listed in §6.

---

## 1. Run FUXA

```sh
docker compose -f tools/fuxa/docker-compose.yml up -d     # http://<host>:1881
```

- **Image:** FUXA 1.3.5, pinned by digest; the Docker Hub tags lag behind.
- **Volumes:** everything FUXA keeps lives on named volumes `fuxa_*`:
  - the project and settings (`_appdata`, `_db`);
  - the plugins (`_pkg`).
- **Recreating the container** therefore loses nothing.

A new FUXA opens on an **empty page**: `/` shows the project's screens and
there are none yet (§3 makes one). The connections are at `/device`, the
editor at `/editor`. Its first-run *Initial Security Setup* dialog can be closed;
FUXA's own login is a separate matter (§5).

---

## 2. The gateway side: a token for FUXA

FUXA is a program, so it logs in with a **service token**, not a person's
account.

1. In the gateway: **Settings → Security → Service tokens**.
2. Name `fuxa`, role **viewer** (reads only; **operator** if FUXA should
   also write set points, see §5).
3. Copy the token (`mbgst_…`). It is shown once.
4. **Settings → OPC UA** must be on. Encryption *required* is right; FUXA
   speaks Basic256Sha256 · Sign & Encrypt.

Keep the token in a file only you can read, never in the repository:

```sh
install -m 600 /dev/null ~/.config/mbg-fuxa.env
echo 'MBG_OPCUA_TOKEN=mbgst_…' > ~/.config/mbg-fuxa.env
```

---

## 3. Provision FUXA

The scripts need `asyncua`, so run them in the gateway's test image or any
Python with `requirements.txt`:

```sh
set -a; . ~/.config/mbg-fuxa.env; set +a
docker run --rm --network host -v "$PWD":/srv -w /srv -e MBG_OPCUA_TOKEN \
  mbg-test:local python tools/fuxa/provision.py \
  --fuxa http://127.0.0.1:1881 --mbg opc.tcp://<gateway-host>:4840/mbg --user fuxa
```

What it does, in order:

1. **Installs FUXA's OPC UA plugin** (`node-opcua`) if missing. It is the
   same as Settings → Plugins → OPCUA → install, and takes about a minute.
   Without it, *OPCUA* is missing from FUXA's connection types.
2. **Browses the gateway** over OPC UA with the token: Sign & Encrypt,
   logged in as `fuxa`. A wrong token stops here, before FUXA is touched.
3. **Creates the connection** `MBG` in FUXA:
   - type OPCUA;
   - the endpoint;
   - Basic256Sha256 · Sign & Encrypt;
   - polling 1 s (the gateway's update interval).

   FUXA keeps the user and the token in its own credential store, not in
   the project file.
4. **Creates one tag per value:**
   - name `<device>/<field>`;
   - label `<device> · <label>`;
   - the OPC UA type;
   - address = the node id, e.g. `ns=1;s=Devices/umg512/power_active_total`.

   Each device's `_Online` and `_Health` are tags too. These are the tags
   FUXA's own *browse and add* would make.
5. **Adds the connection's status tag** on FUXA Server, as the editor does.

| Option | What |
|---|---|
| `--name MBG` | the connection's name in FUXA |
| `--device <id>` | only these gateway devices (repeatable) |
| `--polling 1000` | ms |
| `--dry-run` | browse and say what would change, change nothing |
| `--prune` | drop tags whose nodes the gateway no longer serves |
| `--remove` | delete the connection and its status tag |

**Run it again** after adding devices to the gateway:
- existing tags keep their ids, so screens bound to them survive;
- only the new values are added.

The commands under `Devices/<dev>/Commands` are **not** provisioned. FUXA
reads; see §5 for writing.

### A screen

```sh
docker run --rm --network host -v "$PWD":/srv -w /srv mbg-test:local \
  python tools/fuxa/screen.py --fuxa http://127.0.0.1:1881 --tag umg512/frequency
```

**What the screen shows:**
- one row per value: device, label, the live value, the unit;
- by default, every device's `_Online`, `_Health` and `power_active_total`
  (when it has one);
- `--tag <device>/<field>` adds rows.

**Where it lands:** it becomes FUXA's home page (`/`) when there is none
and gets a menu entry; other screens are left alone. **Rerunning it**
redraws the same screen, with the same id.

For a real HMI, start from it in FUXA's editor (`/editor`), or draw from
scratch and bind the provisioned tags.

---

## 4. Check the data: gateway against FUXA

```sh
docker run --rm --network host -v "$PWD":/srv -w /srv -e MBG_OPCUA_TOKEN \
  mbg-test:local python tools/fuxa/compare.py --fuxa http://127.0.0.1:1881 --mbg opc.tcp://<gateway-host>:4840/mbg
```

**How it compares each tag of the `MBG` connection:**
1. Read the node from the gateway.
2. Read FUXA's value (`/api/getTagValue`).
3. Read the gateway again.

**Moving values:** FUXA gets a value one publish after the gateway has
it, so a tag matches when FUXA shows a value the gateway held in the last
~3 s; one that still differs is tried again, up to 4 times. **Numbers**
match within 1e-6.

**Batching:** FUXA takes the tag ids in the query string, so they are asked
in batches of 100; 1199 ids at once is an HTTP 431.

It prints one `PASS`/`FAIL` line per tag (with the gateway's status code
when it is not Good) and exits 0 only when all match.

**What FUXA's own screens show** (Playwright):

```sh
cd tools/e2e && FUXA_URL=http://127.0.0.1:1881 FUXA_CONN=MBG SHOT=/tmp/shots \
  CHROMIUM_PATH=<chrome> node fuxa_ui_check.mjs
```

It checks four things and saves `fuxa-connections.png`, `fuxa-tags.png`
and `fuxa-home.png`:
- the connection is on Connections and green;
- its tag list holds every tag;
- every row on its first page has received a value (the editor fills
  them as FUXA pushes, which takes some seconds with a thousand tags);
- the home screen (§3) shows a live value in every row
  (`FUXA_SCREEN=0` skips this).

---

## 5. Know before building screens

- **FUXA ignores OPC UA quality.** It takes the value and drops the
  StatusCode. A value the gateway marks *UncertainNoCommunication…*
  (device not answering, last value kept) looks normal in FUXA.
  - **Put the device's `_Online` (or `_Health`) on every screen** next to
    its values, and colour or hide them on it.
  - The comparison tool prints the status, so a stale value is visible
    there.
- **A gateway restart needs nothing on FUXA's side.** FUXA reconnects
  within seconds and subscribes again to every tag. FUXA never retries a
  node it could not subscribe to, so the gateway has its whole address
  space up before it accepts the first client: every value its served
  devices declare is there from the start, *BadWaitingForInitialData*
  until read, the units an endpoint finds seconds later included
  ([opcua.md](opcua.md#which-devices-and-values-are-served)).

  `tools/fuxa/restart_check.sh` proves it. It restarts the gateway, leaves
  FUXA alone, waits for FUXA to reconnect, then runs `compare.py`:

  ```sh
  set -a; . ~/.config/mbg-fuxa.env; set +a
  tools/fuxa/restart_check.sh            # [gateway container] [fuxa container]
  ```
- **The cost to the gateway:** FUXA subscribed to all 1199 values at 1 s
  adds about 10 % of one CPU core to the gateway. Serve only what the HMI
  shows: Device → Outputs → OPC UA, *only the values I choose* (or turn a
  device off there). `provision.py` provisions what the gateway serves;
  run it again after changing the choice, with `--prune` to drop tags
  that are no longer served.
- **Writing set points:**
  - give the token the role **operator**;
  - add the command's `Devices/<dev>/Commands/<cmd>/value` node as a tag
    (FUXA: the connection → *browse*, or by address);
  - bind it to an input.

  The write runs the command with all the gateway's checks (see
  [opcua.md](opcua.md#commands-set-points-and-methods)) and is audited as
  `opcua:fuxa`. A refusal comes back as a Bad status, and `LastResult` says
  why.
- **FUXA's own login** is off on a fresh install: anyone on the LAN can
  edit the project. Turn it on in FUXA → Settings → Security
  (`secureEnabled`) before it leaves a test bench.
- **The client certificate:** the gateway does not pin OPC UA client
  certificates; the login is what admits a client. FUXA makes its own
  certificate and needs no trust step on the gateway.
- **Namespace index:** the node ids carry `ns=<n>`, resolved from
  `urn:multi-bus-gateway` at provisioning time. It has been stable: it
  depends only on the gateway's code, not on its devices.

---

## 6. FUXA's REST API, as its editor uses it (1.3.5)

Recorded with Playwright on the editor's Connections page. The scripts use
only these:

| Request | Body | What |
|---|---|---|
| `GET /api/project` | — | the whole project; `devices` keyed by id |
| `GET /api/plugins` | — | plugins; `current` set when installed |
| `POST /api/plugins` | `{params: <plugin from the list>}` | install |
| `POST /api/device` | `{params: {query: 'security', name: <device id>, value: {mode: {endpointUrl, securityMode: '3', securityPolicy: '…#Basic256Sha256'}, uid, pwd}}}` | the login, stored apart from the project |
| `POST /api/projectData` | `{cmd: 'set-device', data: <device>}` | create or replace a connection with its tags |
| `POST /api/projectData` | `{cmd: 'del-device', data: <device>}` | delete it |
| `GET /api/getTagValue?ids=[…]` | — | live values `{id, value, ts}` |

**An OPCUA connection:**
`{id: 'd_…', name, type: 'OPCUA', enabled, polling, property: {address: 'opc.tcp://…'}, tags}`.

**A tag:**
`{id: 't_…', name, label, type: 'Double'|'Boolean'|'String'|…, address: '<node id>', daq: {…}}`.

`securityMode` `'3'` is SignAndEncrypt (`'1'` none, `'2'` sign). With
FUXA's login on, these calls need its token (`x-access-token`); the
scripts assume a bench FUXA with its login off.

---

## 7. Verified

| Date | Against | Result |
|---|---|---|
| 2026-10-19 | a test copy of the gateway (encryption required, viewer token) | provision → 4 tags; compare 4/4; UI check 4/4; rerun keeps ids; `--remove` clean |
| 2026-10-19 | production, 18 devices (viewer token `fuxa`) | provision → 1199 tags; compare **1199/1199**; screen 44 values; UI check 5/5 |
| 2026-10-19 | production, gateway restart with FUXA connected | before the fix: 52 tags frozen (nodes made at the first value; late-found devices remade). After (address space built from the configuration): deploy restart and `restart_check.sh` both **1199/1199**, FUXA untouched |
