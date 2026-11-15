# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
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
"""The device-driver contract (ADR-0033).

Eight client classes read devices (`ModbusClient`, `HttpClient`,
`MqttInputClient`, `CanClient`, `RtuTapClient`, `SeplosV2Client`,
`MeterOnlyClient`, `MultiSourceClient`). Before this module the interface they
share existed only in part and had no name: nothing checked it, so a new
transport that misspelled a method lost read-back, health or reload with no
error, and capabilities were discovered by probing (59 ``hasattr`` calls).

`DeviceDriver` names the **common contract** every caller uses today — not the
union of the per-client variants. Optional capabilities a client may also offer
(register reads, keepalive, a preview snapshot) are separate, declared
Protocols, so a consumer asks ``isinstance(drv, RegisterReadable)`` instead of
``hasattr(drv, 'read_register')``.

These are `typing.Protocol`s, not a base class: a client is a driver because it
satisfies the contract, not because it inherits from anything (ADR-0033 §5,
"no base class"). `runtime_checkable` lets a conformance test and the consumers
check membership with ``isinstance``; that proves the members exist, and
`tests/test_driver_protocol.py` additionally pins their call shapes and
behaviour.
"""
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class DeviceDriver(Protocol):
    """The contract every device client satisfies (ADR-0033 §1).

    Members are the common nine. Their signatures are what every caller uses
    today; a client whose own signature is wider (an extra optional keyword, a
    ``*a, **kw`` catch-all) still satisfies it.
    """

    #: True while the client's transport is up. A plain attribute or a property.
    connected: bool
    #: Where the client delivers ``(poll_group, data)`` batches; set by the
    #: runtime after construction. A plain attribute or a property.
    publish_callback: Optional[Callable]

    def connect(self) -> bool:
        """Open the transport once; return whether it came up. Pollers retry."""
        ...

    def disconnect(self) -> None:
        """Stop delivering and release the transport."""
        ...

    def start_polling(self) -> None:
        """Begin delivering values. A push client (MQTT, CAN) is already
        receiving, so its implementation is a documented no-op."""
        ...

    def update_registers(self, registers: list, poll_groups: Optional[dict] = None) -> None:
        """Swap the register set (and, when given, the poll groups). ``None``
        poll groups means "keep the current ones"."""
        ...

    def reload_registers(self) -> None:
        """Re-apply the register set already given (restart the pollers with it).
        A client with nothing to re-apply implements it as a documented no-op."""
        ...

    def data_health(self, stale_threshold_s: Optional[float] = None) -> Dict[str, Any]:
        """Acquisition-pipeline health. ``None`` means "the client's own default
        staleness threshold". Returns at least a ``status`` key."""
        ...

    def get_stats(self) -> Dict[str, Any]:
        """Observability counters for the status API."""
        ...


@runtime_checkable
class RegisterReadable(Protocol):
    """A client that answers ad-hoc register reads and an on-demand poll
    (Modbus today). Consumers that read a single register or force a poll ask
    for this instead of probing ``hasattr(drv, 'read_register')``."""

    def read_register(self, *args: Any, **kwargs: Any) -> Any: ...
    def read_registers_batch(self, registers: list) -> Dict[int, Any]: ...
    def poll_now(self, *args: Any, **kwargs: Any) -> Any: ...


@runtime_checkable
class KeepaliveCapable(Protocol):
    """A client that sends an application keepalive and can block until a
    predicate holds (the MQTT input today)."""

    def send_keepalive(self, first: bool = False) -> bool: ...
    def wait_for(self, predicate: Callable, timeout: float) -> bool: ...


@runtime_checkable
class PreviewSnapshot(Protocol):
    """A client that exposes a public point-in-time snapshot of its decoded
    state, so preview/diagnostics never read private attributes (ADR-0033 §2,
    the public accessor replacing the ``_lock``/``.words`` reads; adopted in
    the Q1-A12 lot)."""

    def snapshot(self, *args: Any, **kwargs: Any) -> Any: ...
