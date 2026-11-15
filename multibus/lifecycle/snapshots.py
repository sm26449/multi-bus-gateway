# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
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
"""Config snapshots, rollback, and the last-known-good boot seatbelt.

A snapshot is a VERBATIM ZIP of the config bundle (config.yaml with secrets —
same trust domain as the live file on the same disk, per-device register
files, calculated templates, user device templates, virtual meters), taken
automatically after every successful config mutation. Rollback restores one
through the same validated writer the backup import uses.

LKG ("last known good") is a separate snapshot marked only after the app has
BOOTED AND STAYED HEALTHY — at startup, a config.yaml that no longer parses
is restored from it automatically, so a bad edit can't brick an unattended
box.
"""
from __future__ import annotations

import copy
import io
import json
import logging
import os
import threading
import time
import zipfile

import yaml
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

BUNDLE_EXTRAS = ("calculated_templates.json",
                 "rules.yaml", "sequences.yaml", "passkeys.json",
                 "mqtt_discovery.json",
                 "attributes.yaml")               # beside the classic backup set
_INDEX = "index.json"
_LKG_ZIP = "lkg.zip"
_LKG_META = "lkg.json"


def _is_redaction_of(live_url, incoming_url) -> bool:
    """True when the imported URL is exactly the live URL's redacted form
    (userinfo dropped / secret query values → ``***``) — i.e. a sanitized
    export masked a credential that a merge-import must not clobber.

    Deliberately an EXACT match: if the live credential was rotated after the
    backup was taken, the backup's redacted form no longer matches and the
    import installs the ``***`` URL — treated as a deliberately-changed URL.
    Matching any ***-bearing URL instead would guess wrong in murkier ways
    (e.g. a genuinely retargeted endpoint whose query also got masked)."""
    if not live_url or not incoming_url or live_url == incoming_url:
        return False
    try:
        from multibus.core.redact import redact_url
        return redact_url(str(live_url)) == str(incoming_url)
    except Exception:  # noqa: BLE001
        return False


# the field names a sanitized export drops wherever they sit under devices[]
# or endpoints[] (the export side in api.py uses the same tuple)
SECRET_FIELDS = ("password", "passwd", "token", "secret", "api_key", "apikey",
                 "client_secret", "headers")


def _reinject_secret_tree(incoming, live) -> None:
    """Refill, in place, what a sanitized export stripped from ``incoming``:
    every secret-named field that is absent or empty but present in the
    matching ``live`` node, and every URL that is exactly the live URL's
    redacted form. Walks dicts by key and lists of ``{id: …}`` items by id, so
    a device's sources and an installation's groups and sources are covered
    as well as the device itself (3.83.0)."""
    if isinstance(incoming, dict) and isinstance(live, dict):
        for k in SECRET_FIELDS:
            if not incoming.get(k) and live.get(k):
                incoming[k] = live[k]
        if _is_redaction_of(live.get("url"), incoming.get("url")):
            incoming["url"] = live["url"]
        for k, v in incoming.items():
            if k in SECRET_FIELDS:
                continue
            lv = live.get(k)
            if isinstance(v, (dict, list)) and isinstance(lv, (dict, list)):
                _reinject_secret_tree(v, lv)
    elif isinstance(incoming, list) and isinstance(live, list):
        live_by_id = {d.get("id"): d for d in live
                      if isinstance(d, dict) and d.get("id") is not None}
        for item in incoming:
            if isinstance(item, dict) and item.get("id") in live_by_id:
                _reinject_secret_tree(item, live_by_id[item.get("id")])


def _merge_devices(live_list, incoming_list):
    """Merge the devices[] list per id: the incoming list is authoritative for
    the device SET, but each device's stripped secret fields (connection /
    http / mqtt_in / rest_push / sources[] passwords, headers, redacted URLs)
    are refilled from the matching live device when the incoming value is
    absent, empty, or the live value's redacted form."""
    out = [dev for dev in incoming_list]
    _reinject_secret_tree(out, live_list)
    return out


def _merge_endpoints(live_list, incoming_list):
    """Same contract as _merge_devices for endpoints[]: the incoming set wins,
    stripped source credentials come back from the live installation."""
    out = [ep for ep in incoming_list]
    _reinject_secret_tree(out, live_list)
    return out


def _reinject_stripped_secrets(merged, live):
    """Restore secrets a sanitized export blanked at the top level
    (alerts.webhook_url, rest_push.url, influxdb.url — device secrets are
    handled per-id in _merge_devices)."""
    m_al, l_al = merged.get("alerts"), live.get("alerts")
    if isinstance(m_al, dict) and isinstance(l_al, dict):
        m_url, l_url = m_al.get("webhook_url", ""), l_al.get("webhook_url", "")
        # the export strips the query/userinfo; if the live URL is the same
        # endpoint with more (a token), the imported bare form loses it — keep
        # the live one so the webhook still authenticates.
        if l_url and m_url and l_url.startswith(m_url) and l_url != m_url:
            m_al["webhook_url"] = l_url
    # rest_push.url / influxdb.url are exported through redact_url — keep the
    # live URL when the import carries exactly its redacted form
    for sect in ("rest_push", "influxdb"):
        m_s, l_s = merged.get(sect), live.get(sect)
        if isinstance(m_s, dict) and isinstance(l_s, dict):
            if _is_redaction_of(l_s.get("url"), m_s.get("url")):
                m_s["url"] = l_s["url"]


