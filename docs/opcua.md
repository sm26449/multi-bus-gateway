# OPC UA server: the gateway for SCADA and HMI panels

The gateway can serve every device and value it reads over **OPC UA**, the
protocol SCADA systems (Ignition, WinCC, Zenon, FUXA) and modern HMI panels
(Weintek cMT, Siemens Unified) read natively. Each value comes with its
**quality** and the **time it was read**, which Modbus cannot carry.

**Status:**
- **Reads and commands** today: set points and methods, through the command
  path.
- **The opposite direction** — the gateway as an OPC UA *client*, reading
  OpenPLC, an S7-1200 or another server — is next.

---

## 1. Turn it on

**Settings → OPC UA:** switch it on, check the port (4840 is the standard),
**Save & Apply**. The status line reads, for example:

> ● Running — opc.tcp://0.0.0.0:4840/mbg · 6 devices, 214 values · the gateway accounts

| Setting | Default | What it does |
|---|---|---|
| Port | 4840 | the TCP port clients connect to |
| Listen on | 0.0.0.0 | every interface of the host; `127.0.0.1` for the host alone |
| Encryption | required | **required**: only Basic256Sha256, sign and encrypt. **optional**: also without, for older panels; passwords then travel in clear on the LAN. **off**: only on an isolated network |
| Anonymous clients | off | otherwise clients log in with the gateway's accounts |
| Update every | 1 s | how often values are pushed into the address space |
| Public address | empty | behind a load balancer only: its `opc.tcp://` address (see *Behind a load balancer* below) |

In `config.yaml` it is the `opcua:` section, with the same names.

**In Docker:** publish the port. Add `- "4840:4840"` to the gateway's
`ports:`, unless it runs with `network_mode: host`.

### Behind a load balancer

Two gateways in a high-availability pair are one OPC UA server to their
clients: a load balancer (HAProxy) in front sends the connections to the
active one (it asks `GET /api/role`: 200 active, 503 standby). Set
**Public address** to the balancer's, `opc.tcp://<balancer>:4840/mbg`:

