#!/usr/bin/env python3
"""Append the live HL runner's equity to a persistent JSONL so the dashboard's
equity-vs-BTC chart has history beyond journald's retention window.

Reads the runner's own health.json (written every safety cycle) and appends one
{ts, equity, peak, cb} line — idempotent on ts, so running it on a cron faster
than the cycle cadence is harmless. Skips the sim period (equity >= 1000 in
MAINNET_DRY) so only real-money points are logged. No venue call, no creds.

Cron: */15 * * * * /usr/bin/python3 /opt/trading-bot/scripts/equity_sampler.py
"""
import json
import sys
from pathlib import Path

STATE = Path(__file__).resolve().parent.parent / "state" / "hl_xsectional" / "mainnet"
HEALTH = STATE / "health.json"
OUT = STATE / "equity_history.jsonl"


def main() -> int:
    try:
        h = json.loads(HEALTH.read_text())
    except Exception:
        return 1
    ts, eq = h.get("ts"), h.get("equity")
    if ts is None or eq is None:
        return 1
    if float(eq) >= 1000:          # MAINNET_DRY sim notional — not real money
        return 0
    if OUT.exists():               # idempotent: skip an unchanged latest ts
        try:
            last = OUT.read_text().strip().splitlines()[-1]
            if json.loads(last).get("ts") == ts:
                return 0
        except Exception:
            pass
    with open(OUT, "a") as f:
        f.write(json.dumps({"ts": ts, "equity": float(eq),
                            "peak": h.get("peak_equity"), "cb": h.get("cb_state")}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
