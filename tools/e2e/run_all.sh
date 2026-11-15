#!/bin/bash
# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <stefan@multibus.dev>  — AGPL-3.0-or-later
#
# Run every script of tools/e2e once, ONE AT A TIME, each against a fresh
# throwaway gateway of the fixture it expects, and write one line per script:
#
#   PASS|FAIL|SKIP  <script>  <seconds>  <reason>
#
#   tools/e2e/run_all.sh                       # everything
#   RUN_ONLY=rules_e2e,dialogs_e2e tools/e2e/run_all.sh   (ONLY= still works, see below)
#   RUN_DIR=<dir> E2E_PORT=18701 E2E_TIMEOUT=900 tools/e2e/run_all.sh
#
# RUN_DIR (default ${TMPDIR:-/tmp}/mbg-e2e-all/<timestamp>) receives
# results.tsv, one <script>.log per script, the screenshots (OUT/SHOTS_DIR
# point there) and the fixtures' config dirs (fx/). Every gateway binds to
# 127.0.0.1 only, runs this checkout's ui/ and multibus/, reaches nothing but
# its own loopback (or a network of its own), and is capped at 2 CPUs; every
# container the runner (or a script it runs) starts is removed after each
# script. cmd_dispatch_live and can_vcan run through their own run.sh (both
# fully isolated, both hold /tmp/mbg-pytest.lock).
#
# Needs: node + tools/e2e/node_modules (playwright), chromium (CHROMIUM_PATH),
# docker, the image MBG_IMAGE (default multi-bus-gateway:latest).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
IMAGE=${MBG_IMAGE:-multi-bus-gateway:latest}
export MBG_IMAGE=$IMAGE
# one preflight for the whole run (which code the fixtures execute); the ~130 per-script
# fixture_up.sh calls then skip it (MBG_PREFLIGHT=0) so it is not repeated.
MBG_TREE=$REPO MBG_IMAGE=$IMAGE sh "$HERE/../preflight.sh" >&2 || exit 2
export MBG_PREFLIGHT=0
RUN=${RUN_DIR:-${TMPDIR:-/tmp}/mbg-e2e-all/$(date +%Y%m%d-%H%M%S)}
PORT=${E2E_PORT:-18701}
PORT2=$((PORT + 1))
TIMEOUT=${E2E_TIMEOUT:-900}
FX=mbg-e2eall            # the gateway; companions are $FX-<something>
FX2=mbg-e2eall-b         # the second gateway a few scripts want
NET=mbg-e2eall-net       # a docker network of our own (new_user, bridges)
PASS_QA=e2e-pass
export CHROMIUM_PATH=${CHROMIUM_PATH:-/root/.cache/ms-playwright/chromium-1117/chrome-linux/chrome}
export CHROME_PATH=${CHROME_PATH:-$CHROMIUM_PATH}
mkdir -p "$RUN/fx" "$RUN/shots"
RESULTS=$RUN/results.tsv
: > "$RESULTS"
URL=http://127.0.0.1:$PORT
URL2=http://127.0.0.1:$PORT2

log() { echo "[$(date +%H:%M:%S)] $*" >&2; }

