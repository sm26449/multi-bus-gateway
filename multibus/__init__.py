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
"""Multi-Bus Gateway package."""

__version__ = "3.101.0-rc3"
__author__ = "sm26449"

# No eager re-exports. Importing ``multibus`` (or any ``multibus.x``) must not
# drag in pymodbus / paho / influxdb_client, and ``from multibus import Config``
# etc. were used nowhere (grep: 0). Removing them breaks the only top-level
# import cycle.
# Import from the defining module instead: ``from multibus.core.config import Config``.
# ``__version__`` stays a plain literal — tools/demo/demo_up.sh seds this line
# and docs/releasing.md reads it with ``import multibus``.
