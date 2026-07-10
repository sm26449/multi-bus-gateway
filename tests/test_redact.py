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
"""redact_url: strip credentials from URLs before they reach the logs."""
from multibus.redact import redact_url


def test_userinfo_dropped():
    assert redact_url("https://user:s3cr3t@host.local/ingest") == "https://host.local/ingest"


def test_secret_query_params_masked():
    out = redact_url("https://h/push?api_key=ABC&token=XYZ&sig=Q&page=2")
    assert "ABC" not in out and "XYZ" not in out and "Q" not in out
    assert "api_key=%2A%2A%2A" in out or "api_key=***" in out.replace("%2A", "*")
    assert "page=2" in out                       # benign param kept


def test_benign_url_unchanged():
    url = "http://sms-gateway:5080/api/sms/send"
    assert redact_url(url) == url


def test_port_preserved():
    assert redact_url("https://user:pw@host:8443/x") == "https://host:8443/x"


def test_empty_and_none():
    assert redact_url("") == ""
    assert redact_url(None) is None


def test_malformed_never_raises_and_strips_creds():
    # never raises, always returns a string
    for bad in ("::::garbage::::", "http://[::1", "not a url at all", "//"):
        assert isinstance(redact_url(bad), str)
    # even an odd-but-parseable authority drops the userinfo credential
    assert "s3cr3t" not in redact_url("ftp://user:s3cr3t@10.0.0.1/x")


def test_case_insensitive_key_match():
    out = redact_url("https://h/p?ApiKey=SECRETVAL&Access_Token=TOKENVAL")
    assert "SECRETVAL" not in out and "TOKENVAL" not in out
