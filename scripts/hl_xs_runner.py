#!/usr/bin/env python3
"""Cross-sectional momentum runner on HYPERLIQUID — the executable venue.

Same strategy as scripts/xs_runner.py (long top-m / short bottom-m of the OKX-
validated basket, dollar-neutral, rebalanced every `rebal` days) but data +
execution come from Hyperliquid, the one venue an NL retail user can actually
trade perps on (docs/VENUE-ACCESS-RESEARCH.md). The momentum edge was confirmed
to survive on Hyperliquid's own data (docs/VENUE-ACCESS-RESEARCH.md → HL
validation: ADVANCE, net +252%, null 99.9th).

Execution rides scripts/hl_adapter.py (official SDK, EIP-712 signing). The
adapter's three-state gate governs behaviour:
  * network=testnet                    → TESTNET      real signed orders, mock funds
  * network=mainnet & allow_live=false → MAINNET_DRY  data only; SIMULATED fills,
                                                       no orders, no wallet needed
  * network=mainnet & allow_live=true  → MAINNET_LIVE real money (hard-gated)

So MAINNET_DRY forward-papers on real mainnet prices+funding with no wallet and
no orders — the safe default for validating the loop. Credentials (an API/agent
wallet key) come from env: HL_PRIVATE_KEY (+ optional HL_ACCOUNT_ADDRESS).

Usage:
    python -m scripts.hl_xs_runner --config configs/hl-xsectional.json --once
    python -m scripts.hl_xs_runner --config configs/hl-xsectional.json --loop
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from xs_runner import XSState, Position, compute_target_weights  # noqa: E402
from hl_adapter import (  # noqa: E402
    HLAdapter, MODE_TESTNET, MODE_MAINNET_DRY, MODE_MAINNET_LIVE,
)

PROJECT_ROOT = HERE.parent
STATE_ROOT = PROJECT_ROOT / "state" / "hl_xsectional"


@dataclass
class HLXSConfig:
    instance_name: str = "main"
    universe: List[str] = field(default_factory=lambda: [
        "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "DOT", "LINK"])
    lookback_days: int = 120
    rebal_days: int = 5
    m: int = 3
    initial_capital: float = 5000.0     # notional used in sim (no-wallet) modes
    gross_exposure: float = 1.0
    cost_rate: float = 0.00045          # HL taker, for sim accounting
    flat_funding_annual: float = 0.06   # HL-calibrated headwind (sim)
    slippage: float = 0.05              # marketable-IOC slippage (live)
    min_assets: int = 6
    halt_drawdown_pct: float = 0.25
    network: str = "mainnet"            # 'mainnet' (DRY default) or 'testnet'
    allow_live: bool = False            # mainnet real-money gate


def load_config(path: str) -> HLXSConfig:
    raw = json.loads(Path(path).read_text())
    known = {f for f in HLXSConfig().__dataclass_fields__}
    return HLXSConfig(**{k: v for k, v in raw.items() if k in known})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HLXSRunner:
    def __init__(self, cfg: HLXSConfig):
        self.cfg = cfg
        pk = os.environ.get("HL_PRIVATE_KEY") or None
        addr = os.environ.get("HL_ACCOUNT_ADDRESS") or None
        self.adapter = HLAdapter(network=cfg.network, private_key=pk,
                                 account_address=addr, allow_live=cfg.allow_live)
        self.mode = self.adapter.mode
        # We place real orders only with a wallet AND in a trade-enabled mode.
        self.live_trading = (self.mode in (MODE_TESTNET, MODE_MAINNET_LIVE)
                             and self.adapter.exchange is not None)
        self.dir = STATE_ROOT / cfg.instance_name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.trades_path = self.dir / "trades.log"
        self.health_path = self.dir / "health.json"

    # -- persistence (same shape as xs_runner) -----------------------------
    def load_state(self) -> XSState:
        if self.state_path.exists():
            return XSState.from_json(json.loads(self.state_path.read_text()))
        return XSState(cash=self.cfg.initial_capital, equity=self.cfg.initial_capital,
                       peak_equity=self.cfg.initial_capital,
                       started_ts=_utcnow().isoformat(), dry_run=not self.live_trading)

    def save_state(self, s: XSState) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(s.to_json(), indent=2))
        tmp.replace(self.state_path)

    def log(self, event: dict) -> None:
        with open(self.trades_path, "a") as f:
            f.write(json.dumps({"ts": _utcnow().isoformat(), **event}) + "\n")

    # -- marking -----------------------------------------------------------
    def _sim_mark(self, s: XSState, mids: Dict[str, float]) -> float:
        upnl = 0.0
        for sym, p in s.positions.items():
            px = mids.get(sym)
            if px and p.entry_price > 0:
                upnl += p.side * p.notional * (px / p.entry_price - 1.0)
        return s.cash + upnl

    def equity(self, s: XSState, mids: Dict[str, float]) -> float:
        if self.adapter.wallet is not None:
            return self.adapter.account_value()      # real account
        return self._sim_mark(s, mids)               # sim notional

    # -- targets -----------------------------------------------------------
    def _targets(self, closes, equity) -> Dict[str, float]:
        w = compute_target_weights(closes, self.cfg.lookback_days, self.cfg.m)
        gross_book = equity * self.cfg.gross_exposure
        return {c: wt * gross_book for c, wt in w.items() if wt != 0.0}

    # -- execution: LIVE (testnet / mainnet-live) --------------------------
    def _execute_live(self, targets: Dict[str, float], now: datetime) -> dict:
        cur = self.adapter.positions()               # {coin:{szi,...}}
        orders = []
        # close positions leaving the basket or flipping side
        for coin, p in cur.items():
            tgt = targets.get(coin, 0.0)
            cur_side = 1 if p["szi"] > 0 else -1
            if tgt == 0.0 or int(np.sign(tgt)) != cur_side:
                r = self.adapter.close(coin)
                orders.append({"act": "close", "coin": coin, "ok": r.ok, "err": r.error})
        # open coins not already held on the correct side (size drift accepted v1)
        cur2 = self.adapter.positions()
        for coin, tgt in targets.items():
            held = cur2.get(coin)
            if held and int(np.sign(held["szi"])) == int(np.sign(tgt)):
                continue
            r = self.adapter.market_order_usd(coin, tgt > 0, abs(tgt), slippage=self.cfg.slippage)
            orders.append({"act": "open", "coin": coin, "side": int(np.sign(tgt)),
                           "ok": r.ok, "filled": r.filled_sz, "err": r.error})
        longs = sorted(c for c, t in targets.items() if t > 0)
        shorts = sorted(c for c, t in targets.items() if t < 0)
        return {"action": "rebalance", "mode": self.mode, "execution": "live",
                "longs": longs, "shorts": shorts, "orders": orders}

    # -- execution: SIM (mainnet-dry / no wallet) --------------------------
    def _execute_sim(self, s: XSState, targets: Dict[str, float],
                     mids: Dict[str, float], now: datetime) -> dict:
        traded = 0.0
        for sym in list(s.positions.keys()):
            p = s.positions[sym]
            tgt = targets.get(sym, 0.0)
            if tgt == 0.0 or int(np.sign(tgt)) != p.side:
                px = mids.get(sym)
                if px is None:
                    continue
                s.cash += p.side * p.notional * (px / p.entry_price - 1.0)
                traded += abs(p.notional)
                del s.positions[sym]
        for sym, tgt in targets.items():
            px = mids.get(sym)
            if px is None:
                continue
            cur = s.positions.get(sym)
            if cur and cur.side == int(np.sign(tgt)):
                continue
            traded += abs(tgt)
            s.positions[sym] = Position(side=int(np.sign(tgt)), notional=abs(tgt),
                                        entry_price=px, entered_ts=now.isoformat())
        fee = traded * self.cfg.cost_rate
        s.cash -= fee
        s.fees_paid_total += fee
        longs = sorted(c for c, t in targets.items() if t > 0)
        shorts = sorted(c for c, t in targets.items() if t < 0)
        return {"action": "rebalance", "mode": self.mode, "execution": "sim",
                "longs": longs, "shorts": shorts, "fee": round(fee, 2),
                "traded_notional": round(traded, 2)}

    # -- reconcile ---------------------------------------------------------
    def reconcile(self, s: XSState, targets: Optional[Dict[str, float]]) -> dict:
        errs = []
        if self.live_trading:
            pos = self.adapter.positions()
            longs = [c for c, p in pos.items() if p["szi"] > 0]
            shorts = [c for c, p in pos.items() if p["szi"] < 0]
            if pos and abs(len(longs) - len(shorts)) > 1:
                errs.append(f"venue not balanced: {len(longs)}L/{len(shorts)}S")
            if len(pos) > 2 * self.cfg.m:
                errs.append(f"venue {len(pos)} positions > 2m={2*self.cfg.m}")
        else:
            ln = sum(p.notional for p in s.positions.values() if p.side > 0)
            sn = sum(p.notional for p in s.positions.values() if p.side < 0)
            g = ln + sn
            if g > 0 and abs(ln - sn) / g > 0.10:
                errs.append(f"sim not dollar-neutral: {ln:.0f}L/{sn:.0f}S")
        s.last_reconcile_ok = not errs
        return {"ok": not errs, "errors": errs}

    # -- health ------------------------------------------------------------
    def write_health(self, s: XSState, extra: dict) -> None:
        dd = (s.peak_equity - s.equity) / s.peak_equity if s.peak_equity > 0 else 0.0
        h = {"ts": _utcnow().isoformat(), "instance": self.cfg.instance_name,
             "mode": self.mode, "venue": "hyperliquid", "live_trading": self.live_trading,
             "have_wallet": self.adapter.wallet is not None,
             "account": (self.adapter.address if self.adapter.wallet else None),
             "cycles_total": s.cycles_total, "rebalances_total": s.rebalances_total,
             "skips_total": s.skips_total, "equity": round(s.equity, 2),
             "peak_equity": round(s.peak_equity, 2), "drawdown_pct": round(dd * 100, 2),
             "cb_state": s.cb_state, "n_positions": len(s.positions),
             "fees_paid_total": round(s.fees_paid_total, 2),
             "last_rebalance_ts": s.last_rebalance_ts, "last_reconcile_ok": s.last_reconcile_ok,
             "config": {"lookback_days": self.cfg.lookback_days, "rebal_days": self.cfg.rebal_days,
                        "m": self.cfg.m, "universe": self.cfg.universe},
             **extra}
        tmp = self.health_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(h, indent=2))
        tmp.replace(self.health_path)

    def _should_rebalance(self, s: XSState, now: datetime) -> bool:
        if s.last_rebalance_ts is None:
            return True
        last = datetime.fromisoformat(s.last_rebalance_ts)
        return (now - last).total_seconds() >= self.cfg.rebal_days * 86400 - 3600

    # -- one cycle ---------------------------------------------------------
    def run_once(self) -> dict:
        cfg = self.cfg
        s = self.load_state()
        now = _utcnow()
        s.cycles_total += 1
        s.last_cycle_ts = now.isoformat()

        closes = self.adapter.daily_closes(cfg.universe, cfg.lookback_days)
        if len(closes) < cfg.min_assets:
            s.skips_total += 1
            self.save_state(s)
            self.write_health(s, {"last_action": "skip", "reason": f"{len(closes)} assets"})
            return {"action": "skip", "reason": f"insufficient data ({len(closes)} assets)"}
        mids = self.adapter.all_mids()

        s.equity = self.equity(s, mids)
        if s.cycles_total == 1:                      # anchor peak to the true starting equity
            s.peak_equity = s.equity
        if self.live_trading and s.equity < 5.0:     # live account not funded yet
            s.skips_total += 1
            self.save_state(s)
            self.write_health(s, {"last_action": "skip",
                                  "reason": "account unfunded — deposit USDC to trade"})
            return {"action": "skip",
                    "reason": f"account unfunded (eq={s.equity}); deposit USDC to {self.adapter.address}"}
        s.peak_equity = max(s.peak_equity, s.equity)
        dd = (s.peak_equity - s.equity) / s.peak_equity if s.peak_equity > 0 else 0.0
        if dd >= cfg.halt_drawdown_pct and s.cb_state != "halted":
            s.cb_state = "halted"
            self.log({"action": "circuit_breaker_halt", "drawdown_pct": round(dd * 100, 2)})
        if s.cb_state == "halted":
            self.reconcile(s, None)
            self.save_state(s)
            self.write_health(s, {"last_action": "halted"})
            return {"action": "halted", "drawdown_pct": round(dd * 100, 2)}

        result = {"action": "noop"}
        targets = None
        if self._should_rebalance(s, now):
            equity = self.equity(s, mids)
            targets = self._targets(closes, equity)
            if not targets:
                result = {"action": "skip", "reason": "no target weights"}
                s.skips_total += 1
            else:
                result = (self._execute_live(targets, now) if self.live_trading
                          else self._execute_sim(s, targets, mids, now))
                s.rebalances_total += 1
                s.last_rebalance_ts = now.isoformat()
            self.log(result)

        s.equity = self.equity(s, mids)
        s.peak_equity = max(s.peak_equity, s.equity)
        rec = self.reconcile(s, targets)
        if not rec["ok"]:
            self.log({"action": "reconcile", **rec})
        self.save_state(s)
        self.write_health(s, {"last_action": result["action"], "n_assets": len(closes),
                              "reconcile_ok": rec["ok"]})
        return {**result, "equity": round(s.equity, 2), "reconcile_ok": rec["ok"]}

    def run_loop(self, interval_sec: int = 3600) -> None:
        print(f"[hl_xs_runner] {self.cfg.instance_name} mode={self.mode} "
              f"live_trading={self.live_trading} lb={self.cfg.lookback_days} "
              f"rebal={self.cfg.rebal_days} m={self.cfg.m} universe={len(self.cfg.universe)}")
        while True:
            try:
                r = self.run_once()
                print(f"[{_utcnow().isoformat()}] {r.get('action')} eq={r.get('equity')} "
                      f"reconcile_ok={r.get('reconcile_ok')}")
            except Exception as e:
                print(f"[hl_xs_runner] cycle error: {e}", file=sys.stderr)
            import time
            time.sleep(interval_sec)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(PROJECT_ROOT / "configs" / "hl-xsectional-main.json"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval-sec", type=int, default=3600)
    args = ap.parse_args()
    cfg = load_config(args.config) if Path(args.config).exists() else HLXSConfig()
    runner = HLXSRunner(cfg)
    if args.loop:
        runner.run_loop(args.interval_sec)
        return 0
    print(json.dumps(runner.run_once(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
