#!/usr/bin/env python3
"""Daily-bar backtest harness for long-or-flat, position-sized strategies.

This is the M0 milestone of `docs/STRATEGY-V1-TREND-VOLTARGET.md`.  It is a
*thin* companion to the 5m `backtest/backtester.py` — it reuses that module's
Fase-1 metric machinery (`BacktestResult`, the benchmark / drawdown / Calmar /
conditional-regime helpers) wholesale.  It does NOT touch the 5m engine.

What it simulates, once per daily bar:

  * a strategy decides — using only *closed* bars up to and including day t (the
    lookahead-audit convention) — a **target exposure fraction in [0, L_max]**
    for day t+1 (0 == flat, 1.0 == fully invested, no leverage in v1);
  * if the relative change vs the current exposure exceeds a **no-trade band**
    the position is rebalanced (buy/sell the *delta only*); fees + slippage are
    charged on the traded notional delta, not the whole position;
  * fees use a blended **maker/taker mix** (config knob);
  * a long position pays **funding** at each 8h settlement (~3×/day) from the
    real funding series, scaled by exposure × notional;
  * equity is marked to that day's close; the per-day equity curve, BTC close
    series, exposure / turnover / fee / funding diagnostics are recorded.

The result is wrapped in `BacktestResult` so every Fase-1 metric (return,
Calmar, max-DD, DD-duration, time-under-water, alpha-vs-BH, bull/bear/sideways
conditional metrics) comes for free.

Strategy protocol (duck-typed):

    class MyStrategy:
        # optional — called once before the run
        def reset(self): ...
        # required — `history` is a DataFrame of fully-closed daily bars
        # (columns: timestamp, open, high, low, close, volume), the LAST row
        # being day t.  Return the target exposure fraction for day t+1.
        def target_exposure(self, history: pd.DataFrame) -> float: ...
"""

import os
import sys
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from backtester import BacktestConfig, BacktestResult  # noqa: E402  (sibling module)


# ---------------------------------------------------------------------------
# Funding series loading
# ---------------------------------------------------------------------------

