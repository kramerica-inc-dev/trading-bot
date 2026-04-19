#!/usr/bin/env python3
"""Plan E paper-trade runner (multi-instance, feature-flagged).

Supports parallel paper-trade instances comparing strategy variants on the
same market data:
  - plan-e-base    : baseline Plan E (control)
  - plan-e-c       : + BTC vol-halt (Agent C)
  - plan-e-g       : + breadth tail-skip (Agent G)
  - plan-e-cg      : + C + G stacked
  - plan-e-i       : + outlier exclusion (Agent I)
  - plan-e-12h     : 12h rebalance cadence
  - plan-e-48h     : 48h rebalance cadence

Each instance has its own state directory and trade log but shares the
market-data cache so all variants see identical prices.

Usage:
    python -m scripts.plan_e_runner --mode paper --once --config configs/plan-e-base.json
    python -m scripts.plan_e_runner --mode paper --loop --config configs/plan-e-c.json
    python -m scripts.plan_e_runner --mode paper --dry-run --config configs/plan-e-cg.json
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT))

from blofin_api import BlofinAPI  # noqa: E402
from backtest.data_collector import DataCollector  # noqa: E402


# =========================  Feature-flag configs  =========================

@dataclass
class VolHaltConfig:
    """Agent C: skip rebalance when recent BTC vol >> long-window BTC vol."""
    enabled: bool = False
    k: float = 1.5                   # recent/MA ratio threshold
    vol_window_h: int = 24           # recent vol window
    ma_window_h: int = 720           # long-window (30d)


@dataclass
class BreadthSkipConfig:
    """Agent G: skip rebalance when breadth (% above SMA) hits tail."""
    enabled: bool = False
    sma_window_h: int = 200
    low_thr: float = 0.15            # skip if breadth <= this
    high_thr: float = 0.85           # skip if breadth >= this


@dataclass
class OutlierExcludeConfig:
    """Agent I: drop assets with |72h return / 60d sigma| > K from ranking."""
    enabled: bool = False
    k: float = 4.0
    sigma_window_h: int = 1440       # 60d of 1h bars


@dataclass
class StopLossConfig:
    """Agent TRAIL: per-leg trailing stop-loss, armed after favorable move.

    Mirrors the SL-TRAIL backtest: the stop only activates once the leg is
    already favorable by `arm_gain_pct`; then a `trail_pct` ratchet follows
    the peak (high for longs, low for shorts). If the stop triggers, the
    position is closed and stays flat until the next rebalance re-evaluates.
    """
    enabled: bool = False
    mode: str = "trailing"               # currently only "trailing" is supported
    trail_pct: float = 0.10              # trail distance once armed
    arm_gain_pct: float = 0.05           # arm after this much favorable move
    check_interval_sec: int = 300        # min seconds between stop-check fetches


# =========================  Main config  =========================

@dataclass
class PlanEConfig:
    # Identity + cadence
    instance_name: str = "plan-e-base"
    universe: List[str] = field(default_factory=lambda: [
        "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT",
        "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "DOT-USDT", "LINK-USDT",
    ])
    lookback_hours: int = 72
    rebalance_interval_hours: int = 24   # 12/24/48 supported; must divide 24 for 24h alignment
    rebalance_hour_utc: int = 0          # anchor hour (UTC)
    # Portfolio construction
    long_n: int = 3
    short_n: int = 3
    leg_notional_pct: float = 0.10
    k_exit: int = 6
    signal_sign: int = -1                # -1 = REV (validated), +1 = MOM
    initial_balance: float = 5000.0
    min_assets_with_data: int = 6
    fee_rate: float = 0.0006
    slippage_rate: float = 0.0005
    data_staleness_hours: int = 2
    # Feature flags
    vol_halt: VolHaltConfig = field(default_factory=VolHaltConfig)
    breadth_skip: BreadthSkipConfig = field(default_factory=BreadthSkipConfig)
    outlier_exclude: OutlierExcludeConfig = field(default_factory=OutlierExcludeConfig)
    stop_loss: StopLossConfig = field(default_factory=StopLossConfig)


def load_config(path: Optional[str]) -> PlanEConfig:
    """Load JSON config with nested feature blocks; defaults if path is None."""
    if not path:
        return PlanEConfig()
    with open(path) as f:
        data = json.load(f)
    vh = VolHaltConfig(**data.pop("vol_halt", {}))
    bs = BreadthSkipConfig(**data.pop("breadth_skip", {}))
    oe = OutlierExcludeConfig(**data.pop("outlier_exclude", {}))
    sl = StopLossConfig(**data.pop("stop_loss", {}))
    return PlanEConfig(
        vol_halt=vh, breadth_skip=bs, outlier_exclude=oe, stop_loss=sl, **data,
    )


# =========================  Portfolio state  =========================

@dataclass
class Position:
    symbol: str
    side: str                            # "long" | "short"
    entry_price: float
    notional: float
    entered_ts: str
    # Trailing-stop state (populated only when StopLossConfig.enabled).
    # Kept Optional so old portfolio.json files (without these fields) still
    # deserialize via Position(**p).
    peak_price: Optional[float] = None       # running high (long) / low (short)
    stop_armed: bool = False
    stop_level: Optional[float] = None       # current trail level


@dataclass
class PortfolioState:
    cash: float
    equity: float
    positions: Dict[str, Position] = field(default_factory=dict)
    last_rebalance_ts: Optional[str] = None
    rebalances_total: int = 0
    skips_total: int = 0
    stop_losses_total: int = 0
    started_ts: Optional[str] = None

    def to_json(self) -> dict:
        return {
            "cash": self.cash,
            "equity": self.equity,
            "positions": {s: asdict(p) for s, p in self.positions.items()},
            "last_rebalance_ts": self.last_rebalance_ts,
            "rebalances_total": self.rebalances_total,
            "skips_total": self.skips_total,
            "stop_losses_total": self.stop_losses_total,
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
            skips_total=data.get("skips_total", 0),
            stop_losses_total=data.get("stop_losses_total", 0),
            started_ts=data.get("started_ts"),
        )


def unrealized_pnl(pos: Position, last_price: Optional[float]) -> float:
    if last_price is None or last_price <= 0:
        return 0.0
    qty = pos.notional / pos.entry_price
    if pos.side == "long":
        return qty * (last_price - pos.entry_price)
    else:
        return qty * (pos.entry_price - last_price)


def mark_equity(state: PortfolioState, last_prices: Dict[str, float]) -> float:
    eq = state.cash
    for sym, pos in state.positions.items():
        eq += unrealized_pnl(pos, last_prices.get(sym))
    return eq


# =========================  Signal + selection  =========================

def compute_signal(
    closes: Dict[str, pd.Series], lookback_h: int, sign: int,
) -> Dict[str, float]:
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

    new_shorts = [s for s in new_shorts if s not in set(new_longs)]
    return new_longs, new_shorts


# =========================  Feature gates  =========================

def compute_vol_halt(
    closes: Dict[str, pd.Series], cfg: VolHaltConfig,
) -> Tuple[bool, Dict[str, Any]]:
    """Return (should_halt, diagnostics)."""
    btc = closes.get("BTC-USDT")
    if btc is None or len(btc) < cfg.ma_window_h + 2:
        return False, {"enabled": cfg.enabled, "reason": "insufficient BTC data"}
    log_rets = np.log(btc / btc.shift(1)).dropna()
    if len(log_rets) < cfg.ma_window_h:
        return False, {"enabled": cfg.enabled, "reason": "insufficient returns"}
    recent_vol = float(log_rets.iloc[-cfg.vol_window_h:].std())
    ma_vol = float(log_rets.iloc[-cfg.ma_window_h:].std())
    if ma_vol <= 0:
        return False, {"enabled": cfg.enabled, "reason": "zero MA vol"}
    ratio = recent_vol / ma_vol
    halt = cfg.enabled and ratio > cfg.k
    return halt, {
        "enabled": cfg.enabled, "ratio": ratio, "k": cfg.k,
        "recent_vol": recent_vol, "ma_vol": ma_vol, "halt": halt,
    }


def compute_breadth_skip(
    closes: Dict[str, pd.Series], cfg: BreadthSkipConfig,
) -> Tuple[bool, Dict[str, Any]]:
    above = 0
    total = 0
    for sym, ser in closes.items():
        if len(ser) < cfg.sma_window_h:
            continue
        sma = float(ser.iloc[-cfg.sma_window_h:].mean())
        total += 1
        if float(ser.iloc[-1]) > sma:
            above += 1
    if total == 0:
        return False, {"enabled": cfg.enabled, "reason": "no assets with sufficient history"}
    breadth = above / total
    skip = cfg.enabled and (breadth <= cfg.low_thr or breadth >= cfg.high_thr)
    return skip, {
        "enabled": cfg.enabled, "breadth": breadth, "low": cfg.low_thr,
        "high": cfg.high_thr, "skip": skip, "n_assets": total,
    }


def compute_outlier_set(
    closes: Dict[str, pd.Series], cfg: OutlierExcludeConfig, lookback_h: int,
) -> Tuple[set, Dict[str, Any]]:
    excluded: set = set()
    per_sym: Dict[str, Any] = {}
    for sym, ser in closes.items():
        need = max(cfg.sigma_window_h + 1, lookback_h + 1)
        if len(ser) < need:
            continue
        r_lb = float(np.log(ser.iloc[-1] / ser.iloc[-1 - lookback_h]))
        hourly_rets = np.log(ser / ser.shift(1)).dropna()
        if len(hourly_rets) < cfg.sigma_window_h:
            continue
        sigma_hr = float(hourly_rets.iloc[-cfg.sigma_window_h:].std())
        sigma_lb = sigma_hr * np.sqrt(lookback_h)
        if sigma_lb <= 0:
            continue
        z = r_lb / sigma_lb
        per_sym[sym] = {"return": r_lb, "sigma": sigma_lb, "z": z}
        if cfg.enabled and abs(z) > cfg.k:
            excluded.add(sym)
    return excluded, {"enabled": cfg.enabled, "k": cfg.k, "excluded": sorted(excluded),
                      "per_symbol": per_sym}


# =========================  Executor (paper)  =========================

def paper_execute_rebalance(
    state: PortfolioState,
    target_longs: List[str],
    target_shorts: List[str],
    last_prices: Dict[str, float],
    cfg: PlanEConfig,
    now_iso: str,
) -> dict:
    cost_per_side = cfg.fee_rate + cfg.slippage_rate
    changes: List[dict] = []
    fees_paid = 0.0

    target_long_set = set(target_longs)
    target_short_set = set(target_shorts)

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

    eq_after_closes = mark_equity(state, last_prices)
    leg_size = eq_after_closes * cfg.leg_notional_pct

    def open_leg(sym: str, side: str) -> None:
        nonlocal fees_paid
        if sym in state.positions:
            return
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
        instance_dir = PROJECT_ROOT / "state" / cfg.instance_name
        instance_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = state_path or (instance_dir / "portfolio.json")
        self.log_path = log_path or (instance_dir / "trades.log")
        self.api = BlofinAPI(
            api_key=os.getenv("BLOFIN_API_KEY", "public"),
            api_secret=os.getenv("BLOFIN_API_SECRET", "public"),
            passphrase=os.getenv("BLOFIN_PASSPHRASE", "public"),
        )
        # SHARED cache: all instances see identical prices.
        shared_cache = PROJECT_ROOT / "state" / "shared_cache"
        shared_cache.mkdir(parents=True, exist_ok=True)
        self.dc = DataCollector(self.api, data_dir=str(shared_cache))
        logging.info("Runner instance=%s state=%s", cfg.instance_name, self.state_path)

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
        closes: Dict[str, pd.Series] = {}
        last_prices: Dict[str, float] = {}
        now = datetime.now(timezone.utc)
        # need enough bars for longest feature window (30d vol-halt ma, 60d outlier sigma)
        feature_min_days = 0
        if self.cfg.vol_halt.enabled:
            feature_min_days = max(feature_min_days, self.cfg.vol_halt.ma_window_h // 24 + 2)
        if self.cfg.outlier_exclude.enabled:
            feature_min_days = max(feature_min_days,
                                   self.cfg.outlier_exclude.sigma_window_h // 24 + 2)
        if self.cfg.breadth_skip.enabled:
            feature_min_days = max(feature_min_days,
                                   self.cfg.breadth_skip.sma_window_h // 24 + 2)
        days_needed = max(5, (self.cfg.lookback_hours // 24) + 2, feature_min_days)

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
        logging.info("Cycle @ %s [%s]", now_iso, self.cfg.instance_name)
        logging.info("State before: cash=$%.2f equity=$%.2f positions=%d",
                     state.cash, state.equity, len(state.positions))

        closes, last_prices = self.fetch_closes()
        if len(closes) < self.cfg.min_assets_with_data:
            msg = (f"Only {len(closes)} assets with usable data "
                   f"(need >= {self.cfg.min_assets_with_data}); aborting")
            logging.error(msg)
            return {"status": "aborted", "reason": msg, "ts": now_iso}

        state.equity = mark_equity(state, last_prices)

        # Feature gates: vol-halt and breadth-skip may abort the rebalance.
        halt_reasons: List[str] = []
        gate_diag: Dict[str, Any] = {}

        halt_vol, diag_vol = compute_vol_halt(closes, self.cfg.vol_halt)
        gate_diag["vol_halt"] = diag_vol
        if halt_vol:
            halt_reasons.append("vol_halt")

        skip_breadth, diag_breadth = compute_breadth_skip(closes, self.cfg.breadth_skip)
        gate_diag["breadth_skip"] = diag_breadth
        if skip_breadth:
            halt_reasons.append("breadth_skip")

        excluded, diag_outlier = compute_outlier_set(
            closes, self.cfg.outlier_exclude, self.cfg.lookback_hours)
        gate_diag["outlier_exclude"] = diag_outlier

        if halt_reasons and not dry_run:
            state.skips_total += 1
            state.last_rebalance_ts = now_iso  # count the tick so cadence advances
            skip_entry = {
                "ts": now_iso,
                "action": "skip",
                "reasons": halt_reasons,
                "gates": gate_diag,
                "cash_after": state.cash,
                "equity_after": state.equity,
                "skips_total": state.skips_total,
            }
            self.save_state(state)
            self.append_log(skip_entry)
            logging.info("SKIP [%s]: %s | equity=$%.2f",
                         self.cfg.instance_name, halt_reasons, state.equity)
            return {"status": "skipped", **skip_entry}

        signals = compute_signal(closes, self.cfg.lookback_hours, self.cfg.signal_sign)
        for sym in excluded:
            signals.pop(sym, None)
        ranked = rank_signals(signals)
        logging.info("Universe (%d, excluded=%s): %s",
                     len(ranked), sorted(excluded) if excluded else [], ranked)

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
                "instance": self.cfg.instance_name,
                "ranked": ranked,
                "excluded": sorted(excluded),
                "gates": gate_diag,
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
        trade["gates"] = gate_diag
        trade["excluded"] = sorted(excluded)
        state.last_rebalance_ts = now_iso
        state.rebalances_total += 1
        trade["rebalances_total"] = state.rebalances_total
        trade["instance"] = self.cfg.instance_name

        self.save_state(state)
        self.append_log(trade)
        logging.info("Rebalance done [%s]. Equity: $%.2f | fees: $%.4f",
                     self.cfg.instance_name, state.equity, trade["fees_paid"])
        return {"status": "ok", **trade}

    def check_stops_cycle(self, now: datetime) -> Optional[dict]:
        """Trailing-stop check on open positions.

        Uses the latest 1h OHLC bar per symbol. For each open position:
          1. Update peak (long: bar.high; short: bar.low).
          2. Arm once peak reaches entry*(1+arm_gain) long / entry*(1-arm_gain) short.
          3. While armed, ratchet stop to peak*(1-trail) / peak*(1+trail).
          4. Trigger if bar.low <= stop (long) or bar.high >= stop (short).
             Fill at bar.open on gap-through, else at stop level.
          5. On trigger: realize PnL, deduct one-side fee, remove position.
             The position stays closed until the next rebalance re-selects it.

        Returns a summary dict when anything changes; None otherwise.
        """
        cfg = self.cfg
        if not cfg.stop_loss.enabled or cfg.stop_loss.mode != "trailing":
            return None

        state = self.load_state()
        if not state.positions:
            return None

        cost_per_side = cfg.fee_rate + cfg.slippage_rate
        sl = cfg.stop_loss
        events: List[dict] = []
        armings: List[dict] = []
        changed = False
        last_close: Dict[str, float] = {}

        for sym in list(state.positions.keys()):
            pos = state.positions[sym]
            try:
                df = self.dc.get_data(inst_id=sym, bar="1H", days=2, force_refresh=True)
            except Exception as e:
                logging.debug("stop-check fetch %s failed: %s", sym, e)
                continue
            if len(df) == 0:
                continue
            df = df.copy()
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df = df.sort_values("timestamp").reset_index(drop=True)
            latest = df.iloc[-1]
            age_h = (now - latest["timestamp"].to_pydatetime()).total_seconds() / 3600.0
            if age_h > cfg.data_staleness_hours:
                logging.debug("stop-check %s: latest bar %.1fh old, skipping", sym, age_h)
                continue

            bar_open = float(latest["open"])
            bar_high = float(latest["high"])
            bar_low = float(latest["low"])
            bar_close = float(latest["close"])
            last_close[sym] = bar_close

            if pos.side == "long":
                prior_peak = pos.peak_price if pos.peak_price is not None else pos.entry_price
                new_peak = max(prior_peak, bar_high)
                if new_peak != prior_peak or pos.peak_price is None:
                    pos.peak_price = new_peak
                    changed = True
                if not pos.stop_armed and new_peak >= pos.entry_price * (1.0 + sl.arm_gain_pct):
                    pos.stop_armed = True
                    armings.append({"symbol": sym, "side": "long",
                                    "peak": new_peak, "entry": pos.entry_price})
                    changed = True
                if pos.stop_armed:
                    candidate = new_peak * (1.0 - sl.trail_pct)
                    if pos.stop_level is None or candidate > pos.stop_level:
                        pos.stop_level = candidate
                        changed = True
                    if bar_low <= pos.stop_level:
                        fill = bar_open if bar_open <= pos.stop_level else pos.stop_level
                        qty = pos.notional / pos.entry_price
                        pnl = qty * (fill - pos.entry_price)
                        fee = pos.notional * cost_per_side
                        state.cash += pnl - fee
                        state.stop_losses_total += 1
                        events.append({
                            "ts": now.isoformat(),
                            "action": "stop_loss",
                            "instance": cfg.instance_name,
                            "symbol": sym, "side": "long",
                            "entry_price": pos.entry_price,
                            "fill_price": fill,
                            "stop_level": pos.stop_level,
                            "peak_price": new_peak,
                            "bar_open": bar_open, "bar_high": bar_high,
                            "bar_low": bar_low, "bar_close": bar_close,
                            "bar_ts": latest["timestamp"].isoformat(),
                            "gap_fill": bar_open <= pos.stop_level,
                            "notional": pos.notional, "pnl": pnl, "fee": fee,
                            "stop_losses_total": state.stop_losses_total,
                        })
                        del state.positions[sym]
                        changed = True
                        continue
            else:  # short
                prior_peak = pos.peak_price if pos.peak_price is not None else pos.entry_price
                new_peak = min(prior_peak, bar_low)
                if new_peak != prior_peak or pos.peak_price is None:
                    pos.peak_price = new_peak
                    changed = True
                if not pos.stop_armed and new_peak <= pos.entry_price * (1.0 - sl.arm_gain_pct):
                    pos.stop_armed = True
                    armings.append({"symbol": sym, "side": "short",
                                    "peak": new_peak, "entry": pos.entry_price})
                    changed = True
                if pos.stop_armed:
                    candidate = new_peak * (1.0 + sl.trail_pct)
                    if pos.stop_level is None or candidate < pos.stop_level:
                        pos.stop_level = candidate
                        changed = True
                    if bar_high >= pos.stop_level:
                        fill = bar_open if bar_open >= pos.stop_level else pos.stop_level
                        qty = pos.notional / pos.entry_price
                        pnl = qty * (pos.entry_price - fill)
                        fee = pos.notional * cost_per_side
                        state.cash += pnl - fee
                        state.stop_losses_total += 1
                        events.append({
                            "ts": now.isoformat(),
                            "action": "stop_loss",
                            "instance": cfg.instance_name,
                            "symbol": sym, "side": "short",
                            "entry_price": pos.entry_price,
                            "fill_price": fill,
                            "stop_level": pos.stop_level,
                            "peak_price": new_peak,
                            "bar_open": bar_open, "bar_high": bar_high,
                            "bar_low": bar_low, "bar_close": bar_close,
                            "bar_ts": latest["timestamp"].isoformat(),
                            "gap_fill": bar_open >= pos.stop_level,
                            "notional": pos.notional, "pnl": pnl, "fee": fee,
                            "stop_losses_total": state.stop_losses_total,
                        })
                        del state.positions[sym]
                        changed = True
                        continue

            time.sleep(0.05)

        if not changed:
            return None

        state.equity = mark_equity(state, last_close)
        self.save_state(state)
        for ev in events:
            self.append_log(ev)
        if events:
            logging.info("STOP-LOSS [%s]: triggered %d leg(s): %s | equity=$%.2f",
                         cfg.instance_name, len(events),
                         [e["symbol"] for e in events], state.equity)
        return {"triggered": len(events), "armed": len(armings),
                "stop_events": events, "arm_events": armings}

    def _should_fire_now(self, state: PortfolioState, now: datetime) -> bool:
        """Cadence check. Supports arbitrary rebalance_interval_hours."""
        interval_h = self.cfg.rebalance_interval_hours
        anchor = self.cfg.rebalance_hour_utc

        # Must be in a slot aligned to (anchor, interval_h) within first 5 minutes.
        hour_ok = (now.hour % interval_h == anchor % interval_h) and now.minute < 5
        if not hour_ok:
            return False

        if state.last_rebalance_ts is None:
            return True

        last = datetime.fromisoformat(state.last_rebalance_ts)
        elapsed_h = (now - last).total_seconds() / 3600.0
        # Require ~interval_h elapsed (with small tolerance so a late-by-1-min loop still fires).
        return elapsed_h >= interval_h - 0.5

    def run_loop(self, check_interval_sec: int = 60) -> None:
        cfg = self.cfg
        logging.info(
            "Loop mode [%s]; interval=%dh anchor=UTC %02d:00 vol_halt=%s breadth=%s outlier=%s stop_loss=%s",
            cfg.instance_name, cfg.rebalance_interval_hours, cfg.rebalance_hour_utc,
            cfg.vol_halt.enabled, cfg.breadth_skip.enabled, cfg.outlier_exclude.enabled,
            cfg.stop_loss.enabled,
        )
        last_stop_check: Optional[datetime] = None
        while True:
            now = datetime.now(timezone.utc)
            state = self.load_state()
            if self._should_fire_now(state, now):
                try:
                    self.do_rebalance_cycle()
                except Exception as e:
                    logging.exception("Cycle crashed: %s", e)
                # After a rebalance, reset stop-check throttle so the next
                # tick can record peaks immediately.
                last_stop_check = None
            elif cfg.stop_loss.enabled:
                # Throttle stop-check fetches to avoid hammering the API.
                interval = max(30, int(cfg.stop_loss.check_interval_sec))
                due = (last_stop_check is None
                       or (now - last_stop_check).total_seconds() >= interval)
                if due:
                    try:
                        self.check_stops_cycle(now)
                    except Exception as e:
                        logging.exception("Stop-check crashed: %s", e)
                    last_stop_check = now
            time.sleep(check_interval_sec)


# =========================  CLI  =========================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["paper"], default="paper")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default=None,
                    help="JSON config; defaults if omitted")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    cfg = load_config(args.config)
    runner = PlanERunner(cfg, mode=args.mode)

    if args.loop:
        runner.run_loop()
        return 0

    result = runner.do_rebalance_cycle(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("status") in ("ok", "dry_run", "skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
