#!/usr/bin/env python3
"""Plan E paper-trade runner.

Portfolio-native cross-sectional reversal strategy. Paper-only in this cut;
live mode requires explicit sign-off per Plan E final doc.

Usage:
    # One shot (cron-friendly, fires one rebalance if at rebalance time):
    python -m scripts.plan_e_runner --mode paper --once

    # Continuous loop (systemd-friendly):
    python -m scripts.plan_e_runner --mode paper --loop

    # Preview next rebalance without persisting:
    python -m scripts.plan_e_runner --mode paper --dry-run

Config: defaults are the validated Plan E config. Override via --config
pointing to a JSON file with the PlanEConfig fields.

State files (created under PROJECT_ROOT/state/):
    plan_e_portfolio.json   current portfolio state (cash/equity/positions)
    plan_e_trades.log       JSONL of every rebalance event

Signal invariant: at rebalance time t, we use the close of the LAST
completed 1h bar (t-1h end). We never peek at bars that haven't closed yet.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT))

from blofin_api import BlofinAPI  # noqa: E402
from backtest.data_collector import DataCollector  # noqa: E402


# =========================  Config  =========================

@dataclass
class PlanEConfig:
    universe: List[str] = field(default_factory=lambda: [
        "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT",
        "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "DOT-USDT", "LINK-USDT",
    ])
    lookback_hours: int = 72
    rebalance_hour_utc: int = 0          # UTC hour of daily rebalance
    long_n: int = 3
    short_n: int = 3
    leg_notional_pct: float = 0.10       # 10% of equity per leg
    k_exit: int = 6                      # hysteresis band
    signal_sign: int = -1                # -1 = REV (validated), +1 = MOM
    initial_balance: float = 5000.0
    min_assets_with_data: int = 6        # abort if universe shrinks below this
    fee_rate: float = 0.0006             # taker
    slippage_rate: float = 0.0005
    data_staleness_hours: int = 2        # newest bar must be within this window


# =========================  Portfolio state  =========================

@dataclass
class Position:
    symbol: str
    side: str                            # "long" | "short"
    entry_price: float
    notional: float                      # absolute $ notional at entry
    entered_ts: str


@dataclass
class PortfolioState:
    cash: float
    equity: float
    positions: Dict[str, Position] = field(default_factory=dict)
    last_rebalance_ts: Optional[str] = None
    rebalances_total: int = 0
    started_ts: Optional[str] = None

    def to_json(self) -> dict:
        return {
            "cash": self.cash,
            "equity": self.equity,
            "positions": {s: asdict(p) for s, p in self.positions.items()},
            "last_rebalance_ts": self.last_rebalance_ts,
            "rebalances_total": self.rebalances_total,
            "started_ts": self.started_ts,
        }

    @classmethod
    def from_json(cls, data: dict) -> "PortfolioState":
        positions = {s: Position(**p) for s, p in data.get("positions", {}).items()}
        return cls(
            cash=data["cash"],
            equity=data["equity"],
            positions=positions,
            last_rebalance_ts=data.get("last_rebalance_ts"),
            rebalances_total=data.get("rebalances_total", 0),
            started_ts=data.get("started_ts"),
        )


def unrealized_pnl(pos: Position, last_price: Optional[float]) -> float:
    """P&L for an open position at last_price. Returns 0 if price missing."""
    if last_price is None or last_price <= 0:
        return 0.0
    qty = pos.notional / pos.entry_price          # always positive
    if pos.side == "long":
        return qty * (last_price - pos.entry_price)
    else:
        return qty * (pos.entry_price - last_price)


def mark_equity(state: PortfolioState, last_prices: Dict[str, float]) -> float:
    """state.equity = cash + sum(unrealized P&L of open positions)."""
    eq = state.cash
    for sym, pos in state.positions.items():
        eq += unrealized_pnl(pos, last_prices.get(sym))
    return eq


# =========================  Signal + selection  =========================

def compute_signal(
    closes: Dict[str, pd.Series], lookback_h: int, sign: int,
) -> Dict[str, float]:
    """sign-adjusted log-return signal for each asset."""
    out: Dict[str, float] = {}
    for sym, series in closes.items():
        if len(series) <= lookback_h:
            continue
        latest = float(series.iloc[-1])
        past = float(series.iloc[-1 - lookback_h])
        if latest > 0 and past > 0 and np.isfinite(latest) and np.isfinite(past):
            out[sym] = sign * float(np.log(latest / past))
    return out


def rank_signals(signals: Dict[str, float]) -> List[str]:
    return [s for s, _ in sorted(signals.items(), key=lambda kv: -kv[1])]


def select_positions(
    ranked: List[str],
    current: Dict[str, Position],
    long_n: int,
    short_n: int,
    k_exit: int,
) -> Tuple[List[str], List[str]]:
    """Hysteresis selection. Retain a leg inside its keep band; fill from top/bottom."""
    keep_long_band = set(ranked[:k_exit])
    keep_short_band = set(ranked[-k_exit:])
    cur_longs = {s for s, p in current.items() if p.side == "long"}
    cur_shorts = {s for s, p in current.items() if p.side == "short"}

    new_longs: List[str] = [s for s in ranked if s in cur_longs and s in keep_long_band]
    for s in ranked:
        if len(new_longs) >= long_n:
            break
        if s not in new_longs:
            new_longs.append(s)
    new_longs = new_longs[:long_n]

    new_shorts: List[str] = [s for s in reversed(ranked)
                             if s in cur_shorts and s in keep_short_band]
    for s in reversed(ranked):
        if len(new_shorts) >= short_n:
            break
        if s not in new_shorts:
            new_shorts.append(s)
    new_shorts = new_shorts[:short_n]

    # A symbol can't be both long and short
    new_shorts = [s for s in new_shorts if s not in set(new_longs)]
    return new_longs, new_shorts


# =========================  Executor (paper)  =========================

def paper_execute_rebalance(
    state: PortfolioState,
    target_longs: List[str],
    target_shorts: List[str],
    last_prices: Dict[str, float],
    cfg: PlanEConfig,
    now_iso: str,
) -> dict:
    """Apply target allocations. Realizes P&L on closed legs, takes fees on turnover."""
    cost_per_side = cfg.fee_rate + cfg.slippage_rate
    changes: List[dict] = []
    fees_paid = 0.0

    target_long_set = set(target_longs)
    target_short_set = set(target_shorts)

    # 1. Close positions that don't match target side
    to_close = [
        sym for sym, pos in state.positions.items()
        if not ((pos.side == "long" and sym in target_long_set)
                or (pos.side == "short" and sym in target_short_set))
    ]
    for sym in to_close:
        pos = state.positions[sym]
        px = last_prices.get(sym)
        if px is None:
            logging.warning("Cannot close %s: no price; leaving in book", sym)
            continue
        pnl = unrealized_pnl(pos, px)
        fee = pos.notional * cost_per_side
        fees_paid += fee
        state.cash += pnl - fee
        changes.append({
            "action": "close", "symbol": sym, "side": pos.side,
            "entry_price": pos.entry_price, "exit_price": px,
            "notional": pos.notional, "pnl": pnl, "fee": fee,
        })
        del state.positions[sym]

    # 2. Size new legs off current equity (marked AFTER closes)
    eq_after_closes = mark_equity(state, last_prices)
    leg_size = eq_after_closes * cfg.leg_notional_pct

    def open_leg(sym: str, side: str) -> None:
        nonlocal fees_paid
        if sym in state.positions:
            return  # retained from previous cycle
        px = last_prices.get(sym)
        if px is None:
            logging.warning("Cannot open %s (%s): no price", sym, side)
            return
        fee = leg_size * cost_per_side
        fees_paid += fee
        state.cash -= fee
        state.positions[sym] = Position(
            symbol=sym, side=side, entry_price=px,
            notional=leg_size, entered_ts=now_iso,
        )
        changes.append({
            "action": "open", "symbol": sym, "side": side,
            "entry_price": px, "notional": leg_size, "fee": fee,
        })

    for sym in target_longs:
        open_leg(sym, "long")
    for sym in target_shorts:
        open_leg(sym, "short")

    state.equity = mark_equity(state, last_prices)

    return {
        "ts": now_iso,
        "action": "rebalance",
        "target_longs": target_longs,
        "target_shorts": target_shorts,
        "changes": changes,
        "fees_paid": fees_paid,
        "cash_after": state.cash,
        "equity_after": state.equity,
    }


# =========================  Runner  =========================

class PlanERunner:
    def __init__(
        self,
        cfg: PlanEConfig,
        mode: str = "paper",
        state_path: Optional[Path] = None,
        log_path: Optional[Path] = None,
    ) -> None:
        assert mode == "paper", "This build supports paper mode only."
        self.cfg = cfg
        self.mode = mode
        self.state_path = state_path or (PROJECT_ROOT / "state" / "plan_e_portfolio.json")
        self.log_path = log_path or (PROJECT_ROOT / "state" / "plan_e_trades.log")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.api = BlofinAPI(
            api_key=os.getenv("BLOFIN_API_KEY", "public"),
            api_secret=os.getenv("BLOFIN_API_SECRET", "public"),
            passphrase=os.getenv("BLOFIN_PASSPHRASE", "public"),
        )
        # Separate cache dir so runner doesn't clobber 12mo backtest CSVs
        runner_cache = PROJECT_ROOT / "state" / "runner_cache"
        runner_cache.mkdir(parents=True, exist_ok=True)
        self.dc = DataCollector(self.api, data_dir=str(runner_cache))

    def load_state(self) -> PortfolioState:
        if self.state_path.exists():
            with open(self.state_path) as f:
                return PortfolioState.from_json(json.load(f))
        return PortfolioState(
            cash=self.cfg.initial_balance,
            equity=self.cfg.initial_balance,
            started_ts=datetime.now(timezone.utc).isoformat(),
        )

    def save_state(self, state: PortfolioState) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(state.to_json(), f, indent=2, default=str)
        tmp.replace(self.state_path)

    def append_log(self, entry: dict) -> None:
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def fetch_closes(self) -> Tuple[Dict[str, pd.Series], Dict[str, float]]:
        """Fetch enough recent 1h bars to compute the signal. Enforces freshness."""
        closes: Dict[str, pd.Series] = {}
        last_prices: Dict[str, float] = {}
        now = datetime.now(timezone.utc)
        days_needed = max(5, (self.cfg.lookback_hours // 24) + 2)

        for sym in self.cfg.universe:
            try:
                df = self.dc.get_data(
                    inst_id=sym, bar="1H",
                    days=days_needed, force_refresh=True,
                )
                if len(df) < self.cfg.lookback_hours + 1:
                    logging.warning("%s: only %d bars (need >%d), skipping",
                                    sym, len(df), self.cfg.lookback_hours)
                    continue
                df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
                df = df.sort_values("timestamp").reset_index(drop=True)
                latest_bar_ts = df["timestamp"].iloc[-1]
                age_h = (now - latest_bar_ts.to_pydatetime()).total_seconds() / 3600.0
                if age_h > self.cfg.data_staleness_hours:
                    logging.warning("%s: latest bar %.1fh old, skipping", sym, age_h)
                    continue
                closes[sym] = df["close"]
                last_prices[sym] = float(df["close"].iloc[-1])
            except Exception as e:
                logging.error("%s: fetch failed: %s", sym, e)
            time.sleep(0.1)
        return closes, last_prices

    def do_rebalance_cycle(self, dry_run: bool = False) -> dict:
        state = self.load_state()
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        logging.info("=" * 60)
        logging.info("Rebalance cycle @ %s", now_iso)
        logging.info("State before: cash=$%.2f equity=$%.2f positions=%d",
                     state.cash, state.equity, len(state.positions))

        closes, last_prices = self.fetch_closes()
        if len(closes) < self.cfg.min_assets_with_data:
            msg = (f"Only {len(closes)} assets with usable data "
                   f"(need >= {self.cfg.min_assets_with_data}); aborting")
            logging.error(msg)
            return {"status": "aborted", "reason": msg, "ts": now_iso}

        # Mark-to-market current equity with fresh prices
        state.equity = mark_equity(state, last_prices)

        signals = compute_signal(closes, self.cfg.lookback_hours, self.cfg.signal_sign)
        ranked = rank_signals(signals)
        logging.info("Universe (%d): %s", len(ranked), ranked)

        new_longs, new_shorts = select_positions(
            ranked, state.positions,
            self.cfg.long_n, self.cfg.short_n, self.cfg.k_exit,
        )
        cur_longs = sorted(s for s, p in state.positions.items() if p.side == "long")
        cur_shorts = sorted(s for s, p in state.positions.items() if p.side == "short")
        logging.info("Current  longs: %s", cur_longs)
        logging.info("Target   longs: %s", new_longs)
        logging.info("Current shorts: %s", cur_shorts)
        logging.info("Target  shorts: %s", new_shorts)

        if dry_run:
            return {
                "status": "dry_run", "ts": now_iso,
                "ranked": ranked,
                "target_longs": new_longs,
                "target_shorts": new_shorts,
                "current_longs": cur_longs,
                "current_shorts": cur_shorts,
                "signals": signals,
                "marked_equity": state.equity,
            }

        trade = paper_execute_rebalance(
            state, new_longs, new_shorts, last_prices, self.cfg, now_iso,
        )
        state.last_rebalance_ts = now_iso
        state.rebalances_total += 1
        trade["rebalances_total"] = state.rebalances_total

        self.save_state(state)
        self.append_log(trade)
        logging.info("Rebalance done. Equity: $%.2f | fees this cycle: $%.4f",
                     state.equity, trade["fees_paid"])
        return {"status": "ok", **trade}

    def run_loop(self, rebalance_hour: int, check_interval_sec: int = 60) -> None:
        logging.info("Loop mode; rebalance daily at UTC %02d:00", rebalance_hour)
        last_fired_date = None
        while True:
            now = datetime.now(timezone.utc)
            in_window = (now.hour == rebalance_hour and now.minute < 5)
            if in_window and now.date() != last_fired_date:
                try:
                    self.do_rebalance_cycle()
                    last_fired_date = now.date()
                except Exception as e:
                    logging.exception("Rebalance cycle crashed: %s", e)
                    # Don't update last_fired_date; retry on next tick if still in window
            time.sleep(check_interval_sec)


# =========================  CLI  =========================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["paper"], default="paper")
    ap.add_argument("--once", action="store_true",
                    help="run one rebalance cycle and exit (cron)")
    ap.add_argument("--loop", action="store_true",
                    help="continuous loop, fire at rebalance hour (service)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print next rebalance's target, don't persist or trade")
    ap.add_argument("--config", default=None,
                    help="optional JSON config; defaults if not given")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    cfg = PlanEConfig()
    if args.config:
        with open(args.config) as f:
            cfg = PlanEConfig(**json.load(f))

    runner = PlanERunner(cfg, mode=args.mode)

    if args.loop:
        runner.run_loop(cfg.rebalance_hour_utc)
        return 0

    result = runner.do_rebalance_cycle(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") in ("ok", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
