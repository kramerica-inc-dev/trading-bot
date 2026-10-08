#!/usr/bin/env bash
# Daily green-button watch for the HL carry lane: read the gate, Telegram-alert
# when trailing-90d funding is above the +5%/yr deploy trigger.
# Deploy decision stays with the operator — this only watches.
# Installed 2026-06-10 with explicit user approval ("installeer watcher").
#
# 2026-10-08: uses `--gate-only` instead of `--once`. `--once` ran a full
# runner cycle on the SAME config + state as the carry@hl-btc unit: after
# `carry_hl_go go` the config is live and, without carry-hl-btc.env, the cycle
# crashed daily (P3 without HL_CONFIRM_LIVE → fail-closed); WITH that env it
# would have run a second LIVE cycle next to the unit. `--gate-only` is forced
# DRY, strips credentials, reads/writes no state and takes no lock — so it
# deliberately does NOT source carry-hl-btc.env.
set -uo pipefail
cd /opt/trading-bot
OUT=$(sudo -u botuser python3 -m scripts.carry_runner --config configs/carry-hl-btc.json --gate-only 2>/dev/null)
set -a; . /etc/trading-bot/carry-alerts.env 2>/dev/null || . /etc/trading-bot/hl-watchdog-mainnet.env 2>/dev/null; set +a
OUT="$OUT" python3 - <<'PY'
import json, os, sys
sys.path.insert(0, "scripts")
import notify
raw = os.environ.get("OUT", "")
try:
    d = json.loads(raw[raw.index("{"):])
    g = d.get("gate") or {}
    ann = g.get("trailing_annualised") or 0.0
    if g.get("on"):
        notify.send("🟢 CARRY GREEN-BUTTON ON: trailing-90d funding %.2f%%/yr > 5%% (%s samples) — carry@hl-btc kwalificeert; vóór `go` eerst de B2-checklist in hl-lanes/LANE-carry.md." % (ann*100, g.get("samples")))
    elif not g.get("samples"):
        notify.send("⚠️ carry_green_watch: geen funding-historie gelezen (%s) — gate onbekend." % g.get("reason"))
    print("gate_on=%s ann=%.2f%%/yr samples=%s" % (g.get("on"), ann*100, g.get("samples")))
except Exception as e:
    notify.send("⚠️ carry_green_watch faalde: %s" % e)
    print("watch error:", e)
PY
