#!/bin/sh
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
#
# Start (or restart from scratch) a throwaway gateway on one of the
# installation_safety fixture variants, running this checkout's code.
#
#   tools/e2e/fixture_up.sh VARIANT PORT [--writes] [--login PASSWORD]
#
#   VARIANT  safety | plant | drive | cmdui | types | qa | endpoint | fronius | vbms
#            (tools/e2e/fixtures/installation_safety/)
#   PORT     host port on 127.0.0.1 (the scripts' defaults: safety 18087,
#            plant 18085, drive 18090)
#
# The container is `mbg-fx-<VARIANT>` (or $MBG_FX_NAME); its config lives in a temp dir that is
# rebuilt on every start. Every address in the fixture is the container's own
# loopback, where nothing listens: nothing real is ever reached.
# Stop it with: docker rm -f mbg-fx-<VARIANT>
# `fronius` also runs tools/e2e/fake_solar_api.mjs in a companion container
# `<name>-solar` (node, NODE_IMAGE) inside the gateway's network namespace: the
# Solar API stand-in answers on the gateway's 127.0.0.1:18099 and nowhere else.
# Stop both with: docker rm -f mbg-fx-fronius mbg-fx-fronius-solar
set -eu
VARIANT=$1 PORT=$2
shift 2
REPO=$(cd "$(dirname "$0")/../.." && pwd)
IMAGE=${MBG_IMAGE:-multi-bus-gateway:latest}
# MBG_FX_NAME runs a second gateway of the same variant beside the first
NAME=${MBG_FX_NAME:-mbg-fx-$VARIANT}
CFG=${TMPDIR:-/tmp}/$NAME

# Which code this gateway will actually run (tools/preflight.sh): it mounts this checkout's
# multibus/+ui/ but runs the IMAGE-baked main.py, so a changed main.py runs stale until the image
# is rebuilt. Fails fast on a wrong tree / absent image. MBG_PREFLIGHT=0 skips it (run_all.sh sets
# that and runs the preflight once, not per-script).
if [ "${MBG_PREFLIGHT:-1}" != 0 ]; then
  MBG_TREE="$REPO" MBG_IMAGE="$IMAGE" sh "$(dirname "$0")/../preflight.sh" >&2 \
    || { echo "fixture_up: preflight refused the run (see above)" >&2; exit 2; }
fi

docker rm -f "$NAME-solar" "$NAME" >/dev/null 2>&1 || true
docker run --rm --user 0 -v "$REPO":/repo:ro -v "$(dirname "$CFG")":/out --entrypoint python \
  "$IMAGE" /repo/tools/e2e/fixtures/installation_safety/load_fixture.py \
  "$VARIANT" "/out/$(basename "$CFG")" "$@"
# MBG_FX_VCAN=1 gives the container two throwaway vcan interfaces (vcan0, vcan1) in its own
# network namespace — for the Virtual BMS port-picker e2e. CAP_NET_ADMIN only to make them there;
# never a host interface. tools/ is mounted so vcan.py (no iproute2 needed) can run.
VCAN_RUN=""; VCAN_TOOLS=""
if [ "${MBG_FX_VCAN:-0}" = "1" ]; then
  VCAN_RUN="--cap-add NET_ADMIN"
  VCAN_TOOLS="-v $REPO/tools:/app/tools:ro"
fi
docker run -d --name "$NAME" -p "127.0.0.1:$PORT:8080" -e UI_HOST=0.0.0.0 \
  $VCAN_RUN \
  -v "$CFG":/app/config $VCAN_TOOLS \
  -v "$REPO/ui":/app/ui:ro -v "$REPO/multibus":/app/multibus:ro \
  "$IMAGE" python main.py -c config/config.yaml >/dev/null
if [ "${MBG_FX_VCAN:-0}" = "1" ]; then
  for i in 0 1; do
    docker exec "$NAME" python /app/tools/e2e/can_vcan/vcan.py add "vcan$i" >/dev/null 2>&1 \
      || echo "warning: could not create vcan$i (CAP_NET_ADMIN? vcan module?)" >&2
  done
fi
if [ "$VARIANT" = fronius ]; then
  docker run -d --name "$NAME-solar" --network "container:$NAME" -e FAKE_PORT=18099 \
    -v "$REPO/tools/e2e/fake_solar_api.mjs":/fake/fake_solar_api.mjs:ro \
    "${NODE_IMAGE:-node:24-slim}" node /fake/fake_solar_api.mjs >/dev/null
fi

for _ in $(seq 1 30); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/auth/status")" = 200 ] \
    && { echo "$NAME up on http://127.0.0.1:$PORT"; exit 0; }
  sleep 2
done
echo "$NAME did not answer on :$PORT — docker logs $NAME" >&2
exit 1
