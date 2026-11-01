#!/bin/sh
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# serial-access.sh — let the serial bridge container open this board's own
# UARTs (RS-485 HAT channels ttySC*, ttymxc*, ttyO*, real ttyS*), and nothing
# else. ADR-0016.
#
# The bridge runs with /dev read-only, non-root, every capability dropped,
# and a device cgroup that allows exact majors only. USB adapters (188, 166)
# and the Pi's PL011 UARTs (ttyAMA, 204) are allowed in the compose files
# already; an SC16IS7xx HAT gets a DYNAMIC major at boot (509 on a Pi 5), so
# compose cannot name it. This script reads the majors of the UARTs present
# from /sys/class/tty/*/dev and writes them into .env as four fixed slots the
# compose files read:
#
#   BRIDGE_TTY_1..BRIDGE_TTY_4 = "<major>:*", or "4:<minor>" for a real ttyS
#
# (major 4 also holds the virtual consoles tty0..63, so a ttyS port is
# granted by its exact minor, never the whole major). An unused slot n names
# the USB node 188:<n-1>, which 188:* allows anyway (compose wants the rules
# distinct). Run it again after a kernel update or a new HAT; it is
# idempotent.
#
#   sh scripts/serial-access.sh                    # .env here, docker-compose.yml
#   sh ../scripts/serial-access.sh --compose docker-compose.bridge.yml   # in serial-bridge/
#
# Options:
#   --env-file FILE  the .env to update (default: ./.env)
#   --compose FILE   the compose file, for the command printed at the end
#   --dry-run        print what would be written, change nothing
#   --root DIR       read sys/ and proc/ under DIR instead of / (tests)
#
# Exit status: 0 done, 1 refused (nothing written), 2 usage.
set -eu

ENV_FILE=.env
COMPOSE=
DRY=0
ROOT=${SERIAL_ACCESS_ROOT:-}
SLOTS=4
MARK='# serial-access.sh: board UART device rules (ADR-0016) — re-run after a kernel update'

die() { printf 'serial-access: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '/^#   sh scripts/,/^# Exit status/p' "$0" >&2; exit 2; }

while [ $# -gt 0 ]; do
    case $1 in
        --env-file) [ $# -ge 2 ] || usage; ENV_FILE=$2; shift 2 ;;
        --compose)  [ $# -ge 2 ] || usage; COMPOSE=$2; shift 2 ;;
        --root)     [ $# -ge 2 ] || usage; ROOT=$2; shift 2 ;;
        --dry-run)  DRY=1; shift ;;
        -h|--help)  usage ;;
        *) printf 'serial-access: unknown option %s\n' "$1" >&2; usage ;;
    esac
done

SYS=$ROOT/sys
[ -d "$SYS/class/tty" ] || die "no $SYS/class/tty — is this Linux with sysfs?"

# what a path resolves to, symlinks followed (POSIX: no readlink -f)
resolve() { (cd -P "$1" 2>/dev/null && pwd -P); }
attr() { [ -r "$1" ] && tr -d ' \n' <"$1" || true; }
unset_addr() { case $1 in ''|0|0x0|0x00000000) return 0 ;; esac; return 1; }
driver_of() { d=$(resolve "$1/device/driver") && printf '%s' "${d##*/}" || printf '%s' -; }

# the board's Bluetooth controller sits on this UART: a bluetooth node under it
# in the device tree, a serdev child with a bluetooth device, or a bound hci
# (the same checks as the bridge's)
is_bluetooth() {
    dev=$(resolve "$1/device") || return 1
    for b in "$dev"/of_node/bluetooth "$dev"/of_node/bluetooth@* \
             "$dev"/*/bluetooth "$dev"/*/*/bluetooth; do
        [ -d "$b" ] && return 0
    done
    for h in "$SYS"/class/bluetooth/*/device; do
        [ -e "$h" ] && [ "$(resolve "$h")" = "$dev" ] && return 0
    done
    return 1
}

# consoles: never grant a ttyS console by its minor
CONSOLES=" "
words() { [ -r "$1" ] && tr '\n' ' ' <"$1" || true; }
set -f                                                # no globbing of these words
for tok in $(words "$ROOT/proc/cmdline"); do
    case $tok in console=*) c=${tok#console=}; CONSOLES="$CONSOLES${c%%,*} " ;; esac
done
for c in $(words "$SYS/class/tty/console/active"); do CONSOLES="$CONSOLES$c "; done
set +f

RULES=
DRIVERS=                                              # one per rule, same order
FOUND=
REFUSED=
add_rule() {                                          # rule driver
    case " $RULES " in
        *" $1 "*) ;;
        *) RULES="${RULES:+$RULES }$1"; DRIVERS="${DRIVERS:+$DRIVERS }$2" ;;
    esac
}

for cls in "$SYS"/class/tty/*; do
    name=${cls##*/}
    case $name in
        ttySC*) num=${name#ttySC} ;;
        ttyAMA*) num=${name#ttyAMA} ;;
        ttymxc*) num=${name#ttymxc} ;;
        ttyO*) num=${name#ttyO} ;;
        ttyS*) num=${name#ttyS} ;;
        *) continue ;;
    esac
    case $num in ''|*[!0-9]*) continue ;; esac
    [ -e "$cls/device" ] || continue                  # no hardware behind it
    dev=$(attr "$cls/dev")
    case $dev in
        *[!0-9:]*|:*|*:|*:*:*|'') REFUSED="$REFUSED $name(bad dev '$dev')"; continue ;;
        *:*) ;;
        *) REFUSED="$REFUSED $name(bad dev '$dev')"; continue ;;
    esac
    major=${dev%:*} minor=${dev#*:}
    # it must be a tty-class character device, whatever its name says
    sub=$(resolve "$SYS/dev/char/$dev/subsystem") || sub=
    if [ "${sub##*/}" != tty ]; then
        REFUSED="$REFUSED $name($dev is not a tty device)"; continue
    fi
    if is_bluetooth "$cls"; then
        printf '  %-10s %-8s skipped: Bluetooth UART\n' "$name" "$dev"; continue
    fi
    if [ "$major" = 4 ]; then
        # 8250 placeholder with no hardware (PC ttyS0..31)
        typ=$(attr "$cls/type") port=$(attr "$cls/port") mem=$(attr "$cls/iomem_base")
        [ "$typ" = 0 ] && continue
        if unset_addr "$port" && unset_addr "$mem"; then continue; fi
        [ "$minor" -ge 64 ] || { REFUSED="$REFUSED $name(4:$minor is a virtual console)"; continue; }
        case $CONSOLES in *" $name "*) printf '  %-10s %-8s skipped: kernel console\n' "$name" "$dev"; continue ;; esac
        FOUND="$FOUND $name"
        add_rule "4:$minor" "$(driver_of "$cls")"
        continue
    fi
    case $major in
        5|136|137|138|139|140|141|142|143) REFUSED="$REFUSED $name(major $major is not a UART)"; continue ;;
    esac
    FOUND="$FOUND $name"
    case $major in 188|166|204) ;; *) add_rule "$major:*" "$(driver_of "$cls")" ;; esac   # already in compose
