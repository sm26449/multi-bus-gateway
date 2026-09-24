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
"""HTTP/JSON input driver (Tier 2).

Polls an HTTP endpoint that returns JSON (Fronius Solar API, Shelly, Tasmota,
Enphase, …) and extracts values by a per-register ``json_path``, producing the
SAME normalized ``{address: {value, register, ts}}`` batches a ModbusClient
emits — so every downstream sink (MQTT, InfluxDB, virtual meters) works
unchanged. Mirrors the parts of ModbusClient the API/UI rely on
(connect/start_polling/get_stats/data_health/disconnect + publish_callback).

Values come from JSON already in engineering units, so there is no scale-factor
or block-size handling to worry about (unlike a fragile Modbus gateway)."""
import concurrent.futures as _futures
import http.client
import ipaddress
import json
import logging
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .counter_filter import DailyCounterFilter, MonotonicFilter
from .redact import redact_url
from .value_decode import apply_corrections

logger = logging.getLogger(__name__)


def _classify_lan(addrs) -> Optional[str]:
    """Return an error string if ANY resolved address is not a private LAN
    address, else None. Rejecting on *any* non-LAN address means a rebinding
    reply mixing one LAN + one public/metadata IP is refused outright."""
    for a in addrs:
        ip = ipaddress.ip_address(a)
        # Normalize IPv4-mapped IPv6 (::ffff:169.254.169.254) to its v4 form so a
        # mapped literal can't smuggle a link-local/loopback/metadata target past
        # the class checks (mapped addresses report is_link_local/is_loopback=False).
        if getattr(ip, "ipv4_mapped", None) is not None:
            ip = ip.ipv4_mapped
        if (not ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_unspecified or ip.is_reserved):
            return f"host must be a private LAN address ({a} is not)"
    return None


# Bound DNS resolution by the caller's timeout: socket.getaddrinfo ignores any
# timeout, so a slow/unreachable resolver (a flaky link) can wedge a poller
# thread for tens of seconds past its configured timeout. Run it in a small pool
# and stop waiting on time-out — the orphaned lookup finishes on its own and
# frees its worker; we never block on it.
_RESOLVER_POOL = _futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="dns-resolve")


def _getaddrinfo(host: str, timeout: float):
    """``socket.getaddrinfo(host, None)`` bounded by ``timeout`` seconds."""
    return _RESOLVER_POOL.submit(socket.getaddrinfo, host, None).result(timeout=timeout)


def lan_url_error(url: str, timeout: float = 5.0) -> Optional[str]:
    """SSRF guard for server-side HTTP device fetches: return an error string if
    the URL's host does not resolve to ONLY private/LAN addresses, else None.
    Blocks reaching the public internet, loopback services, and link-local /
    cloud-metadata endpoints (e.g. 169.254.169.254)."""
    host = urlparse(url).hostname
    if not host:
        return "could not parse a host from the URL"
    try:
        infos = _getaddrinfo(host, timeout)
    except Exception:  # noqa: BLE001 — unresolved OR resolver timed out
        return f"host {host!r} does not resolve"
    addrs = {i[4][0] for i in infos}
    if not addrs:
        return f"host {host!r} does not resolve"
    return _classify_lan(addrs)


