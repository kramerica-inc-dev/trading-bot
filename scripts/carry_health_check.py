#!/usr/bin/env python3
"""Carry-lane block for the daily Telegram health report.

The prod `health_report.py` (prod-only, not in this repo/mirror) has no carry
coverage. This module is the hook: it reads ONLY
`state/carry/<instance>/health.json` (written by carry_runner every cycle) and
returns report lines + a list of issues. Stdlib-only, never raises.

Hook into prod health_report.py (see docs/HL-CARRY-ONBOARD.md):

    from carry_health_check import carry_report_lines
    lines += carry_report_lines()           # default instance: btc-hl

CLI (also usable as an ad-hoc check, exit 1 when there are issues):

    python3 -m scripts.carry_health_check [--instance btc-hl] [--state-root …]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_ROOT = PROJECT_ROOT / "state" / "carry"
DEFAULT_INSTANCE = "btc-hl"
STALE_AFTER_SEC = 300          # runner cycles every 60s


def _age_sec(ts: Optional[str], now: datetime) -> Optional[float]:
    if not ts:
        return None
    try:
        return (now - datetime.fromisoformat(ts)).total_seconds()
    except (TypeError, ValueError):
        return None


def carry_check(instance: str = DEFAULT_INSTANCE,
                state_root: Path = DEFAULT_STATE_ROOT,
                now: Optional[datetime] = None) -> Dict[str, Any]:
    """→ {"ok": bool, "issues": [...], "summary": {...}}"""
    now = now or datetime.now(timezone.utc)
    issues: List[str] = []
    path = Path(state_root) / instance / "health.json"
    try:
        h = json.loads(path.read_text())
    except FileNotFoundError:
        return {"ok": False, "issues": [f"{path} ontbreekt — draait carry@{'hl-btc' if instance == 'btc-hl' else instance}?"],
                "summary": {}}
    except Exception as e:
        return {"ok": False, "issues": [f"{path} onleesbaar: {e}"], "summary": {}}

    age = _age_sec(h.get("last_cycle_ts"), now)
    if age is None or age > STALE_AFTER_SEC:
        issues.append(f"STALE: laatste cycle {('%.0f s' % age) if age is not None else 'onbekend'} geleden")
    if h.get("halted"):
        issues.append(f"HALTED: {h.get('halt_reason')}")
    if h.get("reconcile_ok") is False:
        issues.append(f"reconcile faalt ({h.get('reconcile_errors_count')} errors)")
    if (h.get("legging_aborts_total") or 0) > 0:
        issues.append(f"legging aborts: {h.get('legging_aborts_total')}")

    gate = h.get("gate") or {}
    pnl = h.get("pnl") or {}
    pos = h.get("simulated_position") or {}
    summary = {
        "mode": h.get("mode"),
        "cycles_total": h.get("cycles_total"),
        "age_sec": age,
        "gate_on": gate.get("on"),
        "gate_trailing_annualised": gate.get("trailing_annualised"),
        "gate_samples": gate.get("samples"),
        "spot_qty": pos.get("spot_qty", 0.0),
        "perp_qty": pos.get("perp_qty", 0.0),
        "funding_accrued": pnl.get("funding_accrued"),
        "fees_paid": pnl.get("fees_paid"),
        "basis_pnl": pnl.get("basis_pnl"),
        "net_pnl": pnl.get("net_pnl"),
        "realized_pnl": pnl.get("realized_pnl"),
        "simulated_equity": h.get("simulated_equity"),
        "sim_started_ts": h.get("sim_started_ts"),
        "last_action": h.get("last_action"),
    }
    return {"ok": not issues, "issues": issues, "summary": summary}


def _usd(v: Any) -> str:
    try:
        return f"${float(v):+,.2f}"
    except (TypeError, ValueError):
        return "—"


def carry_report_lines(instance: str = DEFAULT_INSTANCE,
                       state_root: Path = DEFAULT_STATE_ROOT,
                       now: Optional[datetime] = None) -> List[str]:
    """Plain-text lines for the daily report. Never raises."""
    try:
        r = carry_check(instance, state_root, now)
    except Exception as e:  # pragma: no cover — report must never break
        return [f"⚠️ carry {instance}: check faalde ({e})"]
    s = r["summary"]
    head = ("✅" if r["ok"] else "⚠️") + f" Carry {instance}"
    if not s:
        return [head] + [f"  • {i}" for i in r["issues"]]
    ann = s.get("gate_trailing_annualised")
    lines = [
        f"{head} — {s.get('mode')} · cycles {s.get('cycles_total')}",
        "  gate {} ({}/jr, {} samples)".format(
            "AAN" if s.get("gate_on") else "UIT",
            f"{ann * 100:.2f}%" if isinstance(ann, (int, float)) else "—",
            s.get("gate_samples")),
    ]
    open_book = abs(float(s.get("spot_qty") or 0.0)) > 1e-9
    lines.append("  boek {} · actie {}".format(
        f"{float(s['spot_qty']):.6f} BTC long / {float(s['perp_qty']):.6f} perp"
        if open_book else "FLAT", s.get("last_action")))
    if s.get("net_pnl") is not None:
        lines.append(
            f"  P&L netto {_usd(s.get('net_pnl'))} (funding "
            f"{_usd(s.get('funding_accrued'))}, fees {_usd(-(s.get('fees_paid') or 0.0))}, "
            f"basis {_usd(s.get('basis_pnl'))}, gerealiseerd {_usd(s.get('realized_pnl'))})")
    if s.get("mode") == "DRY_RUN" and s.get("sim_started_ts"):
        lines.append(f"  proef sinds {s['sim_started_ts'][:10]} · "
                     f"sim-equity ${float(s.get('simulated_equity') or 0):,.2f}")
    lines += [f"  • {i}" for i in r["issues"]]
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--instance", default=DEFAULT_INSTANCE)
    ap.add_argument("--state-root", default=str(DEFAULT_STATE_ROOT))
    a = ap.parse_args(argv)
    lines = carry_report_lines(a.instance, Path(a.state_root))
    print("\n".join(lines))
    return 0 if carry_check(a.instance, Path(a.state_root))["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
