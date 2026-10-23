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
"""Where the sun is: elevation and azimuth for a place and a moment.

The NOAA solar calculator (after Meeus), in pure Python with no dependency.
About 0.01° between latitudes ±72° for 1900–2100, a few microseconds a call.
That is far more than a rule needs ("is the sun above 5°?").

- **Elevation** is apparent (refraction included): what is seen. Sunrise
  and sunset are defined on the true, geometric elevation, at −0.833°
  (standard refraction plus the sun's radius); ``is_up`` uses that.
- **Azimuth** is in compass degrees: 0 north, 90 east, 180 south, 270 west.
- **Time** is UTC, so the installation's timezone does not matter here.
- **Polar day and night** give an elevation, never an error.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Tuple

SUNRISE_DEG = -0.833        # true (geometric) elevation at sunrise and sunset


def _refraction(e: float) -> float:
    """Atmospheric refraction in degrees for a true elevation ``e`` (NOAA)."""
    if e > 85.0:
        return 0.0
    t = math.tan(math.radians(e))
    if e > 5.0:
        r = 58.1 / t - 0.07 / t ** 3 + 0.000086 / t ** 5
    elif e > -0.575:
        r = 1735.0 + e * (-518.2 + e * (103.4 + e * (-12.79 + e * 0.711)))
    else:
        r = -20.772 / t
    return r / 3600.0


def position(lat: float, lon: float, when: datetime) -> Tuple[float, float]:
    """(apparent elevation, azimuth) in degrees for a place and a moment.
    A naive ``when`` is taken as UTC."""
    e_true, e_app, az = position3(lat, lon, when)
    return e_app, az


def is_up(lat: float, lon: float, when: datetime) -> bool:
    """Between sunrise and sunset (true elevation above −0.833°)."""
    return position3(lat, lon, when)[0] > SUNRISE_DEG


def position3(lat: float, lon: float, when: datetime) -> Tuple[float, float, float]:
    """(true elevation, apparent elevation, azimuth) in degrees."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    jd = when.timestamp() / 86400.0 + 2440587.5
    jc = (jd - 2451545.0) / 36525.0
    L = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    M = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    Mr = math.radians(M)
    C = (math.sin(Mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
         + math.sin(2 * Mr) * (0.019993 - 0.000101 * jc)
         + math.sin(3 * Mr) * 0.000289)
    omega = math.radians(125.04 - 1934.136 * jc)
    app_long = L + C - 0.00569 - 0.00478 * math.sin(omega)
    obliq0 = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    obliq = math.radians(obliq0 + 0.00256 * math.cos(omega))
    decl = math.asin(math.sin(obliq) * math.sin(math.radians(app_long)))
    y = math.tan(obliq / 2.0) ** 2
    Lr = math.radians(L)
    eq_time = 4.0 * math.degrees(
        y * math.sin(2 * Lr) - 2 * ecc * math.sin(Mr) + 4 * ecc * y * math.sin(Mr) * math.cos(2 * Lr)
        - 0.5 * y * y * math.sin(4 * Lr) - 1.25 * ecc * ecc * math.sin(2 * Mr))
    minutes = (when.hour * 60.0 + when.minute + when.second / 60.0 + when.microsecond / 6e7)
    tst = (minutes + eq_time + 4.0 * lon) % 1440.0
    ha = tst / 4.0 - 180.0                       # tst is in [0, 1440)
    latr, har = math.radians(lat), math.radians(ha)
    cos_zen = math.sin(latr) * math.sin(decl) + math.cos(latr) * math.cos(decl) * math.cos(har)
    zen = math.acos(max(-1.0, min(1.0, cos_zen)))
    true_elev = 90.0 - math.degrees(zen)
    elev = true_elev + _refraction(true_elev)
    true_out = true_elev
    denom = math.cos(latr) * math.sin(zen)
    if abs(denom) < 1e-12:                       # the sun at the zenith, or a pole
        az = 180.0 if lat > 0 else 0.0
    else:
        x = max(-1.0, min(1.0, (math.sin(latr) * math.cos(zen) - math.sin(decl)) / denom))
        a = math.degrees(math.acos(x))
        az = (a + 180.0) % 360.0 if ha > 0 else (540.0 - a) % 360.0
    return true_out, elev, az