def resolve_lan_ip(url: str, timeout: float = 5.0) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve+validate the URL's host ONCE and return ``(pinned_ip, host, error)``.

    The caller connects to ``pinned_ip`` (a literal address that passed the LAN
    check) instead of re-resolving the hostname — so a low-TTL DNS rebind cannot
    swap in a metadata/public target between the check and the connect (the
    classic resolve-then-reconnect TOCTOU). ``host`` is returned so it can be
    sent as the ``Host`` header / TLS SNI for name-based vhosts + cert checks."""
    host = urlparse(url).hostname
    if not host:
        return None, None, "could not parse a host from the URL"
    try:
        infos = _getaddrinfo(host, timeout)
    except Exception:  # noqa: BLE001 — unresolved OR resolver timed out
        return None, host, f"host {host!r} does not resolve"
    addrs = {i[4][0] for i in infos}
    if not addrs:
        return None, host, f"host {host!r} does not resolve"
    err = _classify_lan(addrs)
    if err:
        return None, host, err
    return sorted(addrs)[0], host, None


# ── DNS-rebinding-safe connection pinning ─────────────────────────────────────
# Connect to a pre-validated literal IP while keeping the hostname for the Host
# header + TLS SNI/cert verification. urllib would otherwise re-resolve the name
# at connect time, re-opening the rebinding window the guard just closed.
def _pinned_http_connection(pinned_ip: str):
    class _Conn(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.create_connection(
                (pinned_ip, self.port), self.timeout, self.source_address)
            if self._tunnel_host:
                self._tunnel()
    return _Conn


def _pinned_https_connection(pinned_ip: str):
    class _Conn(http.client.HTTPSConnection):
        def connect(self):
            sock = socket.create_connection(
                (pinned_ip, self.port), self.timeout, self.source_address)
            if self._tunnel_host:
                self.sock = sock
                self._tunnel()
                sock = self.sock
            # SNI + certificate hostname check use self.host (the ORIGINAL name),
            # not the pinned IP — TLS identity is still verified against the name.
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
    return _Conn


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, pinned_ip: str):
        super().__init__()
        self._factory = _pinned_http_connection(pinned_ip)

    def http_open(self, req):
        return self.do_open(self._factory, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, pinned_ip: str, context=None):
        super().__init__(context=context)
        self._factory = _pinned_https_connection(pinned_ip)

    def https_open(self, req):
        return self.do_open(self._factory, req, context=self._context)


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect target against the LAN guard. Without this a
    LAN host could 302 the fetch to a public / metadata / loopback URL (urllib
    follows redirects by default, defeating the initial-URL SSRF check)."""

    def __init__(self, allow_nonlan: bool):
        self.allow_nonlan = allow_nonlan

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.allow_nonlan:
            err = lan_url_error(newurl)
            if err:
                raise urllib.error.HTTPError(
                    newurl, code, f"SSRF guard (redirect): {err}", headers, fp)
        # Never follow an HTTPS→HTTP downgrade — it would send the request (and
        # any auth) in clear, and is a classic redirect-attack primitive.
        if urlparse(req.full_url).scheme == 'https' and urlparse(newurl).scheme == 'http':
            raise urllib.error.HTTPError(
                newurl, code, "redirect refused: HTTPS→HTTP downgrade", headers, fp)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        # On a cross-HOST hop, strip credential headers so a redirect can't
        # replay Authorization / API tokens to a third party.
        if new is not None and urlparse(req.full_url).hostname != urlparse(newurl).hostname:
            for h in list(new.headers):
                if h.lower() in ('authorization', 'proxy-authorization', 'cookie', 'x-api-key'):
                    del new.headers[h]
        return new


def _origin(url: str) -> tuple:
    """(scheme, host, effective-port) — the full same-origin identity."""
    u = urlparse(url)
    port = u.port or (443 if u.scheme == 'https' else 80)
    return (u.scheme, u.hostname, port)