# ── containers ───────────────────────────────────────────────────────────
ours() { docker ps -a --format '{{.Names}}' | grep -E "^($FX|$FX2)(-|$)|^mbg-e2e-mqtt$|^mbg-demo(-|$)" || true; }
down_all() {
  local n
  for n in $(ours); do
    case $n in mbg-demo*) "$REPO/tools/demo/demo_down.sh" >/dev/null 2>&1 ;; *) docker rm -f "$n" >/dev/null 2>&1 ;; esac
  done
  docker network rm "$NET" >/dev/null 2>&1 || true
}
# every container of ours that a script started without a cap gets one
cap_watch() {
  while :; do
    for n in $(ours); do
      [ "$(docker inspect -f '{{.HostConfig.NanoCpus}}' "$n" 2>/dev/null)" = 0 ] \
        && docker update --cpus 2 "$n" >/dev/null 2>&1
    done
    sleep 3
  done
}
wait_up() {   # URL
  local i
  for i in $(seq 1 60); do
    [ "$(curl -s -o /dev/null -w '%{http_code}' "$1/api/auth/status")" = 200 ] && { sleep 3; return 0; }
    sleep 2
  done
  return 1
}
# fx VARIANT [--writes] [--login P] — fixture_up.sh as the scripts document it
fx() { fx_named "$FX" "$PORT" "$@"; }
fx_named() {   # NAME PORT VARIANT [--writes] [--login P]
  local name=$1 port=$2 variant=$3; shift 3
  MBG_FX_NAME=$name TMPDIR=$RUN/fx "$HERE/fixture_up.sh" "$variant" "$port" "$@" || return 1
  docker update --cpus 2 "$name" >/dev/null 2>&1
  [ "$variant" = fronius ] && docker update --cpus 2 "$name-solar" >/dev/null 2>&1
  wait_up "http://127.0.0.1:$port"
}
# a config of our own: base.yaml + a variant or an extra file, then patches
#   mkcfg DIR [--variant V] [--extra F] [--rules F] [--writes] [--login P] [--set a.b=JSON]...
mkcfg() {
  local dir=$1; shift
  rm -rf "$dir"; mkdir -p "$dir"
  docker run --rm --user 0 --network none -v "$REPO":/repo:ro -v "$dir":/out --entrypoint python "$IMAGE" -c '
import json, shutil, sys, yaml
sys.path.insert(0, "/repo")
fx = "/repo/tools/e2e/fixtures/"
cfg = yaml.safe_load(open(fx + "installation_safety/base.yaml"))
a = sys.argv[1:]
while a:
    k = a.pop(0)
    if k == "--variant": cfg.update(yaml.safe_load(open(fx + "installation_safety/" + a.pop(0) + ".yaml")))
    elif k == "--extra": cfg.update(yaml.safe_load(open("/repo/" + a.pop(0))))
    elif k == "--rules": shutil.copy("/repo/" + a.pop(0), "/out/rules.yaml")
    elif k == "--writes": cfg["security"]["allow_writes"] = True
    elif k == "--login":
        from multibus.auth import hash_password
        cfg["ui"]["auth"].update(enabled=True, password=hash_password(a.pop(0)))
    elif k == "--set":
        path, val = a.pop(0).split("=", 1)
        node = cfg
        *parents, leaf = path.split(".")
        for p in parents: node = node.setdefault(p, {})
        if val == "DELETE": node.pop(leaf, None)
        else: node[leaf] = json.loads(val)
yaml.safe_dump(cfg, open("/out/config.yaml", "w"), sort_keys=False)
' "$@" || return 1
}
# start a gateway on a config dir made by mkcfg (as fixture_up.sh starts one)
gw() {   # NAME PORT CFGDIR
  docker rm -f "$1" >/dev/null 2>&1
  docker run -d --name "$1" --cpus 2 -p "127.0.0.1:$2:8080" -e UI_HOST=0.0.0.0 \
    -v "$3":/app/config -v "$REPO/ui":/app/ui:ro -v "$REPO/multibus":/app/multibus:ro \
    "$IMAGE" python main.py -c config/config.yaml >/dev/null || return 1
  wait_up "http://127.0.0.1:$2"
}
# the unchanged_save sandbox: base + config-extra + rules, writes on, auth off
us() { mkcfg "$RUN/fx/$FX" --extra tools/e2e/fixtures/unchanged_save/config-extra.yaml \
         --rules tools/e2e/fixtures/unchanged_save/rules.yaml --writes && gw "$FX" "$PORT" "$RUN/fx/$FX"; }
