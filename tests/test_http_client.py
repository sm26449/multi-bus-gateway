"""HTTP/JSON input driver: json-path resolver + client surface."""
from multibus.http_client import resolve_json_path, HttpClient


def test_resolve_dot_bracket_and_numeric_keys():
    doc = {"Body": {"Data": {"1": {"P": 123}, "PowerReal_P_Sum": -1845.9}},
           "list": [{"x": 9}, {"x": 10}]}
    assert resolve_json_path(doc, "Body.Data.1.P") == 123          # numeric string key
    assert resolve_json_path(doc, "Body.Data.PowerReal_P_Sum") == -1845.9
    assert resolve_json_path(doc, "list[0].x") == 9                # bracket index
    assert resolve_json_path(doc, "list.1.x") == 10               # dotted index
    assert resolve_json_path(doc, "Body.Nope.x") is None          # miss → None
    assert resolve_json_path(doc, "") is None


def test_http_client_stats_shape():
    c = HttpClient({"url": "http://127.0.0.1:1/x"}, registers=[], poll_groups={})
    s = c.get_stats()
    for k in ("connected", "successful_reads", "failed_reads", "staleness_age_s",
              "poll_rate", "total_registers"):
        assert k in s
    # no registers/pollers → health is idle-ok, never a false 'down'
    assert c.data_health()["status"] == "ok"


# ── P1: allow_nonlan redirects refuse downgrade + strip auth cross-host ───────

def test_guarded_redirect_refuses_downgrade_and_strips_auth():
    import urllib.error
    import urllib.request
    from multibus.http_client import _GuardedRedirect
    g = _GuardedRedirect(allow_nonlan=True)
    req = urllib.request.Request("https://a.example.com/x",
                                 headers={"Authorization": "Bearer secret",
                                          "X-Api-Key": "k", "Accept": "application/json"})
    # HTTPS→HTTP downgrade is refused
    try:
        g.redirect_request(req, None, 302, "Found", {}, "http://a.example.com/y")
        assert False, "downgrade should raise"
    except urllib.error.HTTPError as e:
        assert "downgrade" in str(e)
    # cross-host hop keeps working but drops credential headers
    new = g.redirect_request(req, None, 302, "Found", {}, "https://evil.example.net/y")
    hdrs = {k.lower() for k in new.headers}
    assert "authorization" not in hdrs and "x-api-key" not in hdrs
    assert "accept" in hdrs                                    # non-secret header kept
