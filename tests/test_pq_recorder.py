# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""PQ recorder: reason-bitmask decoding (validated against the Jasic
firmware's events.js), waveform channel selection, and the poller's
new-event / first-sync semantics."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from multibus.pq_recorder import (PqRecorder, alert_severity_for,
                                  decode_reason, supports_pq_recorder,
                                  waveform_channels_for)


# --- decode_reason ---------------------------------------------------------

def test_decode_single_ln_causes():
    assert decode_reason(0x1) == [("over_voltage_ln", "L1")]
    assert decode_reason(0x20) == [("under_voltage_ln", "L2")]
    assert decode_reason(0x400) == [("voltage_outage_ln", "L3")]
    assert decode_reason(0x8000) == [("over_current", "L4")]


def test_decode_ll_causes():
    assert decode_reason(0x100000) == [("over_voltage_ll", "L1-L2")]
    assert decode_reason(0x2000000) == [("under_voltage_ll", "L2-L3")]
    assert decode_reason(0x40000000) == [("voltage_outage_ll", "L3-L1")]


def test_decode_hi_half_rvc():
    # Observed live on the UMG512 (2026-08-27): reason_hi=0x200000 is a
    # rapid voltage change on L2, hi=0x1000000 is RVC on pair L1-L2.
    assert decode_reason(0x200000 << 32) == [("rapid_voltage_change_ln", "L2")]
    assert decode_reason(0x1000000 << 32) == [("rapid_voltage_change_ll", "L1-L2")]


def test_decode_frequency_and_multi():
    assert decode_reason(0x100 << 32) == [("over_frequency", "all")]
    assert decode_reason(0x1000 << 32) == [("under_frequency", "all")]
    assert decode_reason(0x10000000 << 32) == [
        ("rapid_voltage_change_multi", "all")]


def test_decode_combined_masks():
    got = decode_reason((0x200000 << 32) | 0x20)
    assert ("under_voltage_ln", "L2") in got
    assert ("rapid_voltage_change_ln", "L2") in got


def test_decode_unknown_mask_is_labelled_not_lost():
    assert decode_reason(0)[0][0].startswith("unknown_")


# --- waveform channel selection --------------------------------------------

def test_waveform_channels_voltage_and_current():
    assert waveform_channels_for([("under_voltage_ln", "L2")]) == ["UL2"]
    assert waveform_channels_for([("over_current", "L1")]) == ["IL1"]
    assert waveform_channels_for([("under_voltage_ll", "L2-L3")]) == ["UL2-L3"]


def test_waveform_channels_frequency_defaults_to_phases():
    assert waveform_channels_for([("under_frequency", "all")]) == \
        ["UL1", "UL2", "UL3"]


def test_waveform_channels_deduplicated():
    chans = waveform_channels_for([("under_voltage_ln", "L2"),
                                   ("rapid_voltage_change_ln", "L2")])
    assert chans == ["UL2"]


# --- template capability ----------------------------------------------------

def test_supports_pq_recorder():
    assert supports_pq_recorder("janitza_umg512_pro")
    assert supports_pq_recorder("JANITZA_UMG604")
    assert not supports_pq_recorder("eastron_sdm630")
    assert not supports_pq_recorder("")


# --- alert severity ----------------------------------------------------------

def test_alert_severity_mapping():
    assert alert_severity_for([("voltage_outage_ln", "L2")]) == "critical"
    assert alert_severity_for([("under_voltage_ll", "L1-L2")]) == "warning"
    assert alert_severity_for([("over_frequency", "all")]) == "warning"
    assert alert_severity_for([("rapid_voltage_change_ln", "L3")]) == "info"
    # outage wins over anything else in the same event
    assert alert_severity_for([("rapid_voltage_change_ln", "L3"),
                               ("voltage_outage_ll", "L1-L2")]) == "critical"


# --- poller semantics --------------------------------------------------------

def _make_recorder(tmp_path: Path, ring, counters=None) -> PqRecorder:
    rec = PqRecorder(
        device_id="jz", base_url="http://meter", poll_s=300,
        archive_waveforms=False, influx_bucket="janitza",
        influx_device_tag="janitza_umg512", mqtt_topic_prefix="janitza",
        get_influx=lambda: None, get_mqtt=lambda: None,
        event_log=MagicMock(), state_dir=tmp_path)
    payloads = {
        "/lib/events/getevt.html": {"events": ring},
        "/json.do?_EVT_COUNT%2C_FLAG_COUNT%2C_TRANS_COUNT%2C":
            counters or {"_EVT_COUNT": [10, ""], "_FLAG_COUNT": [0, ""],
                         "_TRANS_COUNT": [2, ""]},
    }
    rec._fetch_json = lambda path, timeout=15.0: json.loads(
        json.dumps(payloads[path]))
    return rec


EV1 = [1000.0, 1000.1, 20.0, 25.0, 1.0, 230.0, 0, 0x200000]
EV2 = [2000.0, 2000.5, 20.0, 25.0, 0.5, 231.0, 0x20, 0]


def test_first_sync_archives_but_does_not_announce(tmp_path):
    rec = _make_recorder(tmp_path, [EV1])
    rec._announce = MagicMock()
    rec._poll_once()
    rec._announce.assert_not_called()          # ring history is not "news"
    assert rec._high_water == 1000.0
    assert rec.ring()[0]["causes"] == [
        {"cause": "rapid_voltage_change_ln", "channel": "L2"}]


def test_new_event_after_sync_is_announced_once(tmp_path):
    rec = _make_recorder(tmp_path, [EV1])
    rec._announce = MagicMock()
    rec._poll_once()                            # first sync
    rec._fetch_json = _make_recorder(tmp_path, [EV1, EV2])._fetch_json
    rec._poll_once()                            # EV2 arrived
    assert rec._announce.call_count == 1
    assert rec._announce.call_args[0][0]["start"] == 2000.0
    rec._poll_once()                            # same ring again → no repeat
    assert rec._announce.call_count == 1


def test_high_water_persists_across_restart(tmp_path):
    rec = _make_recorder(tmp_path, [EV1])
    rec._poll_once()
    rec2 = _make_recorder(tmp_path, [EV1])      # same state_dir → same state file
    assert rec2._high_water == 1000.0
    rec2._announce = MagicMock()
    rec2._poll_once()                           # nothing newer → quiet
    rec2._announce.assert_not_called()


def test_new_event_fires_alert_with_severity(tmp_path):
    alerts = MagicMock()
    rec = _make_recorder(tmp_path, [EV1])
    rec._get_alerts = lambda: alerts
    rec._poll_once()                            # first sync — silent
    alerts.fire.assert_not_called()
    rec._fetch_json = _make_recorder(tmp_path, [EV1, EV2])._fetch_json
    rec._poll_once()                            # EV2 = under_voltage_ln L2
    alerts.fire.assert_called_once()
    sev, key, source, _msg = alerts.fire.call_args[0]
    assert sev == "warning" and key == "pq_jz" and source == "pq"


def test_counters_parsed(tmp_path):
    rec = _make_recorder(tmp_path, [EV1])
    rec._poll_once()
    assert rec.status()["counters"] == {"events": 10, "flags": 0,
                                        "transients": 2}