tap_feeder() {   # [TAP_UNITS]
  docker cp "$HERE/tap_feeder.py" "$FX:/tmp/tap_feeder.py" >/dev/null &&
  docker exec -d -u 10001 ${1:+-e TAP_UNITS=$1} "$FX" python /tmp/tap_feeder.py && sleep 3
}
bridge_sims() {  # ARGS… (each a quoted bridge_sims.py command line)
  docker cp "$HERE/sims/bridge_sims.py" "$FX:/tmp/bridge_sims.py" >/dev/null || return 1
  local a
  for a in "$@"; do
    docker exec -d -u 10001 "$FX" sh -c "python3 /tmp/bridge_sims.py $a > /tmp/br_$(echo "$a" | tr -c 'a-z0-9' _).log 2>&1"
  done
  sleep 3
}
own_net() { docker network create "$NET" >/dev/null 2>&1; docker network connect "$NET" "$FX"; }

# ── one script ───────────────────────────────────────────────────────────
record() {   # STATUS NAME SECONDS REASON
  printf '%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" >> "$RESULTS"
  printf '%-5s %-34s %5ss  %s\n' "$1" "$2" "$3" "$4"
}
skip() { record SKIP "$1" 0 "$2"; }
# run NAME TIMEOUT SETUP… -- ENV=… — SETUP is a shell snippet (may be empty)
run() {
  local name=$1 to=$2 setup=$3; shift 3
  local logf=$RUN/$name.log t0 rc st reason
  t0=$(date +%s)
  down_all
  mkdir -p "$RUN/shots/$name"
  (
    echo "== $name  setup: ${setup:-none}  env: $*"
    if [ -n "$setup" ] && ! eval "$setup"; then echo "SETUP FAILED"; exit 97; fi
    cd "$HERE" && env OUT="$RUN/shots/$name" SHOTS_DIR="$RUN/shots/$name" "$@" \
      timeout --kill-after=20 "$to" node "$name.mjs"
  ) >"$logf" 2>&1
  rc=$?
  for c in $(ours); do docker logs "$c" >"$RUN/$name.$c.docker.log" 2>&1; done
  down_all
  case $rc in
    0) # exit 0 with not one check is not a pass (E2E-398-F4)
       if grep -qE '^\s*(PASS|✓)' "$logf"; then st=PASS; reason=''
       else st=FAIL; reason='exit 0 but no check ran'; fi ;;
    97) st=FAIL; reason='fixture setup failed' ;;
    124|137) st=FAIL; reason="timeout after ${to}s" ;;
    *) st=FAIL; reason="exit $rc; $(grep -cE '^\s*(FAIL|✗)' "$logf") FAIL lines" ;;
  esac
  record "$st" "$name" "$(( $(date +%s) - t0 ))" "$reason"
}
# The runner's selector is RUN_ONLY. A few scripts (qa_397_conformance, device_etag,
# qa_397_sweep) read ONLY as their OWN filter, so a runner ONLY= leaked into them and
# they ran 0 checks and reported PASS (E2E-398-F4). ONLY= is still accepted for the
# runner, and is then taken out of the scripts' environment.
if [ -z "${RUN_ONLY:-}" ] && [ -n "${ONLY:-}" ]; then RUN_ONLY=$ONLY; fi
unset ONLY
want() { [ -z "${RUN_ONLY:-}" ] || [[ ",$RUN_ONLY," == *",$1,"* ]]; }
SEEN=" "                 # every script this file names (R or S), wanted or not
R() { SEEN="$SEEN$1 "; want "$1" && run "$@"; }
S() { SEEN="$SEEN$1 "; want "$1" && skip "$@"; }

trap 'kill $CAPPID 2>/dev/null; down_all' EXIT
trap 'exit 130' INT TERM
cap_watch & CAPPID=$!
log "run dir $RUN, image $IMAGE, tree $(git -C "$REPO" rev-parse --short HEAD), port $PORT"

# ── pure model tests (no gateway) ────────────────────────────────────────
for t in adddev_model_test bridge_diag_model_test bridge_ports_model_test cmd_binding_model_test \
         connform_can_model_test connform_model_test conn_step_model_test device_shell_model_test \
         device_store_model_test device_view_model_test lock_text_test security_model_test \
         serial_ports_model_test status_model_test template_transports_test ui_g_findings_test; do
  R $t 300 ''
done

