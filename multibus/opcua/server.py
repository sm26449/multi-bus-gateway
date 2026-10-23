# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""The OPC UA server: every device and value the gateway reads, with the
quality and the time it was read.

    Objects/MBG/Devices/<device>/<value>       Variable, read-only
    Objects/MBG/Devices/<device>/_Online       Boolean — the device answers
    Objects/MBG/Devices/<device>/_Health       String — ok / degraded / down

Node ids are stable strings (``ns=<mbg>;s=Devices/<device>/<value>``), so a
SCADA tag list survives a restart. Each value's DataValue carries:

* **StatusCode** — Good while fresh; UncertainLastUsableValue once older
  than three poll intervals (at least 60 s);
  UncertainNoCommunicationLastUsableValue while the device does not answer
  (the standard's code for "the link failed, this is the last good value" —
  a Bad code would make clients drop the value); and
  BadWaitingForInitialData before the first reading;
* **SourceTimestamp** — when the value was READ from the device, not when
  it was published.

It runs on its own thread with its own event loop: nothing it does can hold
the pollers or the API. Clients log in with the gateway's own accounts or a
service token (user = the token's name, password = the token; anonymous only
when allowed); five wrong passwords lock a user name out for five minutes.

**Commands** (a device's enabled ones), for the gateway's admins and
operators only:

    Objects/MBG/Devices/<device>/Commands/<command>/value       Double, writable — the
        command's main value: writing it RUNS the command (other parameters at
        their defaults) — what an HMI set point does
    Objects/MBG/Devices/<device>/Commands/<command>/Run(...)    Method — every parameter
    Objects/MBG/Devices/<device>/Commands/<command>/LastResult  String — the verdict in words

Every one goes through the gateway's command path (writes allowed, bounds,
the channel's referee and protection, the audit as ``opcua:<user>``).
"""
from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import math
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
# asyncua logs every monitored item and every publish request at INFO: a
# SCADA with a thousand tags would bury the gateway's own log
logging.getLogger("asyncua").setLevel(logging.WARNING)

NAMESPACE = "urn:multi-bus-gateway"
LOCKOUT_FAILS, LOCKOUT_S = 5, 300.0
COMMAND_ROLES = ('admin', 'operator')          # who may run a command over OPC UA
# who is calling: asyncua hands a method no session, so the session's call is
# wrapped to note its user here (a context variable: per request, per task)
_CALLER: contextvars.ContextVar = contextvars.ContextVar('mbg_opcua_caller', default=None)


def _patch_sessions() -> None:
    from asyncua.server.internal_session import InternalSession
    if getattr(InternalSession, '_mbg_caller', False):
        return
    orig_call, orig_write = InternalSession.call, InternalSession.write

    async def call(self, params):
        token = _CALLER.set(self.user)
        try:
            return await orig_call(self, params)
        finally:
            _CALLER.reset(token)

    async def write(self, params):
        token = _CALLER.set(self.user)
        try:
            return await orig_write(self, params)
        finally:
            _CALLER.reset(token)
    InternalSession.call, InternalSession.write = call, write
    InternalSession._mbg_caller = True


def quality_of(entry: Optional[Dict[str, Any]], live: bool, now_mono: float) -> str:
    """good | stale | down | waiting — what a value's StatusCode says."""
    if not entry or entry.get('value') is None:
        return 'waiting'
    if not live:
        return 'down'
    mono, interval = entry.get('mono'), entry.get('interval')
    if mono is None:
        return 'stale'                               # no read time known: fail closed
    if now_mono - float(mono) > max(3.0 * float(interval or 5), 60.0):
        return 'stale'
    return 'good'


