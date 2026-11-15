#!/usr/bin/env python3
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""Resolve every intra-package import in ``multibus/`` statically — including
imports nested inside functions (lazy imports, 78% of the graph) that pytest
only exercises when the function runs.

It imports nothing from ``multibus`` (safe on the production host): it parses
each module with ``ast``, resolves every ``import``/``from`` that targets a
``multibus.*`` module, and fails when the target MODULE does not exist, or when
a named import (``from .x import y``) resolves to a module that exists but
neither defines ``y`` nor has a submodule ``y`` — the exact breakage a move
introduces in a lazy import.

Conservative on names: if the target module uses ``from z import *`` at top
level, or defines a module-level ``__getattr__``, its exported-name set is
unknowable statically, so any requested name is accepted (no false failure).

Exit 0 = clean. Exit 1 = at least one unresolved import (printed file:line).
Usage: ``python3 -I tools/check_imports.py [package_root]``
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT_PKG = "multibus"

# Pre-existing unresolved imports that the refactor did NOT introduce and does
# NOT fix (phase 0 changes no behaviour). Each is a real latent bug recorded in
# the findings register; the phase-1 section that rewrites the file removes both
# the bug and its entry here. A known entry that no longer matches is an error
# (remove it), mirroring import-linter's unmatched-ignore policy.
#
# Keyed by (file, import) with NO line number — the display message keeps the
# line, but the allowlist matches on the line-number-free KEY (D5/M12: the old
# "relpath:lineno:" key forced 4 re-pins in 5 days as unrelated edits shifted
# lines). An entry here is the exact text main() prints after "key:", e.g.
#   "multibus/x.py: from multibus.y import Z -> name not found in multibus.y"
# DUP-19 (the CounterFilter copy-paste in rtu_tap.py + seplos_v2.py) was fixed to
# MonotonicFilter on 2026-11-07 (remediation C2/H6), so there are no longer any
# known-unresolved intra-package imports — the allowlist is empty.
# NOTE: device_runtime.py does a string-based `import_module(<driver path>)` to
# load a driver by name; that dynamic import is invisible to this AST checker by
# design (the driver module's existence is covered by the §5 DeviceDriver tests).
KNOWN_UNRESOLVED: set = set()

# strip ":<lineno>:" down to ":" so the allowlist key is stable across edits
_KEY = re.compile(r":\d+:")


def _module_name(pkg_root: Path, path: Path) -> str:
    rel = path.relative_to(pkg_root.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _collect(pkg_root: Path):
    """modname -> (is_package, Path, set(exported names), knows_names:bool)."""
    mods = {}
    for path in sorted(pkg_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        modname = _module_name(pkg_root, path)
        is_pkg = path.name == "__init__.py"
        names, knows = _exported_names(path)
        mods[modname] = (is_pkg, path, names, knows)
    return mods


def _exported_names(path: Path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"),
                         filename=str(path))
    except SyntaxError:
        return set(), False
    names: set[str] = set()
    knows = True
    for node in tree.body:  # top level only
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            if node.name == "__getattr__":
                knows = False
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
                elif isinstance(t, (ast.Tuple, ast.List)):
                    names.update(e.id for e in t.elts if isinstance(e, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if any(a.name == "*" for a in node.names):
                knows = False  # star import: names unknowable
            for a in node.names:
                if a.name != "*":
                    names.add(a.asname or a.name)
    return names, knows


def _resolve_from(modname: str, is_pkg: bool, node: ast.ImportFrom) -> str | None:
    """Absolute module the ImportFrom targets, or None if not intra-package."""
    if node.level == 0:
        base = node.module or ""
        return base if base.split(".")[0] == ROOT_PKG else None
    # relative: anchor on the current package
    pkg = modname if is_pkg else modname.rsplit(".", 1)[0]
    up = node.level - 1
    parts = pkg.split(".")
    if up:
        parts = parts[:-up] if up < len(parts) else []
    base = ".".join(parts)
    if node.module:
        base = f"{base}.{node.module}" if base else node.module
    return base or None


def check(pkg_root: Path) -> list[str]:
    mods = _collect(pkg_root)
    errors: list[str] = []
    base = pkg_root.parent

    def rel(p: Path) -> str:
        try:
            return str(p.relative_to(base))
        except ValueError:
            return str(p)

    def mod_exists(name: str) -> bool:
        return name in mods

    for modname, (is_pkg, path, _n, _k) in mods.items():
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"),
                         filename=str(path))
        for node in ast.walk(tree):  # walk = top-level AND nested (lazy)
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] == ROOT_PKG and not mod_exists(a.name):
                        errors.append(
                            f"{rel(path)}:{node.lineno}: import {a.name}"
                            " -> no such module")
            elif isinstance(node, ast.ImportFrom):
                target = _resolve_from(modname, is_pkg, node)
                if target is None or target.split(".")[0] != ROOT_PKG:
                    continue
                if not mod_exists(target):
                    errors.append(
                        f"{rel(path)}:{node.lineno}: from {target}"
                        " -> no such module")
                    continue
                _ti, _tp, tnames, tknows = mods[target]
                for a in node.names:
                    if a.name == "*":
                        continue
                    if mod_exists(f"{target}.{a.name}"):
                        continue  # submodule
                    if not tknows:
                        continue  # star-import / __getattr__: unknowable, accept
                    if a.name not in tnames:
                        errors.append(
                            f"{rel(path)}:{node.lineno}: from {target} import "
                            f"{a.name} -> name not found in {target}")
    return errors


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(ROOT_PKG)
    if not root.is_dir():
        print(f"no such package dir: {root}", file=sys.stderr)
        return 2
    # map each line-number-free KEY to a full display message (keeps the line)
    found = {_KEY.sub(":", m): m for m in check(root)}
    keys = set(found)
    new = sorted(found[k] for k in (keys - KNOWN_UNRESOLVED))
    stale = sorted(KNOWN_UNRESOLVED - keys)
    for e in new:
        print(f"UNRESOLVED {e}")
    for e in stale:
        print(f"STALE-KNOWN (fixed? remove from KNOWN_UNRESOLVED) {e}")
    n = sum(1 for _ in root.rglob("*.py") if "__pycache__" not in _.parts)
    print(f"check_imports: {n} modules scanned, {len(found)} unresolved "
          f"({len(keys & KNOWN_UNRESOLVED)} known), {len(new)} new, "
          f"{len(stale)} stale-known", file=sys.stderr)
    return 1 if (new or stale) else 0


if __name__ == "__main__":
    raise SystemExit(main())
