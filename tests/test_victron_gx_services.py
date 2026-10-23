"""A Victron GX's batteries (monitor or BMS) and temperature sensors: the
components of its template, each read from the GX's MQTT by instance. The payloads are what a GX with a
SmartShunt, a Seplos BMS on VE.Can and RuuviTags sent (the portal id is not
the real one)."""
import json
from types import SimpleNamespace

import pytest

from multibus import mqtt_input as mi
from multibus.config import SelectedRegister
from multibus.device_template import TemplateRegistry
from multibus.display import active_alarms
from multibus import expressions as ex

PORTAL = "c0ffee000001"


class _Paho:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.sent.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=0)


def _source(tpl, **vars_):
    regs = [SelectedRegister(address=r.address, name=r.name, label=r.label, unit=r.unit,
                             data_type=r.data_type, poll_group='normal', json_path=r.json_path,
                             topic=r.topic, enum=r.enum)
            for r in tpl.registers]
    src = mi.MqttInputClient({'broker': 'gx', 'topic': '', 'vars': {'portal': PORTAL, **vars_},
                              'keepalive': tpl.protocol.get('keepalive')}, regs)
    src._client, src.connected = _Paho(), True
    store = {}

    def keep(_pg, data):
        for e in data.values():
            store[e['register'].name] = e['value']
    src.publish_callback = keep
    return src, store


def _say(src, path, value):
    src._on_message(None, None, SimpleNamespace(topic=f"N/{PORTAL}/{path}",
                                                payload=json.dumps({'value': value}).encode(), retain=False))


@pytest.fixture(scope='module')
def reg():
    return TemplateRegistry()


def test_a_smartshunt_reads_its_state_and_its_history(reg):
    t = reg.get('victron_gx_mqtt:battery')
    assert t is not None and t.kind == 'battery'
    src, store = _source(t, instance='288')
    for path, v in [("Soc", 85.0), ("Dc/0/Power", -968.24), ("Dc/0/Voltage", 53.2), ("Capacity", 2240.0),
                    ("ConsumedAmphours", -377.5), ("TimeToGo", 230219.98), ("History/ChargeCycles", 63),
                    ("History/ChargedEnergy", 17245.59), ("Alarms/LowSoc", 0),
                    ("ProductName", "SmartShunt 1000A/50mV"), ("Dc/0/Temperature", None)]:
        _say(src, f"battery/288/{path}", v)
    assert store['soc'] == 85.0 and store['battery_power'] == pytest.approx(-968.24)
    assert store['cycles'] == 63 and store['charged_energy'] == pytest.approx(17245.59)
    assert store['product_name'] == "SmartShunt 1000A/50mV"           # text arrives as text
    assert 'battery_temperature' not in store                         # a null is no value
    # another instance's topic is not this device's
    _say(src, "battery/512/Soc", 10.0)
    assert store['soc'] == 85.0


def test_a_bms_on_the_gx_reads_its_cells_limits_and_alarms(reg):
    t = reg.get('victron_gx_mqtt:battery')
    src, store = _source(t, instance='512')
    for path, v in [("Soc", 83.0), ("Soh", 98.0), ("Dc/0/Voltage", 53.21), ("Dc/0/Current", -18.2),
                    ("Info/MaxChargeCurrent", 940.0), ("Info/MaxChargeVoltage", 57.6),
                    ("System/MinCellVoltage", 3.324), ("System/MaxCellVoltage", 3.328),
                    ("System/MinVoltageCellId", "Pack-07#"), ("System/NrOfModulesOnline", 8),
                    ("Alarms/CellImbalance", 1), ("Alarms/InternalFailure", 2), ("Alarms/LowVoltage", 0),
                    ("CustomName", "SEPLOS V4")]:
        _say(src, f"battery/512/{path}", v)
    assert store['charge_current_limit'] == 940.0 and store['modules_online'] == 8
    assert store['min_voltage_cell'] == "Pack-07#" and store['custom_name'] == "SEPLOS V4"
    # the declared alarms: a warning and a danger, nothing for a 0
    entries = [{'name': k, 'value': v, 'label': k} for k, v in store.items()]
    act = active_alarms(t.display, entries, fresh=None)
    assert (act['warning'], act['danger']) == (1, 1)
    assert {a['field'] for a in act['active']} == {'alarm_cell_imbalance', 'alarm_internal_failure'}
    # the template's own calculation: the cell spread in mV
    calc = {c.name: c for c in t.calculated}
    assert ex.evaluate(calc['cell_delta'].expr, store.get) == pytest.approx(4.0, abs=0.01)


def test_a_ruuvitag_on_the_gx(reg):
    t = reg.get('victron_gx_mqtt:temperature')
    assert t is not None and t.kind == 'sensor'
    src, store = _source(t, instance='23')
    for path, v in [("Temperature", 11.405), ("Humidity", 82.21), ("Pressure", 1014.99),
                    ("BatteryVoltage", 2.679), ("CustomName", "ruuvi_outside"), ("TemperatureType", 2),
                    ("Alarms/LowBattery", 0)]:
        _say(src, f"temperature/23/{path}", v)
    assert store['ambient_temp'] == pytest.approx(11.405) and store['humidity'] == pytest.approx(82.21)
    assert store['air_pressure'] == pytest.approx(1014.99) and store['custom_name'] == "ruuvi_outside"
    _say(src, "temperature/22/Temperature", 30.0)                    # a neighbour sensor
    assert store['ambient_temp'] == pytest.approx(11.405)


def test_both_keep_the_gx_talking_and_never_write(reg):
    for tid, var in (('victron_gx_mqtt:battery', {'instance': '288'}), ('victron_gx_mqtt:temperature', {'instance': '22'})):
        t = reg.get(tid)
        assert not getattr(t, 'commands', None)
        assert not [r for r in t.registers if getattr(r, 'write_topic', '')]
        src, _ = _source(t, **var)
        src._on_connect(src._client, None, None, 0)
        assert src._client.sent[-1][0] == f"R/{PORTAL}/keepalive"


def test_the_components_are_the_gx_templates_and_not_offered_alone(reg):
    gx = reg.get('victron_gx_mqtt')
    assert [c['type'] for c in gx.summary()['components']] == ['battery', 'temperature']
    assert all(not t.component_of for t in reg.list())
    b = reg.get('victron_gx_mqtt:battery')
    assert b.component_of == 'victron_gx_mqtt' and b.protocol == gx.protocol
    assert b.component['discover']['topic'] == 'N/{portal}/battery/+/ProductName'
    assert reg.get('victron_gx_battery_mqtt') is None and reg.get('victron_gx_temperature_mqtt') is None