class _Users:
    """The gateway's accounts as OPC UA users, with a per-name lockout."""

    def __init__(self, authenticate: Callable[[str, str], Optional[str]], allow_anonymous: bool,
                 clock: Callable[[], float] = time.monotonic):
        self._authenticate, self._anon, self._clock = authenticate, allow_anonymous, clock
        self._fails: Dict[str, list] = {}
        self.roles: Dict[str, str] = {}             # user name → the gateway role it logged in with

    def role_of(self, user) -> Optional[str]:
        name = getattr(user, 'name', None)
        return None if not name or name == 'anonymous' else self.roles.get(name)

    def get_user(self, iserver, username=None, password=None, certificate=None):
        from asyncua.crypto.permission_rules import User, UserRole
        if not username:
            return User(role=UserRole.User, name='anonymous') if self._anon else None
        name = str(username)
        now = self._clock()
        fails = [t for t in self._fails.get(name, []) if now - t < LOCKOUT_S]
        if len(fails) >= LOCKOUT_FAILS:
            logger.warning("OPC UA: user %r locked out after %d wrong passwords", name, len(fails))
            return None
        pw = password.decode('utf-8', 'replace') if isinstance(password, (bytes, bytearray)) else str(password or '')
        role = self._authenticate(name, pw)
        if role is None:
            fails.append(now)
            self._fails[name] = fails
            return None
        self._fails.pop(name, None)
        self.roles[name] = role
        # every role is the library's plain User (reads, writes Values, calls
        # methods, never changes the address space); who may COMMAND is the
        # gateway's role, checked where a command runs
        return User(role=UserRole.User, name=name)


