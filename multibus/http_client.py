"""HTTP/JSON input driver (Tier 2).

Polls an HTTP endpoint that returns JSON (Fronius Solar API, Shelly, Tasmota,
Enphase, …) and extracts values by a per-register ``json_path``, producing the
SAME normalized ``{address: {value, register, ts}}`` batches a ModbusClient
emits — so every downstream sink (MQTT, InfluxDB, virtual meters) works
unchanged. Mirrors the parts of ModbusClient the API/UI rely on
(connect/start_polling/get_stats/data_health/disconnect + publish_callback).

Values come from JSON already in engineering units, so there is no scale-factor
or block-size handling to worry about (unlike a fragile Modbus gateway)."""
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

from .redact import redact_url

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


def lan_url_error(url: str) -> Optional[str]:
    """SSRF guard for server-side HTTP device fetches: return an error string if
    the URL's host does not resolve to ONLY private/LAN addresses, else None.
    Blocks reaching the public internet, loopback services, and link-local /
    cloud-metadata endpoints (e.g. 169.254.169.254)."""
    host = urlparse(url).hostname
    if not host:
        return "could not parse a host from the URL"
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:  # noqa: BLE001
        return f"host {host!r} does not resolve"
    addrs = {i[4][0] for i in infos}
    if not addrs:
        return f"host {host!r} does not resolve"
    return _classify_lan(addrs)


def resolve_lan_ip(url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
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
        infos = socket.getaddrinfo(host, None)
    except Exception:  # noqa: BLE001
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
        return super().redirect_request(req, fp, code, msg, headers, newurl)


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
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, (int, float)):
        return val if val == val and val not in (float('inf'), float('-inf')) else None
    if isinstance(val, str):
        try:
            f = float(val)
            return f if f == f else None
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
        self.poll_count = 0
        self.last_poll_time = 0.0

    def run(self):
        self.running = True
        logger.info("HTTP poller %s: %d registers, interval %ss",
                    self.poll_group_name, len(self.registers), self.interval)
        while self.running:
            t0 = time.time()
            try:
                doc = self._fetch()
                if not self.running:            # stopped while blocked in the fetch
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
                        data[reg.address] = {'value': val, 'register': reg, 'ts': t0}
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
            if dt > 0:
                time.sleep(dt)

    def stop(self):
        self.running = False


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
        self.publish_callback = None
        self.connected = False
        self.successful_reads = 0
        self.failed_reads = 0
        self.last_success_ts = None
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
            # behind a CDN with many IPs / redirects) — plain fetch, no pinning.
            opener = urllib.request.build_opener(
                *( [urllib.request.HTTPSHandler(context=ssl_ctx)] if ssl_ctx else [] ))
        else:
            # Resolve+validate ONCE, then connect to that literal IP (not the
            # hostname) so a low-TTL DNS rebind can't swap the target between the
            # check and the connect. Cross-origin redirects are refused.
            pinned, _host, err = resolve_lan_ip(self.url)
            if err:
                raise RuntimeError(f"SSRF guard: {err}")
            opener = urllib.request.build_opener(
                _PinnedHTTPHandler(pinned), _PinnedHTTPSHandler(pinned, ssl_ctx),
                _SameHostRedirect(allow_nonlan=False))
        req = urllib.request.Request(self.url, headers=self.headers)
        with opener.open(req, timeout=self.timeout) as r:  # noqa: S310 (LAN-guarded)
            doc = json.loads(r.read().decode('utf-8', 'replace'))
        self.last_latency_ms = round((time.perf_counter() - _t0) * 1000, 1)
        return doc

    def _note_success(self, n: int):
        with self._lock:
            self.successful_reads += 1
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
                self.last_success_ts = time.time()
            return self.connected
        except Exception as e:  # noqa: BLE001
            logger.warning("HTTP device: initial fetch failed (%s) — pollers will retry", e)
            self.connected = False
            return False

    def start_polling(self):
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
        self.registers = registers
        self.poll_groups = poll_groups
        for p in self.pollers:
            p.stop()
        self.pollers = []
        self.start_polling()
        logger.info("HTTP device: registers updated (%d) — pollers restarted", len(registers))

    def disconnect(self):
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
        now = time.time()
        age = round(now - self.last_success_ts, 1) if self.last_success_ts else None
        return {
            'connected': self.connected,
            'url': self.url,
            'successful_reads': self.successful_reads,
            'failed_reads': self.failed_reads,
            'staleness_age_s': age,
            'last_latency_ms': self.last_latency_ms,
            'poll_rate': round(poll_rate, 2),
            'total_registers': len(self.registers),
        }

    def data_health(self, stale_threshold_s: float = 30) -> Dict:
        if not self.registers or not self.pollers:
            return {"status": "ok", "stale": False, "staleness_age_s": None}
        now = time.time()
        last = self.last_success_ts
        if last is None:
            # cold start: down only once a fetch has actually failed
            return {"status": "down" if self.failed_reads else "ok",
                    "stale": False, "staleness_age_s": None}
        fastest = min((p.interval for p in self.pollers), default=stale_threshold_s)
        thresh = max(stale_threshold_s, fastest * 3)
        age = now - last
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
