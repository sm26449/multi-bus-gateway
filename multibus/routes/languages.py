"""UI languages — file-driven i18n catalog (ui/languages/*.json).

Moved verbatim from create_api(); no shared state beyond the filesystem.
"""
from __future__ import annotations

import json
import os

from fastapi import APIRouter, HTTPException

_LANG_DIR = os.path.join("ui", "languages")


def build(ctx) -> APIRouter:
    r = APIRouter(tags=["languages"])

    @r.get("/api/languages")
    async def list_languages():
        """Available UI languages — scans ui/languages/*.json, so dropping a new
        file (copy en.json -> xx.json and translate) adds a language with no code change."""
        out = []
        try:
            for fn in sorted(os.listdir(_LANG_DIR)):
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(_LANG_DIR, fn), encoding="utf-8") as f:
                        meta = (json.load(f) or {}).get("_meta", {})
                    code = meta.get("code") or fn[:-5]
                    out.append({"code": code, "name": meta.get("name", code),
                                "nativeName": meta.get("nativeName", meta.get("name", code)),
                                "flag": meta.get("flag", "")})
                except Exception:  # noqa: BLE001
                    pass
        except FileNotFoundError:
            pass
        return {"languages": out, "default": "en"}

    @r.get("/api/languages/{code}")
    async def get_language(code: str):
        """Return one language file's translation map."""
        if not (code.isalpha() and code.islower() and 2 <= len(code) <= 8):
            raise HTTPException(status_code=400, detail="bad language code")
        path = os.path.join(_LANG_DIR, f"{code}.json")
        if not os.path.exists(path):
            raise HTTPException(status_code=404, detail="unknown language")
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(e))

    return r