class OpcUaServer:
    """start() / stop() / status(). ``devices()`` yields (device_id,
    display name, live?, store) — the live values as the gateway holds them."""

    def __init__(self, cfg, *, devices: Callable, authenticate: Callable[[str, str], Optional[str]],
                 config_dir: Path, version: str = '', run_command: Optional[Callable] = None):
        self.cfg = cfg
        self._devices = devices
        # run_command(device, name, params, user) -> (http_status, result): the
        # gateway's command path; None = a read-only server
        self._run_command = run_command
        self._users: Optional[_Users] = None
        self._cmd_nodes: Dict[Tuple[str, str], Dict[str, Any]] = {}   # (dev, command) → its nodes
        self._cmd_values: Dict[Any, Tuple[str, str]] = {}              # NodeId of a `value` → (dev, command)
        self._cmd_folders: Dict[str, Any] = {}                         # dev → its Commands folder
        self._authenticate = authenticate
        self._dir = Path(config_dir) / 'opcua'
        self._version = version
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop: Optional[asyncio.Event] = None
        self._ready = threading.Event()
        self.error: Optional[str] = None
        self.endpoint = ''
        self._nodes: Dict[Tuple[str, str], Tuple[Any, str]] = {}    # (dev, name) → (node, variant type)
        self._last: Dict[Tuple[str, str], Tuple] = {}              # what was last written
        self._dev_nodes: Dict[str, Any] = {}
        self._unread: set = set()                        # (dev, name) made before their first value
        self.updates = 0

    # ── the outside ──
    def start(self, wait_s: float = 15.0) -> bool:
        if self._thread is not None:
            return True
        self._ready.clear()
        self.error = None
        self._thread = threading.Thread(target=self._run, name='opcua', daemon=True)
        self._thread.start()
        self._ready.wait(wait_s)
        return self.error is None and self._ready.is_set()

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(10)
        self._thread = None
        self._nodes.clear()
        self._last.clear()
        self._unread.clear()
        self._dev_nodes.clear()
        self._cmd_nodes.clear()
        self._cmd_values.clear()
        self._cmd_folders.clear()

    def status(self) -> Dict[str, Any]:
        return {'enabled': bool(self.cfg.enabled), 'running': bool(self._thread and self._thread.is_alive()
                                                                     and self._ready.is_set() and not self.error),
                'endpoint': self.endpoint, 'error': self.error, 'variables': len(self._nodes),
                'devices': len(self._dev_nodes), 'commands': len(self._cmd_nodes),
                'encryption': self.cfg.encryption,
                'allow_anonymous': bool(self.cfg.allow_anonymous), 'updates': self.updates}

    # ── the thread ──
    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:  # noqa: BLE001 — a failed start is said, never raised into the gateway
            self.error = f"{type(e).__name__}: {e}"
            logger.error("OPC UA server: %s", self.error)
        finally:
            self._ready.set()
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _certificate(self, host: str):
        from cryptography.x509.oid import ExtendedKeyUsageOID
        from asyncua.crypto.cert_gen import setup_self_signed_certificate
        self._dir.mkdir(parents=True, exist_ok=True)
        key, cert = self._dir / 'server_key.pem', self._dir / 'server_cert.der'
        await setup_self_signed_certificate(key, cert, NAMESPACE, host,
                                            [ExtendedKeyUsageOID.SERVER_AUTH],
                                            {'countryName': 'RO', 'organizationName': 'Multi-Bus Gateway',
                                             'commonName': 'Multi-Bus Gateway OPC UA server'})
        try:
            key.chmod(0o600)
        except OSError:
            pass
        return key, cert

    async def _main(self) -> None:
        from asyncua import Server, ua
        cfg = self.cfg
        self._users = _Users(self._authenticate, cfg.allow_anonymous)
        server = Server(user_manager=self._users)
        await server.init()
        if self._run_command is not None:
            _patch_sessions()
            self._intercept_writes(server, ua)
        host = cfg.host if cfg.host not in ('', '0.0.0.0') else '0.0.0.0'
        public = getattr(cfg, 'public_url', '') or ''
        if public:
            # behind a load balancer: clients are told its address, the server
            # listens on its own, and the certificate names the balancer — the
            # pair shares that certificate (it travels with the configuration)
            self.endpoint = public
            server.set_endpoint(public)
            server.socket_address = (host, int(cfg.port))
            cert_host = urlparse(public).hostname or 'localhost'
        else:
            self.endpoint = f"opc.tcp://{host}:{int(cfg.port)}/mbg"
            server.set_endpoint(self.endpoint)
            cert_host = 'localhost' if host == '0.0.0.0' else host
        server.set_server_name("Multi-Bus Gateway")
        await server.set_application_uri(NAMESPACE)
        policies = []
        if cfg.encryption in ('required', 'optional'):
            key, cert = await self._certificate(cert_host)
            await server.load_certificate(str(cert))
            await server.load_private_key(str(key))
            policies.append(ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt)
        if cfg.encryption in ('optional', 'off'):
            policies.append(ua.SecurityPolicyType.NoSecurity)
        server.set_security_policy(policies)
        server.set_security_IDs(["Username"] + (["Anonymous"] if cfg.allow_anonymous else []))
        idx = await server.register_namespace(NAMESPACE)
        mbg = await server.nodes.objects.add_folder(ua.NodeId("MBG", idx), ua.QualifiedName("MBG", idx))
        devs = await mbg.add_folder(ua.NodeId("Devices", idx), ua.QualifiedName("Devices", idx))
        self._stop = asyncio.Event()
        # the whole address space before the first client: one that subscribes
        # the moment the gateway is back must find every node it knew (a node
        # that is not there answers BadNodeIdUnknown, and most clients never
        # ask again)
        try:
            await self._sync(server, idx, devs, ua)
        except Exception as e:  # noqa: BLE001
            logger.exception("OPC UA address space at start: %s", e)
        async with server:
            self._ready.set()
            logger.info("OPC UA server on %s (%s)", self.endpoint,
                        ', '.join(getattr(p, 'name', str(p)) for p in policies))
            while not self._stop.is_set():
                try:
                    await self._sync(server, idx, devs, ua)
                except Exception as e:  # noqa: BLE001 — one bad pass must not stop the server
                    logger.exception("OPC UA update: %s", e)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=float(cfg.update_s))
                except asyncio.TimeoutError:
                    pass

    # ── keeping the address space current ──
    @staticmethod
    def _variant(value) -> Tuple[Any, str]:
        if isinstance(value, bool):
            return value, 'Boolean'
        if isinstance(value, (int, float)) and not (isinstance(value, float) and not math.isfinite(value)):
            return float(value), 'Double'
        return ('' if value is None else str(value)), 'String'

    async def _device_node(self, devs, idx, dev: str, name: str, ua):
        node = self._dev_nodes.get(dev)
        if node is None:
            node = await devs.add_object(ua.NodeId(f"Devices/{dev}", idx), ua.QualifiedName(dev, idx))
            await node.write_attribute(ua.AttributeIds.DisplayName,
                                       ua.DataValue(ua.Variant(ua.LocalizedText(name or dev), ua.VariantType.LocalizedText)))
            self._dev_nodes[dev] = node
        return node

    async def _variable(self, parent, idx, dev: str, name: str, value, label: str, unit: str, ua,
                        vt: Optional[str] = None):
        key = (dev, name)
        if key in self._nodes:
            return self._nodes[key]
        if vt is None:
            v, vt = self._variant(value)
        else:                                             # declared, before its first value
            v = {'Double': 0.0, 'Boolean': False}.get(vt, '')
            self._unread.add(key)
        var = await parent.add_variable(ua.NodeId(f"Devices/{dev}/{name}", idx), ua.QualifiedName(name, idx),
                                        ua.Variant(v, getattr(ua.VariantType, vt)))
        text = (label or name) + (f" [{unit}]" if unit else '')
        await var.write_attribute(ua.AttributeIds.Description,
                                  ua.DataValue(ua.Variant(ua.LocalizedText(text), ua.VariantType.LocalizedText)))
        if label:
            await var.write_attribute(ua.AttributeIds.DisplayName,
                                      ua.DataValue(ua.Variant(ua.LocalizedText(label), ua.VariantType.LocalizedText)))
        self._nodes[key] = (var, vt)
        return self._nodes[key]

    async def _put(self, server, key, var, vt: str, value, quality: str, ts: Optional[float], ua) -> None:
        sig = (value, quality, ts)
        if self._last.get(key) == sig:
            return
        v, actual = self._variant(value)
        if value is None:                                 # nothing read yet: the type's empty value
            v, actual = {'Double': 0.0, 'Boolean': False}.get(vt, ''), vt
        elif key in self._unread:
            self._unread.discard(key)
            if actual != vt:
                # declared as one type, read as another (a text field of an HTTP
                # template that says nothing): the node takes the real type in
                # place, keeping its id and every subscription to it
                await server.write_attribute_value(var.nodeid, ua.DataValue(ua.Variant(
                    ua.NodeId(getattr(ua.ObjectIds, actual)), ua.VariantType.NodeId)), ua.AttributeIds.DataType)
                vt = actual
                self._nodes[key] = (var, vt)
        if actual != vt:                                  # the type a node was born with stays
            v = str(value) if vt == 'String' else (float(value) if vt == 'Double' and isinstance(value, (int, float))
                                                    else (bool(value) if vt == 'Boolean' else v))
            if vt == 'Double' and not isinstance(v, float):
                v, quality = 0.0, 'waiting'
        code = {'good': ua.StatusCodes.Good, 'stale': ua.StatusCodes.UncertainLastUsableValue,
                'down': ua.StatusCodes.UncertainNoCommunicationLastUsableValue,
                'waiting': ua.StatusCodes.BadWaitingForInitialData}[quality]
        dv = ua.DataValue(ua.Variant(v, getattr(ua.VariantType, vt)), StatusCode_=ua.StatusCode(code),
                          SourceTimestamp=datetime.fromtimestamp(ts, timezone.utc) if ts else None,
                          ServerTimestamp=datetime.now(timezone.utc))
        await server.write_attribute_value(var.nodeid, dv)
        self._last[key] = sig
        self.updates += 1

    async def _sync(self, server, idx, devs, ua) -> None:
        """One pass. Each item: (device, display name, live?, health, store,
        commands, declared, chosen) — declared = [(name, label, unit, type)]
        it will read, made before their first value; chosen = the value names
        served, None = every one."""
        now = time.monotonic()
        seen = set()
        for item in list(self._devices()):
            dev, display, live, health, store = item[:5]
            commands = item[5] if len(item) > 5 else {}
            declared = item[6] if len(item) > 6 else ()
            chosen = item[7] if len(item) > 7 else None
            attrs = item[8] if len(item) > 8 else {}
            seen.add(dev)
            node = await self._device_node(devs, idx, dev, display, ua)
            if self._run_command is not None:
                for cname, cdef in (commands or {}).items():
                    if (dev, cname) not in self._cmd_nodes:
                        await self._command_nodes(server, node, idx, dev, cname, cdef, ua)
            for special, value, vt in (('_Online', bool(live), 'Boolean'), ('_Health', str(health or ''), 'String')):
                var, _vt = await self._variable(node, idx, dev, special, value, special.strip('_'), '', ua)
                await self._put(server, (dev, special), var, vt, value, 'good', time.time(), ua)
            for name, label, unit, vt in declared or ():
                if (dev, name) not in self._nodes and (chosen is None or name in chosen):
                    var, vt = await self._variable(node, idx, dev, name, None, label, unit, ua, vt=vt)
                    await self._put(server, (dev, name), var, vt, None, 'waiting', None, ua)
            for entry in list((store or {}).values()):
                name = entry.get('name')
                if not name or chosen is not None and name not in chosen:
                    continue
                if entry.get('value') is None and (dev, name) not in self._nodes:
                    continue
                var, vt = await self._variable(node, idx, dev, name, entry.get('value'),
                                               entry.get('label') or '', entry.get('unit') or '', ua)
                await self._put(server, (dev, name), var, vt, entry.get('value'),
                                quality_of(entry, live, now), entry.get('ts'), ua)
            # its asset attributes (docs/attributes.md): Attributes.<category>.<field>,
            # facts that change only when someone edits them
            names = set()
            for path, value in (attrs or {}).items():
                name = f"Attributes.{path}"
                names.add(name)
                var, vt = await self._variable(node, idx, dev, name, value, name, '', ua)
                await self._put(server, (dev, name), var, vt, value, 'good', None, ua)
            for key in [k for k in self._nodes if k[0] == dev and k[1].startswith('Attributes.')
                        and k[1] not in names]:
                await server.delete_nodes([self._nodes.pop(key)[0]])
                self._last.pop(key, None)
            if chosen is not None:                       # values no longer chosen
                for key in [k for k in self._nodes if k[0] == dev and k[1] not in chosen
                            and k[1] not in ('_Online', '_Health') and not k[1].startswith('Attributes.')]:
                    await server.delete_nodes([self._nodes.pop(key)[0]])
                    self._last.pop(key, None)
                    self._unread.discard(key)
        for dev in [d for d in self._dev_nodes if d not in seen]:       # off on OPC UA, or gone
            await server.delete_nodes([self._dev_nodes.pop(dev)], recursive=True)
            for key in [k for k in self._nodes if k[0] == dev]:
                self._nodes.pop(key, None)
                self._last.pop(key, None)
                self._unread.discard(key)
            self._cmd_folders.pop(dev, None)
            for key in [k for k in self._cmd_nodes if k[0] == dev]:
                for nid in [n for n, v in self._cmd_values.items() if v == key]:
                    self._cmd_values.pop(nid, None)
                self._cmd_nodes.pop(key, None)

    # ── commands ──
    async def _command_nodes(self, server, dev_node, idx, dev: str, cname: str, cdef: Dict[str, Any], ua) -> None:
        folder = self._cmd_folders.get(dev)
        if folder is None:
            folder = await dev_node.add_folder(ua.NodeId(f"Devices/{dev}/Commands", idx), ua.QualifiedName("Commands", idx))
            self._cmd_folders[dev] = folder
        base = f"Devices/{dev}/Commands/{cname}"
        obj = await folder.add_object(ua.NodeId(base, idx), ua.QualifiedName(cname, idx))
        label = cdef.get('label') or cname
        await obj.write_attribute(ua.AttributeIds.Description,
                                  ua.DataValue(ua.Variant(ua.LocalizedText(label), ua.VariantType.LocalizedText)))
        nodes: Dict[str, Any] = {}
        result = await obj.add_variable(ua.NodeId(f"{base}/LastResult", idx), ua.QualifiedName("LastResult", idx),
                                        ua.Variant('', ua.VariantType.String))
        nodes['result'] = result
        params = cdef.get('params') or {}
        if 'value' in params:
            p = params['value']
            var = await obj.add_variable(ua.NodeId(f"{base}/value", idx), ua.QualifiedName("value", idx),
                                         ua.Variant(0.0, ua.VariantType.Double))
            await var.set_writable(True)
            bounds = f" {p.get('min', '')}…{p.get('max', '')}" if (p.get('min') is not None or p.get('max') is not None) else ''
            await var.write_attribute(ua.AttributeIds.Description, ua.DataValue(ua.Variant(
                ua.LocalizedText(f"{label} — writing it runs the command{bounds}{(' ' + p['unit']) if p.get('unit') else ''}"),
                ua.VariantType.LocalizedText)))
            await server.write_attribute_value(var.nodeid, ua.DataValue(
                ua.Variant(0.0, ua.VariantType.Double),
                StatusCode_=ua.StatusCode(ua.StatusCodes.BadWaitingForInitialData)))
            nodes['value'] = var
            self._cmd_values[var.nodeid] = (dev, cname)
        args = [ua.Argument(Name=pn, DataType=ua.NodeId(ua.ObjectIds.Double), ValueRank=-1, ArrayDimensions=[],
                            Description=ua.LocalizedText(f"{pd.get('label') or pn}"
                                                         + ("" if pd.get('required') else
                                                            f" (default {pd.get('default')})" if pd.get('default') is not None
                                                            else " (optional)")))
                for pn, pd in params.items()]
        out = [ua.Argument(Name='Result', DataType=ua.NodeId(ua.ObjectIds.String), ValueRank=-1, ArrayDimensions=[],
                           Description=ua.LocalizedText('the verdict in words'))]
        names = list(params)

        async def run_method(parent, *inputs):
            values = {}
            for pn, v in zip(names, inputs):
                val = getattr(v, 'Value', v)
                if val is not None:
                    values[pn] = val
            code, text = await self._run_cmd(server, dev, cname, values, _CALLER.get(), ua)
            res = ua.CallMethodResult()
            res.StatusCode = ua.StatusCode(code)
            res.OutputArguments = [ua.Variant(text, ua.VariantType.String)]
            return res
        await obj.add_method(ua.NodeId(f"{base}/Run", idx), ua.QualifiedName("Run", idx), run_method, args, out)
        self._cmd_nodes[(dev, cname)] = nodes

    def _verdict(self, http: int, res: Dict[str, Any], ua) -> int:
        status = (res or {}).get('status')
        if status in ('success', 'unverified'):
            return ua.StatusCodes.Good
        if http == 403:
            return ua.StatusCodes.BadUserAccessDenied
        if http == 422:
            return ua.StatusCodes.BadOutOfRange
        if http in (409, 429):
            return ua.StatusCodes.BadInvalidState
        if status == 'mismatch':
            return ua.StatusCodes.BadInvalidState
        return ua.StatusCodes.BadCommunicationError

    async def _run_cmd(self, server, dev: str, cname: str, params: Dict[str, Any], user, ua) -> Tuple[int, str]:
        role = self._users.role_of(user) if self._users is not None else None
        who = getattr(user, 'name', None) or 'anonymous'
        if role not in COMMAND_ROLES:
            text = f"refused: {who} may not run commands (an admin or operator account may)"
            code = ua.StatusCodes.BadUserAccessDenied
        else:
            loop = asyncio.get_running_loop()
            try:
                http, res = await loop.run_in_executor(None, functools.partial(
                    self._run_command, dev, cname, dict(params), who))
            except Exception as e:  # noqa: BLE001 — a command that crashed is a failed one, said
                http, res = 500, {'status': 'error', 'reason': f"{type(e).__name__}: {e}"}
            code = self._verdict(http, res, ua)
            text = f"{res.get('status', '?')}" + (f" — {res.get('reason')}" if res.get('reason') else '') \
                + f" · {cname} {params} by opcua:{who}"
        nodes = self._cmd_nodes.get((dev, cname)) or {}
        if nodes.get('result') is not None:
            await server.write_attribute_value(nodes['result'].nodeid, ua.DataValue(
                ua.Variant(text, ua.VariantType.String), SourceTimestamp=datetime.now(timezone.utc)))
        if code == ua.StatusCodes.Good and nodes.get('value') is not None and 'value' in params:
            await server.write_attribute_value(nodes['value'].nodeid, ua.DataValue(
                ua.Variant(float(params['value']), ua.VariantType.Double),
                SourceTimestamp=datetime.now(timezone.utc)))
        logger.info("OPC UA command %s %s %s by %s: %s", dev, cname, params, who, text)
        return code, text

    def _intercept_writes(self, server, ua) -> None:
        """A write to a command's `value` runs the command (with the writer's
        role and the gateway's command path); every other write goes on to
        the library as before."""
        service = server.iserver.attribute_service
        orig = service.write

        async def write(params, user=None):
            mine = [i for i, w in enumerate(params.NodesToWrite)
                    if w.NodeId in self._cmd_values and w.AttributeId == ua.AttributeIds.Value]
            if not mine:
                return await orig(params, user=user) if user is not None else await orig(params)
            results: list = [None] * len(params.NodesToWrite)
            rest = [w for i, w in enumerate(params.NodesToWrite) if i not in mine]
            if rest:
                done = await orig(ua.WriteParameters(NodesToWrite=rest), user=user) if user is not None \
                    else await orig(ua.WriteParameters(NodesToWrite=rest))
                it = iter(done)
                for i in range(len(results)):
                    if i not in mine:
                        results[i] = next(it)
            for i in mine:
                w = params.NodesToWrite[i]
                dev, cname = self._cmd_values[w.NodeId]
                val = getattr(getattr(w.Value, 'Value', None), 'Value', None)
                if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(float(val)):
                    results[i] = ua.StatusCode(ua.StatusCodes.BadTypeMismatch)
                    continue
                code, _text = await self._run_cmd(server, dev, cname, {'value': float(val)}, user or _CALLER.get(), ua)
                results[i] = ua.StatusCode(code)
            return results
        service.write = write
