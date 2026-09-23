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
"""Identity-bearing files (passkeys, audit) are created 0600."""
import os
import stat


def test_passkeys_file_is_0600(tmp_path):
    from multibus.passkeys import PasskeyStore
    p = tmp_path / "passkeys.json"
    store = PasskeyStore(str(p))
    store._creds = [{"id": "x"}]
    store._save()
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600, oct(mode)


def test_audit_file_is_0600(tmp_path):
    from multibus.audit import AuditLog
    p = tmp_path / "audit.jsonl"
    log = AuditLog(str(p))
    log.append(user="admin", ip="10.0.0.1", action="test", target="x")
    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o600, oct(mode)


def test_write_leases_file_is_0600(tmp_path):
    from multibus.write_lease import WriteLeaseManager
    p = tmp_path / "write_leases.json"
    m = WriteLeaseManager(persist_path=p)
    m._leases[("d", "x", 1)] = {"meta": {"device": "d"}}
    m._persist_locked()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_audit_entries_are_mirrored_to_the_process_log(tmp_path, caplog):
    import logging
    from multibus.audit import AuditLog
    with caplog.at_level(logging.INFO, logger="multibus.audit"):
        AuditLog(str(tmp_path / "audit.jsonl")).append(user="u", ip="1.2.3.4", action="write", status="ok")
    assert any("AUDIT" in r.getMessage() and '"action": "write"' in r.getMessage() for r in caplog.records)
