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
