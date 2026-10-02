#!/bin/sh
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
# SPDX-License-Identifier: AGPL-3.0-or-later
# Root only long enough to hand the mounted /data volume to the bridge user
# (bind mounts arrive with host ownership), then drop. --init-groups keeps
# the supplementary dialout membership the tty nodes require.
set -e
if [ "$(id -u)" = "0" ]; then
    chown bridge:bridge /data
    # --no-new-privs, like the gateway's entrypoint: compose sets
    # no-new-privileges too, but the image documents standalone `docker run`,
    # where this is the only place the drop is made final (audit 2026-10-01)
    exec setpriv --reuid=bridge --regid=bridge --init-groups --no-new-privs "$@"
fi
exec "$@"
