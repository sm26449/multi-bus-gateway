"""Phase 3: virtual meters are device-aware — a meter sources its values from
ONE device's live cache; absent `device` falls back to the primary (byte-
identical migration)."""
from multibus.virtual_meter_manager import VirtualMeterManager, make_provider


def _mgr():
    primary = {1: {"name": "_P", "value": 100, "timestamp": None}}
    dev2 = {1: {"name": "_P", "value": 999, "timestamp": None}}
    m = VirtualMeterManager(primary, device_values={"umg512": primary, "meter2": dev2},
                            primary_device_id="umg512")
    return m, primary, dev2


def test_store_for_selects_source_device():
    m, primary, dev2 = _mgr()
    # no device field → primary cache
    assert m._store_for({"template": "t"}) is primary
    # explicit primary → primary cache
    assert m._store_for({"template": "t", "device": "umg512"}) is primary
    # another device → that device's cache
    assert m._store_for({"template": "t", "device": "meter2"}) is dev2
    # unknown explicit device → EMPTY store (fail-safe), never the primary
    assert m._store_for({"template": "t", "device": "ghost"}) == {}


def test_inst_device_defaults_to_primary():
    m, _p, _d = _mgr()
    assert m._inst_device({"template": "t"}) == "umg512"
    assert m._inst_device({"template": "t", "device": "meter2"}) == "meter2"


def test_list_sources_per_device():
    m, _p, _d = _mgr()
    assert [s["value"] for s in m.list_sources()] == [100]          # primary
    assert [s["value"] for s in m.list_sources("meter2")] == [999]  # device 2


def test_provider_reads_named_value():
    _m, primary, _d = _mgr()
    prov = make_provider(primary)
    assert prov("_P") == (100, None)
    assert prov("_missing") is None


def test_store_for_never_falls_back_to_primary_for_explicit_device():
    """A vmeter whose explicit source device disappeared must serve NOTHING
    (fail-safe stale), never the primary's data (wrong meter to a consumer)."""
    m, primary, _d = _mgr()
    store = m._store_for({"template": "t", "device": "ghost"})
    assert store == {} and store is not primary