def load_funding_series(path: str) -> pd.DataFrame:
    """Load an 8h funding CSV (as produced by `fetch_funding_history`).

    Returns a DataFrame with a tz-aware 'timestamp' (settlement time) and a
    'funding_rate' column, sorted ascending.  Missing/empty file -> empty frame.
    """
    if not path or not os.path.exists(path):
        return pd.DataFrame(columns=["timestamp", "funding_rate"])
    df = pd.read_csv(path)
    if df.empty:
        return pd.DataFrame(columns=["timestamp", "funding_rate"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df[["timestamp", "funding_rate"]]


def _funding_paid_over(funding_df: pd.DataFrame, start, end) -> float:
    """Sum of funding rates whose settlement time falls in (start, end].

    A long position holding over that window pays `notional * sum_of_rates`
    (positive rate == longs pay).  `start`/`end` are tz-aware Timestamps.
    """
    if funding_df is None or funding_df.empty:
        return 0.0
    mask = (funding_df["timestamp"] > start) & (funding_df["timestamp"] <= end)
    if not mask.any():
        return 0.0
    return float(funding_df.loc[mask, "funding_rate"].sum())


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class DailyBacktestConfig:
    """Configuration for the daily-bar backtest harness.

    Costs are modelled honestly but kept simple (turnover at vol-target +
    no-trade band is low, so they're second-order):

      fee = maker_fraction * fee_maker + (1 - maker_fraction) * fee_taker,
            charged on the |traded notional delta| at each rebalance;
      slippage = slippage_pct of the |traded notional delta|.
    """
    initial_balance: float = 5000.0      # spec §1 — realistic v1 book size
    fee_maker: float = 0.0002            # post-only maker rate
    fee_taker: float = 0.0006            # taker rate (matches the 5m baseline)
    maker_fraction: float = 0.80         # assumed maker fill rate (spec §5)
    slippage_pct: float = 0.05           # 0.05% per fill (matches 5m baseline)
    no_trade_band_pct: float = 15.0      # rebalance only if |Δexposure|/cur > this %
    L_max: float = 1.0                   # max exposure fraction (no leverage in v1)
    funding_series_path: Optional[str] = None  # path to 8h funding CSV; None -> no funding

    @property
    def blended_fee(self) -> float:
        f = max(0.0, min(1.0, self.maker_fraction))
        return f * self.fee_maker + (1.0 - f) * self.fee_taker


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class DailyBacktester:
    """Replays a daily OHLCV DataFrame through a long-or-flat sized strategy."""

    # number of decimal places to round exposures to before band comparison —
    # avoids float-noise rebalances
    _EPS = 1e-9

    def __init__(self, strategy, config: Optional[DailyBacktestConfig] = None):
        self.strategy = strategy
        self.config = config or DailyBacktestConfig()
        self._funding_df = load_funding_series(self.config.funding_series_path) \
            if self.config.funding_series_path else pd.DataFrame()

    def run(self, daily_df: pd.DataFrame) -> BacktestResult:
        cfg = self.config
        df = daily_df.reset_index(drop=True)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        n = len(df)
        if n < 2:
            raise ValueError("daily_backtester needs at least 2 bars")

        if hasattr(self.strategy, "reset"):
            self.strategy.reset()

        closes = df["close"].astype(float).tolist()
        timestamps = df["timestamp"].tolist() if "timestamp" in df.columns else list(range(n))

        balance = float(cfg.initial_balance)   # cash + position value, marked daily
        exposure = 0.0                          # current fraction of equity in BTC
        # equity curve starts at bar 0 (flat, full cash)
        equity_curve: List[float] = [balance]
        ec_closes: List[float] = [closes[0]]
        ec_ts: List = [timestamps[0]]

        # diagnostics, one entry per equity-curve point (index 0 = flat start)
        exposures: List[float] = [0.0]
        turnover_frac: List[float] = [0.0]   # |Δexposure| traded that bar
        fees_paid: List[float] = [0.0]
        funding_paid: List[float] = [0.0]
        n_rebalances = 0

        band = cfg.no_trade_band_pct / 100.0
        blended_fee = cfg.blended_fee
        slip = cfg.slippage_pct / 100.0

        # Walk bars 0..n-1.  At bar t the strategy sees history[0..t] (all
        # closed) and sets the target exposure that we *enter* at bar t's close
        # and hold through bar t+1.  Equity is then marked at bar t+1's close.
        for t in range(0, n - 1):
            history = df.iloc[: t + 1]
            try:
                target = float(self.strategy.target_exposure(history))
            except Exception:
                target = 0.0
            if not np.isfinite(target):
                target = 0.0
            target = max(0.0, min(cfg.L_max, target))

            # --- decide whether to rebalance (no-trade band) ---
            prev_exposure = exposure
            if prev_exposure <= self._EPS:
                # currently flat: any non-trivial target triggers entry
                do_rebalance = target > self._EPS
            else:
                rel_change = abs(target - prev_exposure) / prev_exposure
                do_rebalance = rel_change > band
            # always allow going fully flat (risk reduction is never blocked)
            if target <= self._EPS and prev_exposure > self._EPS:
                do_rebalance = True

            traded = 0.0
            fee_cost = 0.0
            if do_rebalance:
                traded = abs(target - prev_exposure)
                # cost charged on the traded notional fraction of *current* equity
                fee_cost = traded * balance * (blended_fee + slip)
                balance -= fee_cost
                exposure = target
                n_rebalances += 1
            else:
                exposure = prev_exposure  # hold

            # --- funding over the (t -> t+1) holding day ---
            t0 = timestamps[t]
            t1 = timestamps[t + 1]
            fund_cost = 0.0
            if exposure > self._EPS and not self._funding_df.empty \
                    and hasattr(t0, "to_pydatetime"):
                rate_sum = _funding_paid_over(self._funding_df, t0, t1)
                # notional held = exposure * equity (post-fee), longs pay positive
                fund_cost = exposure * balance * rate_sum
                balance -= fund_cost

            # --- mark to bar t+1 close: the BTC leg moves with price ---
            px0 = closes[t]
            px1 = closes[t + 1]
            if px0 > 0:
                ret = px1 / px0 - 1.0
            else:
                ret = 0.0
            # equity = cash part + BTC part; cash part untouched, BTC part scales
            # by (1+ret).  Equivalently: equity *= (1 + exposure*ret).
            balance = balance * (1.0 + exposure * ret)

            equity_curve.append(balance)
            ec_closes.append(px1)
            ec_ts.append(t1)
            exposures.append(exposure)
            turnover_frac.append(traded)
            fees_paid.append(fee_cost)
            funding_paid.append(fund_cost)

        # Wrap in the Fase-1 BacktestResult.  It expects a BacktestConfig; build
        # a minimal one carrying the right initial_balance for ROI math.
        bt_cfg = BacktestConfig(initial_balance=cfg.initial_balance)
        result = BacktestResult([], equity_curve, ec_ts, bt_cfg, closes=ec_closes)

        # BacktestResult zeroes total_roi / total_pnl / max_drawdown_pct when
        # there are no *trades* — but this harness is trade-list-free (positions
        # are continuous, not discrete round-trips), so derive those from the
        # equity curve directly.  (Calmar / DD-duration / time-under-water / the
        # benchmark / conditional metrics are already computed from the curve.)
        eq0, eqN = float(equity_curve[0]), float(equity_curve[-1])
        result.total_roi = (eqN / eq0 - 1.0) * 100.0 if eq0 else 0.0
        result.total_pnl = eqN - eq0
        eq_arr = np.asarray(equity_curve, dtype=float)
        peak = np.maximum.accumulate(eq_arr)
        with np.errstate(divide="ignore", invalid="ignore"):
            dd = np.where(peak > 0, (peak - eq_arr) / peak, 0.0)
        result.max_drawdown_pct = float(np.max(dd)) * 100.0 if len(dd) else 0.0
        result.max_drawdown = float(np.max(peak - eq_arr)) if len(eq_arr) else 0.0
        result._finalize_alpha()  # recompute alpha now that total_roi is set

        # Attach the daily-specific diagnostics.
        result.daily_exposure = exposures
        result.daily_turnover = turnover_frac
        result.daily_fees = fees_paid
        result.daily_funding = funding_paid
        result.total_fees = float(np.sum(fees_paid))
        result.total_funding = float(np.sum(funding_paid))
        result.total_turnover_frac = float(np.sum(turnover_frac))
        result.n_rebalances = int(n_rebalances)
        result.avg_exposure = float(np.mean(exposures))
        result.time_in_market_frac = float(np.mean([1.0 if e > self._EPS else 0.0
                                                    for e in exposures]))
        result.daily_config = cfg
        return result


def load_daily_btc(path: Optional[str] = None) -> pd.DataFrame:
    """Convenience loader for the canonical daily BTC CSV."""
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "BTC-USDT_1d.csv")
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


DEFAULT_FUNDING_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "data", "funding_btc_usdt.csv")