class _SameHostRedirect(_GuardedRedirect):
    """Redirect policy for a PINNED fetch: only a SAME-ORIGIN hop (identical
    scheme + host + port) is safe, because the pinned IP stays valid for it. A
    change of host, port, or scheme is refused: it would connect the new target
    to the old pinned IP and replay the request headers to an unintended service
    (e.g. a 302 from :80 to :22 same-host port-probe, or https→http downgrade).
    Same-origin hops still get LAN re-validated by the parent as a second layer."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != _origin(req.full_url):
            raise urllib.error.HTTPError(
                newurl, code, "SSRF guard (redirect): cross-origin redirect blocked "
                "(scheme/host/port change)", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def resolve_json_path(obj: Any, path: str) -> Any:
    """Resolve a dot/bracket path into a parsed-JSON object.

    Supports ``a.b.c``, list indices ``a[0].b`` or ``a.0.b``, and numeric dict
    keys stored as strings (``Body.Data.1.PowerReal_P_Sum`` — the Fronius Solar
    API keys devices by string id). Returns None on any miss (never raises)."""
    if not path:
        return None
    cur = obj
    for raw in path.split('.'):
        if cur is None:
            return None
        token = raw.strip()
        # split "name[0][1]" into key + bracket indices
        key = token
        idxs: List[int] = []
        b = token.find('[')
        if b != -1:
            key = token[:b]
            rest = token[b:]
            try:
                for part in rest.replace(']', '').split('[')[1:]:
                    idxs.append(int(part))
            except ValueError:
                return None
        if key != '':
            if isinstance(cur, dict):
                cur = cur.get(key)
            elif isinstance(cur, list) and key.lstrip('-').isdigit():
                i = int(key)
                cur = cur[i] if -len(cur) <= i < len(cur) else None
            else:
                return None
        for i in idxs:
            if isinstance(cur, list) and -len(cur) <= i < len(cur):
                cur = cur[i]
            elif isinstance(cur, dict):
                cur = cur.get(str(i))
            else:
                return None
    return cur


def _coerce_numeric(val) -> Optional[float]:
    """Coerce a json_path result to a finite number, or None to skip it.
    bool → 0/1; numeric strings parse; dict/list/text → None (fail-safe)."""
    import math
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        # math.isfinite(int) raises OverflowError above ~1e308; an integer
        # wider than 64 bits is no measurement either (F-63, 3.83.0)
        return val if -(2 ** 63) <= val < 2 ** 63 else None
    if isinstance(val, float):
        return val if math.isfinite(val) else None
    if isinstance(val, str):
        try:
            f = float(val)
            return f if math.isfinite(f) else None    # "inf"/"nan" strings rejected too
        except ValueError:
            return None
    return None


class _JsonPoller(threading.Thread):
    """One poll-group thread: fetch the endpoint, extract its registers, publish."""

    def __init__(self, name: str, interval: float, registers: list,
                 fetch: Callable[[], Optional[dict]], publish_callback, owner):
        super().__init__(daemon=True, name=f"HttpPoller-{name}")
        self.poll_group_name = name
        self.interval = max(0.1, float(interval))
        self.registers = registers
        self._fetch = fetch
        self.publish_callback = publish_callback
        self._owner = owner
        self.running = False
        self._stop_event = threading.Event()   # single stop primitive (M1)
        self.poll_count = 0
        self.last_poll_time = 0.0
        # monotonic-counter guards, per register (same as the Modbus poller);
        # re-seeded on reload since a fresh poller is built each time
        self._counter_filters: dict = {}
        self._daily_filters: dict = {}

    def run(self):
        # Stop event is the single source of truth — a stop() landing between
        # Thread.start() and here must never be overwritten (audit M1: the
        # `running` flag alone lost that race and left a zombie poller).
        if self._stop_event.is_set():
            return
        self.running = True                    # observability mirror only
        logger.info("HTTP poller %s: %d registers, interval %ss",
                    self.poll_group_name, len(self.registers), self.interval)
        while not self._stop_event.is_set():
            t0 = time.time()
            t0_mono = time.monotonic()
            try:
                doc = self._fetch()
                if self._stop_event.is_set():   # stopped while blocked in the fetch
                    break                        # → never publish after disconnect
                if doc is not None:
                    data: Dict[int, Dict] = {}
                    for reg in self.registers:
                        val = resolve_json_path(doc, getattr(reg, 'json_path', ''))
                        val = _coerce_numeric(val)
                        if val is None:
                            # non-numeric (dict/list/text) values NEVER go downstream:
                            # a string in a numeric InfluxDB field type-conflicts the
                            # whole batch, and a vmeter can't encode it (stalls the
                            # block refresh). Skip the register instead.
                            continue
                        # SHARED correction pipeline (apply_corrections): this
                        # path used to apply only scale+offset — a declared
                        # nan sentinel published as a real measurement, an
                        # enum stayed a bare int and a monotonic energy
                        # counter got zero rollover protection on HTTP
                        # sources (external audit). Undeclared registers pass
                        # through unchanged.
                        _cf = None
                        if getattr(reg, 'monotonic', False):
                            _cf = self._counter_filters.get(reg.address)
                            if _cf is None:
                                _cf = self._counter_filters[reg.address] = MonotonicFilter()
                        _df = None
                        if getattr(reg, 'daily', False):
                            _df = self._daily_filters.get(reg.address)
                            if _df is None:
                                _df = self._daily_filters[reg.address] = DailyCounterFilter()
                        val = apply_corrections(val, reg, counter_filter=_cf, daily_filter=_df)
                        if val is None:
                            continue          # sentinel/decode/filter → hold last-good
                        data[reg.address] = {'value': val, 'register': reg,
                                             'ts': t0, 'mono': t0_mono,
                                             'interval': self.interval}
                    if data and self.publish_callback:
                        self.publish_callback(self.poll_group_name, data)
                    self._owner._note_success(len(data))
                else:
                    self._owner._note_failure()
            except Exception as e:  # noqa: BLE001
                self._owner._note_failure()
                logger.debug("HTTP poller %s error: %s", self.poll_group_name, e)
            self.poll_count += 1
            self.last_poll_time = time.time()
            dt = self.interval - (time.time() - t0)
            # On time → wait the remainder (exact cadence). If the fetch OVERRAN
            # the interval (dt <= 0), still back off (up to 1 s) instead of
            # re-fetching immediately — a slow endpoint must not be hammered.
            # Event.wait, not time.sleep: a stop() interrupts the pause, so a
            # 60 s group's thread no longer outlives its device by a full
            # interval (audit L2 — join() always timed out on slow groups).
            self._stop_event.wait(dt if dt > 0 else min(self.interval, 1.0))
        self.running = False                    # mirror follows the event

    def stop(self):
        self.running = False
        self._stop_event.set()


class HttpClient:
    """HTTP/JSON polling client with a ModbusClient-compatible surface."""

    def __init__(self, http_cfg: Dict[str, Any], registers: list, poll_groups: dict,
                 allow_nonlan: bool = False):
        self.url = str(http_cfg.get('url', '')).strip()
        self.timeout = float(http_cfg.get('timeout', 8))
        self.headers = dict(http_cfg.get('headers', {}) or {})
        # Per-device TLS verification. Default ON; an operator may disable it for a
        # LAN device with a self-signed cert (explicit, logged — never silent).
        self.verify_tls = bool(http_cfg.get('verify_tls', True))
        if not self.verify_tls and self.url.lower().startswith('https'):
            logger.warning("HTTP device %s: TLS certificate verification is DISABLED "
                           "(verify_tls=false) — only do this on a trusted LAN device",
                           redact_url(self.url))
        # SSRF guard: fetches are restricted to private/LAN hosts unless the
        # operator explicitly opts out (security.allow_nonlan_http_devices).
        self.allow_nonlan = bool(allow_nonlan)
        self.registers = registers
        self.poll_groups = poll_groups
        self.pollers: List[_JsonPoller] = []
        # serializes start_polling / update_registers / disconnect (external
        # audit: concurrent Applies interleaved stop/rebuild → doubled pollers)
        self._lifecycle_lock = threading.RLock()
        self.publish_callback = None
        self.connected = False
        self.successful_reads = 0
        self.failed_reads = 0
        self.last_success_mono = None   # step-immune staleness
        self.last_success_ts: Optional[float] = None
        self.last_latency_ms = None
        self._lock = threading.Lock()

    # ── fetch ─────────────────────────────────────────────────────────────
    def _fetch(self) -> Optional[dict]:
        if not self.url:
            return None
        _t0 = time.perf_counter()
        # Honor per-device TLS verification (default: verify). An unverified
        # context is built only when the operator explicitly set verify_tls=false.
        ssl_ctx = None
        if not self.verify_tls:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        if self.allow_nonlan:
            # Operator explicitly opted out of the LAN guard (e.g. a cloud API
            # behind a CDN with many IPs / redirects) — no pinning, but redirects
            # still refuse HTTPS→HTTP downgrade and strip auth on cross-host hops.
            opener = urllib.request.build_opener(
                *( [urllib.request.HTTPSHandler(context=ssl_ctx)] if ssl_ctx else [] ),
                _GuardedRedirect(allow_nonlan=True))
        else:
            # Resolve+validate ONCE, then connect to that literal IP (not the
            # hostname) so a low-TTL DNS rebind can't swap the target between the
            # check and the connect. Cross-origin redirects are refused.
            pinned, _host, err = resolve_lan_ip(self.url, timeout=self.timeout)
            if err:
                raise RuntimeError(f"SSRF guard: {err}")
            opener = urllib.request.build_opener(
                _PinnedHTTPHandler(pinned), _PinnedHTTPSHandler(pinned, ssl_ctx),
                _SameHostRedirect(allow_nonlan=False))
        req = urllib.request.Request(self.url, headers=self.headers)
        with opener.open(req, timeout=self.timeout) as r:  # noqa: S310 (LAN-guarded)
            # Bounded read: a misbehaving / hostile endpoint must not stream GBs
            # into memory and OOM the gateway. A JSON telemetry payload is tiny;
            # 8 MiB is a generous ceiling. One extra byte past the cap → reject.
            _MAX = 8 * 1024 * 1024
            raw = r.read(_MAX + 1)
            if len(raw) > _MAX:
                raise RuntimeError(f"response exceeds {_MAX} bytes — refusing to buffer")
            doc = json.loads(raw.decode('utf-8', 'replace'))
        self.last_latency_ms = round((time.perf_counter() - _t0) * 1000, 1)
        return doc

    def _note_success(self, n: int):
        with self._lock:
            self.successful_reads += 1
            self.last_success_mono = time.monotonic()
            self.last_success_ts = time.time()
            self.connected = True

    def _note_failure(self):
        with self._lock:
            self.failed_reads += 1
            self.connected = False

    # ── lifecycle (ModbusClient-compatible) ───────────────────────────────
    def connect(self) -> bool:
        try:
            doc = self._fetch()
            self.connected = doc is not None
            if self.connected:
                self.last_success_mono = time.monotonic()
            return self.connected
        except Exception as e:  # noqa: BLE001
            logger.warning("HTTP device: initial fetch failed (%s) — pollers will retry", e)
            self.connected = False
            return False

    def start_polling(self):
        with self._lifecycle_lock:
            if self.pollers:                    # double-start guard
                logger.warning("HTTP start_polling with %d pollers already "
                               "running — stopping them first", len(self.pollers))
                for p in self.pollers:
                    p.stop()
                self.pollers = []
            by_group: Dict[str, list] = {}
            for reg in self.registers:
                by_group.setdefault(reg.poll_group, []).append(reg)
            for group_name, regs in by_group.items():
                gc = self.poll_groups.get(group_name) or self.poll_groups.get('normal')
                if not gc:
                    continue
                p = _JsonPoller(group_name, gc.interval, regs, self._fetch,
                                self.publish_callback, self)
                p.start()
                self.pollers.append(p)
            logger.info("HTTP device: started %d polling threads", len(self.pollers))

    def update_registers(self, registers: list, poll_groups: dict):
        """Swap the register set + poll groups and live-restart the pollers."""
        with self._lifecycle_lock:
            self.registers = registers
            self.poll_groups = poll_groups
            for p in self.pollers:
                p.stop()
            self.pollers = []
            self.start_polling()
        logger.info("HTTP device: registers updated (%d) — pollers restarted", len(registers))

    def disconnect(self):
        with self._lifecycle_lock:
            pollers, self.pollers = self.pollers, []
        for p in pollers:
            p.stop()
        # bounded join so a poller blocked in urlopen can't publish a late batch
        # into a replaced client's stores (device update swaps clients live)
        for p in pollers:
            p.join(timeout=self.timeout + 2)
        self.connected = False

    # ── observability (shape matches ModbusClient) ────────────────────────
    def get_stats(self) -> Dict:
        poll_rate = sum(1.0 / p.interval for p in self.pollers
                        if p.running and p.interval > 0)
        age = (round(time.monotonic() - self.last_success_mono, 1)
               if self.last_success_mono else None)
        return {
            'connected': self.connected,
            'url': self.url,
            'successful_reads': self.successful_reads,
            'failed_reads': self.failed_reads,
            'staleness_age_s': age,
            'last_success_ts': self.last_success_ts,
            'last_latency_ms': self.last_latency_ms,
            'poll_rate': round(poll_rate, 2),
            'total_registers': len(self.registers),
        }

    def data_health(self, stale_threshold_s: float = 30) -> Dict:
        if not self.registers:
            return {"status": "idle", "stale": False, "staleness_age_s": None}
        if not self.pollers:
            # selected but not running: not idle — the connection gate downstream
            # turns this into degraded
            return {"status": "ok", "stale": False, "staleness_age_s": None}
        last_mono = self.last_success_mono
        if last_mono is None:
            # cold start: down only once a fetch has actually failed
            return {"status": "down" if self.failed_reads else "ok",
                    "stale": False, "staleness_age_s": None}
        fastest = min((p.interval for p in self.pollers), default=stale_threshold_s)
        thresh = max(stale_threshold_s, fastest * 3)
        age = time.monotonic() - last_mono          # NTP-step-proof
        stale = age > thresh
        status = "ok" if (self.connected and not stale) else ("down" if stale else "degraded")
        return {"status": status, "stale": stale, "staleness_age_s": round(age, 1)}

    def test_read(self) -> Dict:
        """One-shot fetch for the device 'Test connection' button.

        Retries once: single-client sources (e.g. a Fronius DataManager) can hand
        back an empty/partial body when another reader is mid-poll, so a lone 0/N
        or a transient fetch error is usually just contention, not a bad map/URL.
        """
        attempts = 2
        last_err = None
        for attempt in range(attempts):
            try:
                doc = self._fetch()
                if doc is None:
                    return {"ok": False, "message": "no URL configured"}
                hits = sum(1 for r in self.registers
                           if resolve_json_path(doc, getattr(r, 'json_path', '')) is not None)
                if hits == 0 and self.registers and attempt + 1 < attempts:
                    time.sleep(0.6)   # likely contention — give the source a beat
                    continue
                return {"ok": True, "message": f"HTTP OK — {hits}/{len(self.registers)} paths resolved"}
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt + 1 < attempts:
                    time.sleep(0.6)
                    continue
        return {"ok": False, "message": str(last_err) if last_err else "no response"}
