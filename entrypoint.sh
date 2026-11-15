#!/bin/sh
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
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
    #
    # Opt-in (MBG_CAN_NET_ADMIN=1, with --cap-add NET_ADMIN on the container):
    # keep CAP_NET_ADMIN as an AMBIENT capability across the drop, so the
    # non-root app can switch a CAN interface's mode (ADR-0046 L2) via `ip` —
    # and ONLY that capability. Ambient caps survive --no-new-privs; nothing
    # else is gained. Off by default: the app stays fully unprivileged.
    AMBIENT=""
    if [ "${MBG_CAN_NET_ADMIN:-0}" = "1" ]; then
        AMBIENT="--inh-caps +net_admin --ambient-caps +net_admin"
    fi
    exec setpriv --reuid=mbg --regid=mbg --init-groups $AMBIENT --no-new-privs "$@"
fi
exec "$@"
