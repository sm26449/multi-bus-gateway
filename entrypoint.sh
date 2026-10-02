#!/bin/sh
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>
# SPDX-License-Identifier: AGPL-3.0-or-later
# Root only long enough to hand the mounted config volume to the app user —
# a bind mount arrives with HOST ownership (root on a fresh install, so the
# non-root app could read but never write snapshots/saves) — then drop
# privileges for the actual process. Started with an explicit --user, there
# is nothing to fix up: exec straight through.
set -e
if [ "$(id -u)" = "0" ]; then
    # Only when the mount actually arrived with foreign ownership: a recursive
    # chown over a config dir full of snapshots/buffer files on EVERY start is
    # slow and wears SD cards — and, best-effort, so a deliberately read-only
    # config mount doesn't fail the boot (audit 2026-10-01).
    if [ "$(stat -c %u /app/config)" != "$(id -u mbg)" ]; then
        chown -R mbg:mbg /app/config 2>/dev/null \
            || echo "entrypoint: /app/config not chown-able (read-only mount?) — continuing" >&2
    fi
    # --no-new-privs: nothing this process starts can regain privilege (setuid
    # binaries, file capabilities) — the drop is final (3.80.0)
    exec setpriv --reuid=mbg --regid=mbg --init-groups --no-new-privs "$@"
fi
exec "$@"
