#!/bin/sh
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
#
# Start (or restart from scratch) a throwaway gateway on one of the
# installation_safety fixture variants, running this checkout's code.
#
#   tools/e2e/fixture_up.sh VARIANT PORT [--writes] [--login PASSWORD]
#
#   VARIANT  safety | plant | drive   (tools/e2e/fixtures/installation_safety/)
#   PORT     host port on 127.0.0.1 (the scripts' defaults: safety 18087,
#            plant 18085, drive 18090)
#
# The container is `mbg-fx-<VARIANT>` (or $MBG_FX_NAME); its config lives in a temp dir that is
# rebuilt on every start. Every address in the fixture is the container's own
# loopback, where nothing listens: nothing real is ever reached.
# Stop it with: docker rm -f mbg-fx-<VARIANT>
set -eu
VARIANT=$1 PORT=$2
shift 2
REPO=$(cd "$(dirname "$0")/../.." && pwd)
IMAGE=${MBG_IMAGE:-multi-bus-gateway:latest}
# MBG_FX_NAME runs a second gateway of the same variant beside the first
NAME=${MBG_FX_NAME:-mbg-fx-$VARIANT}
CFG=${TMPDIR:-/tmp}/$NAME

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run --rm --user 0 -v "$REPO":/repo:ro -v "$(dirname "$CFG")":/out --entrypoint python \
  "$IMAGE" /repo/tools/e2e/fixtures/installation_safety/load_fixture.py \
  "$VARIANT" "/out/$(basename "$CFG")" "$@"
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8080" -e UI_HOST=0.0.0.0 \
  -v "$CFG":/app/config \
  -v "$REPO/ui":/app/ui:ro -v "$REPO/multibus":/app/multibus:ro \
  "$IMAGE" python main.py -c config/config.yaml >/dev/null

for _ in $(seq 1 30); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/auth/status")" = 200 ] \
    && { echo "$NAME up on http://127.0.0.1:$PORT"; exit 0; }
  sleep 2
done
echo "$NAME did not answer on :$PORT — docker logs $NAME" >&2
exit 1
