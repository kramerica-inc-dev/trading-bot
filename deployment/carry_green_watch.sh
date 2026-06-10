#!/usr/bin/env bash
# Daily green-button watch for the PARKED HL carry lane: one DRY cycle, then
# Telegram-alert when trailing-90d funding crosses the +5%/yr deploy trigger.
# Deploy decision stays with the operator — this only watches.
# Installed 2026-06-10 with explicit user approval ("installeer watcher").
set -uo pipefail
cd /opt/trading-bot
OUT=$(sudo -u botuser python3 -m scripts.carry_runner --config configs/carry-hl-btc.json --once 2>/dev/null)
set -a; . /etc/trading-bot/hl-watchdog-mainnet.env 2>/dev/null; set +a
python3 - <<PY
import json, sys
sys.path.insert(0, "scripts")
import notify
raw = """$OUT"""
try:
    d = json.loads(raw[raw.index("{"):])
    g = d.get("gate") or {}
    ann = g.get("trailing_annualised") or 0.0
    if g.get("on"):
        notify.send("🟢 CARRY GREEN-BUTTON ON: trailing-90d funding %.2f%%/yr > 5%% — de geparkeerde carry-hl@btc lane kwalificeert voor deploy (B2-checklist eerst!)." % (ann*100))
    print("gate_on=%s ann=%.2f%%/yr" % (g.get("on"), ann*100))
except Exception as e:
    notify.send("⚠️ carry_green_watch faalde: %s" % e)
    print("watch error:", e)
PY