# ── safety fixture, writes off ───────────────────────────────────────────
for t in installation_safety_e2e rules_e2e commands_e2e bridge_diag_e2e bridge_ports_e2e \
         bus_occupancy_e2e ov_tuning_e2e rules_tuning_e2e serial_ports_e2e pickers_e2e can_device_form_e2e identity_e2e; do
  R $t "$TIMEOUT" 'fx safety' MBG_URL=$URL ENDPOINT=sunfield
done
R unit_page_e2e "$TIMEOUT" 'fx safety' MBG_URL=$URL ENDPOINT=sunfield UNIT=sunfield-u1
R measurements_e2e "$TIMEOUT" 'fx safety' MBG_URL=$URL UNIT=sunfield-u1
R source_registers_e2e "$TIMEOUT" 'fx safety' MBG_URL=$URL DEVICE=sunfield-u1
R attributes_e2e "$TIMEOUT" 'fx safety' MBG_URL=$URL ATTR_DEVICE=sunfield-meter-240
R attributes_use_e2e "$TIMEOUT" 'fx safety' MBG_URL=$URL
R endpoint_outputs_e2e "$TIMEOUT" 'fx safety && tap_feeder 2,3' MBG_URL=$URL

# ── safety fixture, writes on ────────────────────────────────────────────
R device_page_e2e "$TIMEOUT" 'fx safety --writes' MBG_URL=$URL
R used_by_e2e "$TIMEOUT" 'fx safety --writes' MBG_URL=$URL
R installations_step3_e2e "$TIMEOUT" 'fx safety --writes && curl -s -XPOST -H "Content-Type: application/json" $URL/api/devices -d "{\"id\":\"pq1\",\"name\":\"PQ test\",\"template\":\"janitza_umg512_pro\",\"enabled\":false,\"connection\":{\"protocol\":\"tcp\",\"host\":\"127.0.0.1\",\"port\":15503,\"unit_id\":1}}"' MBG_URL=$URL
R findability_e2e 1800 'fx safety --writes && fx_named $FX2 $PORT2 safety --writes && python3 $HERE/fixtures/scale/load_scale.py $URL2 200 100' MBG_URL=$URL SCALE_URL=$URL2
R new_user_e2e 1800 'fx safety --writes && own_net' MBG_URL=$URL CTR=$FX DOCKER_NET=$NET

# ── "the sandbox" scripts: any auth-off gateway that may be written ──────
# (they once ran on a long-lived sandbox; a fresh safety --writes is that)
for t in build_banner_e2e canonical_unit_e2e mqtt_commands_e2e mqtt_device_form_e2e paste_check_e2e \
         security_settings_e2e status_page_e2e ux_gaps_e2e linked_meter_e2e rule_presets_e2e \
         template_advanced_e2e template_notes_e2e; do
  R $t "$TIMEOUT" 'fx safety --writes' MBG_URL=$URL CTR=$FX
done
R per_fc_e2e "$TIMEOUT" 'fx safety --writes' MBG_URL=$URL CTR=$FX
R bridges_e2e 1800 'fx safety --writes && own_net' MBG_URL=$URL CTR=$FX DOCKER_NET=$NET
R bridges_p3_e2e "$TIMEOUT" 'fx safety --writes' MBG_URL=$URL CTR=$FX
R battery_bank_display_e2e "$TIMEOUT" 'fx safety --writes && tap_feeder 2,3' MBG_URL=$URL CTR=$FX
R rtu_tap_device_e2e "$TIMEOUT" 'fx safety --writes && tap_feeder' MBG_URL=$URL CTR=$FX
R csp_smoke "$TIMEOUT" 'fx drive --login $PASS_QA' MBG_URL=$URL MBG_USER=admin MBG_PASS=$PASS_QA

