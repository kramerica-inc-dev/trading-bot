#!/usr/bin/env python3
"""External watchdog for the Hyperliquid momentum runner — a SECOND, independent
process that catches the runner being dead/wedged or the account bleeding out,
since a hung `hl_xs_runner` writes no health and trips none of its own breakers.

Per instance it reads state/hl_xsectional/<instance>/health.json and alerts when:
  (i)  the health is STALE — `ts` (or the file mtime) is older than
       `--stale-after-sec` (default 900) → the runner is dead/wedged, OR
  (ii) `equity` < `--min-equity-usd` → the live account has drawn below a floor.

Default behaviour is **alert-only**: every alert is appended to
state/hl_xsectional/<instance>/watchdog.events.jsonl AND printed to stderr (the
journal). It does NOT auto-flatten by default — a second process flattening a
live book is risky (races the runner, double-closes). The opt-in
`--flatten-on-stale` flag flattens via the SAME `HLAdapter` (same env key) ONLY
when the health is stale AND positions actually exist, so the safe default can
never produce a false-positive flatten.

Usage (one-shot; drive on a cadence from a systemd .timer):
    python -m scripts.hl_watchdog --instance mainnet --min-equity-usd 40
    python -m scripts.hl_watchdog --instance mainnet --min-equity-usd 40 --flatten-on-stale
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
STATE_ROOT = PROJECT_ROOT / "state" / "hl_xsectional"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(ts: Optional[str]) -> Optional[float]:
    """ISO ts -> epoch seconds, or None if unparseable."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return None


def health_age_sec(health: dict, health_path: Path, now: Optional[float] = None) -> Optional[float]:
    """Age of the health snapshot in seconds: from its own `ts` when parseable,
    else from the file mtime. None only when neither is available."""
    now = now if now is not None else time.time()
    ts = _parse_ts(health.get("ts"))
    if ts is None:
        try:
            ts = health_path.stat().st_mtime
        except OSError:
            return None
    return max(0.0, now - ts)


def evaluate(health: Optional[dict], health_path: Path, *, stale_after_sec: float,
             min_equity_usd: Optional[float], now: Optional[float] = None) -> List[dict]:
    """Pure decision: the alerts (possibly empty) for one instance's health.
    A MISSING/unreadable health.json is itself a 'stale' alert (the runner has
    never written, or the file is gone). Each alert is {kind, reason, ...}."""
    alerts: List[dict] = []
    if health is None:
        alerts.append({"kind": "stale", "reason": "health.json missing or unreadable"})
        return alerts
    age = health_age_sec(health, health_path, now=now)
    if age is None or age > stale_after_sec:
        alerts.append({"kind": "stale", "reason": f"health age {age}s > {stale_after_sec}s",
                       "age_sec": (round(age, 1) if age is not None else None)})
    if min_equity_usd is not None:
        eq = health.get("equity")
        if isinstance(eq, (int, float)) and eq < min_equity_usd:
            alerts.append({"kind": "low_equity", "reason": f"equity {eq} < floor {min_equity_usd}",
                           "equity": eq})
    return alerts


class Watchdog:
    def __init__(self, instance: str, *, stale_after_sec: float = 900.0,
                 min_equity_usd: Optional[float] = None, flatten_on_stale: bool = False,
                 state_root: Path = STATE_ROOT):
        self.instance = instance
        self.stale_after_sec = stale_after_sec
        self.min_equity_usd = min_equity_usd
        self.flatten_on_stale = flatten_on_stale
        self.dir = state_root / instance
        self.health_path = self.dir / "health.json"
        self.events_path = self.dir / "watchdog.events.jsonl"

    def read_health(self) -> Optional[dict]:
        try:
            return json.loads(self.health_path.read_text())
        except (OSError, ValueError):
            return None

    def emit(self, event: dict) -> None:
        """Append to the JSONL event log AND print to stderr (the journal)."""
        rec = {"ts": _utcnow().isoformat(), "instance": self.instance, **event}
        line = json.dumps(rec)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            # Rotate to <name>.1 at 10MB so the event log can't fill the LXC disk.
            if self.events_path.exists() and self.events_path.stat().st_size > 10 * 1024 * 1024:
                self.events_path.replace(self.events_path.parent / (self.events_path.name + ".1"))
            with open(self.events_path, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
        print(f"[hl_watchdog] {line}", file=sys.stderr)

    def _make_adapter(self):
        """Construct the SAME HLAdapter the runner uses (same env key) — imported
        lazily so the alert-only path needs neither the SDK nor any credentials."""
        sys.path.insert(0, str(HERE))
        from hl_adapter import HLAdapter  # noqa: E402
        cfg = self._load_runner_cfg()
        pk = os.environ.get("HL_PRIVATE_KEY") or None
        addr = os.environ.get("HL_ACCOUNT_ADDRESS") or None
        return HLAdapter(network=cfg.get("network", "mainnet"), private_key=pk,
                         account_address=addr, allow_live=bool(cfg.get("allow_live", False)))

    def _load_runner_cfg(self) -> dict:
        """Best-effort read of the runner's JSON config for network/allow_live;
        defaults to mainnet/not-live if absent."""
        p = PROJECT_ROOT / "configs" / f"hl-xsectional-{self.instance}.json"
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return {}

    def maybe_flatten(self, alerts: List[dict]) -> Optional[dict]:
        """Opt-in flatten: ONLY when --flatten-on-stale is set AND a stale alert
        fired AND positions actually exist on the venue. Returns the flatten
        result, or None if it did not act."""
        if not self.flatten_on_stale:
            return None
        if not any(a["kind"] == "stale" for a in alerts):
            return None
        try:
            adapter = self._make_adapter()
            coins = list(adapter.positions().keys())
        except Exception as e:
            self.emit({"kind": "flatten_error", "reason": f"adapter/positions: {e}"})
            return {"acted": False, "error": str(e)}
        if not coins:                                # nothing to do — no false-positive flatten
            self.emit({"kind": "flatten_skipped", "reason": "no open positions"})
            return {"acted": False, "positions": 0}
        out = []
        for coin in coins:
            try:
                r = adapter.close(coin)
                out.append({"coin": coin, "ok": getattr(r, "ok", False),
                            "err": getattr(r, "error", None)})
            except Exception as e:
                out.append({"coin": coin, "ok": False, "err": str(e)})
        self.emit({"kind": "flatten", "reason": "stale + positions present", "closed": out})
        return {"acted": True, "closed": out}

    def check_once(self) -> dict:
        health = self.read_health()
        alerts = evaluate(health, self.health_path, stale_after_sec=self.stale_after_sec,
                          min_equity_usd=self.min_equity_usd)
        for a in alerts:
            self.emit({"kind": "alert", **a})
        flatten = self.maybe_flatten(alerts) if alerts else None
        return {"instance": self.instance, "alerts": alerts, "flatten": flatten,
                "ok": not alerts}


def main() -> int:
    # Guarantee live journald output regardless of PYTHONUNBUFFERED in the unit.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instance", required=True, help="runner instance name (e.g. mainnet)")
    ap.add_argument("--stale-after-sec", type=float, default=900.0)
    ap.add_argument("--min-equity-usd", type=float, default=None,
                    help="alert if health equity falls below this floor")
    ap.add_argument("--flatten-on-stale", action="store_true",
                    help="opt-in: flatten via HLAdapter when stale AND positions exist")
    args = ap.parse_args()
    wd = Watchdog(args.instance, stale_after_sec=args.stale_after_sec,
                  min_equity_usd=args.min_equity_usd, flatten_on_stale=args.flatten_on_stale)
    r = wd.check_once()
    print(json.dumps(r, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
