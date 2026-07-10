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
"""Per-attempt communication-error taxonomy: timeout vs exception_N vs connection."""
from pymodbus.exceptions import ConnectionException, ModbusIOException
from pymodbus.pdu import ExceptionResponse

from multibus import modbus_client as mc
from multibus.config import ModbusConfig


def test_classify_error_kinds():
    assert mc._classify_error(ExceptionResponse(3, 2)) == "exception_2"
    assert mc._classify_error(ExceptionResponse(3, 11)) == "exception_11"
    assert mc._classify_error(ModbusIOException("no response")) == "timeout"
    assert mc._classify_error(TimeoutError("timeout")) == "timeout"
    assert mc._classify_error(ConnectionException("Failed to connect")) == "connection"
    assert mc._classify_error(OSError("Connection reset by peer")) == "connection"
    assert mc._classify_error(ValueError("weird framer state")) == "other"


def test_count_and_snapshot():
    conn = mc.ModbusConnection(ModbusConfig())
    conn._count_error(ExceptionResponse(3, 2))
    conn._count_error(ExceptionResponse(3, 2))
    conn._count_error(ModbusIOException("no response"))
    snap = conn.snapshot_errors()
    assert snap == {"exception_2": 2, "timeout": 1}
    snap["exception_2"] = 99                     # copie, nu referință
    assert conn.snapshot_errors()["exception_2"] == 2


class _ExcClient:
    """Always answers with a Modbus exception (e.g. a merged-block read on a
    gapped slave)."""
    def connect(self):
        return True
    def close(self):
        pass
    def is_socket_open(self):
        return True
    def read_holding_registers(self, address, count, slave):
        return ExceptionResponse(3, 2)


def test_read_path_counts_each_attempt(monkeypatch):
    monkeypatch.setattr(mc, "_build_client", lambda cfg: _ExcClient())
    cfg = ModbusConfig(retry_attempts=3, retry_delay=0.0)
    conn = mc.ModbusConnection(cfg)
    assert conn.connect() is True
    assert conn.read_registers(100, 2) is None
    # fiecare attempt a lovit firul → 3 în taxonomie, dar UN singur failed_read
    assert conn.snapshot_errors() == {"exception_2": 3}
    assert conn.failed_reads == 1


def test_stats_expose_error_counts(monkeypatch):
    monkeypatch.setattr(mc, "_build_client", lambda cfg: _ExcClient())
    client = mc.ModbusClient(config=ModbusConfig(retry_attempts=1, retry_delay=0.0),
                             registers=[], poll_groups={}, device_id="t1")
    client.connect()
    client.connection.read_registers(0, 2)
    assert client.get_stats()["error_counts"] == {"exception_2": 1}
