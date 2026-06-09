#!/usr/bin/env python3
"""Append the live HL runner's equity to a persistent JSONL so the dashboard's
equity-vs-BTC chart has history beyond journald's retention window.

Reads the runner's own health.json (written every safety cycle) and appends one
{ts, equity, peak, cb} line — idempotent on ts, so running it on a cron faster
than the cycle cadence is harmless. Samples ONLY when health says
live_trading=true (the runner's own real-money flag) — an equity threshold is
NOT a mode signal: the old `equity >= 1000` heuristic silently stopped sampling
the moment the live account grew past $1k. No venue call, no creds.

Cron: */15 * * * * /usr/bin/python3 /opt/trading-bot/scripts/equity_sampler.py
"""
import argparse
import json
import sys
from pathlib import Path

STATE_ROOT = Path(__file__).resolve().parent.parent / "state" / "hl_xsectional"


def sample(health_path: Path, out_path: Path) -> int:
    try:
        h = json.loads(health_path.read_text())
    except Exception:
        return 1
    ts, eq = h.get("ts"), h.get("equity")
    if ts is None or eq is None:
        return 1
    if h.get("live_trading") is not True:    # sim/DRY → not real-money history
        return 0
    if out_path.exists():                    # idempotent: skip an unchanged latest ts
        try:
            last = out_path.read_text().strip().splitlines()[-1]
            if json.loads(last).get("ts") == ts:
                return 0
        except Exception:
            pass
    with open(out_path, "a") as f:
        f.write(json.dumps({"ts": ts, "equity": float(eq),
                            "peak": h.get("peak_equity"), "cb": h.get("cb_state")}) + "\n")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instance", default="mainnet")
    args = ap.parse_args(argv)
    d = STATE_ROOT / args.instance
    return sample(d / "health.json", d / "equity_history.jsonl")


if __name__ == "__main__":
    sys.exit(main())
