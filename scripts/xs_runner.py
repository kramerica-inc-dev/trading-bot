#!/usr/bin/env python3
"""Cross-sectional momentum paper runner (M5) — the OKX-perp long-short lead.

This is the deployable form of the sweep's surviving candidate
(docs/SWEEP-RESULTS.md, docs/XSECTIONAL-HARDENING.md): every `rebal` days, rank
the OKX-perp universe by trailing `lookback`-day return, **long the top-m /
short the bottom-m in equal weight, dollar-neutral**. Hardened lead config:
lookback=120, rebal=5, m=3, 10 OKX perps (verdict PARTIAL — OOS-surviving but
modest, so this is a *paper* runner with tight kill-criteria, never live on this
evidence).

Structure mirrors `plan_e_runner.py` (atomic state, JSONL trade log,
health.json, per-cycle reconcile, daily cycle loop). Strategy mirrors
`backtest/sweep/xsectional.py` (the backtest the lead was validated in). Data
comes from the OKX **public** candle API — so DRY_RUN forward-papering needs **no
credentials**. The three-state gate (`resolve_mode`, mirroring carry_runner)
keeps DRY_RUN the default; P2_DEMO places real orders on the OKX demo venue once
credentials are supplied; P3_LIVE is hard-gated behind `allow_live`.

Modes:
  * dry_run=true                      → DRY_RUN  (simulate fills at the close)
  * dry_run=false & okx_demo=true     → P2_DEMO  (real orders, x-simulated-trading)
  * dry_run=false & okx_demo=false & allow_live=true   → P3_LIVE (refused here:
        needs perp acctLv>=2 + a separate real-money review)

Usage:
    python -m scripts.xs_runner --config configs/xsectional-okx.json --once
    python -m scripts.xs_runner --config configs/xsectional-okx.json --loop
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from okx_api import OkxAPI  # noqa: E402

PROJECT_ROOT = HERE.parent
STATE_ROOT = PROJECT_ROOT / "state" / "xsectional"
YEAR_DAYS = 365.0

MODE_DRY = "DRY_RUN"
MODE_P2 = "P2_DEMO"
MODE_P3 = "P3_LIVE"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class XSRunnerConfig:
    instance_name: str = "okx"
    universe: List[str] = field(default_factory=lambda: [
        "BTC-USDT", "ETH-USDT", "SOL-USDT", "BNB-USDT", "XRP-USDT",
        "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "DOT-USDT", "LINK-USDT"])
    lookback_days: int = 120
    rebal_days: int = 5
    m: int = 3                       # longs = top m, shorts = bottom m
    bar: str = "1Dutc"
    initial_capital: float = 5000.0
    gross_exposure: float = 1.0      # gross notional = gross_exposure * equity
    cost_rate: float = 0.0015        # per unit of traded notional (taker+slippage)
    funding_model: str = "live"      # 'live' (fetch per-asset) | 'flat' | 'off'
    flat_funding_annual: float = 0.04  # fallback/flat headwind (~4%/yr, calibrated)
    min_assets: int = 6              # abort cycle if fewer assets have data
    data_staleness_hours: int = 36   # latest daily bar must be this fresh
    halt_drawdown_pct: float = 0.25  # circuit-breaker: halt if DD exceeds this
    # mode gate
    dry_run: bool = True
    okx_demo: bool = False
    allow_live: bool = False


def load_config(path: str) -> XSRunnerConfig:
    raw = json.loads(Path(path).read_text())
    known = {f for f in XSRunnerConfig().__dataclass_fields__}
    return XSRunnerConfig(**{k: v for k, v in raw.items() if k in known})


def resolve_mode(cfg: XSRunnerConfig) -> str:
    """Three-state safety gate (mirrors carry_runner.resolve_mode)."""
    if cfg.dry_run:
        return MODE_DRY
    if cfg.okx_demo:
        return MODE_P2
    if not cfg.allow_live:
        raise RuntimeError(
            "Refusing to run live: dry_run=false + okx_demo=false requires "
            "allow_live=true (P3 gate). Use dry_run=true (DRY_RUN) or "
            "okx_demo=true (P2_DEMO).")
    return MODE_P3


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class Position:
    side: int                # +1 long, -1 short
    notional: float          # USD notional at entry
    entry_price: float
    entered_ts: str


@dataclass
class XSState:
    cash: float = 0.0
    equity: float = 0.0
    peak_equity: float = 0.0
    positions: Dict[str, Position] = field(default_factory=dict)
    rebalances_total: int = 0
    skips_total: int = 0
    cycles_total: int = 0
    funding_paid_total: float = 0.0     # cumulative net funding outflow (USD)
    fees_paid_total: float = 0.0
    cb_state: str = "normal"            # normal | halted | op_halt | catastrophe_halt
    last_settled_equity: Optional[float] = None   # last accepted settled equity (intracycle-drop ref)
    last_funding_ts: Optional[str] = None         # last sim-funding accrual (HL runner)
    data_outage_streak: int = 0                   # consecutive insufficient/stale-data cycles
    equity_jump_streak: int = 0                   # consecutive suspicious equity-jump reads (HL runner)
    started_ts: Optional[str] = None
    last_rebalance_ts: Optional[str] = None
    last_cycle_ts: Optional[str] = None
    last_reconcile_ok: bool = True
    dry_run: bool = True

    def to_json(self) -> dict:
        d = asdict(self)
        d["positions"] = {s: asdict(p) for s, p in self.positions.items()}
        return d

    @classmethod
    def from_json(cls, d: dict) -> "XSState":
        pos = {s: Position(**p) for s, p in (d.get("positions") or {}).items()}
        d = {**d, "positions": pos}
        known = {f for f in cls().__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# Data (OKX public)
# ---------------------------------------------------------------------------

def _to_swap(base: str) -> str:
    return base if base.endswith("-SWAP") else f"{base}-SWAP"


def fetch_daily_closes(api: OkxAPI, universe: List[str], lookback: int, *,
                       bar: str = "1Dutc", pad: int = 12
                       ) -> tuple[Dict[str, np.ndarray], Dict[str, float], Optional[int]]:
    """Latest `lookback+pad` CLOSED daily closes per asset (public, no auth).

    Returns (closes_by_asset, latest_close_by_asset, latest_bar_ms)."""
    need = lookback + pad
    closes: Dict[str, np.ndarray] = {}
    latest: Dict[str, float] = {}
    latest_ms: Optional[int] = None
    for base in universe:
        swap = _to_swap(base)
        try:
            resp = api.get_candles(inst_id=swap, bar=bar, limit=min(need, 300))
        except Exception:
            continue
        if not isinstance(resp, dict) or str(resp.get("code")) != "0":
            continue
        rows = resp.get("data") or []
        # OKX returns newest-first; keep only confirmed (closed) bars.
        clean = [(int(r[0]), float(r[4])) for r in rows
                 if len(r) >= 6 and str(r[-1]) in ("1", "1.0")]
        # the newest bar is in-progress (confirm=0); the signal needs only
        # lookback+1 CLOSED bars, so require that, not the full padded request.
        if len(clean) < lookback + 1:
            continue
        clean.sort(key=lambda x: x[0])             # oldest-first
        ts = np.array([c[0] for c in clean])
        cl = np.array([c[1] for c in clean], dtype=float)
        closes[base] = cl
        latest[base] = float(cl[-1])
        latest_ms = max(latest_ms or 0, int(ts[-1]))
        time.sleep(0.08)
    return closes, latest, latest_ms


def fetch_funding_daily(api: OkxAPI, base: str) -> Optional[float]:
    """Current OKX funding rate (per 8h settlement) -> daily rate (x3). Public."""
    try:
        resp = api.get_funding_rate(inst_id=_to_swap(base))
        data = (resp.get("data") or [{}])[0]
        return float(data["fundingRate"]) * 3.0
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Strategy (the hardened lead — mirrors backtest/sweep/xsectional.py)
# ---------------------------------------------------------------------------

def compute_target_weights(closes: Dict[str, np.ndarray], lookback: int, m: int
                           ) -> Dict[str, float]:
    """Dollar-neutral equal-weight basket: +1/m on the top-m, -1/m on the
    bottom-m by trailing `lookback`-day return. Lookahead-free (uses only the
    available history). Returns {} if too few assets."""
    trail = {}
    for sym, cl in closes.items():
        if len(cl) > lookback and cl[-1 - lookback] > 0:
            trail[sym] = cl[-1] / cl[-1 - lookback] - 1.0
    if len(trail) < 2 * m:
        return {}
    ranked = sorted(trail, key=lambda s: trail[s])     # worst -> best
    weights = {s: 0.0 for s in trail}
    for s in ranked[-m:]:
        weights[s] = 1.0 / m                           # longs (top)
    for s in ranked[:m]:
        weights[s] = -1.0 / m                          # shorts (bottom)
    return weights


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class XSRunner:
    def __init__(self, cfg: XSRunnerConfig):
        self.cfg = cfg
        self.mode = resolve_mode(cfg)
        self.dir = STATE_ROOT / cfg.instance_name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.trades_path = self.dir / "trades.log"
        self.health_path = self.dir / "health.json"
        demo = (self.mode == MODE_P2)
        # DRY_RUN still constructs the client for public market data (no keys needed).
        self.api = OkxAPI(demo=demo) if self.mode != MODE_DRY else OkxAPI()

    # -- persistence -------------------------------------------------------
    def load_state(self) -> XSState:
        if self.state_path.exists():
            return XSState.from_json(json.loads(self.state_path.read_text()))
        s = XSState(cash=self.cfg.initial_capital, equity=self.cfg.initial_capital,
                    peak_equity=self.cfg.initial_capital,
                    started_ts=_utcnow().isoformat(), dry_run=self.cfg.dry_run)
        return s

    def save_state(self, s: XSState) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(s.to_json(), indent=2))
        tmp.replace(self.state_path)

    def log(self, event: dict) -> None:
        event = {"ts": _utcnow().isoformat(), **event}
        # Rotate to <name>.1 at 10MB so the append-only log can't fill the LXC disk.
        try:
            if self.trades_path.exists() and self.trades_path.stat().st_size > 10 * 1024 * 1024:
                self.trades_path.replace(self.trades_path.parent / (self.trades_path.name + ".1"))
        except OSError:
            pass
        with open(self.trades_path, "a") as f:
            f.write(json.dumps(event) + "\n")

    # -- marking -----------------------------------------------------------
    def mark_equity(self, s: XSState, prices: Dict[str, float]) -> float:
        upnl = 0.0
        for sym, p in s.positions.items():
            px = prices.get(sym)
            if px and p.entry_price > 0:
                upnl += p.side * p.notional * (px / p.entry_price - 1.0)
        return s.cash + upnl

    # -- funding -----------------------------------------------------------
    def apply_funding(self, s: XSState, prices: Dict[str, float],
                      elapsed_days: float) -> float:
        """Accrue funding pro-rata for the time positions were held since the
        last cycle (frequency-independent: a daily rate * elapsed_days)."""
        if self.cfg.funding_model == "off" or not s.positions or elapsed_days <= 0:
            return 0.0
        total = 0.0
        flat_daily = self.cfg.flat_funding_annual / YEAR_DAYS
        for sym, p in s.positions.items():
            rate = None
            if self.cfg.funding_model == "live":
                rate = fetch_funding_daily(self.api, sym)
            if rate is None:
                rate = flat_daily                   # robust fallback
            # long pays when rate>0, short receives -> pnl = -side*notional*rate
            total += -p.side * p.notional * rate * elapsed_days
        s.cash += total
        s.funding_paid_total += -total              # outflow positive
        return total

    # -- rebalance ---------------------------------------------------------
    def _should_rebalance(self, s: XSState, now: datetime) -> bool:
        if s.last_rebalance_ts is None:
            return True
        last = datetime.fromisoformat(s.last_rebalance_ts)
        return (now - last).total_seconds() >= self.cfg.rebal_days * 86400 - 3600

    def rebalance(self, s: XSState, closes: Dict[str, np.ndarray],
                  prices: Dict[str, float], now: datetime) -> dict:
        cfg = self.cfg
        weights = compute_target_weights(closes, cfg.lookback_days, cfg.m)
        if not weights:
            return {"action": "skip", "reasons": ["no target weights (too few assets)"]}

        equity = self.mark_equity(s, prices)
        gross_book = equity * cfg.gross_exposure
        # target notional per asset (signed)
        target = {sym: w * gross_book for sym, w in weights.items()}

        # close positions no longer wanted (or side flip) -> realise PnL
        changes = []
        traded_notional = 0.0
        for sym in list(s.positions.keys()):
            p = s.positions[sym]
            tgt = target.get(sym, 0.0)
            if tgt == 0.0 or (np.sign(tgt) != p.side):
                px = prices.get(sym)
                if px is None:
                    continue
                pnl = p.side * p.notional * (px / p.entry_price - 1.0)
                s.cash += pnl
                traded_notional += abs(p.notional)
                changes.append({"sym": sym, "act": "close", "side": p.side,
                                "notional": round(p.notional, 2), "pnl": round(pnl, 2)})
                del s.positions[sym]

        # open / resize toward target
        for sym, tgt in target.items():
            if tgt == 0.0:
                continue
            px = prices.get(sym)
            if px is None:
                continue
            cur = s.positions.get(sym)
            cur_notional = (cur.side * cur.notional) if cur else 0.0
            delta = tgt - cur_notional
            if abs(delta) < 1e-6:
                continue
            traded_notional += abs(delta)
            # simple model: re-establish the position at target (paper)
            s.positions[sym] = Position(side=int(np.sign(tgt)), notional=abs(tgt),
                                        entry_price=px, entered_ts=now.isoformat())
            if not cur:
                changes.append({"sym": sym, "act": "open", "side": int(np.sign(tgt)),
                                "notional": round(abs(tgt), 2)})
            else:
                changes.append({"sym": sym, "act": "resize", "side": int(np.sign(tgt)),
                                "notional": round(abs(tgt), 2)})

        fee = traded_notional * cfg.cost_rate
        s.cash -= fee
        s.fees_paid_total += fee
        s.rebalances_total += 1
        s.last_rebalance_ts = now.isoformat()
        s.equity = self.mark_equity(s, prices)
        longs = sorted([sy for sy, w in weights.items() if w > 0])
        shorts = sorted([sy for sy, w in weights.items() if w < 0])

        # NOTE: in P2_DEMO/P3_LIVE this is where real OKX orders would be routed
        # (OkxAdapter.place_order per leg). DRY_RUN simulates fills at the close.
        if self.mode != MODE_DRY:
            raise NotImplementedError(
                "P2_DEMO/P3_LIVE order routing not wired yet — supply demo creds "
                "and implement the OkxAdapter leg loop before flipping dry_run.")

        return {"action": "rebalance", "mode": self.mode, "longs": longs,
                "shorts": shorts, "traded_notional": round(traded_notional, 2),
                "fee": round(fee, 2), "changes": changes,
                "equity_after": round(s.equity, 2)}

    # -- reconcile ---------------------------------------------------------
    def reconcile(self, s: XSState, prices: Dict[str, float]) -> dict:
        errs = []
        # R1: dollar-neutrality (long notional ~ short notional)
        ln = sum(p.notional for p in s.positions.values() if p.side > 0)
        sn = sum(p.notional for p in s.positions.values() if p.side < 0)
        gross = ln + sn
        if gross > 0 and abs(ln - sn) / gross > 0.10:
            errs.append(f"not dollar-neutral: long {ln:.0f} vs short {sn:.0f}")
        # R2: position count <= 2m
        if len(s.positions) > 2 * self.cfg.m:
            errs.append(f"{len(s.positions)} positions > 2m={2*self.cfg.m}")
        # R3: dry_run consistency
        if bool(s.dry_run) != bool(self.cfg.dry_run):
            errs.append(f"state.dry_run={s.dry_run} != cfg {self.cfg.dry_run}")
        # R4: equity sanity
        if not math.isfinite(s.equity) or s.equity <= 0:
            errs.append(f"non-positive equity {s.equity}")
        s.last_reconcile_ok = not errs
        return {"ok": not errs, "errors": errs}

    # -- health ------------------------------------------------------------
    def write_health(self, s: XSState, extra: dict) -> None:
        dd = (s.peak_equity - s.equity) / s.peak_equity if s.peak_equity > 0 else 0.0
        h = {
            "ts": _utcnow().isoformat(), "instance": self.cfg.instance_name,
            "mode": self.mode, "cycles_total": s.cycles_total,
            "rebalances_total": s.rebalances_total, "skips_total": s.skips_total,
            "equity": round(s.equity, 2), "peak_equity": round(s.peak_equity, 2),
            "drawdown_pct": round(dd * 100, 2), "cb_state": s.cb_state,
            "n_positions": len(s.positions),
            "funding_paid_total": round(s.funding_paid_total, 2),
            "fees_paid_total": round(s.fees_paid_total, 2),
            "last_rebalance_ts": s.last_rebalance_ts,
            "last_reconcile_ok": s.last_reconcile_ok,
            "config": {"lookback_days": self.cfg.lookback_days, "rebal_days": self.cfg.rebal_days,
                       "m": self.cfg.m, "universe": self.cfg.universe},
            **extra,
        }
        tmp = self.health_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(h, indent=2))
        tmp.replace(self.health_path)

    # -- one cycle ---------------------------------------------------------
    def run_once(self) -> dict:
        cfg = self.cfg
        s = self.load_state()
        now = _utcnow()
        prev_cycle_ts = s.last_cycle_ts
        s.cycles_total += 1
        s.last_cycle_ts = now.isoformat()
        elapsed_days = 0.0
        if prev_cycle_ts:
            elapsed_days = max(0.0, (now - datetime.fromisoformat(prev_cycle_ts)).total_seconds() / 86400.0)

        closes, prices, latest_ms = fetch_daily_closes(
            self.api, cfg.universe, cfg.lookback_days, bar=cfg.bar)
        if len(closes) < cfg.min_assets:
            s.skips_total += 1
            self.save_state(s)
            self.write_health(s, {"last_action": "skip", "reason": f"only {len(closes)} assets"})
            return {"action": "skip", "reason": f"insufficient data ({len(closes)} assets)"}

        # staleness guard
        if latest_ms is not None:
            age_h = (now.timestamp() * 1000 - latest_ms) / 3.6e6
            if age_h > cfg.data_staleness_hours:
                s.skips_total += 1
                self.save_state(s)
                self.write_health(s, {"last_action": "skip", "reason": f"stale data {age_h:.1f}h"})
                return {"action": "skip", "reason": f"stale data ({age_h:.1f}h)"}

        funding = self.apply_funding(s, prices, elapsed_days)
        s.equity = self.mark_equity(s, prices)
        s.peak_equity = max(s.peak_equity, s.equity)
        dd = (s.peak_equity - s.equity) / s.peak_equity if s.peak_equity > 0 else 0.0

        # circuit breaker
        if dd >= cfg.halt_drawdown_pct and s.cb_state != "halted":
            s.cb_state = "halted"
            self.log({"action": "circuit_breaker_halt", "drawdown_pct": round(dd * 100, 2),
                      "equity": round(s.equity, 2)})
        if s.cb_state == "halted":
            self.reconcile(s, prices)
            self.save_state(s)
            self.write_health(s, {"last_action": "halted", "funding": round(funding, 4)})
            return {"action": "halted", "drawdown_pct": round(dd * 100, 2)}

        result = {"action": "noop"}
        if self._should_rebalance(s, now):
            result = self.rebalance(s, closes, prices, now)
            if result["action"] == "skip":
                s.skips_total += 1
            self.log(result)

        s.equity = self.mark_equity(s, prices)
        s.peak_equity = max(s.peak_equity, s.equity)
        rec = self.reconcile(s, prices)
        if not rec["ok"]:
            self.log({"action": "reconcile", **rec})
        self.save_state(s)
        self.write_health(s, {"last_action": result["action"],
                              "funding": round(funding, 4),
                              "n_assets": len(closes)})
        return {**result, "equity": round(s.equity, 2), "funding": round(funding, 4),
                "reconcile_ok": rec["ok"]}

    def run_loop(self, interval_sec: int = 3600) -> None:
        print(f"[xs_runner] {self.cfg.instance_name} mode={self.mode} "
              f"lb={self.cfg.lookback_days} rebal={self.cfg.rebal_days} m={self.cfg.m} "
              f"universe={len(self.cfg.universe)}")
        while True:
            try:
                r = self.run_once()
                print(f"[{_utcnow().isoformat()}] {r.get('action')} "
                      f"eq={r.get('equity')} reconcile_ok={r.get('reconcile_ok')}")
            except Exception as e:
                print(f"[xs_runner] cycle error: {e}", file=sys.stderr)
            time.sleep(interval_sec)


def main() -> int:
    # Guarantee live journald output regardless of PYTHONUNBUFFERED in the unit.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(PROJECT_ROOT / "configs" / "xsectional-okx.json"))
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--loop", action="store_true", help="run the daily cycle loop")
    ap.add_argument("--interval-sec", type=int, default=3600)
    args = ap.parse_args()

    cfg = load_config(args.config) if Path(args.config).exists() else XSRunnerConfig()
    runner = XSRunner(cfg)
    if args.loop:
        runner.run_loop(args.interval_sec)
        return 0
    r = runner.run_once()
    print(json.dumps(r, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