def validate_bundle(zf: zipfile.ZipFile, *, allow_nonlan: bool = False,
                    live: Optional[Dict] = None, namespace: str = "refuse") -> List[str]:
    """The checks the live routes make, applied to a backup or snapshot BEFORE
    any of it is written (F-18, 3.83.0): an allowlist or trusted-proxy entry
    that does not parse, a webhook that is not http(s), a write flag that is
    not a boolean, an HTTP source outside the LAN, a rule that would not
    validate. Returns the error list (empty = fine).

    ``live``: the running config.yaml when the bundle is MERGED into it (an
    import): a top-level list the bundle does not carry stays the live one, so
    the id namespace is judged on what the merge will hold (R2D-NAMESPACE-1).
    ``namespace``: ``refuse`` (an import), ``warn`` (the restore of the gateway's own
    snapshot — a collision it was taken with is logged, never a block on the way back) or
    ``off`` (the caller judges the result of its own merge)."""
    import ipaddress
    from urllib.parse import urlsplit
    errors: List[str] = []
    names = zf.namelist()
    if "config.yaml" in names:
        try:
            data = yaml.safe_load(zf.read("config.yaml")) or {}
        except Exception as e:  # noqa: BLE001
            return [f"config.yaml: not valid YAML ({e})"]
        if not isinstance(data, dict):
            return ["config.yaml: not a mapping"]
        # every Virtual BMS instance passes the same schema the UI routes enforce — an
        # import must not bring in what a PATCH refuses (R2D-VBMS-2)
        vb = data.get("virtual_bms")
        if vb is not None:
            if not isinstance(vb, list):
                errors.append("virtual_bms: must be a list of instances")
            else:
                from multibus.outputs.vbms_registry import structural_errors
                seen_ids = set()
                for n, inst in enumerate(vb):
                    label = (inst.get("id") if isinstance(inst, dict) else None) or f"#{n}"
                    errors += [f"virtual_bms {label}: {e}" for e in structural_errors(inst)]
                    if isinstance(inst, dict) and isinstance(inst.get("id"), str):
                        if inst["id"] in seen_ids:
                            errors.append(f"virtual_bms {label}: the id is used twice")
                        seen_ids.add(inst["id"])
        # one id, one MQTT identity: no Virtual BMS under a device, component or installation
        # unit id — judged on what the config will hold after the write (R2D-NAMESPACE-1)
        from multibus.outputs.vbms_registry import namespace_errors
        _keys = ("devices", "endpoints", "virtual_bms")
        _eff = {k: data[k] if k in data else (live or {}).get(k) for k in _keys}
        _ns = namespace_errors(_eff) if namespace != "off" else []
        if namespace == "warn":
            for _e in _ns:
                logger.warning("restore: %s — kept as the snapshot holds it", _e)
        else:
            errors += _ns
        sec = data.get("security") or {}
        if not isinstance(sec, dict):
            errors.append("security: must be a mapping")
            sec = {}
        for k in ("allow_writes", "allow_nonlan_http_devices", "primary_write_locked"):
            if k in sec and not isinstance(sec[k], bool):
                errors.append(f"security.{k}: must be true or false")
        for key in ("allowlist", "metrics_allowlist"):
            al = sec.get(key)
            if al is None:
                continue
            if not isinstance(al, list):
                errors.append(f"security.{key}: must be a list of CIDRs")
                continue
            for entry in al:
                try:
                    ipaddress.ip_network(str(entry).strip(), strict=False)
                except ValueError:
                    errors.append(f"security.{key}: {entry!r} is not an address or CIDR")
        ui = data.get("ui") or {}
        tp = ui.get("trusted_proxies") if isinstance(ui, dict) else None
        if tp is not None:
            if not isinstance(tp, list):
                errors.append("ui.trusted_proxies: must be a list")
            else:
                for entry in tp:
                    try:
                        ipaddress.ip_network(str(entry).strip(), strict=False)
                    except ValueError:
                        errors.append(f"ui.trusted_proxies: {entry!r} is not an address or CIDR")
        cu = ui.get("canonical_url") if isinstance(ui, dict) else None
        if cu and not str(cu).startswith(("http://", "https://")):
            errors.append("ui.canonical_url: must start with http:// or https://")
        al_ = data.get("alerts") or {}
        wh = al_.get("webhook_url") if isinstance(al_, dict) else None
        if wh and not str(wh).startswith(("http://", "https://")):
            errors.append("alerts.webhook_url: must start with http:// or https://")
        nonlan = bool(sec.get("allow_nonlan_http_devices", allow_nonlan))
        if not nonlan:
            try:
                from multibus.commissioning.discovery import lan_host_error
            except Exception:  # noqa: BLE001
                lan_host_error = None

            def _urls(node, where):
                if isinstance(node, dict):
                    for k, v in node.items():
                        if k == "url" and isinstance(v, str) and v.startswith(("http://", "https://")):
                            host = urlsplit(v).hostname or ""
                            err = lan_host_error(host) if lan_host_error else None
                            if err:
                                errors.append(f"{where}.url: {v!r} — {err} (set security.allow_nonlan_http_devices to allow it)")
                        else:
                            _urls(v, f"{where}.{k}")
                elif isinstance(node, list):
                    for i, v in enumerate(node):
                        _urls(v, f"{where}[{i}]")
            _urls(data.get("devices"), "devices")
            _urls(data.get("endpoints"), "endpoints")
            if isinstance(data.get("rest_push"), dict):
                _urls(data.get("rest_push"), "rest_push")
    if "rules.yaml" in names:
        try:
            from multibus.control.rules import validate_rule_def
            rdoc = yaml.safe_load(zf.read("rules.yaml")) or {}
            rules = rdoc.get("rules") if isinstance(rdoc, dict) else rdoc
            for i, raw in enumerate(rules or []):
                for e in validate_rule_def(raw if isinstance(raw, dict) else {}):
                    errors.append(f"rules.yaml[{i}]: {e}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"rules.yaml: {e}")
    if "attributes.yaml" in names:
        errors.extend(validate_attributes_doc(zf.read("attributes.yaml")))
    if "sequences.yaml" in names:
        try:
            from multibus.control.sequences import validate_sequence
            sdoc = yaml.safe_load(zf.read("sequences.yaml")) or {}
            for i, raw in enumerate((sdoc.get("sequences") if isinstance(sdoc, dict) else None) or []):
                # its devices are this installation's question, asked when it
                # is saved or run; a restore checks only its shape
                for e in validate_sequence(raw, devices=lambda d: None):
                    if "no device '" not in e:
                        errors.append(f"sequences.yaml[{i}]: {e}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"sequences.yaml: {e}")
    return errors


def validate_attributes_doc(raw: bytes) -> List[str]:
    """attributes.yaml as a restore would install it: a mapping of known
    levels, each category checked against the catalog."""
    from multibus.catalog.attribute_catalog import validate as _validate
    try:
        doc = yaml.safe_load(raw) or {}
    except Exception as e:  # noqa: BLE001
        return [f"attributes.yaml: not valid YAML ({e})"]
    if not isinstance(doc, dict):
        return ["attributes.yaml: not a mapping"]
    errors: List[str] = []

    def _cats(level, node, where):
        if not isinstance(node, dict):
            errors.append(f"attributes.yaml {where}: a mapping of categories")
            return
        for c, v in node.items():
            _clean, errs, _w = _validate(str(c), v, level=level)
            errors.extend(f"attributes.yaml {where}: {e}" for e in errs)
    from multibus.catalog.attribute_catalog import PUBLISH_MODES
    for lv in ('settings', 'site', 'installation', 'device'):
        if lv in doc and doc[lv] is not None and not isinstance(doc[lv], dict):
            errors.append(f"attributes.yaml {lv}: a mapping")
    if errors:
        return errors
    mode = (doc.get('settings') or {}).get('location_publish')
    if mode is not None and mode not in PUBLISH_MODES:
        errors.append(f"attributes.yaml settings.location_publish: one of {', '.join(PUBLISH_MODES)}")
    serials = (doc.get('settings') or {}).get('publish_serials')
    if serials is not None and not isinstance(serials, bool):
        errors.append("attributes.yaml settings.publish_serials: true or false")
    _cats('site', doc.get('site') or {}, 'site')
    for lv in ('installation', 'device'):
        held = doc.get(lv) or {}
        if not isinstance(held, dict):
            errors.append(f"attributes.yaml {lv}: a mapping of ids")
            continue
        for tid, node in held.items():
            _cats(lv, node or {}, f"{lv} {tid}")
    return errors


def merge_attributes_doc(live_raw: Optional[bytes], incoming_raw: bytes) -> bytes:
    """A merge import of attributes.yaml, the way config.yaml merges devices:
    what the backup holds replaces the same category of the same id; what
    only the live file holds stays. A location a sanitised export left out
    keeps the live one (coordinates are identity)."""
    from multibus.catalog.attributes import normalize_doc
    raw_in = yaml.safe_load(incoming_raw) or {}
    incoming = normalize_doc(raw_in)
    merged = normalize_doc((yaml.safe_load(live_raw) or {}) if live_raw else {})
    if isinstance(raw_in, dict) and isinstance(raw_in.get('settings'), dict):
        merged['settings'].update(incoming['settings'])
    merged['site'].update(incoming['site'])
    for lv in ('installation', 'device'):
        for tid, cats in incoming[lv].items():
            merged[lv].setdefault(tid, {}).update(cats)
    return yaml.safe_dump(merged, default_flow_style=False, allow_unicode=True,
                          sort_keys=False).encode('utf-8')


def strip_attribute_locations(raw: bytes) -> bytes:
    """attributes.yaml for a sanitised export: no coordinates. Raises
    ValueError on a file that does not parse (the caller leaves it out)."""
    try:
        doc = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"attributes.yaml does not parse: {e}")
    if isinstance(doc, dict):
        (doc.get('site') or {}).pop('location', None)
        for node in (doc.get('installation') or {}).values():
            if isinstance(node, dict):
                node.pop('location', None)
    return yaml.safe_dump(doc, default_flow_style=False, allow_unicode=True,
                          sort_keys=False).encode('utf-8')


def write_bundle_files(zf: zipfile.ZipFile, *, cfg_dir: Path, user_tpl_dir: Path,
                       registers_path_for, replace_config: bool) -> Dict:
    """Write a bundle's files into the config dir (shared by backup import and
    snapshot restore). Paths are mapped through ``registers_path_for`` so the
    PRIMARY device's registers land in its real legacy-root file, not a dead
    copy under devices/. ``replace_config`` False deep-merges config.yaml over
    the live one (sanitized backups must not wipe secrets); True writes it
    verbatim (full-fidelity snapshots ARE the desired state)."""
    import yaml as _yaml

    names = zf.namelist()
    for n in names:
        if n.startswith("/") or ".." in Path(n).parts:
            raise ValueError(f"unsafe path in archive: {n}")

    # Two-phase, all-or-nothing (REFAC-TXN-1): phase 1 writes every file to a
    # ``.tmp`` sibling and records the live file's prior bytes; phase 2 (_commit)
    # swaps them all in. If any swap fails, every swap already done is rolled back
    # to its prior content (or removed if the file was new), so a mid-apply failure
    # never leaves a half-written bundle on disk.
    _staged = []   # (tmp: Path, final: Path, prior: bytes | None)

    def _atomic(path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        binary = isinstance(data, (bytes, bytearray))
        tmp = path.with_suffix(path.suffix + ".tmp")
        # 0600: restored files land in the private config dir and some
        # (config.yaml, passkeys.json) carry secrets — a restore must not
        # downgrade them to world-readable.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb" if binary else "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _staged.append((tmp, path, path.read_bytes() if path.exists() else None))

    def _commit() -> None:
        done = []   # (final, prior) swapped so far — for rollback
        try:
            for tmp, final, prior in _staged:
                os.replace(tmp, final)
                done.append((final, prior))
        except BaseException:
            for final, prior in reversed(done):          # undo the swaps that landed
                try:
                    if prior is None:
                        final.unlink(missing_ok=True)
                    else:
                        final.write_bytes(prior)
                except OSError:
                    pass
            for tmp, _f, _p in _staged:                  # drop any leftover .tmp
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    summary = {"config": False, "device_registers": 0, "templates": 0,
               "virtual_meters": False, "extras": 0}
    cfg_path = cfg_dir / "config.yaml"
    for n in names:
        if n == "config.yaml":
            incoming = _yaml.safe_load(zf.read(n)) or {}
            if replace_config:
                merged = incoming
            else:
                live = (_yaml.safe_load(cfg_path.read_text()) or {}) if cfg_path.exists() else {}
                # `_deep_merge` mutates `base` (=`live`) in place and returns it, so
                # after the merge `merged is live` and the live top-level URLs have
                # already been overwritten by the redacted incoming ones. Keep a
                # pre-merge copy for `_reinject_stripped_secrets` below, or it would
                # compare the masked url to itself and never restore it (REFAC-URL-1).
                live_before = copy.deepcopy(live)

                def _deep_merge(base, over):
                    for k, v in over.items():
                        if k == "devices" and isinstance(v, list):
                            base[k] = _merge_devices(base.get(k) or [], v)
                        elif k == "endpoints" and isinstance(v, list):
                            base[k] = _merge_endpoints(base.get(k) or [], v)
                        elif isinstance(v, dict) and isinstance(base.get(k), dict):
                            _deep_merge(base[k], v)
                        else:
                            base[k] = v
                    return base
                merged = _deep_merge(live, incoming)
                # a sanitized export strips per-device connection.password /
                # headers and the alerts.webhook_url token — re-inject the live
                # values so a merge-import keeps them, as the docstring promises
                # (from the PRE-merge copy — see live_before above)
                _reinject_stripped_secrets(merged, live_before)
            _atomic(cfg_path, _yaml.dump(merged, default_flow_style=False,
                                         allow_unicode=True, sort_keys=False))
            summary["config"] = True
        elif n.startswith("devices/") and n.endswith("selected_registers.json"):
            dev_id = Path(n).parts[1]
            _atomic(registers_path_for(dev_id), zf.read(n))
            summary["device_registers"] += 1
        elif n.startswith("devices/") and n.endswith("/device.json"):
            # deleted-device tombstone — restore beside the config tree so a
            # later "restore device" still finds its kept settings
            dev_id = Path(n).parts[1]
            _atomic(cfg_dir / "devices" / dev_id / "device.json", zf.read(n))
            summary.setdefault("tombstones", 0)
            summary["tombstones"] += 1
        elif n.startswith("device_templates/") and not n.endswith("/"):
            _atomic(user_tpl_dir / Path(n).name, zf.read(n))
            summary["templates"] += 1
        elif n == "virtual_meters.yaml":
            _atomic(cfg_dir / "virtual_meters.yaml", zf.read(n))
            summary["virtual_meters"] = True
        elif n.startswith("templates/") and n.endswith((".yaml", ".yml")):
            (cfg_dir / "templates").mkdir(parents=True, exist_ok=True)
            _atomic(cfg_dir / "templates" / Path(n).name, zf.read(n))
            summary["extras"] += 1
        elif n in BUNDLE_EXTRAS:
            # passkeys.json IS an auth-identity store: importing it installs
            # whatever WebAuthn credentials the bundle carries. Only honor it on
            # a FULL replace (replace_config=True — an operator restoring a
            # trusted own-system snapshot). A merge-import (replace_config=False,
            # e.g. the sanitized /api/config/import path) must NOT silently swap
            # the auth registry from a possibly-foreign backup.
            if n == "passkeys.json" and not replace_config:
                summary.setdefault("skipped", []).append("passkeys.json (merge-import)")
                continue
            if n == "attributes.yaml":
                # under the store's own lock: never interleaved with a change
                from multibus.core.config_store import lock_for
                live_p = cfg_dir / n
                with lock_for(live_p):
                    data = zf.read(n) if replace_config else merge_attributes_doc(
                        live_p.read_bytes() if live_p.exists() else None, zf.read(n))
                    _atomic(live_p, data)
                summary["extras"] += 1
                continue
            _atomic(cfg_dir / n, zf.read(n))
            summary["extras"] += 1
    _commit()   # swap every staged file in, all-or-nothing (REFAC-TXN-1)
    return summary



def _write_bytes_0600(path: Path, data: bytes) -> None:
    """Atomically write a snapshot ZIP at 0600 — it embeds config.yaml with
    secrets (0600 elsewhere), so it must not sit world-readable in
    config/snapshots/."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class BundleInvalid(ValueError):
    """A bundle failed validation — carries the per-error list the routes map to
    a 422 ``{"errors": [...]}`` body. Raised before any file is written."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class BundleAppliedReloadFailed(Exception):
    """The bundle's files were WRITTEN to disk but the reload of the new config
    failed (e.g. a restored register file has a bad value). This is NOT "nothing
    happened": the config is half-applied, so the routes map it to a **500** (not
    the 422 a validation failure gets) and name the pre-op snapshot to roll back
    to. Fixes the §6 L1 drift where a reload failure after write returned 422
    (FINDINGS REFAC-S6-1). Not a ``ValueError`` on purpose, so the routes' `except
    ValueError → 422` does not swallow it."""

    def __init__(self, pre_label: str, cause: BaseException):
        self.pre_label = pre_label
        self.cause = cause
        super().__init__(f"bundle written but reload failed ({type(cause).__name__}: {cause}); "
                         f"the pre-op snapshot '{pre_label}' holds the previous state")


class PreopSnapshotFailed(Exception):
    """The pre-op (undo) snapshot could not be taken, so the apply was ABORTED
    before any file was written — the config is unchanged (C3, fail-closed). The
    routes map it to a 500 telling the operator to fix the snapshot store and
    retry; a half-applied bundle with no way back is never acceptable. Not a
    ``ValueError`` so the routes' `except ValueError → 422` does not swallow it."""

    def __init__(self, pre_label: str, cause: BaseException):
        self.pre_label = pre_label
        self.cause = cause
        super().__init__(f"could not take the pre-op snapshot '{pre_label}' "
                         f"({type(cause).__name__}: {cause}); nothing was applied")


def _attr(o, k, default=None):
    """Read a key from a dataclass-ish object OR a plain dict (bundle configs are
    parsed to either depending on the call site)."""
    return o.get(k, default) if isinstance(o, dict) else getattr(o, k, default)


def write_lock_disclosure(devices, rules=None, *, is_restore):
    """F20 (ADR-0041 L2): what a bundle would UNLOCK/ARM — so a restore/import
    never silently re-enables hardware writes. Counts, over the bundle's own
    (pre-force-lock) state:

    - ``devices`` / ``components`` / ``units``: write paths with ``write_locked``
      false — top-level devices, their components (``component_of``), and endpoint/
      installation units (``endpoint_id``). Virtual meters are read-only, not here.
    - ``bindings``: enabled command bindings on those UNLOCKED write paths (a binding
      on a locked device cannot fire, so counting it would over-warn).
    - ``rules``: armed+enabled rules — **restore only** (``rules.yaml`` is not in the
      export/import bundle, so an import can never re-arm; R is then 0).

    ``nothing`` is true when every count is zero — a reassuring "nothing is unlocked".
    Pure: takes already-parsed devices (DeviceConfig or dicts) and rule objects."""
    dev = comp = unit = bindings = 0
    for d in devices or []:
        if bool(_attr(d, 'write_locked', False)):
            continue
        if _attr(d, 'component_of', ''):
            comp += 1
        elif _attr(d, 'endpoint_id', ''):
            unit += 1
        else:
            dev += 1
        for c in (_attr(d, 'commands', []) or []):
            if _attr(c, 'enabled', False):
                bindings += 1
    armed = 0
    if is_restore:
        for r in (rules or []):
            if _attr(r, 'mode', '') == 'armed' and _attr(r, 'enabled', True):
                armed += 1
    total = dev + comp + unit + bindings + armed
    return {'devices': dev, 'components': comp, 'units': unit,
            'bindings': bindings, 'rules': armed, 'total': total, 'nothing': total == 0}


def bundle_write_disclosure(bundle_bytes: bytes, *, is_restore: bool):
    """F20 (ADR-0041 L2): the write-lock disclosure for a bundle WITHOUT applying it
    — for the dry-run and the confirm dialog. Loads a throwaway Config from the
    bundle's files (so endpoint units and components are expanded exactly as a real
    load would) and, for a restore, reads the armed rules from the bundle's
    ``rules.yaml`` (an import bundle carries none → R is 0). Returns the same shape as
    ``write_lock_disclosure``. The caller handles a malformed bundle (it would also
    fail validation on apply)."""
    import tempfile
    from multibus.core.config import Config
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        with zipfile.ZipFile(io.BytesIO(bundle_bytes)) as z:
            for n in z.namelist():
                parts = Path(n).parts
                if n.startswith('/') or '..' in parts:
                    continue   # never write outside the temp dir
                z.extract(n, tdp)
        cfgp = tdp / 'config.yaml'
        if not cfgp.exists():
            return write_lock_disclosure([], None, is_restore=is_restore)
        cfg = Config(str(cfgp))
        rules = None
        if is_restore:
            rp = tdp / 'rules.yaml'
            if rp.exists():
                try:
                    rules = (yaml.safe_load(rp.read_text()) or {}).get('rules') or []
                except Exception:  # noqa: BLE001
                    rules = []
        return write_lock_disclosure(cfg.devices, rules, is_restore=is_restore)


def apply_bundle(zf: zipfile.ZipFile, *, cfg_dir: Path, user_tpl_dir: Path,
                 registers_path_for, replace_config: bool, allow_nonlan: bool,
                 snapshot_store, pre_label: str, user: str, reload_from_disk,
                 apply: bool, restore_identity: bool, logger=None,
                 lock_writes: bool = True, lock_writes_fn=None):
    """The one bundle-apply path backup import and snapshot restore share
    (ADR-0041 §6): validate → snapshot the pre-op state → write the files back →
    reload. ``replace_config`` is the import(merge, False) / restore(verbatim,
    True) switch. Returns ``(summary, identity_rotated)``. Raises
    ``BundleInvalid`` (→422) before any write; ``write_bundle_files`` may raise a
    plain ``ValueError`` (unsafe path) which the route also maps to 422.

    F20 safe-by-default (ADR-0041 L2): when ``lock_writes`` (default true) and a
    ``lock_writes_fn`` is given (the api layer's "this gateway can lock" flag), a
    ``.lock-writes-pending`` marker is written next to config.yaml **before the bundle
    is written**. ``Config.load()`` applies the lock to its staging copy and to the file
    before it publishes anything, then removes the marker (R2D-F20-1). So the bundle's
    unlocked state is never the live config — not between the load and a later hook, not
    when the lock step fails (the reload is refused, the running config stays), and not
    after a crash (the marker is still there at the next boot). ``lock_writes=False`` is
    the operator's explicit choice and clears a marker left by an earlier attempt.

    The HTTP-only parts (the ZIP size/bomb input guards, the authz gates and the
    identity-rotation cookie reissue) stay in the route layer (ADR-0038)."""
    live = None
    if not replace_config:                       # an import merges into the running file
        try:
            cfg_file = Path(cfg_dir) / "config.yaml"
            live = (yaml.safe_load(cfg_file.read_text()) or {}) if cfg_file.exists() else None
        except Exception:  # noqa: BLE001 — an unreadable live file: judge the bundle alone
            live = None
        live = live if isinstance(live, dict) else None
    # a verbatim restore is the gateway's own snapshot: what it held is not a reason to refuse
    # the way back (review L3); an import of a bundle stays strict
    errs = validate_bundle(zf, allow_nonlan=allow_nonlan, live=live,
                           namespace="warn" if replace_config else "refuse")
    if errs:
        raise BundleInvalid(errs)
    # the pre-op (undo) snapshot is taken BEFORE any write. If it cannot be taken
    # we FAIL CLOSED (C3): nothing is written, so the config is unchanged and the
    # operator is told to fix the snapshot store — a half-applied bundle with no
    # way back is never acceptable.
    try:
        snapshot_store.create(pre_label, user=user)
    except Exception as e:  # noqa: BLE001
        if logger is not None:
            logger.exception("%s snapshot failed — aborting the apply (nothing written)", pre_label)
        raise PreopSnapshotFailed(pre_label, e) from e
    # write_bundle_files checks every path BEFORE writing, so an unsafe-path
    # ValueError here means nothing was written → the route's 422 is correct.
    marker = Path(cfg_dir) / ".lock-writes-pending"
    had_marker = marker.exists()          # left by an earlier attempt whose bundle is on disk
    if lock_writes and lock_writes_fn is not None:
        marker.write_text(f"{pre_label}\n", encoding="utf-8")     # BEFORE the bundle lands
    else:
        marker.unlink(missing_ok=True)
    try:
        summary = write_bundle_files(zf, cfg_dir=cfg_dir, user_tpl_dir=user_tpl_dir,
                                     registers_path_for=registers_path_for,
                                     replace_config=replace_config)
    except ValueError:
        # nothing of THIS bundle was written, so its own marker goes — but one an earlier
        # attempt left stays: that attempt's unlocked bundle is still the file on disk
        if not had_marker:
            marker.unlink(missing_ok=True)
        raise
    # past this point the files ARE on disk; a reload failure is a half-applied
    # config, not a rejection — surface it as a 500 that names the pre-op snapshot
    # (H1 / REFAC-S6-1), never as the 422 a validation failure gets.
    # F20: the reload's Config.load() finds the marker and locks BEFORE it publishes.
    try:
        identity_rotated = reload_from_disk(apply, restore_identity=restore_identity)
    except Exception as e:  # noqa: BLE001
        raise BundleAppliedReloadFailed(pre_label, e) from e
    return summary, identity_rotated


class SnapshotStore:
    """Bounded, indexed snapshot directory under <config>/snapshots/."""

    def __init__(self, cfg_dir: Path, user_tpl_dir: Path, *,
                 device_ids=None, registers_path_for=None, keep: int = 50):
        self.cfg_dir = Path(cfg_dir)
        self.user_tpl_dir = Path(user_tpl_dir)
        self.dir = self.cfg_dir / "snapshots"
        self.keep = keep
        # late-bound callables so the store never holds a stale device list
        self._device_ids = device_ids or (lambda: [])
        self._registers_path_for = registers_path_for or (
            lambda dev_id: self.cfg_dir / "devices" / dev_id / "selected_registers.json")
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._pending: Optional[Dict] = None
        self._seq = 0        # uniquifies ids created within the same second

    # ── bundle ───────────────────────────────────────────────────────────────

    def build_bundle_bytes(self) -> bytes:
        """Verbatim ZIP of the live config bundle (secrets INCLUDED — this is a
        local restore point, not an export for sharing)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            cfg = self.cfg_dir / "config.yaml"
            if cfg.exists():
                z.writestr("config.yaml", cfg.read_text())
            for dev_id in self._device_ids():
                p = self._registers_path_for(dev_id)
                if p.exists():
                    z.writestr(f"devices/{dev_id}/selected_registers.json", p.read_text())
            # tombstones: a DELETED device's kept settings live at
            # devices/<id>/device.json (no longer in the active _device_ids
            # list). Without these the restore loses delete→restore recovery.
            _devs_dir = self.cfg_dir / "devices"
            if _devs_dir.is_dir():
                for _tomb in sorted(_devs_dir.glob("*/device.json")):
                    z.writestr(f"devices/{_tomb.parent.name}/device.json",
                               _tomb.read_text())
            if self.user_tpl_dir.is_dir():
                for f in sorted(self.user_tpl_dir.iterdir()):
                    if f.suffix.lower() in (".json", ".yaml", ".yml"):
                        z.writestr(f"device_templates/{f.name}", f.read_text())
            for name in ("virtual_meters.yaml", *BUNDLE_EXTRAS):
                p = self.cfg_dir / name
                if p.exists():
                    z.writestr(name, p.read_text())
            tpl_dir = self.cfg_dir / "templates"   # virtual-meter templates
            if tpl_dir.is_dir():
                for f in sorted(tpl_dir.iterdir()):
                    if f.suffix.lower() in (".yaml", ".yml"):
                        z.writestr(f"templates/{f.name}", f.read_text())
            z.writestr("manifest.json", json.dumps(
                {"backup_version": 1, "snapshot": True,
                 "include_secrets": True, "include_identity": True,
                 "devices": list(self._device_ids())}, indent=1))
        return buf.getvalue()

    # ── index ────────────────────────────────────────────────────────────────

    def _read_index(self) -> List[Dict]:
        try:
            return json.loads((self.dir / _INDEX).read_text())
        except Exception:  # noqa: BLE001 — missing/corrupt index rebuilds empty
            return []

    def _write_index(self, entries: List[Dict]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / (_INDEX + ".tmp")
        with open(tmp, "w") as f:
            f.write(json.dumps(entries, indent=1, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())               # durable before the rename
        os.replace(tmp, self.dir / _INDEX)

    def list(self) -> List[Dict]:
        """Snapshots, newest first, plus the LKG entry when one exists."""
        # (ts, id) — ids carry a per-boot sequence, so same-instant snapshots
        # (a burst in tests, a fast operator) still order deterministically
        out = sorted(self._read_index(), key=lambda e: (e["ts"], e["id"]), reverse=True)
        lkg = self.lkg_meta()
        if lkg:
            out.insert(0, {**lkg, "lkg": True})
        return out

    def get_path(self, sid: str) -> Optional[Path]:
        if sid == "lkg":
            p = self.dir / _LKG_ZIP
            return p if p.exists() else None
        if not sid.replace("-", "").replace("_", "").isalnum():
            return None
        p = self.dir / f"{sid}.zip"
        return p if p.exists() and any(e["id"] == sid for e in self._read_index()) else None

    # ── create / prune / delete ──────────────────────────────────────────────

    def create(self, trigger: str, *, user: str = "", note: str = "") -> Dict:
        with self._lock:
            data = self.build_bundle_bytes()
            ts = time.time()
            self._seq += 1
            sid = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts)) + f"-{self._seq:04d}"
            self.dir.mkdir(parents=True, exist_ok=True)
            _write_bytes_0600(self.dir / f"{sid}.zip", data)
            meta = {"id": sid, "ts": round(ts, 3), "trigger": trigger[:120],
                    "user": user[:40], "note": note[:200], "size": len(data)}
            entries = [e for e in self._read_index() if e["id"] != sid]
            entries.append(meta)
            entries.sort(key=lambda e: (e["ts"], e["id"]))
            # prune beyond keep — oldest first, files then index
            while len(entries) > self.keep:
                old = entries.pop(0)
                try:
                    (self.dir / f"{old['id']}.zip").unlink(missing_ok=True)
                except OSError:
                    pass
            self._write_index(entries)
            logger.info(f"config snapshot {sid} ({trigger}, {len(data)} bytes)")
            return meta

    def delete(self, sid: str) -> bool:
        with self._lock:
            entries = self._read_index()
            keep = [e for e in entries if e["id"] != sid]
            if len(keep) == len(entries):
                return False
            try:
                (self.dir / f"{sid}.zip").unlink(missing_ok=True)
            except OSError:
                pass
            self._write_index(keep)
            return True

    # ── debounced auto-trigger ───────────────────────────────────────────────

    def schedule(self, trigger: str, user: str = "", delay_s: float = 2.0) -> None:
        """Coalesce a burst of config mutations into ONE snapshot capturing the
        state after the burst. Never raises (a snapshot failure must not break
        the request that triggered it)."""
        try:
            with self._lock:
                self._pending = {"trigger": trigger, "user": user}
                if self._timer is not None:
                    self._timer.cancel()
                self._timer = threading.Timer(delay_s, self._fire)
                self._timer.daemon = True
                self._timer.start()
        except Exception:  # noqa: BLE001
            logger.exception("snapshot scheduling failed")

    def _fire(self) -> None:
        try:
            with self._lock:
                pending, self._pending, self._timer = self._pending, None, None
            if pending:
                self.create(pending["trigger"], user=pending["user"])
        except Exception:  # noqa: BLE001
            logger.exception("auto-snapshot failed")

    # ── last known good ──────────────────────────────────────────────────────

    def mark_lkg(self) -> Dict:
        """Stamp the CURRENT bundle as last-known-good (call only after the app
        has proven healthy). Overwrites the previous LKG."""
        with self._lock:
            data = self.build_bundle_bytes()
            self.dir.mkdir(parents=True, exist_ok=True)
            _write_bytes_0600(self.dir / _LKG_ZIP, data)
            meta = {"id": "lkg", "ts": round(time.time(), 3), "size": len(data),
                    "trigger": "healthy-boot", "user": "", "note": "last known good"}
            # atomic + durable like the zip beside it — a torn meta sidecar
            # would make a good LKG zip un-restorable
            _write_bytes_0600(self.dir / _LKG_META,
                              json.dumps(meta, indent=1).encode("utf-8"))
            logger.info("last-known-good config snapshot updated")
            return meta

    def lkg_meta(self) -> Optional[Dict]:
        try:
            if (self.dir / _LKG_ZIP).exists():
                return json.loads((self.dir / _LKG_META).read_text())
        except Exception:  # noqa: BLE001
            pass
        return None


def boot_seatbelt(config_path: str, load_config) -> object:
    """Load the config; on a parse/validation failure restore the LKG bundle
    and retry ONCE. Called from main.py before anything else spins up, so an
    unattended box survives a bad manual edit or a torn write."""
    cfg_path = Path(config_path)
    try:
        return load_config()
    except Exception as boot_err:  # noqa: BLE001
        # Only a config that cannot be PARSED or VALIDATED is the LKG's
        # business. A bug, an import error, a read-only volume or a full
        # disk must not roll credentials, passkeys and months of settings
        # back to the snapshot (F-26, 3.83.0) — re-raise those.
        import yaml as _yaml
        _restorable = (_yaml.YAMLError, ValueError, TypeError, KeyError, AttributeError)
        if not isinstance(boot_err, _restorable) or isinstance(boot_err, (OSError, ImportError)):
            logger.critical("config failed to load with %s — NOT a broken file, the LKG "
                            "seatbelt does not apply: %s", type(boot_err).__name__, boot_err)
            raise
        lkg = cfg_path.parent / "snapshots" / _LKG_ZIP
        if not lkg.exists():
            raise
        logger.critical(f"config failed to load ({boot_err}) — restoring last known good")
        try:
            with zipfile.ZipFile(lkg) as zf:
                # devices/<id>/ paths inside the bundle mirror the on-disk layout
                # except the primary; without a parsed config we use the same rule
                # config.device_registers_path applies (primary = legacy root).
                from multibus.core.config import PRIMARY_DEVICE_ID
                write_bundle_files(
                    zf, cfg_dir=cfg_path.parent,
                    user_tpl_dir=cfg_path.parent / "device_templates",
                    registers_path_for=lambda dev_id: (
                        cfg_path.parent / "selected_registers.json"
                        if dev_id == PRIMARY_DEVICE_ID
                        else cfg_path.parent / "devices" / dev_id / "selected_registers.json"),
                    replace_config=True)
        except Exception:  # noqa: BLE001
            logger.exception("LKG restore failed")
            raise boot_err
        cfg = load_config()
        logger.critical("config restored from last known good — review recent changes")
        return cfg


# ── semantic diff between config bundles ────────────────────────────────────
# "What changed since this snapshot" — key-level YAML/JSON comparison (never
# line-based, so reordering is not noise), secrets masked at EMIT time (the
# comparison runs on raw values, so a rotated password still shows as changed
# — just not what it changed to).

_DIFF_LIMIT = 400            # per file — enough for honesty, bounded for sanity


def _mask(path: str, value):
    """Mask the value when its key smells secret; DEEP-redact dict/list values
    (an added subtree like a whole mqtt section may carry secrets inside)."""
    from multibus.core.audit import _is_url_key, redact_obj
    from multibus.core.redact import _is_secret_key, redact_url
    leaf = path.rsplit(".", 1)[-1].split("[")[0]
    if _is_secret_key(leaf):
        return "***"
    # coordinates are identity: the diff is viewer-readable
    from multibus.catalog.attribute_catalog import COORD_FIELDS, mask_coordinates
    if leaf in ('latitude_deg', 'longitude_deg') and isinstance(value, (int, float)):
        return round(float(value), 2)
    if leaf in COORD_FIELDS:
        return "***"
    if isinstance(value, dict):
        value = mask_coordinates(value)
    if isinstance(value, (dict, list)):
        return redact_obj(value)
    # a SCALAR at a url leaf can embed userinfo/query secrets — the diff is
    # viewer-readable, so mask it like redact_obj does for url-keyed strings
    if isinstance(value, str) and _is_url_key(leaf):
        return redact_url(value)
    return value


def _short(v):
    s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= 120 else s[:117] + "…"


def _diff_values(a, b, path: str, out: List[Dict]) -> None:
    if len(out) >= _DIFF_LIMIT:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(a.keys() | b.keys()):
            p = f"{path}.{k}" if path else str(k)
            if k not in b:
                out.append({"path": p, "kind": "removed", "old": _short(_mask(p, a[k]))})
            elif k not in a:
                out.append({"path": p, "kind": "added", "new": _short(_mask(p, b[k]))})
            else:
                _diff_values(a[k], b[k], p, out)
        return
    if isinstance(a, list) and isinstance(b, list):
        # lists of keyed dicts (registers by address, meters by id/name) diff
        # per element; anything else compares as a whole
        key = next((k for k in ("address", "id", "name", "template")
                    if a + b and all(isinstance(x, dict) and k in x for x in a + b)), None)
        if key:
            am = {str(x[key]): x for x in a}
            bm = {str(x[key]): x for x in b}
            for k in sorted(am.keys() | bm.keys()):
                p = f"{path}[{key}={k}]"
                if k not in bm:
                    out.append({"path": p, "kind": "removed", "old": _short(_mask(p, am[k]))})
                elif k not in am:
                    out.append({"path": p, "kind": "added", "new": _short(_mask(p, bm[k]))})
                else:
                    _diff_values(am[k], bm[k], p, out)
        elif a != b:
            out.append({"path": path, "kind": "changed",
                        "old": f"[{len(a)} items]", "new": f"[{len(b)} items]"})
        return
    if a != b:
        out.append({"path": path, "kind": "changed",
                    "old": _short(_mask(path, a)), "new": _short(_mask(path, b))})


def _parse_bundle(data: bytes) -> Dict[str, object]:
    """ZIP bundle → {filename: parsed YAML/JSON} (manifest excluded)."""
    import yaml as _yaml
    out: Dict[str, object] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for n in zf.namelist():
            if n == "manifest.json" or n.endswith("/"):
                continue
            raw = zf.read(n)
            try:
                if n.endswith((".yaml", ".yml")):
                    out[n] = _yaml.safe_load(raw) or {}
                elif n.endswith(".json"):
                    out[n] = json.loads(raw or b"{}")
                else:
                    out[n] = raw.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 — an unparsable member still diffs as text
                out[n] = raw.decode("utf-8", "replace")
    return out


def diff_bundles(old_zip: bytes, new_zip: bytes) -> Dict:
    """Per-file semantic diff, old → new. For 'snapshot vs live', old is the
    snapshot: the result reads as "what changed since" (a rollback undoes it)."""
    a, b = _parse_bundle(old_zip), _parse_bundle(new_zip)
    files = []
    for name in sorted(a.keys() | b.keys()):
        if name not in b:
            files.append({"name": name, "status": "removed", "changes": []})
            continue
        if name not in a:
            files.append({"name": name, "status": "added", "changes": []})
            continue
        changes: List[Dict] = []
        _diff_values(a[name], b[name], "", changes)
        files.append({"name": name,
                      "status": "changed" if changes else "unchanged",
                      "truncated": len(changes) >= _DIFF_LIMIT,
                      "changes": changes})
    totals = {"files_changed": sum(1 for f in files if f["status"] != "unchanged"),
              "changes": sum(len(f["changes"]) for f in files)}
    return {"files": files, "totals": totals}
