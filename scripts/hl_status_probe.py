#!/usr/bin/env python3
"""Out-of-band Hyperliquid maintenance pre-warn (Layer 4).

Polls the public Hyperliquid statuspage and writes
`state/venue_status.json` so the dashboard / watchdog can show an ADVANCE
warning before a scheduled network-upgrade window (and flag an active one).

ADVISORY ONLY. The runner does NOT gate trading on this file — the in-loop
guards are the authoritative defence:
  * chain-clock freshness on every account read (rejects a halted-chain read),
  * an exchangeStatus corroboration on a suspicious drop (holds, never believes),
  * a post-only order-reject hold (no flatten churn during the upgrade window).
This probe just gives a human/operator early notice the venue announced.

No creds, no venue trading call — a single GET against the public statuspage.
Cron (every 5 min):
  */5 * * * * /usr/bin/python3 /opt/trading-bot/scripts/hl_status_probe.py
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SUMMARY_URL = "https://hyperliquid.statuspage.io/api/v2/summary.json"
OUT = Path(__file__).resolve().parent.parent / "state" / "venue_status.json"
# statuses that mean a maintenance window is happening / imminent vs merely planned
ACTIVE = ("in_progress", "verifying")
PLANNED = ("scheduled",)


def _fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "hl-status-probe"})
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.load(r)


def _slim(m: dict) -> dict:
    return {"name": m.get("name"), "status": m.get("status"),
            "scheduled_for": m.get("scheduled_for"),
            "scheduled_until": m.get("scheduled_until")}


def build(summary: dict, now_iso: str) -> dict:
    maints = summary.get("scheduled_maintenances") or []
    incidents = summary.get("incidents") or []
    active = [_slim(m) for m in maints if m.get("status") in ACTIVE]
    upcoming = [_slim(m) for m in maints if m.get("status") in PLANNED]
    return {
        "ts": now_iso,
        "indicator": (summary.get("status") or {}).get("indicator"),
        "description": (summary.get("status") or {}).get("description"),
        "maintenance_active": bool(active),
        "active_maintenance": active,
        "upcoming_maintenance": upcoming,
        "open_incidents": len(incidents),
    }


def main(argv=None) -> int:
    try:
        summary = _fetch(SUMMARY_URL)
    except Exception as e:                       # network/parse hiccup — leave the last file in place
        sys.stderr.write(f"hl_status_probe: fetch failed: {e}\n")
        return 1
    now_iso = datetime.now(timezone.utc).isoformat()
    out = build(summary, now_iso)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=2))
    tmp.replace(OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
