#!/usr/bin/env python3
"""Strategies that plug into the daily-bar harness (`backtest/daily_backtester.py`).

For now: the two benchmarks from `docs/STRATEGY-V1-TREND-VOLTARGET.md` §2 —

  * `BuyAndHold`            — B1: always fully invested (exposure 1.0).
  * `TrailingStopBH`        — B2: in-market until BTC closes ≥ X% below the
                              running high since entry → flat; re-enter when BTC
                              closes at a new N-day high.  X=10%, N=20 by default
                              (the diagnosis used a −10% trailing stop; N=20 is a
                              standard breakout lookback — documented, not tuned).
  * `ScheduleStrategy`      — exposure follows a precomputed in/out boolean
                              schedule (1.0 while "in", 0 while "out"); used by
                              the random-entry null harness.

A strategy here implements `target_exposure(history: pd.DataFrame) -> float`
(and optionally `reset()`); `history`'s last row is the most recent *closed*
day t, and the return value is the target exposure for day t+1.  See the harness
module docstring for the full protocol.
"""

import numpy as np
import pandas as pd


class BuyAndHold:
    """B1 — plain buy-and-hold: exposure is always `exposure` (default 1.0)."""

    def __init__(self, exposure: float = 1.0):
        self.exposure = float(exposure)

    def reset(self):
        pass

    def target_exposure(self, history: pd.DataFrame) -> float:
        return self.exposure


class TrailingStopBH:
    """B2 — trailing-stop regime switch on BTC (the edge-diagnosis winner).

    State machine, decided at the close of day t (closed bars only); the result
    is the exposure held over day t+1:

      * IN-MARKET: hold `in_exposure` (default 1.0).  Track the running high of
        the daily *high* since (re-)entry.  If day t's *low* ≤ running_high *
        (1 - X) the trailing stop is hit → go flat.  (Using high for the trail
        ceiling and low for the trigger mirrors how the 5m diagnosis saw
        intraday extremes inside a day.)
      * FLAT: wait.  If `reenter` is True, re-enter when day t's *close* is a
        new N-day high of the close.  If `reenter` is False this never
        re-enters — it stays in cash for the rest of the run (this is exactly
        the one-shot "BH + trailing −10% stop, then cash" rule from
        `docs/edge-diagnosis/I-ablations.md` row 14).

    Defaults: X = 0.10 (the diagnosis's −10%), N = 20 (a standard breakout
    lookback; spec §3 allows N ≈ 20–50 — documented here, not tuned), reenter =
    True (the task's "re-enter on a new N-day high").

    `in_exposure` lets a caller make the in-market leg vol-targeted instead of
    flat 1.0; the diagnosis used plain 1.0, which is the default.
    """

    def __init__(self, trail_pct: float = 0.10, breakout_days: int = 20,
                 in_exposure: float = 1.0, reenter: bool = True):
        self.trail_pct = float(trail_pct)
        self.breakout_days = int(breakout_days)
        self.in_exposure = float(in_exposure)
        self.reenter = bool(reenter)
        self.reset()

    def reset(self):
        self._in_market = True       # start invested (BH, then a stop on top)
        self._running_high = None    # running high of the daily *high* since entry
        self._done = False           # set when reenter=False and the stop fired

    def target_exposure(self, history: pd.DataFrame) -> float:
        if "high" in history.columns and "low" in history.columns:
            highs = history["high"].astype(float).to_numpy()
            lows = history["low"].astype(float).to_numpy()
        else:  # close-only fallback
            highs = lows = history["close"].astype(float).to_numpy()
        closes = history["close"].astype(float).to_numpy()
        if len(closes) == 0:
            return 0.0
        last_close = float(closes[-1])
        last_high = float(highs[-1])
        last_low = float(lows[-1])

        if self._done:
            return 0.0

        if self._in_market:
            if self._running_high is None:
                self._running_high = last_high
            else:
                self._running_high = max(self._running_high, last_high)
            if last_low <= self._running_high * (1.0 - self.trail_pct):
                # trailing stop hit -> go flat over day t+1
                self._in_market = False
                self._running_high = None
                if not self.reenter:
                    self._done = True
                return 0.0
            return self.in_exposure

        # FLAT: re-enter on a new N-day high of the close (if enabled)
        if not self.reenter:
            return 0.0
        n = self.breakout_days
        window = closes[-n:] if len(closes) >= n else closes
        if len(window) >= 1 and last_close >= float(np.max(window)) - 1e-12:
            self._in_market = True
            self._running_high = last_high
            return self.in_exposure
        return 0.0


class ScheduleStrategy:
    """Exposure driven by a precomputed per-day in/out boolean schedule.

    `in_market` is a sequence aligned to the daily bars: `in_market[t]` is True
    if we should be invested *over day t+1* (i.e. it is the decision made at the
    close of day t).  When True the target exposure is `in_exposure` (default
    1.0), else 0.  Used by the random-entry null harness.
    """

    def __init__(self, in_market, in_exposure: float = 1.0):
        self._in_market = list(bool(x) for x in in_market)
        self.in_exposure = float(in_exposure)
        self._t = 0

    def reset(self):
        self._t = 0

    def target_exposure(self, history: pd.DataFrame) -> float:
        # history's last row is day t == len(history) - 1
        t = len(history) - 1
        on = self._in_market[t] if 0 <= t < len(self._in_market) else False
        return self.in_exposure if on else 0.0
