# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
"""assemble / split of the config document to/from base + config.d/ (ADR-0047 P3): the tree is an
on-disk detail, so the invariant that matters is **assemble(split(x)) == x** — exact, with entity
LIST order preserved (the UI order) and empty lists omitted exactly as the writer omits them."""
from multibus.core.config.assemble import BASE, assemble, split


def _cfg(**over):
    data = {
        "config_version": "3.99.0",
        "primary_device": True,
        "identity_scope": "legacy",
        "modbus": {"host": "1.2.3.4", "port": 502},
        "mqtt": {"enabled": True, "broker": "x"},
        "security": {"allow_writes": False},
        "devices": [{"id": "b"}, {"id": "a"}, {"id": "c"}],     # deliberately NOT sorted — UI order
        "bridges": [{"id": "br1", "host": "127.0.0.1"}],
    }
    data.update(over)
    return data


def test_round_trip_is_exact():
    data = _cfg()
    assert assemble(split(data)) == data


def test_list_order_is_preserved_not_alphabetized():
    data = _cfg()
    out = assemble(split(data))
    assert [d["id"] for d in out["devices"]] == ["b", "a", "c"]   # not ["a","b","c"]


def test_split_emits_base_plus_one_file_per_entity():
    files = split(_cfg())
    assert BASE in files
    assert "config.d/device.b.yaml" in files and "config.d/device.a.yaml" in files
    assert "config.d/bridge.br1.yaml" in files
    # singletons live in base, entity lists do not
    assert files[BASE]["modbus"] == {"host": "1.2.3.4", "port": 502}
    assert "devices" not in files[BASE] and "bridges" not in files[BASE]


def test_empty_and_absent_lists_round_trip_to_nothing():
    # no devices key at all, and an explicitly empty bridges list
    data = {"modbus": {"host": "h"}, "mqtt": {"enabled": False}, "bridges": []}
    files = split(data)
    assert not any(p.startswith("config.d/") for p in files)     # no entity files
    assert assemble(files) == {"modbus": {"host": "h"}, "mqtt": {"enabled": False}}


def test_a_config_with_no_entities_is_just_base():
    data = {"modbus": {"host": "h"}, "security": {"allow_writes": True}}
    files = split(data)
    assert list(files) == [BASE] and assemble(files) == data


def test_endpoints_and_device_types_also_split():
    data = {"modbus": {"host": "h"}, "mqtt": {"enabled": True},
            "endpoints": [{"id": "ep1", "groups": []}],
            "device_types": [{"type": "custom_meter", "name": "X"}]}
    files = split(data)
    assert "config.d/endpoint.ep1.yaml" in files
    assert "config.d/device-type.custom_meter.yaml" in files   # device_type keyed by `type`
    assert assemble(files) == data


# ── P3-PATH-1: the file map is injective, duplicates survive, a missing file is said ─────

import pytest  # noqa: E402

from multibus.core.config.assemble import AssembleError, check, path_for  # noqa: E402


def test_kind_and_id_never_collide_across_kinds():
    """`device` + `type-foo` and `device-type` + `foo` used to map to the same file."""
    data = {"modbus": {"host": "h"},
            "devices": [{"id": "type-foo", "name": "a device"}],
            "device_types": [{"id": "foo", "labels": {"en": "a type"}}]}
    files = split(data)
    assert path_for("device", "type-foo") != path_for("device-type", "foo")
    assert len([p for p in files if p.startswith("config.d/")]) == 2
    assert assemble(files) == data


@pytest.mark.parametrize("a,b", [("Pump", "pump"), ("a/b", "a_b"), ("a.b", "a-b"), ("x y", "x%20y"),
                                 ("..", "%2e%2e"), ("é", "e"), ("~0", "0")])
def test_ids_that_differ_map_to_files_that_differ_on_any_filesystem(a, b):
    """Injective, and safe: lower-case only (a case-insensitive disk), no path separators,
    no dots — whatever the id holds."""
    pa, pb = path_for("device", a), path_for("device", b)
    assert pa != pb and pa.lower() != pb.lower()
    for p in (pa, pb):
        name = p[len("config.d/"):]
        assert "/" not in name and ".." not in name and name == name.lower()
        assert name.endswith(".yaml") and name.startswith("device.")


def test_duplicate_ids_round_trip_instead_of_collapsing():
    """A file with two entries of one id still loads today (the first wins). Splitting it must
    not lose the second, or assemble would hand back the first one twice."""
    data = {"modbus": {"host": "h"},
            "devices": [{"id": "a", "name": "first"}, {"id": "b"}, {"id": "a", "name": "second"}]}
    files = split(data)
    assert len([p for p in files if p.startswith("config.d/")]) == 3
    assert assemble(files) == data


def test_entries_without_an_id_and_non_dict_entries_round_trip():
    data = {"modbus": {"host": "h"},
            "devices": [{"name": "no id"}, "not-a-dict", {"id": "~0"}, {"id": "device-0"}]}
    files = split(data)
    assert len([p for p in files if p.startswith("config.d/")]) == 4
    assert assemble(files) == data


def test_an_id_too_long_for_a_file_name_is_refused_before_anything_is_written():
    with pytest.raises(AssembleError, match="too long"):
        split({"modbus": {"host": "h"}, "devices": [{"id": "x" * 300}]})


def test_a_listed_file_that_is_missing_is_named_not_a_keyerror():
    files = split(_cfg())
    del files[path_for("device", "a")]
    with pytest.raises(AssembleError, match="device.a.yaml"):
        assemble(files)


def test_check_reports_missing_and_unlisted_files():
    files = split(_cfg())
    assert check(files) == {"missing": [], "unlisted": []}
    del files[path_for("device", "a")]
    files["config.d/device.dropped-in.yaml"] = {"id": "dropped-in"}
    assert check(files) == {"missing": [path_for("device", "a")],
                            "unlisted": ["config.d/device.dropped-in.yaml"]}


def test_a_document_that_already_uses_the_index_key_is_refused():
    with pytest.raises(AssembleError, match="_layout_order"):
        split({"modbus": {"host": "h"}, "_layout_order": {"devices": ["x"]}})
