#!/bin/sh
# Root only long enough to hand the mounted /data volume to the bridge user
# (bind mounts arrive with host ownership), then drop. --init-groups keeps
# the supplementary dialout membership the tty nodes require.
set -e
if [ "$(id -u)" = "0" ]; then
    chown bridge:bridge /data
    exec setpriv --reuid=bridge --regid=bridge --init-groups "$@"
fi
exec "$@"
