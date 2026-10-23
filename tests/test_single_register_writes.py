"""protocol.single_register_writes: a device that knows no FC16 (the Delta
VFD-EL answers only FC3, FC6 and FC8) gets every holding write as one FC6 per
word, in order, through every path that writes (commands, setup, raw)."""
import pytest

from multibus.config import ModbusConfig
from multibus.device_template import validate_template
from multibus.modbus_client import ModbusClient, ModbusConnection


class _Ok:
    def isError(self):
        return False


class _Refused:
    def isError(self):
        return True

    def __str__(self):
        return "Exception Response(134, 6, IllegalDataAddress)"


class _Wire:
    """The pymodbus client's write calls, recorded."""
    def __init__(self, refuse_at=None):
        self.calls, self.refuse_at = [], refuse_at

    def write_register(self, address, value, device_id):
        self.calls.append(("fc6", address, value))
        return _Refused() if address == self.refuse_at else _Ok()

    def write_registers(self, address, values, device_id):
        self.calls.append(("fc16", address, list(values)))
        return _Ok()


@pytest.fixture
def conn(monkeypatch):
    c = ModbusConnection(ModbusConfig(host="127.0.0.1", port=1, unit_id=1))
    monkeypatch.setattr(c, "_ensure_connected", lambda: True)
    monkeypatch.setattr("multibus.modbus_client.bus_trace.trace.commit", lambda *a, **k: None)
    return c


def test_a_multi_word_write_goes_out_as_one_fc6_per_word(conn):
    conn.client = _Wire()
    conn.single_writes = True
    assert conn.write(0x2000, values=[6, 5000]) == (True, None)
    assert conn.client.calls == [("fc6", 0x2000, 6), ("fc6", 0x2001, 5000)]


def test_the_first_refusal_stops_the_rest(conn):
    conn.client = _Wire(refuse_at=0x2001)
    conn.single_writes = True
    ok, err = conn.write(0x2000, values=[6, 5000, 2])
    assert not ok and "IllegalDataAddress" in err
    assert conn.client.calls == [("fc6", 0x2000, 6), ("fc6", 0x2001, 5000)]   # 0x2002 never sent


def test_without_the_flag_nothing_changes(conn):
    conn.client = _Wire()
    assert conn.write(0x2000, values=[6, 5000]) == (True, None)
    assert conn.write(0x2000, values=[1], prefer_fc6=True) == (True, None)
    assert conn.client.calls == [("fc16", 0x2000, [6, 5000]), ("fc6", 0x2000, 1)]


def test_the_template_flag_reaches_the_connection_and_survives_a_reconnect():
    cli = ModbusClient(ModbusConfig(host="127.0.0.1", port=1), [], {}, single_writes=True)
    assert cli.connection.single_writes is True
    cli.pollers = []
    cli.start_polling = lambda: None
    from multibus import modbus_client as mc
    orig = mc.ModbusConnection.connect
    mc.ModbusConnection.connect = lambda self: False
    try:
        cli._reconnect_locked()
    finally:
        mc.ModbusConnection.connect = orig
    assert cli.connection.single_writes is True


def test_the_flag_is_a_boolean():
    def tpl(v):
        return {"device_template": {"id": "x_dev", "name": "X", "protocol": {"single_register_writes": v},
                                    "registers": [{"address": 1, "name": "a", "data_type": "uint16"}]}}
    assert not any("single_register_writes" in e for e in validate_template(tpl(True)))
    assert any("single_register_writes" in e for e in validate_template(tpl("yes")))
