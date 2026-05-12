#!/usr/bin/env python3
"""v1 trend rules + volatility-target sizing — milestone M3 of
`docs/STRATEGY-V1-TREND-VOLTARGET.md`.

The architecture (spec §3 × §4):

    target exposure for day t+1  =  trend_filter(history) ∈ {0, 1}
                                    × vol_target_exposure(history)

* `trend_filter` is one of the three candidate rules in §3:
    (a) `TrailingStopRegime`  — long until close ≥ X% below the running high
        since entry → flat; re-enter on a new N-day high.
    (b) `MAFilter`            — long when close > the M-day SMA/EMA, flat
        otherwise; optional confirm: the MA slope must be > 0.
    (c) `DonchianChannel`     — long on a new N-day high, flat on a new K-day low.
* `vol_target_exposure(history)` = clip(σ_target / σ_t, 0, L_max) where σ_t is
  the annualized stdev of daily log-returns over a trailing window (default 30d,
  or EWMA — a knob).  `plain=True` makes it a constant 1.0 (the "plain" variant
  used to show the vol-target's marginal contribution).

All rules are **long-or-flat** (no shorting in v1) and use **closed bars only**
(the last row of `history` is the most-recent fully-closed day t; the returned
exposure is what we hold over day t+1).  Each strategy duck-types the
`target_exposure(history) -> float` / `reset()` protocol of `DailyBacktester`.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Volatility-target sizing (spec §4)
# ---------------------------------------------------------------------------

def realized_vol(closes: np.ndarray, window: int = 30, *, ewma: bool = False,
                 periods_per_year: float = 365.0) -> float:
    """Annualized realized volatility of daily log-returns.

    `closes` is the (ascending) close series up to and including day t.  Returns
    the annualized stdev of the trailing `window` log-returns (or an EWMA with
    span ≈ `window` if `ewma`).  Returns ``nan`` if there isn't enough data.
    """
    c = np.asarray(closes, dtype=float)
    if len(c) < 2:
        return float("nan")
    logret = np.diff(np.log(np.where(c > 0, c, np.nan)))
    logret = logret[np.isfinite(logret)]
    if len(logret) < 2:
        return float("nan")
    if ewma:
        # EWMA variance with span = window (centre of mass ≈ window/2)
        s = pd.Series(logret)
        var = s.ewm(span=max(int(window), 2), adjust=False).var(bias=False).iloc[-1]
        sd = float(np.sqrt(var)) if np.isfinite(var) else float("nan")
    else:
        w = max(int(window), 2)
        tail = logret[-w:]
        if len(tail) < 2:
            return float("nan")
        sd = float(np.std(tail, ddof=1))
    if not np.isfinite(sd) or sd <= 0:
        return float("nan")
    return sd * float(np.sqrt(periods_per_year))


class VolTarget:
    """Volatility-target exposure helper (spec §4).

    `exposure_for(history)` = clip(σ_target / σ_t, 0, L_max) using the trailing
    realized vol of the close series.  Before there is enough history to
    estimate σ_t it returns `warmup_exposure` (default `L_max`, i.e. behave like
    plain BH during the warm-up — the same convention the M0 benchmarks use).

    `plain=True` short-circuits to a constant `L_max` (the "no vol-target"
    variant used to measure the overlay's marginal contribution).
    """

    def __init__(self, sigma_target: float = 0.20, window: int = 30,
                 *, ewma: bool = False, L_max: float = 1.0,
                 plain: bool = False, warmup_exposure: float | None = None):
        self.sigma_target = float(sigma_target)
        self.window = int(window)
        self.ewma = bool(ewma)
        self.L_max = float(L_max)
        self.plain = bool(plain)
        self.warmup_exposure = self.L_max if warmup_exposure is None else float(warmup_exposure)

    def exposure_for(self, history: pd.DataFrame) -> float:
        if self.plain:
            return self.L_max
        closes = history["close"].astype(float).to_numpy()
        # need at least `window`+1 closes for a stable estimate
        if len(closes) < self.window + 1:
            return float(min(self.L_max, max(0.0, self.warmup_exposure)))
        sigma = realized_vol(closes, self.window, ewma=self.ewma)
        if not np.isfinite(sigma) or sigma <= 0:
            return float(min(self.L_max, max(0.0, self.warmup_exposure)))
        return float(np.clip(self.sigma_target / sigma, 0.0, self.L_max))


# ---------------------------------------------------------------------------
# Trend filters (spec §3) — each returns 1.0 (long) or 0.0 (flat)
# ---------------------------------------------------------------------------

class _TrendFilterBase:
    """Common machinery: a trend filter wrapped with a `VolTarget` overlay.

    Subclasses implement `_long(history) -> bool` (the §3 trend rule, closed
    bars only).  `target_exposure` then returns `vol_target.exposure_for(...)`
    when long, else 0.0.
    """

    def __init__(self, vol_target: VolTarget | None = None):
        self.vol_target = vol_target or VolTarget()

    def reset(self):
        pass

    def _long(self, history: pd.DataFrame) -> bool:  # pragma: no cover - abstract
        raise NotImplementedError

    def target_exposure(self, history: pd.DataFrame) -> float:
        if history is None or len(history) == 0:
            return 0.0
        try:
            long = bool(self._long(history))
        except Exception:
            long = False
        if not long:
            return 0.0
        return self.vol_target.exposure_for(history)


class TrailingStopRegime(_TrendFilterBase):
    """(a) Trailing-stop regime switch, re-entering (the diagnosis winner).

    State machine decided at the close of day t (closed bars only):
      * IN-MARKET: track the running high of the daily *high* since (re-)entry.
        If day t's *low* ≤ running_high · (1 − X) → go flat over day t+1.
      * FLAT: re-enter when day t's *close* is a new N-day high of the close.

    Params: X = trail_pct (≈0.10–0.20), N = breakout_days (≈20–50).  This is
    the same logic as `daily_strategies.TrailingStopBH(reenter=True)` but the
    in-market exposure is the vol-target overlay instead of a flat 1.0.
    """

    def __init__(self, trail_pct: float = 0.10, breakout_days: int = 20,
                 vol_target: VolTarget | None = None):
        super().__init__(vol_target)
        self.trail_pct = float(trail_pct)
        self.breakout_days = int(breakout_days)
        self.reset()

    def reset(self):
        self._in_market = True       # start invested (BH, then a stop on top)
        self._running_high = None

    def _long(self, history: pd.DataFrame) -> bool:
        if "high" in history.columns and "low" in history.columns:
            highs = history["high"].astype(float).to_numpy()
            lows = history["low"].astype(float).to_numpy()
        else:
            highs = lows = history["close"].astype(float).to_numpy()
        closes = history["close"].astype(float).to_numpy()
        last_close = float(closes[-1]); last_high = float(highs[-1]); last_low = float(lows[-1])

        if self._in_market:
            self._running_high = last_high if self._running_high is None \
                else max(self._running_high, last_high)
            if last_low <= self._running_high * (1.0 - self.trail_pct):
                self._in_market = False
                self._running_high = None
                return False
            return True
        # FLAT — re-enter on a new N-day high of the close
        n = self.breakout_days
        window = closes[-n:] if len(closes) >= n else closes
        if len(window) >= 1 and last_close >= float(np.max(window)) - 1e-12:
            self._in_market = True
            self._running_high = last_high
            return True
        return False


class MAFilter(_TrendFilterBase):
    """(b) Long-term moving-average filter.

    Long when the latest close > the M-day moving average; flat otherwise.
    `ema=True` uses an EMA (span M) instead of an SMA.  `require_slope_up=True`
    additionally requires the MA to be rising (MA_t > MA_{t-1}).  Before there
    are M closes the filter is "warming up" → flat (no lookahead, no guessing).
    """

    def __init__(self, ma_days: int = 100, *, ema: bool = False,
                 require_slope_up: bool = False, vol_target: VolTarget | None = None):
        super().__init__(vol_target)
        self.ma_days = int(ma_days)
        self.ema = bool(ema)
        self.require_slope_up = bool(require_slope_up)

    def _long(self, history: pd.DataFrame) -> bool:
        closes = history["close"].astype(float).to_numpy()
        m = self.ma_days
        if len(closes) < m:
            return False
        if self.ema:
            ma_series = pd.Series(closes).ewm(span=m, adjust=False).mean().to_numpy()
        else:
            ma_series = pd.Series(closes).rolling(m).mean().to_numpy()
        ma_now = ma_series[-1]
        if not np.isfinite(ma_now):
            return False
        if closes[-1] <= ma_now:
            return False
        if self.require_slope_up:
            if len(ma_series) < 2 or not np.isfinite(ma_series[-2]):
                return False
            if ma_series[-1] <= ma_series[-2]:
                return False
        return True


class DonchianChannel(_TrendFilterBase):
    """(c) Donchian channel breakout (turtle-style).

    Long when day t's close is a new `entry_days`-day high of the close; exit to
    flat when day t's close is a new `exit_days`-day low.  Holds the previous
    state otherwise.  Defaults: entry_days = 50, exit_days = 20 (spec §3).
    """

    def __init__(self, entry_days: int = 50, exit_days: int = 20,
                 vol_target: VolTarget | None = None):
        super().__init__(vol_target)
        self.entry_days = int(entry_days)
        self.exit_days = int(exit_days)
        self.reset()

    def reset(self):
        self._in_market = False

    def _long(self, history: pd.DataFrame) -> bool:
        closes = history["close"].astype(float).to_numpy()
        last = float(closes[-1])
        # use the window EXCLUDING today for a "new high/low" test where possible
        if not self._in_market:
            n = self.entry_days
            prior = closes[-(n + 1):-1] if len(closes) >= n + 1 else closes[:-1]
            if len(prior) >= 1 and last >= float(np.max(prior)) - 1e-12:
                self._in_market = True
        else:
            k = self.exit_days
            prior = closes[-(k + 1):-1] if len(closes) >= k + 1 else closes[:-1]
            if len(prior) >= 1 and last <= float(np.min(prior)) + 1e-12:
                self._in_market = False
        return self._in_market


# ---------------------------------------------------------------------------
# Factory — build the canonical bake-off candidates
# ---------------------------------------------------------------------------

def make_candidate(rule: str, *, plain: bool = False, sigma_target: float = 0.20,
                   vol_window: int = 30, ewma: bool = False, L_max: float = 1.0,
                   **params):
    """Construct a v1 candidate strategy.

    `rule` ∈ {"trailing", "ma", "donchian"}; `params` are the rule's knobs
    (`trail_pct`/`breakout_days`; `ma_days`/`ema`/`require_slope_up`;
    `entry_days`/`exit_days`).  `plain=True` swaps the vol-target overlay for a
    constant L_max (the "no vol-target" comparison).
    """
    vt = VolTarget(sigma_target=sigma_target, window=vol_window, ewma=ewma,
                   L_max=L_max, plain=plain)
    rule = rule.lower()
    if rule in ("trailing", "trailing_stop", "a"):
        return TrailingStopRegime(
            trail_pct=params.get("trail_pct", 0.10),
            breakout_days=params.get("breakout_days", 20), vol_target=vt)
    if rule in ("ma", "ma_filter", "b"):
        return MAFilter(
            ma_days=params.get("ma_days", 100), ema=params.get("ema", False),
            require_slope_up=params.get("require_slope_up", False), vol_target=vt)
    if rule in ("donchian", "donchian_channel", "c"):
        return DonchianChannel(
            entry_days=params.get("entry_days", 50),
            exit_days=params.get("exit_days", 20), vol_target=vt)
    raise ValueError(f"unknown rule {rule!r}")
