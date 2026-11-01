#!/bin/sh
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
#
# CAN end to end on a virtual bus:
#
#   tools/e2e/can_vcan/run.sh [pytest args]
#
#   MBG_TREE        the checkout to test (default: this one), mounted read-only
#   MBG_TEST_IMAGE  the test image (default mbg-test:local)
#   MBG_VCAN_SPEED  replay speed-up of the 2-minute capture (default 10)
#
# One throwaway container, --network none (its own network namespace: vcan0
# lives and dies with it), CAP_NET_ADMIN only to make vcan0 there — never a
# host interface. Creating it may autoload the kernel's can, can_raw and vcan
# modules on the host (harmless, no interface outside the container). Holds
# /tmp/mbg-pytest.lock so it never overlaps another suite.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
TREE=${MBG_TREE:-$(cd "$HERE/../../.." && pwd)}
IMAGE=${MBG_TEST_IMAGE:-mbg-test:local}
NAME=mbg-can-vcan-$$
if [ -z "${CANVCAN_LOCKED:-}" ]; then
  CANVCAN_LOCKED=1 exec flock /tmp/mbg-pytest.lock "$0" "$@"
fi
trap 'docker rm -f "$NAME" >/dev/null 2>&1 || true' EXIT INT TERM
[ -d "$TREE/multibus" ] || { echo "MBG_TREE=$TREE has no multibus/" >&2; exit 2; }
echo "== can_vcan: tree $TREE ($(git -C "$TREE" rev-parse --short HEAD 2>/dev/null || echo '?')), image $IMAGE"
docker run --rm --name "$NAME" --network none --cap-add NET_ADMIN --cpus=2 -u 0 \
  -e PYTHONDONTWRITEBYTECODE=1 -e MBG_VCAN=vcan0 -e MBG_VCAN_SPEED="${MBG_VCAN_SPEED:-10}" \
  -v "$TREE":/w:ro -w /w --entrypoint sh "$IMAGE" -c \
  'python tools/e2e/can_vcan/vcan.py add vcan0 && exec python -m pytest -q -p no:cacheprovider -rs tests/test_can_vcan_e2e.py "$@"' \
  can_vcan "$@"