- the server still listens on its own port; it answers on the path of the
  public address, in the host and port the client used (the balancer's);
- its certificate names the balancer's host, and the standby takes the
  active one's certificate and key with the copy of the configuration — a
  client trusts one certificate, whichever gateway answers;
- after a hand-over the clients' sessions are closed and they reconnect
  (about two seconds through HAProxy, measured).

### Which devices and values are served

Each device chooses, like its MQTT and InfluxDB outputs: **Device →
Outputs → OPC UA**.
- **Enable:** the device is served, or not at all.
- **Every value**, or **only the values I choose** (a filterable list of
  what the device reads). An HMI then gets exactly what it shows, and the
  gateway does less work.
- **An endpoint's units** share the choice of their group: one template,
  one list of values.
- **Defaults:**
  - a device made **before** this choice existed is served with every
    value, as it was;
  - a device or an endpoint made **since** is not served until it is
    turned on.

In `config.yaml`:

```yaml
devices:
  - id: grid-meter
    opcua: { enabled: true, fields: [power_active_total, frequency] }   # fields omitted = every value
endpoints:
  - id: pv
    opcua: { enabled: false }               # the endpoint's default…
    groups:
      - id: inverters
        opcua: { enabled: true }            # …a group's own choice wins
opcua_output: { enabled: true }             # the primary device (umg512)
```

API: `POST /api/devices/<id>/opcua-output {enabled, fields}`; a device's
`sinks.opcua` says whether it is served and whether the server runs.

---

## 2. What a client sees

```
Objects/MBG/Devices/<device>/<value>      a value, read-only
Objects/MBG/Devices/<device>/_Online      true while the device answers
Objects/MBG/Devices/<device>/_Health      ok / degraded / down
Objects/MBG/Devices/<device>/Attributes.<category>.<field>   its asset attributes, read-only
```

**Asset attributes** ([attributes.md](attributes.md)) are flat values:
- `Attributes.grid_connection.max_export_kw`;
- `Attributes.pv_input.2.tilt_deg` (the second PV input row);
- `Attributes.location.latitude_deg` (the device's effective location).

Coordinates appear as the publishing setting says (coarse by default; none
when off). The values change only when someone edits them.

**Node ids** are stable strings, `ns=<n>;s=Devices/grid-meter/power_active_total`,
where `<n>` is the index of the namespace `urn:multi-bus-gateway`. A SCADA
tag list therefore survives a restart of the gateway. Each node has:
- a **display name**: the value's label;
- a **description**: the label and the unit, e.g. `Active power [W]`.

**The quality of each value:**

| StatusCode | When |
|---|---|
| **Good** | read within three poll intervals (at least 60 s) |
| **UncertainLastUsableValue** | older than that: the source is slow or stuck |
| **UncertainNoCommunicationLastUsableValue** | the device does not answer; the value is its last good one |
| **BadWaitingForInitialData** | nothing read yet |

**SourceTimestamp** is the moment the value was read from the device;
ServerTimestamp is the moment it was published.

**From the first second:** every value a served device declares (its
template's registers, its calculated values) is a node as soon as the
server starts, BadWaitingForInitialData until its first read. A SCADA that
subscribes the moment the gateway is back gets each value as it comes,
with no BadNodeIdUnknown; that includes units an endpoint finds seconds
after the start. A value whose type the template does not say (an HTTP
field taken for a number that reads as text) takes its real type in place:
same node id, subscriptions kept.

### Commands: set points and methods

A device's **enabled** commands (Device → Commands) appear under it:

```
Devices/<device>/Commands/<command>/value        writable — writing it RUNS the command
Devices/<device>/Commands/<command>/Run(...)     a method with every parameter
Devices/<device>/Commands/<command>/LastResult   the last verdict, in words
```

- **`value`** is what an HMI set point writes: `run_at` on a drive (Hz),
  `power_limit` on an inverter (%). The command's other parameters take
  their defaults. After a success, `value` reads back what was written.
- **`Run`** takes every parameter (its input arguments carry the names),
  for SCADA systems that call methods. It returns the verdict.
- **Every command goes through the gateway's command path:**
  - writes allowed (Settings → Security);
  - the command enabled on the device, and its bounds;
  - the check after write;
  - the channel's referee and protection;
  - the audit, as `opcua:<user>`.

  A drive held by an armed rule refuses, and **LastResult** says why
  (`owned by rule 'day'`). Pause the rule first, or give the HMI its own
  rule layer.
- **Only the gateway's admin and operator accounts command.** A viewer or
  an anonymous client gets `BadUserAccessDenied`, and LastResult says so.
  The measured values themselves are never writable.

What the client sees as the write's status:

| Status | Meaning |
|---|---|
| Good | the command ran (success, or written but not checked back) |
| BadUserAccessDenied | not an admin/operator, or writes are off in the gateway |
| BadOutOfRange | outside the command's bounds |
| BadInvalidState | an armed rule owns the target, the channel's protection holds it, or the device kept another value |
| BadCommunicationError | the device did not answer |
| BadTypeMismatch | a value that is not a number |

---

## 3. Who may connect

- **The gateway's accounts:** the admin, operator and viewer of Settings →
  Security. Each reads everything; admins and operators also run commands.
  Five wrong passwords lock a user name for five minutes.
- **A service token** (Settings → Security → Service tokens): what a SCADA,
  an HMI or a soft PLC should use, since it is a program, not a person.
  - User = the token's name, password = the token (`mbgst_…`).
  - It carries its role: a `viewer` token reads, an `operator` token also
    commands.
  - It shows in the audit log under its name and is revoked alone.
- **Login turned off in the gateway:** there are no accounts. Turn login on,
  or allow anonymous clients.
- **The server certificate** is self-signed. It is made on the first start
  in `config/opcua/` (the key readable by the gateway alone) and is
  downloadable from the settings card. A client that checks servers
  (UaExpert, Ignition, a panel) is told to trust it once.

---

## 4. Connecting a client

**UaExpert** (free, for checking):
1. Add Server → *Advanced*, endpoint `opc.tcp://<gateway>:4840/mbg`.
2. Security: Basic256Sha256 — Sign & Encrypt.
3. User: a gateway account.
4. Trust the server certificate when asked.

**Weintek cMT (EasyBuilder Pro)**:
1. System Parameters → Device → New → **OPC UA Client**.
2. The same endpoint, security and user.
3. Browse `MBG/Devices`, pick the tags.

**FUXA** (open-source web SCADA): provisioned from the gateway in one command,
with a check that it shows the same values: [fuxa.md](fuxa.md).

**Ignition:** an OPC UA connection to the endpoint, then browse.

Older panels without OPC UA keep the gateway's **virtual Modbus meters**
(Modbus TCP).

---

## 5. Troubleshooting

| What you see | Why |
|---|---|
| *Did not start: … address already in use* | another program holds the port; change the port |
| the client sees no endpoint | encryption is *required* and the client tries without; set its security, or choose *optional* |
| *BadUserAccessDenied* | wrong user or password, a locked name, or login off in the gateway |
| every value **UncertainNoCommunication…** | the devices do not answer; see Devices |
