#!/bin/sh
# Root only long enough to hand the mounted config volume to the app user —
# a bind mount arrives with HOST ownership (root on a fresh install, so the
# non-root app could read but never write snapshots/saves) — then drop
# privileges for the actual process. Started with an explicit --user, there
# is nothing to fix up: exec straight through.
set -e
if [ "$(id -u)" = "0" ]; then
    chown -R mbg:mbg /app/config
    # --no-new-privs: nothing this process starts can regain privilege (setuid
    # binaries, file capabilities) — the drop is final (3.80.0)
    exec setpriv --reuid=mbg --regid=mbg --init-groups --no-new-privs "$@"
fi
exec "$@"
