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
"""Client for an external ESPHome dashboard (the Device Builder backend).

The gateway does NOT compile firmware itself: it drives the stock ESPHome
container over the dashboard's own HTTP/WS API — the same calls its web UI
makes. Verified against ESPHome 2026.5.3 (esphome/dashboard/web_server.py):

  HTTP   GET  /version /devices /downloads?configuration= /edit?configuration=
         POST /edit?configuration=  (body = YAML; creates the file if missing)
         POST /archive?configuration= /unarchive?configuration=
         GET  /download.bin?configuration=&file=
         POST /login  (only when the dashboard runs with --username/--password)
  WS     /compile /validate /upload /run /logs /clean
         client sends {"type":"spawn","configuration":"x.yaml"[,"port":"OTA"]}
         then optional {"type":"stdin","data":...}; server streams
         {"event":"line","data":...} and finally {"event":"exit","code":N}.

Everything works over the network — no shared volume is required, which keeps
deployment to a single URL in config.yaml. HTTP uses stdlib urllib (same as
http_client.py); the WS side uses the `websockets` package that ships with
uvicorn[standard].
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# spawn-style WS commands the dashboard exposes (whitelist — anything else 404s
# there anyway, but we refuse it before opening a socket). "update-all"
# rebuilds+OTAs every node in the config dir and takes no configuration.
WS_COMMANDS = ("compile", "validate", "upload", "run", "logs", "clean",
               "update-all")

# Commands that require a "port" in the spawn message (OTA or a serial path).
PORT_COMMANDS = ("upload", "run", "logs")


class EsphomeError(Exception):
    """One friendly message per failure — routes map this to HTTP 502/503."""


class EsphomeDashboard:
    """Thin, stateless-per-request client for one dashboard instance.

    A cookie from POST /login is cached per instance; routes construct the
    client from the live config at request time, so URL/credential changes
    apply immediately.
    """

    def __init__(self, url: str, username: str = "", password: str = "",
                 timeout_s: float = 10.0):
        u = urllib.parse.urlparse(url or "")
        if u.scheme not in ("http", "https") or not u.netloc:
            raise EsphomeError(f"invalid ESPHome URL: {url!r} (need http(s)://host:port)")
        self.base = f"{u.scheme}://{u.netloc}"
        self.ws_base = ("wss" if u.scheme == "https" else "ws") + f"://{u.netloc}"
        self.username = username or ""
        self.password = password or ""
        self.timeout_s = float(timeout_s or 10.0)
        self._cookie: str = ""

    # ---- HTTP ----------------------------------------------------------------

    def _request(self, method: str, path: str, params: Optional[Dict] = None,
                 body: Optional[bytes] = None,
                 content_type: str = "application/octet-stream",
                 _retry_login: bool = True) -> Tuple[int, bytes]:
        qs = ("?" + urllib.parse.urlencode(params)) if params else ""
        req = urllib.request.Request(self.base + path + qs, data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", content_type)
        if self._cookie:
            req.add_header("Cookie", self._cookie)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            # Dashboard auth returns a redirect-to-login or 401; try one login.
            if e.code in (401, 403) and self.username and _retry_login:
                self._login()
                return self._request(method, path, params, body, content_type,
                                     _retry_login=False)
            return e.code, e.read()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise EsphomeError(
                f"ESPHome unreachable at {self.base}: {getattr(e, 'reason', e)}")

    def _login(self) -> None:
        data = urllib.parse.urlencode(
            {"username": self.username, "password": self.password}).encode()
        req = urllib.request.Request(self.base + "/login", data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            # The dashboard replies 302 with a Set-Cookie; urllib follows the
            # redirect, so grab the cookie via a no-redirect opener.
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(req, timeout=self.timeout_s) as resp:
                cookie = resp.headers.get("Set-Cookie", "")
        except urllib.error.HTTPError as e:
            cookie = e.headers.get("Set-Cookie", "") if e.code in (302, 303) else ""
            if not cookie:
                raise EsphomeError("ESPHome login failed (check username/password)")
        except (urllib.error.URLError, OSError) as e:
            raise EsphomeError(f"ESPHome unreachable at {self.base}: {e}")
        if not cookie:
            raise EsphomeError("ESPHome login failed (no session cookie)")
        self._cookie = cookie.split(";", 1)[0]

    def _get_json(self, path: str, params: Optional[Dict] = None) -> Any:
        status, data = self._request("GET", path, params)
        if status != 200:
            raise EsphomeError(f"ESPHome GET {path} -> HTTP {status}")
        try:
            return json.loads(data.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise EsphomeError(f"ESPHome GET {path}: not JSON (is this an "
                               "ESPHome dashboard URL?)")

    # ---- API surface -----------------------------------------------------------

    def version(self) -> str:
        return str(self._get_json("/version").get("version", ""))

    def devices(self) -> Dict[str, List[Dict[str, Any]]]:
        """Dashboard node lists: {"configured": [...], "importable": [...]}.
        `configured` = nodes with a YAML on the dashboard (name, configuration,
        address, deployed/current version, target platform...); `importable` =
        mDNS-discovered ESPHome devices offering their config for adoption."""
        out = self._get_json("/devices")
        if not isinstance(out, dict):
            raise EsphomeError("ESPHome /devices: unexpected response shape")
        return {"configured": list(out.get("configured") or []),
                "importable": list(out.get("importable") or [])}

    def get_config(self, configuration: str) -> str:
        status, data = self._request("GET", "/edit", {"configuration": configuration})
        if status == 404:
            raise EsphomeError(f"{configuration}: not found on the ESPHome dashboard")
        if status != 200:
            raise EsphomeError(f"ESPHome GET /edit -> HTTP {status}")
        return data.decode("utf-8", "replace")

    def save_config(self, configuration: str, content: str) -> None:
        status, _ = self._request("POST", "/edit", {"configuration": configuration},
                                  body=content.encode("utf-8"),
                                  content_type="application/yaml")
        if status != 200:
            raise EsphomeError(f"ESPHome save {configuration} -> HTTP {status}")

    def archive(self, configuration: str) -> None:
        status, _ = self._request("POST", "/archive",
                                  {"configuration": configuration}, body=b"")
        if status != 200:
            raise EsphomeError(f"ESPHome archive {configuration} -> HTTP {status}")

    def unarchive(self, configuration: str) -> None:
        status, _ = self._request("POST", "/unarchive",
                                  {"configuration": configuration}, body=b"")
        if status != 200:
            raise EsphomeError(f"ESPHome unarchive {configuration} -> HTTP {status}")

    def downloads(self, configuration: str) -> List[Dict[str, Any]]:
        """Artifact list after a build: [{title, description, file, download}]."""
        out = self._get_json("/downloads", {"configuration": configuration})
        return out if isinstance(out, list) else []

    def download_bin(self, configuration: str, file: str,
                     download: str = "") -> Tuple[bytes, str]:
        """Fetch one build artifact; returns (bytes, filename)."""
        params = {"configuration": configuration, "file": file}
        if download:
            params["download"] = download
        status, data = self._request("GET", "/download.bin", params)
        if status != 200:
            raise EsphomeError(f"ESPHome download.bin ({file}) -> HTTP {status} — "
                               "build the node first")
        name = download or f"{configuration.rsplit('.', 1)[0]}-{file.replace('/', '_')}"
        return data, name

    def ws_headers(self) -> List[Tuple[str, str]]:
        """Headers for the WS proxy (session cookie when auth is on)."""
        if self.username and not self._cookie:
            self._login()
        return [("Cookie", self._cookie)] if self._cookie else []

    async def stream_command(self, command: str, configuration: str,
                             port: str = "OTA",
                             extra: Optional[Dict[str, Any]] = None,
                             ) -> AsyncIterator[Dict[str, Any]]:
        """Run one dashboard command, yielding its {"event": ...} messages.

        Yields {"event":"line","data":...} then {"event":"exit","code":N}.
        The caller decides what to relay; cancellation closes the socket,
        which makes the dashboard terminate the subprocess.
        """
        if command not in WS_COMMANDS:
            raise EsphomeError(f"unknown ESPHome command: {command}")
        import websockets  # lazy: ships with uvicorn[standard]
        spawn: Dict[str, Any] = {"type": "spawn"}
        if command != "update-all":
            spawn["configuration"] = configuration
        if command in PORT_COMMANDS:
            spawn["port"] = port or "OTA"
        if extra:
            spawn.update(extra)
        try:
            async with websockets.connect(
                    f"{self.ws_base}/{command}",
                    additional_headers=self.ws_headers(),
                    open_timeout=self.timeout_s, close_timeout=5,
                    max_size=4 * 1024 * 1024) as ws:
                await ws.send(json.dumps(spawn))
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    yield msg
                    if msg.get("event") == "exit":
                        return
        except EsphomeError:
            raise
        except Exception as e:  # noqa: BLE001 — connect/handshake failures
            raise EsphomeError(f"ESPHome websocket /{command} failed: {e}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102
        return None


def from_config(esphome_cfg: Dict[str, Any]) -> EsphomeDashboard:
    """Build a client from the raw `esphome:` config block."""
    return EsphomeDashboard(
        url=str(esphome_cfg.get("url", "") or ""),
        username=str(esphome_cfg.get("username", "") or ""),
        password=str(esphome_cfg.get("password", "") or ""),
        timeout_s=float(esphome_cfg.get("timeout_s", 10) or 10),
    )
