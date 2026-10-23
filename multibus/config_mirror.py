# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A standby takes its partner's configuration — but not what is its own.

Two gateways on the same devices run the same configuration, except where
each machine differs: its own serial adapters, its own address. Those paths
stay as they are on the standby ("kept local"); everything else is the
partner's. A path names a value in config.yaml: dots between keys; a list
entry by its `id`; `*` for any key or any entry.

    devices.*.connection.serial_port      every device's own adapter
    devices.fronius_rtu.connection        that device's whole connection
"""
from __future__ import annotations

import hashlib
import io
import zipfile
from typing import Any, Iterator, List, Tuple

# what differs between two machines of a pair, unless the user says otherwise
DEFAULT_KEEP_LOCAL = (
    'ui.host', 'ui.port', 'ui.tls', 'ui.trusted_proxies', 'ui.canonical_url',
    'modbus.serial_port',
    'devices.*.connection.serial_port', 'devices.*.sources.*.serial_port',
    'endpoints.*.connection.serial_port', 'endpoints.*.sources.*.serial_port',
    'endpoints.*.groups.*.connection.serial_port', 'endpoints.*.groups.*.sources.*.serial_port',
)

Step = Any            # a dict key, or ('id', <id>) for a list entry


def _walk(node: Any, segs: List[str], path: Tuple[Step, ...]) -> Iterator[Tuple[Tuple[Step, ...], Any]]:
    if not segs:
        yield path, node
        return
    seg, rest = segs[0], segs[1:]
    if isinstance(node, dict):
        for k in (list(node) if seg == '*' else [seg] if seg in node else []):
            yield from _walk(node[k], rest, path + (k,))
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, dict) and 'id' in item and (seg == '*' or str(item['id']) == seg):
                yield from _walk(item, rest, path + (('id', str(item['id'])),))


def _set(tree: Any, path: Tuple[Step, ...], value: Any) -> bool:
    """Put value at path in tree, if everything above it exists there."""
    node = tree
    for i, step in enumerate(path):
        last = i == len(path) - 1
        if isinstance(step, tuple):
            if not isinstance(node, list):
                return False
            node = next((x for x in node if isinstance(x, dict) and str(x.get('id')) == step[1]), None)
            if node is None:
                return False
            continue
        if not isinstance(node, dict):
            return False
        if last:
            node[step] = value
            return True
        if step not in node:
            return False
        node = node[step]
    return False


def check_paths(patterns: List[str]) -> List[str]:
    errors = []
    for p in patterns:
        if not isinstance(p, str) or not p.strip() or any(not s for s in p.strip().split('.')) \
                or len(p) > 200:
            errors.append(f"not a path: {p!r} (keys with dots, an entry by its id, * for any)")
    return errors


def keep_local(theirs: dict, mine: dict, patterns: List[str]) -> Tuple[dict, List[str]]:
    """The partner's configuration with this machine's values at `patterns`
    put back. Also: which concrete paths were kept."""
    kept = []
    for pattern in patterns:
        for path, value in list(_walk(mine, pattern.strip().split('.'), ())):
            if _set(theirs, path, value):
                kept.append('.'.join(s if isinstance(s, str) else s[1] for s in path))
    return theirs, kept


def bundle_hash(data: bytes) -> str:
    """What the configuration is, not when it was zipped: the files' names
    and contents, the manifest left out."""
    h = hashlib.sha256()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in sorted(n for n in z.namelist() if n != 'manifest.json'):
            h.update(name.encode() + b'\0' + z.read(name) + b'\0')
    return h.hexdigest()


def replace_config_yaml(data: bytes, text: str) -> bytes:
    """The bundle, with config.yaml replaced."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            dst.writestr(name, text if name == 'config.yaml' else src.read(name))
    return out.getvalue()


# one OPC UA server on two machines, behind a load balancer: the clients trust
# one certificate, so the standby takes its partner's with the configuration
OPCUA_IDENTITY = ('opcua/server_cert.der', 'opcua/server_key.pem')


def with_opcua_identity(data: bytes, cfg_dir) -> bytes:
    """The bundle, plus the OPC UA certificate and key when there are any."""
    from pathlib import Path
    files = [(n, Path(cfg_dir) / n) for n in OPCUA_IDENTITY]
    if not all(p.is_file() for _, p in files):
        return data
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
        for name, p in files:
            dst.writestr(name, p.read_bytes())
    return out.getvalue()


def write_opcua_identity(zf: zipfile.ZipFile, cfg_dir) -> bool:
    """The partner's OPC UA certificate and key, written here (the key 0600)."""
    import os
    from pathlib import Path
    if not all(n in zf.namelist() for n in OPCUA_IDENTITY):
        return False
    for name in OPCUA_IDENTITY:
        path = Path(cfg_dir) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + '.tmp')
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(zf.read(name))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    return True