done

[ -z "$REFUSED" ] || die "refusing, nothing written:$REFUSED"

n=0
for _ in $RULES; do n=$((n + 1)); done
[ "$n" -le "$SLOTS" ] || die "these UARTs need $n device rules ($RULES); the compose files have $SLOTS slots — add BRIDGE_TTY_5.. there, or exclude some ports"

# the driver each slot was written for goes into BRIDGE_TTY_DRIVERS (the
# bridge names it when a kernel update leaves the slot stale) and a comment
BLOCK=$MARK
NOTES=
i=1
set -f                                                # one word per driver, split on purpose
# shellcheck disable=SC2086
set -- $DRIVERS
set +f
for r in $RULES; do
    BLOCK="$BLOCK
# serial-access.sh: BRIDGE_TTY_$i is for driver $1
BRIDGE_TTY_$i=$r"
    NOTES="${NOTES:+$NOTES,}$i:$1"
    shift; i=$((i + 1))
done
while [ "$i" -le "$SLOTS" ]; do BLOCK="$BLOCK
BRIDGE_TTY_$i=188:$((i - 1))"; i=$((i + 1)); done
BLOCK="$BLOCK
BRIDGE_TTY_DRIVERS=$NOTES"

printf 'Board UARTs found:%s\n' "${FOUND:- none}"
if [ -n "$RULES" ]; then
    printf 'Device rules to add:'; for r in $RULES; do printf " 'c %s rmw'" "$r"; done; printf '\n'
else
    printf 'No extra device rules needed (USB and ttyAMA are allowed already).\n'
fi

if [ "$DRY" = 1 ]; then
    printf '%s\n' "$BLOCK"
    exit 0
fi

# Atomic: the new .env is written beside the old one, with its owner and mode,
# and renamed over it — a failed write leaves the old file whole. A symlinked
# .env is updated at its target, the link kept.
target=$ENV_FILE
if [ -L "$target" ]; then
    target=$(readlink -f -- "$ENV_FILE") || die "cannot resolve the link $ENV_FILE; unchanged"
fi
dir=$(dirname -- "$target")
tmp=$(mktemp "$dir/.env.serial-access.XXXXXX") || die "cannot write in $dir; $ENV_FILE unchanged"
trap 'rm -f "$tmp"' EXIT
if [ -f "$target" ]; then
    cp -p -- "$target" "$tmp" || die "cannot copy $ENV_FILE; unchanged"   # owner + mode
    set +e
    grep -v -e '^BRIDGE_TTY_[0-9][0-9]*=' -e '^BRIDGE_TTY_DRIVERS=' -e '^# serial-access.sh:' \
        -- "$target" >"$tmp"
    rc=$?
    set -e
    [ "$rc" -le 1 ] || die "cannot read $ENV_FILE; unchanged"         # 1 = nothing kept
fi                                                     # else mktemp's 0600: .env holds secrets
printf '%s\n' "$BLOCK" >>"$tmp" || die "cannot write the new $ENV_FILE; unchanged"
mv -f -- "$tmp" "$target" || die "cannot replace $ENV_FILE; unchanged"
trap - EXIT
printf 'Wrote %s.\n' "$ENV_FILE"

case ${COMPOSE##*/} in
    docker-compose.bridge.yml) cmd="docker compose -f $COMPOSE up -d" ;;
    '') cmd="docker compose --profile rtu-bridge up -d serial-bridge" ;;
    *) cmd="docker compose -f $COMPOSE --profile rtu-bridge up -d serial-bridge" ;;
esac
printf 'Now recreate the bridge:\n  %s\n' "$cmd"
