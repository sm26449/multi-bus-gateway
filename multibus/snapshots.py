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

import io
import json
import logging
import os
import threading
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

BUNDLE_EXTRAS = ("calculated_templates.json", "builder_profiles.json",
                 "passkeys.json")   # beside the classic backup set
_INDEX = "index.json"
_LKG_ZIP = "lkg.zip"
_LKG_META = "lkg.json"


def _merge_devices(live_list, incoming_list):
    """Merge the devices[] list per id: the incoming list is authoritative for
    the device SET, but each device's stripped secret fields (connection
    password/headers, rest_push headers) are refilled from the matching live
    device when the incoming value is absent or empty."""
    live_by_id = {d.get("id"): d for d in live_list if isinstance(d, dict)}
    out = []
    for dev in incoming_list:
        if not isinstance(dev, dict):
            out.append(dev)
            continue
        live = live_by_id.get(dev.get("id"))
        if live:
            for sect, keys in (("connection", ("password", "headers")),
                               ("rest_push", ("headers",))):
                l_s, i_s = live.get(sect), dev.get(sect)
                if isinstance(l_s, dict) and isinstance(i_s, dict):
                    for key in keys:
                        if not i_s.get(key) and l_s.get(key):
                            i_s[key] = l_s[key]
        out.append(dev)
    return out


def _reinject_stripped_secrets(merged, live):
    """Restore secrets a sanitized export blanked at the top level (only the
    alerts.webhook_url token today — device secrets are handled per-id in
    _merge_devices)."""
    m_al, l_al = merged.get("alerts"), live.get("alerts")
    if isinstance(m_al, dict) and isinstance(l_al, dict):
        m_url, l_url = m_al.get("webhook_url", ""), l_al.get("webhook_url", "")
        # the export strips the query/userinfo; if the live URL is the same
        # endpoint with more (a token), the imported bare form loses it — keep
        # the live one so the webhook still authenticates.
        if l_url and m_url and l_url.startswith(m_url) and l_url != m_url:
            m_al["webhook_url"] = l_url


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
        os.replace(tmp, path)

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

                def _deep_merge(base, over):
                    for k, v in over.items():
                        if k == "devices" and isinstance(v, list):
                            base[k] = _merge_devices(base.get(k) or [], v)
                        elif isinstance(v, dict) and isinstance(base.get(k), dict):
                            _deep_merge(base[k], v)
                        else:
                            base[k] = v
                    return base
                merged = _deep_merge(live, incoming)
                # a sanitized export strips per-device connection.password /
                # headers and the alerts.webhook_url token — re-inject the live
                # values so a merge-import keeps them, as the docstring promises
                _reinject_stripped_secrets(merged, live)
            _atomic(cfg_path, _yaml.dump(merged, default_flow_style=False,
                                         allow_unicode=True, sort_keys=False))
            summary["config"] = True
        elif n.startswith("devices/") and n.endswith("selected_registers.json"):
            dev_id = Path(n).parts[1]
            _atomic(registers_path_for(dev_id), zf.read(n))
            summary["device_registers"] += 1
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
            _atomic(cfg_dir / n, zf.read(n))
            summary["extras"] += 1
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
        tmp.write_text(json.dumps(entries, indent=1, ensure_ascii=False))
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
            (self.dir / _LKG_META).write_text(json.dumps(meta, indent=1))
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
        lkg = cfg_path.parent / "snapshots" / _LKG_ZIP
        if not lkg.exists():
            raise
        logger.critical(f"config failed to load ({boot_err}) — restoring last known good")
        try:
            with zipfile.ZipFile(lkg) as zf:
                # devices/<id>/ paths inside the bundle mirror the on-disk layout
                # except the primary; without a parsed config we use the same rule
                # config.device_registers_path applies (primary = legacy root).
                from .config import PRIMARY_DEVICE_ID
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
    from .audit import redact_obj
    from .redact import _is_secret_key
    leaf = path.rsplit(".", 1)[-1].split("[")[0]
    if _is_secret_key(leaf):
        return "***"
    if isinstance(value, (dict, list)):
        return redact_obj(value)
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
