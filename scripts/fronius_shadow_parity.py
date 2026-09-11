# Multi-Bus Gateway — multi-protocol Modbus/HTTP/MQTT acquisition gateway.
# Copyright (C) 2024-2026 Stefan Maldaianu <sm26449@diysolar.ro>  — AGPL-3.0-or-later
"""Fronius shadow-parity harness (migration phase F1).

Compares the LIVE collector's MQTT tree (``fronius/...``) against the MBG
shadow tree (``mbg/fronius/...``) topic-for-topic, continuously:

- every shadow sample is paired with the live side's latest value for the
  same leaf (and vice versa) when both are fresh (≤ PAIR_MAX_AGE apart);
- agreement is judged per leaf class: EXACT for statuses/identity/counts,
  0.2% for energy counters, and a rate-aware tolerance for moving
  measurements (the two collectors sample seconds apart, so instantaneous
  equality is not the bar — bounded divergence is);
- PF leaves are tracked but EXEMPT (documented Fronius PF scaling quirk —
  raw ±10000 with SF −2: MBG generic ±100 vs collector's forced ±1.0);
- coverage: for each minute, did the shadow publish at least one W sample?

Hourly summaries append to ``<out>/parity-YYYYMMDD.jsonl`` and print to
stdout. ``--report`` summarizes all JSONL files against the F1 gate
(≥ 99.9% agreement AND ≥ 99% coverage over ≥ 7 days).

Run (detached, next to the stack):
  docker run -d --name fronius-shadow-parity --restart unless-stopped \\
    --network host -v /home/user/multi-bus-gateway/scripts:/scr:ro \\
    -v /docker-storage/pv-stack/multi-bus-gateway/shadow-parity:/out \\
    -e MQTT_HOST=localhost -e MQTT_USER=admin -e MQTT_PASS=... \\
    --entrypoint python multi-bus-gateway:latest -u /scr/fronius_shadow_parity.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict

PAIR_MAX_AGE = 45.0          # seconds between the two sides' samples
SUMMARY_EVERY = 3600         # hourly

# (live prefix, shadow prefix) pairs under comparison
PAIRS = [
    ("fronius/inverter/1/", "mbg/fronius/inverter/1/"),
    ("fronius/meter/240/", "mbg/fronius/meter/240/"),
]

EXACT = {"St", "StVnd", "serial_number", "model", "manufacturer",
         "mppt/num_modules"}
ENERGY_SUBSTR = ("WH", "TotWh", "DCWH")
# leaves published by only one side (collector derives them / MBG SF regs)
IGNORE = {"status", "alarm", "active", "events", "availability",
          "runtime", "device_info", "num_inverters", "heartbeat"}
PF_LEAVES = {"PF", "PFphA", "PFphB", "PFphC"}

# absolute tolerance floors by leaf prefix (moving measurements; both sides
# sample the same register seconds apart)
FLOORS = [
    (("W", "DCW", "VA", "Wph", "VAph"), 800.0),   # power flickers with clouds
    (("VAr", "VAR"), 400.0),                       # reactive hovers near zero
    (("A", "Aph", "DCA"), 4.0),
    (("PPV", "PhV", "DCV"), 5.0),
    (("Hz",), 0.15),
    (("Tmp", "mppt/string"), 800.0),               # strings carry power/temp mix
]
REL = 0.10                                          # 10% relative, whichever larger


def leaf_class(leaf: str):
    if leaf in PF_LEAVES:
        return "pf"
    if leaf in EXACT:
        return "exact"
    if any(s in leaf for s in ENERGY_SUBSTR):
        return "energy"
    return "moving"


def floor_for(leaf: str) -> float:
    best = 3.0
    for prefixes, fl in FLOORS:
        for p in prefixes:
            if leaf == p or leaf.startswith(p):
                return fl
    return best


def agree(leaf: str, a, b) -> bool:
    cls = leaf_class(leaf)
    if cls == "exact":
        return str(a) == str(b)
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)
    if cls == "energy":
        return abs(fa - fb) <= max(0.002 * max(abs(fa), abs(fb)), 300)
    return abs(fa - fb) <= max(REL * max(abs(fa), abs(fb)), floor_for(leaf))


class Stats:
    def __init__(self):
        self.match = defaultdict(int)
        self.mismatch = defaultdict(int)
        self.last_mismatch = {}
        self.cover_minutes = set()      # minutes with a shadow W sample
        self.first_ts = time.time()

    def summary(self):
        total_m = sum(self.match.values())
        total_x = sum(self.mismatch.values())
        rate = total_m / max(total_m + total_x, 1)
        now = time.time()
        elapsed_min = max(int((now - self.first_ts) / 60), 1)
        return {
            "ts": int(now),
            "window_min": elapsed_min,
            "pairs_matched": total_m,
            "pairs_mismatched": total_x,
            "agreement": round(rate, 6),
            "coverage": round(len(self.cover_minutes) / elapsed_min, 4),
            "worst": sorted(
                ({"leaf": k, "mismatch": v, "match": self.match.get(k, 0),
                  "last": self.last_mismatch.get(k)}
                 for k, v in self.mismatch.items()),
                key=lambda d: -d["mismatch"])[:10],
        }


def run():
    import paho.mqtt.client as mqtt
    host = os.environ.get("MQTT_HOST", "localhost")
    port = int(os.environ.get("MQTT_PORT", "1883"))
    user = os.environ.get("MQTT_USER", "")
    pw = os.environ.get("MQTT_PASS", "")
    outdir = os.environ.get("OUT_DIR", "/out")
    os.makedirs(outdir, exist_ok=True)

    # last[(pair_idx, side, leaf)] = (value, ts)
    last = {}
    stats = Stats()
    pf_stats = Stats()

    def on_msg(_c, _u, msg):
        now = time.time()
        try:
            payload = msg.payload.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return
        for i, (lp, sp) in enumerate(PAIRS):
            side, leaf = None, None
            if msg.topic.startswith(lp):
                side, leaf = "live", msg.topic[len(lp):]
            elif msg.topic.startswith(sp):
                side, leaf = "shadow", msg.topic[len(sp):]
            if side is None:
                continue
            if leaf in IGNORE or leaf.split("/")[0] in IGNORE:
                return
            last[(i, side, leaf)] = (payload, now)
            if side == "shadow" and leaf == "W":
                stats.cover_minutes.add(int(now // 60))
            other = "live" if side == "shadow" else "shadow"
            peer = last.get((i, other, leaf))
            if peer is None or now - peer[1] > PAIR_MAX_AGE:
                return
            st = pf_stats if leaf_class(leaf) == "pf" else stats
            key = f"{lp}{leaf}"
            if agree(leaf, payload, peer[0]):
                st.match[key] += 1
            else:
                st.mismatch[key] += 1
                st.last_mismatch[key] = {"live": peer[0] if side == "shadow" else payload,
                                         "shadow": payload if side == "shadow" else peer[0]}
            return

    def on_connect(client, _u, _flags, rc):
        # subscriptions MUST live here: paho re-runs on_connect after every
        # reconnect, and subscriptions made outside it are silently lost on
        # the first broker/network blip (the harness then sits "connected"
        # receiving nothing — exactly the failure this replaces).
        print(f"mqtt connected rc={rc} — (re)subscribing", flush=True)
        for lp, sp in PAIRS:
            client.subscribe(lp + "#")
            client.subscribe(sp + "#")

    def on_disconnect(_c, _u, rc):
        print(f"mqtt disconnected rc={rc} — paho will retry", flush=True)

    c = mqtt.Client(client_id="fronius-shadow-parity")
    if user:
        c.username_pw_set(user, pw)
    c.on_connect = on_connect
    c.on_disconnect = on_disconnect
    c.on_message = on_msg
    c.reconnect_delay_set(min_delay=1, max_delay=30)
    c.connect(host, port, 60)
    c.loop_start()
    print(f"parity harness up — pairs: {PAIRS}", flush=True)

    next_summary = time.time() + SUMMARY_EVERY
    while True:
        time.sleep(5)
        if time.time() >= next_summary:
            next_summary += SUMMARY_EVERY
            s = stats.summary()
            s["pf_quirk"] = pf_stats.summary()["worst"][:4]
            day = time.strftime("%Y%m%d")
            with open(os.path.join(outdir, f"parity-{day}.jsonl"), "a") as f:
                f.write(json.dumps(s) + "\n")
            print(json.dumps(s), flush=True)


def report():
    outdir = sys.argv[sys.argv.index("--report") + 1] if len(sys.argv) > 2 else "/out"
    rows = []
    for fn in sorted(os.listdir(outdir)):
        if fn.startswith("parity-") and fn.endswith(".jsonl"):
            for line in open(os.path.join(outdir, fn)):
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    if not rows:
        print("no summaries yet")
        return
    m = sum(r["pairs_matched"] for r in rows)
    x = sum(r["pairs_mismatched"] for r in rows)
    days = len({time.strftime('%Y%m%d', time.localtime(r['ts'])) for r in rows})
    agreement = m / max(m + x, 1)
    cov = sum(r["coverage"] for r in rows) / len(rows)
    print(f"days={days} agreement={agreement:.5f} coverage={cov:.4f} "
          f"matched={m} mismatched={x}")
    print("GATE (>=7 days, agreement>=0.999, coverage>=0.99):",
          "PASS" if days >= 7 and agreement >= 0.999 and cov >= 0.99 else "not yet")
    worst = defaultdict(int)
    for r in rows:
        for w in r.get("worst", []):
            worst[w["leaf"]] += w["mismatch"]
    for leaf, n in sorted(worst.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  worst: {leaf}  {n} mismatches")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    else:
        run()
