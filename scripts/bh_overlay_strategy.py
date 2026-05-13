#!/usr/bin/env python3
"""Pure-function strategy logic for the BH-overlay fallback runner.

Composition (the entire strategy in one line):

    target_exposure = VolTarget(σ_target, vol_window, L_max).exposure_for(history)
                      * (1 if TrailingStopBH(X, N, reenter=True) is in-market else 0)

i.e. M3's vol-target sizing on top of B2 (the re-entering 10%/20d trailing-stop
overlay on BH from `backtest/daily_strategies.py`).  Defaults: σ_target = 0.20
annualised, vol_window = 30d, L_max = 1.0, X = 0.10, N = 20.

> **Honest framing — this is the documented fallback, not a demonstrated edge.**
>
> Per `DECISIONS.md` (2026-05-13) the carry strategy is parked (OKX EU = MiCA
> retail spot-only cap; BloFin = no spot trading API), so the project falls back
> to "this account holds BTC with a drawdown circuit-breaker + vol-targeting"
> — the legitimate endpoint flagged in the 2026-05-12 entry's last paragraph
> when active-strategy candidates don't clear the bar.
>
> The §7.1 random-entry null gate (see `docs/STRATEGY-V1-RESULTS.md`) was NOT
> cleared by the trailing-stop trend rule out-of-sample on the 3.3-year BTC
> series: the rule sits at the 84th percentile of its matched null Calmar,
> inside the 5–95 band.  This module composes the same rule with the M3
> vol-target overlay anyway because (a) DECISIONS.md records it as the
> fallback, (b) on the full series it is marginally risk-adjusted-better than
> plain BH (Calmar +0.81 / max-DD 28% vs BH +0.77 / 52%; the vol-target overlay
> further halves the in-trade DD), and (c) it is EU-executable on spot only
> (no perp short leg, no funding cost, no MiCA blocker).

Public API:

    from bh_overlay_strategy import BHOverlayStrategy, StrategyDecision

    strat = BHOverlayStrategy()  # default params from DECISIONS.md
    decision = strat.decide(daily_df, current_exposure)
    # decision.target_exposure ∈ [0, L_max]
    # decision.signal_on  (bool — trend filter says we should be long)
    # decision.vol_realized, decision.vol_target, decision.drawdown_pct,
    # decision.peak_price, decision.reason

The class is stateful (the trailing-stop state machine carries `in_market` and
`running_high` across days), but `decide()` itself is deterministic given the
history + the internal state, and the state can be serialized to dict and
restored — see `to_state()` / `from_state()`.  No I/O, no network.

This module is intentionally a thin composition layer.  The actual rule
machinery lives in `backtest/v1_strategies.py` (VolTarget, realized_vol) and
`backtest/daily_strategies.py` (TrailingStopBH).  We reuse those — we do NOT
re-implement them — so the live runner matches the backtested behaviour byte
for byte.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

# Reuse backtest building blocks rather than duplicating logic.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backtest.v1_strategies import VolTarget, realized_vol  # noqa: E402
from backtest.daily_strategies import TrailingStopBH  # noqa: E402


# ---------------------------------------------------------------------------
# Decision record
# ---------------------------------------------------------------------------

@dataclass
class StrategyDecision:
    """Per-cycle output of `BHOverlayStrategy.decide()`.

    All numeric fields are populated even when `signal_on=False`, so the
    health.json can show the vol/DD diagnostics during flat periods.
    """
    target_exposure: float          # [0, L_max] — what we WANT to hold over t+1
    signal_on: bool                 # trend filter says long?
    vol_realized: float             # annualised σ_t (NaN until warmed up)
    vol_target: float               # annualised σ target (config)
    vol_target_multiplier: float    # clip(σ_target / σ_t, 0, L_max) — pre-trend
    drawdown_pct: float             # 0..1 — current DD from running high (price)
    peak_price: float               # the running high used for the trail/DD
    last_close: float
    last_high: float
    last_low: float
    reason: str                     # short human/machine label, e.g. "in_market"

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class BHOverlayStrategy:
    """Vol-target × re-entering trailing-stop overlay on BTC spot.

    Pure-function logic over a daily OHLCV history.  Stateful for the trend
    leg (the trailing-stop state machine needs to remember `in_market` and the
    `running_high` since entry); the state is exported by `to_state()` and
    restored by `from_state()` so the runner can persist it atomically.

    Parameters (defaults match DECISIONS.md 2026-05-13 + STRATEGY-V1-RESULTS.md):

      trail_pct      : the trailing-stop X (default 0.10 = 10%)
      breakout_days  : the re-entry N (default 20)
      sigma_target   : annualised vol target σ_target (default 0.20)
      vol_window     : trailing window for realized vol estimate (default 30)
      L_max          : exposure cap (default 1.0 — no leverage)
      no_trade_band  : >0; only the *runner* uses this to suppress small
                       rebalances, the strategy itself just emits a target.
                       Kept here as a documented attribute for the runner to
                       read (single source of truth).

    The `no_trade_band` lives here as a non-decision-affecting attribute — the
    strategy emits the *raw* target, the runner decides whether to act on it.
    Mirrors the M0 backtester convention.
    """

    def __init__(
        self,
        *,
        trail_pct: float = 0.10,
        breakout_days: int = 20,
        sigma_target: float = 0.20,
        vol_window: int = 30,
        L_max: float = 1.0,
        no_trade_band: float = 0.15,
    ):
        self.trail_pct = float(trail_pct)
        self.breakout_days = int(breakout_days)
        self.sigma_target = float(sigma_target)
        self.vol_window = int(vol_window)
        self.L_max = float(L_max)
        self.no_trade_band = float(no_trade_band)

        # Backtest building blocks — single source of truth for the math.
        self._vol = VolTarget(
            sigma_target=self.sigma_target,
            window=self.vol_window,
            L_max=self.L_max,
        )
        self._trail = TrailingStopBH(
            trail_pct=self.trail_pct,
            breakout_days=self.breakout_days,
            in_exposure=1.0,        # we apply the vol-target multiplier separately
            reenter=True,
        )

    # ---------- state serialization ----------

    def to_state(self) -> Dict[str, Any]:
        """Export the internal state machine to a JSON-safe dict.

        Lets the runner persist the trailing-stop "in_market" + "running_high"
        across restarts so a service bounce doesn't reset the trend leg.
        """
        return {
            "in_market": bool(self._trail._in_market),
            "running_high": (
                float(self._trail._running_high)
                if self._trail._running_high is not None else None
            ),
            "done": bool(self._trail._done),
        }

    def from_state(self, state: Optional[Dict[str, Any]]) -> None:
        """Restore the internal state machine from a previously-saved dict."""
        if not state:
            return
        self._trail._in_market = bool(state.get("in_market", True))
        rh = state.get("running_high")
        self._trail._running_high = float(rh) if rh is not None else None
        self._trail._done = bool(state.get("done", False))

    def reset(self) -> None:
        self._trail.reset()

    # ---------- decision ----------

    def decide(self, history: pd.DataFrame) -> StrategyDecision:
        """Compute the target exposure for the next day from a daily-bar history.

        `history` is a DataFrame of fully-closed daily bars (columns: timestamp,
        open, high, low, close, volume) with the last row being the most recent
        closed day t.  The returned `target_exposure` is what we'd want to hold
        over day t+1.

        The trailing-stop state machine mutates as it sees each new bar — to
        replay history from scratch, call `reset()` first.  In production the
        runner makes ONE decision per UTC day and persists the state, so the
        machine only advances one bar at a time.
        """
        if history is None or len(history) == 0:
            return StrategyDecision(
                target_exposure=0.0, signal_on=False,
                vol_realized=float("nan"), vol_target=self.sigma_target,
                vol_target_multiplier=0.0,
                drawdown_pct=0.0, peak_price=0.0,
                last_close=0.0, last_high=0.0, last_low=0.0,
                reason="empty_history",
            )

        closes = history["close"].astype(float).to_numpy()
        if "high" in history.columns and "low" in history.columns:
            highs = history["high"].astype(float).to_numpy()
            lows = history["low"].astype(float).to_numpy()
        else:
            highs = lows = closes
        last_close = float(closes[-1])
        last_high = float(highs[-1])
        last_low = float(lows[-1])

        # 1. trend filter — advances the state machine by one bar.
        signal_exposure = self._trail.target_exposure(history)
        signal_on = signal_exposure > 1e-9

        # 2. vol-target multiplier (size only matters when we're long).
        sigma = realized_vol(closes, self.vol_window)
        vt_mult = self._vol.exposure_for(history)

        # 3. drawdown / peak-price diagnostic (independent of trend state — we
        #    want this in health.json even while flat, so the operator can see
        #    how close we are to a re-entry).
        if self._trail._running_high is not None:
            peak = float(self._trail._running_high)
        else:
            # while flat, the diagnostic peak is the trailing all-time-high of
            # the high series (matches the trail's view when in-market).
            peak = float(np.max(highs))
        dd_pct = 0.0
        if peak > 0:
            dd_pct = max(0.0, (peak - last_close) / peak)

        if signal_on:
            target = float(vt_mult)
            reason = "in_market"
        else:
            target = 0.0
            if self._trail._done:
                reason = "stopped_no_reenter"
            else:
                # distinguish "just stopped this bar" from "flat awaiting re-entry"
                reason = "flat_awaiting_reentry"

        # clip defensively
        target = max(0.0, min(self.L_max, target))

        return StrategyDecision(
            target_exposure=target,
            signal_on=signal_on,
            vol_realized=float(sigma) if np.isfinite(sigma) else float("nan"),
            vol_target=self.sigma_target,
            vol_target_multiplier=float(vt_mult),
            drawdown_pct=float(dd_pct),
            peak_price=float(peak),
            last_close=last_close,
            last_high=last_high,
            last_low=last_low,
            reason=reason,
        )


# ---------------------------------------------------------------------------
# Rebalance helper — shared with the runner so the tests can exercise it
# independently of the runner's plumbing.
# ---------------------------------------------------------------------------

def should_rebalance(
    current_exposure: float,
    target_exposure: float,
    no_trade_band: float,
    eps: float = 1e-9,
) -> bool:
    """Mirror the M0 backtester's no-trade-band logic exactly.

    * If currently flat and target > eps → rebalance (enter).
    * If currently positioned and target ~= 0 → rebalance (always allow exit).
    * Otherwise rebalance only when |Δ| / current > band.
    """
    cur = float(current_exposure)
    tgt = float(target_exposure)
    if cur <= eps:
        return tgt > eps
    if tgt <= eps:
        return True
    rel = abs(tgt - cur) / cur
    return rel > float(no_trade_band)