# ── other fixture variants ───────────────────────────────────────────────
R plant_groups_e2e "$TIMEOUT" 'fx plant' MBG_URL=$URL ENDPOINT=sunfield
R endpoint_page_e2e "$TIMEOUT" 'fx endpoint' MBG_URL=$URL ENDPOINT=sunfield
R device_logs_e2e "$TIMEOUT" 'fx endpoint && sleep 20' MBG_URL=$URL DEVICE=sunfield-u1
R fronius_endpoint_e2e "$TIMEOUT" 'fx fronius' MBG_URL=$URL
R command_binding_e2e "$TIMEOUT" 'fx cmdui' MBG_URL=$URL
R device_types_e2e "$TIMEOUT" 'fx types' MBG_URL=$URL
for t in rules_trim_e2e rules_schedule_e2e rules_timers_e2e device_outputs_e2e influx_heartbeat_e2e opcua_output_e2e; do
  R $t "$TIMEOUT" 'fx drive' MBG_URL=$URL
done
for t in rules_layers_e2e sequences_e2e role_e2e opcua_e2e; do
  R $t "$TIMEOUT" 'fx drive --writes --login $PASS_QA' MBG_URL=$URL MBG_USER=admin MBG_PASS=$PASS_QA
done
R ws_session_gone_e2e "$TIMEOUT" 'fx drive --login $PASS_QA' MBG_URL=$URL MBG_USER=admin MBG_PASS=$PASS_QA MBG_CONTAINER=$FX

# ── qa fixture ───────────────────────────────────────────────────────────
for t in add_device_frictions_e2e add_device_new_e2e conn_step_e2e device_edit_modal_e2e device_view_e2e; do
  R $t "$TIMEOUT" 'fx qa --login $PASS_QA' MBG_URL=$URL MBG_PASS=$PASS_QA
done
R add_device_l39_e2e "$TIMEOUT" 'fx qa --login $PASS_QA' MBG_URL=$URL MBG_PASS=$PASS_QA CTR=$FX
R add_many_e2e "$TIMEOUT" 'fx qa --login $PASS_QA && bridge_sims "transparent 14197 1,2,3,4" "master 14196"' MBG_URL=$URL MBG_PASS=$PASS_QA MBG_FX_NAME=$FX
R undo_restore_e2e "$TIMEOUT" 'fx qa --login $PASS_QA' MBG_URL=$URL MBG_PASS=$PASS_QA FX_CFG=$RUN/fx/$FX
R l32b_write_paths_e2e "$TIMEOUT" 'fx qa' MBG_URL=$URL
R qa_397_conformance_e2e 1800 'fx qa && docker run -d --rm --cpus 2 --name $FX-broker --network container:$FX eclipse-mosquitto:2.1.2-alpine >/dev/null && sleep 5' MBG_URL=$URL QA_BROKER=$FX-broker
R device_etag_e2e 1800 'fx qa && fx_named $FX2 $PORT2 cmdui' MBG_URL=$URL CMD_URL=$URL2

# ── registered 2026-11-15 (E2E-HARNESS-1): each with the fixture its header names ──
for t in alertd_settings_e2e dashboard_drilldown_route_e2e ux13_nav_e2e; do
  R $t "$TIMEOUT" 'fx plant' MBG_URL=$URL
done
for t in command_ownership_lead_e2e device_overview_order_e2e f20_disclosure_e2e fleet_attention_strip_e2e \
         fleet_stable_dom_e2e fleet_stale_values_e2e humanize_keys_e2e mobile_device_identity_e2e \
         rules_action_menu_e2e rules_armed_honesty_e2e settings_apply_feedback_e2e state_block_e2e \
         status_issues_expand_e2e; do
  R $t "$TIMEOUT" 'fx drive' MBG_URL=$URL
done
R tap_bus_diag_e2e "$TIMEOUT" 'fx safety --writes' MBG_URL=$URL
R frontend_a11y_fixes_e2e "$TIMEOUT" 'fx safety --login $PASS_QA' MBG_URL=$URL MBG_USER=admin MBG_PASS=$PASS_QA
R vbms_crud_e2e "$TIMEOUT" 'fx vbms' MBG_URL=$URL
R vbms_arm_e2e "$TIMEOUT" 'fx vbms --writes' MBG_URL=$URL
R vbms_create_e2e "$TIMEOUT" 'MBG_FX_VCAN=1 fx vbmsdev' MBG_URL=$URL

