# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""A device says honestly when it last read: a pushed (MQTT) device by its
last message, a meter-only device by its OLDEST live meter — never "now"."""
import time
from types import SimpleNamespace

from multibus import meter_only
from multibus.meter_only import MeterOnlyClient
from multibus.mqtt_input import MqttInputClient


def test_an_mqtt_device_reports_the_age_of_its_last_message():
    now = time.monotonic()
    fake = SimpleNamespace(connected=True, messages=12, updates=12, last_msg_ts=time.time() - 7.0,
                           last_msg_mono=now - 7.0, availability_topic=None, keepalive=None,
                           events=[], broker='192.0.2.5', port=1883, base_topic='x', available=None,
                           _subscriptions=lambda: ['x/#'])
    st = MqttInputClient.get_stats(fake)
    assert st['last_success_ts'] == fake.last_msg_ts
    assert 6.5 <= st['staleness_age_s'] <= 8.0


def test_an_mqtt_device_that_sent_nothing_has_no_age():
    fake = SimpleNamespace(connected=True, messages=0, updates=0, last_msg_ts=None, last_msg_mono=None,
                           availability_topic=None, keepalive=None, events=[], broker='192.0.2.5',
                           port=1883, base_topic='x', available=None, _subscriptions=lambda: ['x/#'])
    assert MqttInputClient.get_stats(fake)['staleness_age_s'] is None


def test_a_meter_only_device_is_as_old_as_its_oldest_meter(monkeypatch):
    now = time.time()
    seen = {'in': now - 2.0, 'out': now - 9.0}
    clients = {m: SimpleNamespace(get_stats=lambda m=m: {'last_success_ts': seen[m]}) for m in seen}
    monkeypatch.setattr(meter_only, 'find_client', clients.get)
    c = MeterOnlyClient.__new__(MeterOnlyClient)
    monkeypatch.setattr(MeterOnlyClient, '_meter_states', lambda self: {'in': True, 'out': True})
    h = c.data_health()
    assert h['last_success_ts'] == seen['out']            # the oldest, not now
    assert 8.5 <= h['staleness_age_s'] <= 10.0
    assert c.get_stats()['staleness_age_s'] == h['staleness_age_s'] or abs(
        c.get_stats()['staleness_age_s'] - h['staleness_age_s']) < 0.5


def test_a_meter_only_device_whose_meter_cannot_say_has_no_last_read(monkeypatch):
    clients = {'in': SimpleNamespace(get_stats=lambda: {'last_success_ts': time.time()}),
               'out': SimpleNamespace(get_stats=lambda: {})}
    monkeypatch.setattr(meter_only, 'find_client', clients.get)
    c = MeterOnlyClient.__new__(MeterOnlyClient)
    monkeypatch.setattr(MeterOnlyClient, '_meter_states', lambda self: {'in': True, 'out': True})
    h = c.data_health()
    assert h['last_success_ts'] is None and h['staleness_age_s'] is None
