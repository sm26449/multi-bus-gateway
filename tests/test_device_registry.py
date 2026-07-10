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
"""DeviceRegistry semantics — the exact behaviors the api.py sites relied on."""
from types import SimpleNamespace

from multibus.device_registry import DeviceRegistry


def dev(id, primary=False, template=""):
    return SimpleNamespace(id=id, primary=primary, template=template)


def make():
    primary_store = {}
    r = DeviceRegistry("umg512", primary_store)
    r.register(dev("umg512", primary=True), "CLIENT1")
    r.register(dev("em24"), "CLIENT2")
    return r, primary_store


def test_primary_store_is_alias():
    r, primary_store = make()
    assert r.store_for("umg512") is primary_store
    assert r.values["umg512"] is primary_store          # exposed map aliases too
    assert r.ensure_store("umg512") is primary_store


def test_primary_answers_even_unregistered_legacy_mode():
    primary_store = {1: "x"}
    r = DeviceRegistry("umg512", primary_store)          # no register() calls
    assert not r and len(r) == 0
    assert r.store_for("umg512") is primary_store        # legacy single-device


def test_find_and_has():
    r, _ = make()
    i, cfg, client = r.find("em24")
    assert i == 1 and cfg.id == "em24" and client == "CLIENT2"
    assert r.find("ghost") == (None, None, None)
    assert r.has("em24") and not r.has("ghost")


def test_store_isolation_and_ensure():
    r, primary_store = make()
    s = r.store_for("em24")
    assert s == {} and s is not primary_store
    s[100] = {"value": 1}
    assert r.ensure_store("em24") is s                   # setdefault: same dict
    assert r.store_for("ghost") is None                  # unknown → None (not {})


def test_replace_keeps_running_client_by_default():
    r, _ = make()
    r.replace("em24", dev("em24"))
    _i, cfg, client = r.find("em24")
    assert client == "CLIENT2"                           # kept
    r.replace("em24", dev("em24"), client="NEW")
    assert r.find("em24")[2] == "NEW"


def test_replace_missing_silent_unless_add_if_missing():
    r, _ = make()
    r.replace("ghost", dev("ghost"))                     # silent no-op
    assert not r.has("ghost")
    r.replace("ghost", dev("ghost"), client="C3", add_if_missing=True)
    assert r.find("ghost")[2] == "C3"
    assert r.store_for("ghost") == {}                    # store seeded on append


def test_remove_drops_pair_and_store():
    r, _ = make()
    r.store_for("em24")[5] = {"value": 9}
    r.remove("em24")
    assert not r.has("em24")
    assert r.store_for("em24") is None                   # store gone too
    r.remove("em24")                                     # idempotent


def test_resync_matches_clients_by_id_and_rebinds_primary():
    r, primary_store = make()
    # config._build_devices() rebuilt every DeviceConfig (new objects, same ids)
    rebuilt = [dev("umg512", primary=True), dev("em24"), dev("new-one")]
    r.resync(rebuilt, primary_client="PRIMARY_NEW")
    assert r.find("umg512")[2] == "PRIMARY_NEW"          # primary rebound
    assert r.find("em24")[2] == "CLIENT2"                # matched by id
    assert r.find("new-one")[2] is None                  # unknown id → no client
    assert [c.id for c, _ in r] == ["umg512", "em24", "new-one"]
    assert r.store_for("umg512") is primary_store        # alias survives resync