# ── the unchanged_save sandbox ───────────────────────────────────────────
for t in unchanged_save_e2e broken_fixes_e2e connform_e2e connform_review_e2e devices_step3_e2e \
         dialogs_e2e rules_templates_step3_e2e; do
  R $t "$TIMEOUT" 'us' MBG_URL=$URL
done
R one_edit_e2e 1800 'us' MBG_URL=$URL

# ── a config of its own ──────────────────────────────────────────────────
# plant_wizard: auth off, no endpoints, loopback discovery allowed, the Solar
# API stand-in in the gateway's network namespace
R plant_wizard_e2e "$TIMEOUT" 'mkcfg $RUN/fx/$FX --set security.allow_nonlan_http_devices=true && gw $FX $PORT $RUN/fx/$FX && docker run -d --cpus 2 --name $FX-solar --network container:$FX -e FAKE_PORT=18099 -v $HERE/fake_solar_api.mjs:/fake/fake_solar_api.mjs:ro ${NODE_IMAGE:-node:24-slim} node /fake/fake_solar_api.mjs >/dev/null && sleep 3' MBG_URL=$URL FAKE=127.0.0.1:18099

# ── the demo gateway (its own network, demo broker and InfluxDB) ─────────
DEMO='TMPDIR=$RUN/fx $REPO/tools/demo/demo_up.sh $PORT && sleep 10'
R ui_g_findings_e2e "$TIMEOUT" "$DEMO" MBG_URL=$URL
# two devices of different templates with dashboard registers
R dashboard_fixes_e2e "$TIMEOUT" "$DEMO" MBG_URL=$URL DEV_A=ups-in DEV_B=pool-pump
# its simulated GX holds a battery and a sensor, and answers Find
R device_components_e2e "$TIMEOUT" "$DEMO" MBG_URL=$URL PARENT=victron-gx

# ── the self-contained harnesses ─────────────────────────────────────────
if want cmd_dispatch_live; then
  t0=$(date +%s)
  MBG_TREE=$REPO TMPDIR=$RUN/fx timeout 1800 "$HERE/cmd_dispatch_live/run.sh" --quick >"$RUN/cmd_dispatch_live.log" 2>&1
  rc=$?; record "$([ $rc = 0 ] && echo PASS || echo FAIL)" cmd_dispatch_live "$(( $(date +%s) - t0 ))" "$([ $rc = 0 ] || echo "exit $rc")"
fi
if want can_vcan; then
  t0=$(date +%s)
  MBG_TREE=$REPO timeout 1200 "$HERE/can_vcan/run.sh" >"$RUN/can_vcan.log" 2>&1
  rc=$?; record "$([ $rc = 0 ] && echo PASS || echo FAIL)" can_vcan "$(( $(date +%s) - t0 ))" "$([ $rc = 0 ] || echo "exit $rc")"
fi

# ── not run, and why ─────────────────────────────────────────────────────
S pq_e2e 'needs a real Janitza meter and a real InfluxDB (the old :8093 instance)'
S fuxa_ui_check 'drives the FUXA container (:1881) wired to the production gateway'
S capture_docs_screenshots 'a generator: rewrites docs/img from a demo gateway, not a test'
S ui_sweep 'a report (screenshots + report.json), not pass/fail'
S qa_397_sweep_e2e 'a report, not pass/fail; the full walk takes hours'
S fake_solar_api 'a helper (the Solar API stand-in), not a test'

# ── a script this file does not name is a FAIL, never a silent gap ───────
# (E2E-HARNESS-1: 21 scripts added by later lots were never run). Register it
# above with its fixture (R) or say why it is not run (S).
if [ -z "${RUN_ONLY:-}" ]; then
  for f in "$HERE"/*.mjs; do
    n=$(basename "$f" .mjs)
    case "$SEEN" in *" $n "*) ;; *) record FAIL "$n" 0 'not registered in run_all.sh (add R with its fixture, or S with why)' ;; esac
  done
fi

log "done: $(cut -f1 "$RESULTS" | sort | uniq -c | tr '\n' ' ')"
