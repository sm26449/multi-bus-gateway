#!/bin/sh
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
#
# A SCADA must survive a gateway restart on its own: restart the gateway,
# leave FUXA alone, and check that every FUXA tag follows the gateway again
# (compare.py). Nothing is reprovisioned.
#
#   MBG_OPCUA_TOKEN=mbgst_... tools/fuxa/restart_check.sh [gateway container] [fuxa container]
set -eu
MBG=${1:-pv-stack-multi-bus-gateway}
FUXA=${2:-fuxa}
IMAGE=${MBG_TEST_IMAGE:-mbg-test:local}
: "${MBG_OPCUA_TOKEN:?set MBG_OPCUA_TOKEN (the service token)}"
cd "$(dirname "$0")/../.."

since=$(date +%s)              # epoch: no time zone to get wrong
echo "restarting $MBG…"
docker restart "$MBG" >/dev/null
i=0
until docker logs "$FUXA" --since "$since" 2>&1 | grep -q "restored"; do
  i=$((i + 1)); [ $i -lt 120 ] || { echo "FAIL FUXA did not reconnect in 4 min"; exit 1; }
  sleep 2
done
echo "FUXA reconnected; comparing (devices found late get up to 2 min)…"
exec docker run --rm --network host -v "$PWD":/srv -w /srv -e PYTHONDONTWRITEBYTECODE=1 -e MBG_OPCUA_TOKEN \
  "$IMAGE" python -W ignore tools/fuxa/compare.py --tries 40
