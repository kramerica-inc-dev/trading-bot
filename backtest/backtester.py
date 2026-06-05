#!/usr/bin/env python3
"""
Backtesting Engine
Replays historical candles through strategy, simulates trades, computes metrics.
"""

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# Add scripts/ to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from trading_strategy import TradingStrategy, Signal
from risk_utils import calculate_risk_position_size


class HTFCandleSync:
    """Synchronizes higher-timeframe candles with base-timeframe bars.

    For each base-timeframe timestamp, returns the most recently *closed*
    HTF candle — never the current in-progress one.  This eliminates the
    lookahead bias that resampling introduces.
    """

    def __init__(self, htf_datasets: Dict[str, pd.DataFrame] = None):
        """
        Args:
            htf_datasets: dict mapping timeframe string (e.g. '15m', '1H',
                          '4H') to a DataFrame with at least 'timestamp'
                          and OHLCV columns.  Timestamps must be the
                          *open* time of each candle.
        """
        self._indices: Dict[str, List] = {}
        self._candles: Dict[str, List[List]] = {}
        self._tf_ms: Dict[str, int] = {}

        if htf_datasets:
            for tf, df in htf_datasets.items():
                self.load_timeframe(tf, df)

    @staticmethod
    def _tf_to_ms(tf: str) -> int:
        tf = tf.strip().lower()
        if tf.endswith('m'):
            return int(tf[:-1]) * 60_000
        if tf.endswith('h'):
            return int(tf[:-1]) * 3_600_000
        if tf.endswith('d'):
            return int(tf[:-1]) * 86_400_000
        return 300_000  # default 5m

    def load_timeframe(self, tf: str, df: pd.DataFrame) -> None:
        """Load a HTF dataset.  Expects 'timestamp' column as datetime."""
        if df is None or df.empty:
            return
        sorted_df = df.sort_values('timestamp').reset_index(drop=True)
        # Convert timestamps to epoch ms
        ts_ms = sorted_df['timestamp'].astype(np.int64) // 10**6
        self._indices[tf] = ts_ms.tolist()
        # Store as list-of-lists matching BloFin candle format
        candles = []
        for _, row in sorted_df.iterrows():
            candles.append([
                int(row['timestamp'].timestamp() * 1000)
                    if hasattr(row['timestamp'], 'timestamp')
                    else int(row['timestamp']),
                float(row['open']), float(row['high']),
                float(row['low']), float(row['close']),
                float(row['volume']),
                0, 0, 0,
            ])
        self._candles[tf] = candles
        self._tf_ms[tf] = self._tf_to_ms(tf)

    @property
    def available_timeframes(self) -> List[str]:
        return list(self._candles.keys())

    def get_closed_candles(self, tf: str, current_ts_ms: int,
                           max_candles: int = 200) -> List[List]:
        """Return up to *max_candles* most-recently-closed HTF candles.

        A candle is considered closed when:
            candle_open_ts + candle_duration <= current_ts_ms

        This means the candle whose open_ts + duration is still in the
        future is excluded (it hasn't closed yet).
        """
        if tf not in self._candles:
            return []

        bar_ms = self._tf_ms[tf]
        # Closed means: open_ts + bar_ms <= current_ts_ms
        # i.e. open_ts <= current_ts_ms - bar_ms
        cutoff = current_ts_ms - bar_ms
        indices = self._indices[tf]
        candles = self._candles[tf]

        # Binary search for the last index where open_ts <= cutoff
        lo, hi = 0, len(indices) - 1
        pos = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if indices[mid] <= cutoff:
                pos = mid
                lo = mid + 1
            else:
                hi = mid - 1

        if pos < 0:
            return []

        start = max(0, pos - max_candles + 1)
        return candles[start:pos + 1]


# ---------------------------------------------------------------------------
# Benchmark + risk-adjusted metric helpers (Fase 1 — pure measurement)
# ---------------------------------------------------------------------------

# Default bull/bear classification: trailing 30-day return on the underlying
# (BTC) price; > +5% -> bull, < -5% -> bear, else sideways.  Computed over a
# rolling window that ends at bar t (uses only closes <= t — no lookahead).
DEFAULT_REGIME_WINDOW_DAYS = 30.0
DEFAULT_REGIME_THRESHOLD_PCT = 5.0


def _infer_bar_seconds(timestamps: List) -> float:
    """Infer the candle interval in seconds from a list of timestamps.

    Uses the median of consecutive diffs so a few gaps don't skew it.
    Falls back to 300s (5m) if it cannot be determined.
    """
    if not timestamps or len(timestamps) < 2:
        return 300.0
    diffs = []
    for a, b in zip(timestamps[:-1], timestamps[1:]):
        try:
            d = (b - a).total_seconds()
        except AttributeError:
            d = float(b) - float(a)
            # Heuristic: if values look like ms epochs, scale down.
            if d > 1e6:
                d = d / 1000.0
        if d > 0:
            diffs.append(d)
    if not diffs:
        return 300.0
    return float(np.median(diffs))


def _drawdown_stats(equity) -> Dict[str, float]:
    """Compute max drawdown, drawdown duration and time-under-water.

    Args:
        equity: 1-D sequence of equity / price values.

    Returns dict with:
        max_drawdown_pct:        worst peak-to-trough decline, in percent
        max_drawdown_abs:        worst peak-to-trough decline, absolute
        max_dd_duration_bars:    longest underwater stretch, in bars
        time_under_water_pct:    fraction of bars spent below a prior peak (%)
    """
    arr = np.asarray(equity, dtype=float)
    n = len(arr)
    if n == 0:
        return {
            'max_drawdown_pct': 0.0,
            'max_drawdown_abs': 0.0,
            'max_dd_duration_bars': 0,
            'time_under_water_pct': 0.0,
        }
    peak = np.maximum.accumulate(arr)
    # Guard against zero/negative peaks (shouldn't happen for equity/price).
    safe_peak = np.where(peak == 0, 1.0, peak)
    dd = (peak - arr) / safe_peak
    max_dd_pct = float(np.max(dd)) * 100.0 if n > 0 else 0.0
    max_dd_abs = float(np.max(peak - arr)) if n > 0 else 0.0

    underwater = arr < peak
    time_under_water_pct = float(np.mean(underwater)) * 100.0

    # Longest consecutive underwater run.
    longest = 0
    cur = 0
    for uw in underwater:
        if uw:
            cur += 1
            longest = max(longest, cur)
        else:
            cur = 0
    return {
        'max_drawdown_pct': max_dd_pct,
        'max_drawdown_abs': max_dd_abs,
        'max_dd_duration_bars': int(longest),
        'time_under_water_pct': time_under_water_pct,
    }


def _cagr(equity, span_days: float) -> float:
    """Compound annual growth rate from an equity curve over span_days."""
    arr = np.asarray(equity, dtype=float)
    if len(arr) < 2 or arr[0] <= 0 or span_days <= 0:
        return 0.0
    years = span_days / 365.0
    if years <= 0:
        return 0.0
    ratio = arr[-1] / arr[0]
    if ratio <= 0:
        return -1.0
    return float(ratio ** (1.0 / years) - 1.0)


def _annualized_sharpe(returns, periods_per_year: float) -> float:
    """Annualized Sharpe of a per-period return series (risk-free = 0)."""
    arr = np.asarray(returns, dtype=float)
    if len(arr) < 2:
        return 0.0
    sd = np.std(arr)
    if sd <= 0:
        return 0.0
    return float(np.mean(arr) / sd * np.sqrt(periods_per_year))


def compute_benchmark(closes, timestamps, initial_balance: float = 1.0) -> Dict:
    """BTC buy-and-hold benchmark over the same period as the strategy.

    The benchmark equity at bar t reflects holding 1 unit bought at the first
    close, valued at the close of bar t — i.e. it only ever uses closes <= t,
    so there is no lookahead.

    Args:
        closes: sequence of close prices (the BTC-USDT candle series).
        timestamps: matching list of timestamps (datetime).
        initial_balance: scale the equity curve to this starting value.

    Returns a dict with total_return_pct, the (scaled) equity_curve, CAGR and
    drawdown stats.  For < 2 closes everything collapses to zero / flat.
    """
    closes = [float(c) for c in closes]
    n = len(closes)
    if n == 0:
        return {
            'total_return_pct': 0.0,
            'equity_curve': [],
            'cagr': 0.0,
            'max_drawdown_pct': 0.0,
            'max_drawdown_abs': 0.0,
            'max_dd_duration_bars': 0,
            'time_under_water_pct': 0.0,
            'final_balance': initial_balance,
        }
    first = closes[0]
    if first <= 0:
        equity = [initial_balance] * n
    else:
        equity = [initial_balance * (c / first) for c in closes]

    total_return_pct = (equity[-1] / equity[0] - 1.0) * 100.0 if equity[0] else 0.0

    if timestamps and len(timestamps) >= 2:
        try:
            span_days = (timestamps[-1] - timestamps[0]).total_seconds() / 86400.0
        except AttributeError:
            span_days = (float(timestamps[-1]) - float(timestamps[0])) / 86400.0
            if span_days > 1e6:
                span_days = span_days / 1000.0
    else:
        span_days = 0.0
    span_days = max(span_days, 0.0)

    dd = _drawdown_stats(equity)
    return {
        'total_return_pct': total_return_pct,
        'equity_curve': equity,
        'cagr': _cagr(equity, span_days) if span_days > 0 else 0.0,
        'max_drawdown_pct': dd['max_drawdown_pct'],
        'max_drawdown_abs': dd['max_drawdown_abs'],
        'max_dd_duration_bars': dd['max_dd_duration_bars'],
        'time_under_water_pct': dd['time_under_water_pct'],
        'final_balance': equity[-1],
        'span_days': span_days,
    }


def classify_regimes(closes, timestamps,
                     window_days: float = DEFAULT_REGIME_WINDOW_DAYS,
                     threshold_pct: float = DEFAULT_REGIME_THRESHOLD_PCT) -> List[str]:
    """Classify each bar as 'bull' / 'bear' / 'sideways' (no lookahead).

    Rule: trailing return of the close over a rolling window of ~window_days.
    For bar t we look back W bars where W is the number of bars in window_days
    (inferred from the timestamp spacing).  If fewer than W bars of history are
    available the partial window from bar 0..t is used.  The classification of
    bar t therefore only ever uses closes <= t.

    Returns a list of length len(closes); the very first bar is always
    'sideways' (no trailing return defined).
    """
    closes = [float(c) for c in closes]
    n = len(closes)
    if n == 0:
        return []
    bar_sec = _infer_bar_seconds(list(timestamps) if timestamps is not None else [])
    window_bars = max(int(round(window_days * 86400.0 / max(bar_sec, 1.0))), 1)
    thr = threshold_pct / 100.0

    regimes: List[str] = []
    for t in range(n):
        if t == 0:
            regimes.append('sideways')
            continue
        ref_idx = max(0, t - window_bars)
        ref = closes[ref_idx]
        if ref <= 0:
            regimes.append('sideways')
            continue
        ret = closes[t] / ref - 1.0
        if ret > thr:
            regimes.append('bull')
        elif ret < -thr:
            regimes.append('bear')
        else:
            regimes.append('sideways')
    return regimes


def conditional_metrics(equity_curve, timestamps, regimes,
                        bar_seconds: Optional[float] = None) -> Dict[str, Dict]:
    """Strategy performance split by bull / bear / sideways segments.

    For each regime label we collect the per-bar equity returns of all bars
    carrying that label, and report:
        bars:          number of bars in this regime
        total_return_pct: compounded return over those bars
        sharpe:        annualized Sharpe of the per-bar returns
        max_drawdown_pct: max drawdown of the equity *restricted* to those bars
                          (chained returns; an approximation of "how bad it got
                          while we were in this regime")
    "bear" is the segment that matters most — it answers "do we protect capital
    when BTC falls".
    """
    eq = np.asarray(equity_curve, dtype=float)
    n = len(eq)
    out: Dict[str, Dict] = {}
    if n < 2 or not regimes or len(regimes) != n:
        for r in ('bull', 'bear', 'sideways'):
            out[r] = {'bars': 0, 'total_return_pct': 0.0, 'sharpe': 0.0,
                      'max_drawdown_pct': 0.0}
        return out

    if bar_seconds is None:
        bar_seconds = _infer_bar_seconds(list(timestamps) if timestamps is not None else [])
    periods_per_year = 86400.0 * 365.0 / max(bar_seconds, 1.0)

    # Per-bar simple returns; aligned to bar index i (return from i-1 to i),
    # attributed to the regime label of bar i.
    bar_ret = np.zeros(n)
    with np.errstate(divide='ignore', invalid='ignore'):
        bar_ret[1:] = np.where(eq[:-1] != 0, eq[1:] / eq[:-1] - 1.0, 0.0)
    bar_ret = np.nan_to_num(bar_ret)

    for label in ('bull', 'bear', 'sideways'):
        idx = [i for i in range(1, n) if regimes[i] == label]
        if not idx:
            out[label] = {'bars': 0, 'total_return_pct': 0.0, 'sharpe': 0.0,
                          'max_drawdown_pct': 0.0}
            continue
        rets = bar_ret[idx]
        # Compounded return over the regime bars.
        comp = float(np.prod(1.0 + rets) - 1.0)
        # Synthetic equity from chaining the regime-bar returns.
        synth = np.concatenate([[1.0], np.cumprod(1.0 + rets)])
        dd = _drawdown_stats(synth)
        out[label] = {
            'bars': len(idx),
            'total_return_pct': comp * 100.0,
            'sharpe': _annualized_sharpe(rets, periods_per_year),
            'max_drawdown_pct': dd['max_drawdown_pct'],
        }
    return out


def period_concentration(equity_curve, timestamps, top_n: int = 5) -> Dict:
    """PnL-concentration diagnostics: how much of the total PnL comes from the
    best few calendar periods, and what fraction of periods were positive.

    A genuine, broad edge spreads its PnL across many weeks/months and wins a
    decent fraction of them; an over-fit or lucky edge piles its entire PnL into
    a handful of periods (top-N share near or above 100%) while losing most of
    the rest.  This is the "few lucky periods" flag that complements the
    random-entry null and the deflated-Sharpe / multiple-testing discipline.

    Args:
        equity_curve: 1-D sequence of equity values (currency).
        timestamps:   matching datetimes, one per equity point.
        top_n:        how many best periods define the concentration ratio.

    Returns a dict (plain floats / ints):
        weeks, months:                 number of calendar periods covered
        pct_positive_weeks / _months:  % of periods with PnL > 0
        top_n:                         N used for the ratios
        top_n_week_share_pct:          top-N weeks' PnL / net total PnL * 100
                                       (None when net PnL <= 0 — ratio undefined)
        top_n_week_gain_share_pct:     top-N weeks' gains / sum of all positive-
                                       week gains * 100 (always 0..100, sign-safe)
        top_n_month_share_pct / _month_gain_share_pct: monthly analogues

    A top-N *share* near/above 100% with a low *pct_positive* is the classic
    "a few lucky periods carried it" signature.  Empty / single-point inputs
    collapse to a zero/None result.
    """
    empty = {
        'weeks': 0, 'months': 0,
        'pct_positive_weeks': 0.0, 'pct_positive_months': 0.0,
        'top_n': int(top_n),
        'top_n_week_share_pct': None, 'top_n_week_gain_share_pct': 0.0,
        'top_n_month_share_pct': None, 'top_n_month_gain_share_pct': 0.0,
    }
    eq = list(equity_curve) if equity_curve is not None else []
    ts = list(timestamps) if timestamps is not None else []
    if len(eq) < 2 or len(ts) != len(eq):
        return empty
    try:
        idx = pd.DatetimeIndex(pd.to_datetime(ts))
    except Exception:
        return empty
    series = pd.Series(np.asarray(eq, dtype=float), index=idx).sort_index()
    if series.empty:
        return empty
    initial = float(series.iloc[0])

    def _period_pnls(rule: str, alt: str) -> List[float]:
        # 'ME' (month-end) on pandas >= 2.2; fall back to 'M' on older pandas.
        end = None
        for r in (rule, alt):
            try:
                end = series.resample(r).last().dropna()
                break
            except (ValueError, TypeError):
                end = None
        if end is None or end.empty:
            return []
        pnl = end.diff()
        # First period measured from the very first equity point so the opening
        # partial period is counted, not dropped.
        pnl.iloc[0] = float(end.iloc[0]) - initial
        return [float(x) for x in pnl.tolist()]

    def _stats(pnls: List[float]):
        arr = np.asarray(pnls, dtype=float)
        k = int(arr.size)
        if k == 0:
            return 0, 0.0, None, 0.0
        n = max(1, int(top_n))
        pct_pos = float(np.mean(arr > 0)) * 100.0
        order = np.sort(arr)[::-1]
        topn = float(np.sum(order[:n]))
        net = float(np.sum(arr))
        share = (topn / net * 100.0) if net > 0 else None
        pos = arr[arr > 0]
        gains = float(np.sum(pos))
        topn_gain = float(np.sum(np.sort(pos)[::-1][:n]))
        gain_share = (topn_gain / gains * 100.0) if gains > 0 else 0.0
        return k, pct_pos, share, gain_share

    wk, wk_pos, wk_share, wk_gain = _stats(_period_pnls('W', 'W'))
    mo, mo_pos, mo_share, mo_gain = _stats(_period_pnls('ME', 'M'))
    return {
        'weeks': wk, 'months': mo,
        'pct_positive_weeks': wk_pos, 'pct_positive_months': mo_pos,
        'top_n': int(top_n),
        'top_n_week_share_pct': wk_share, 'top_n_week_gain_share_pct': wk_gain,
        'top_n_month_share_pct': mo_share, 'top_n_month_gain_share_pct': mo_gain,
    }


@dataclass
class BacktestTrade:
    """Record of a single completed trade"""
    entry_time: datetime
    exit_time: datetime
    side: str
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    pnl_pct: float
    exit_reason: str
    confidence: float
    indicators: Dict[str, float] = field(default_factory=dict)
    regime: str = 'unknown'
    bars_held: int = 0
    # Fase 5 — populated only when the strategy ran with risk_scoring.enabled.
    # The scalar continuous risk score plus a {component: normalized_value} dict
    # so post-hoc analysis can ask whether high-score trades fared better.
    risk_score: Optional[float] = None
    risk_score_components: Dict[str, float] = field(default_factory=dict)
    # Fase 6 — populated only when the strategy ran with bear_check.enabled.
    # {"score": float, "components": {component: normalized_value}} so post-hoc
    # analysis can ask whether high-bear-check trades fared worse.
    bear_check: Dict[str, float] = field(default_factory=dict)


@dataclass
class BacktestConfig:
    """Configuration for a backtest run"""
    initial_balance: float = 10000.0
    fee_rate: float = 0.0006       # 0.06% per side (BloFin taker)
    slippage_pct: float = 0.05     # 0.05% slippage per fill
    risk_per_trade_pct: float = 10.0
    min_confidence: float = 0.45
    allow_shorts: bool = True
    lookback_candles: int = 100    # How many candles to feed to strategy
    contract_value: float = 0.001  # BTC per contract for BTC-USDT
    use_risk_multiplier: bool = True  # Apply strategy risk_multiplier to sizing
    use_time_exits: bool = True       # Apply max_hold_bars / stale_trade exits
    stale_trade_atr_progress: float = 0.18  # Min ATR progress before stale exit


class BacktestResult:
    """Complete results from a backtest run"""

    def __init__(self, trades: List[BacktestTrade], equity_curve: List[float],
                 timestamps: List[datetime], config: BacktestConfig,
                 closes: Optional[List[float]] = None):
        self.trades = trades
        self.equity_curve = equity_curve
        self.timestamps = timestamps
        self.config = config
        # BTC close series aligned 1:1 with equity_curve / timestamps.  Used
        # for the buy-and-hold benchmark and bull/bear regime detection.  No
        # lookahead — benchmark equity at index i uses only closes[<= i].
        self.closes = list(closes) if closes is not None else []
        self._compute_metrics()

    def _compute_metrics(self):
        self.total_trades = len(self.trades)

        # Benchmark + risk-adjusted metrics depend only on the equity/close
        # series, so compute them regardless of how many trades there were.
        self._compute_benchmark_and_risk_metrics()

        if self.total_trades == 0:
            self.winning_trades = 0
            self.losing_trades = 0
            self.win_rate = 0.0
            self.avg_win = 0.0
            self.avg_loss = 0.0
            self.profit_factor = 0.0
            self.max_drawdown = 0.0
            self.max_drawdown_pct = 0.0
            self.total_pnl = 0.0
            self.total_roi = 0.0
            self.sharpe_ratio = 0.0
            self.avg_trade_pnl = 0.0
            self.long_trades = 0
            self.short_trades = 0
            self.indicator_accuracy = {}
            self.regime_metrics = {}
            self._finalize_alpha()
            return

        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]

        self.winning_trades = len(wins)
        self.losing_trades = len(losses)
        self.win_rate = self.winning_trades / self.total_trades

        self.avg_win = np.mean([t.pnl for t in wins]) if wins else 0.0
        self.avg_loss = np.mean([t.pnl for t in losses]) if losses else 0.0

        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        self.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        self.total_pnl = sum(t.pnl for t in self.trades)
        self.total_roi = (self.total_pnl / self.config.initial_balance) * 100
        self.avg_trade_pnl = self.total_pnl / self.total_trades

        self.long_trades = sum(1 for t in self.trades if t.side == "buy")
        self.short_trades = sum(1 for t in self.trades if t.side == "sell")

        # Max drawdown from equity curve
        equity = np.array(self.equity_curve)
        peak = np.maximum.accumulate(equity)
        drawdowns = (peak - equity) / peak
        self.max_drawdown_pct = float(np.max(drawdowns)) * 100 if len(drawdowns) > 0 else 0.0
        self.max_drawdown = float(np.max(peak - equity)) if len(drawdowns) > 0 else 0.0

        # Sharpe ratio (annualized)
        trade_returns = [t.pnl_pct / 100 for t in self.trades]
        if len(trade_returns) > 1 and np.std(trade_returns) > 0:
            # Estimate trades per year from data timespan
            if len(self.timestamps) >= 2:
                span_days = (self.timestamps[-1] - self.timestamps[0]).total_seconds() / 86400
                trades_per_year = (self.total_trades / max(span_days, 1)) * 365
            else:
                trades_per_year = 252
            self.sharpe_ratio = (np.mean(trade_returns) / np.std(trade_returns)) * np.sqrt(trades_per_year)
        else:
            self.sharpe_ratio = 0.0

        # Per-indicator accuracy
        self.indicator_accuracy = self._compute_indicator_accuracy()

        # Per-regime metrics
        self.regime_metrics = self._compute_regime_metrics()

        self._finalize_alpha()

    def _compute_benchmark_and_risk_metrics(self):
        """Compute the BTC buy-and-hold benchmark, conditional (bull/bear/
        sideways) metrics and the strategy's risk-adjusted metrics (Calmar,
        drawdown duration, time-under-water).  Pure measurement — no lookahead.
        """
        eq = self.equity_curve or []
        ts = self.timestamps or []
        closes = self.closes if self.closes else []

        # Period span (days) for CAGR / annualization.
        if len(ts) >= 2:
            try:
                span_days = (ts[-1] - ts[0]).total_seconds() / 86400.0
            except AttributeError:
                span_days = (float(ts[-1]) - float(ts[0])) / 86400.0
                if span_days > 1e6:
                    span_days = span_days / 1000.0
        else:
            span_days = 0.0
        self.period_days = max(span_days, 0.0)
        self._bar_seconds = _infer_bar_seconds(list(ts))

        # --- Strategy risk-adjusted metrics ---
        strat_dd = _drawdown_stats(eq)
        self.dd_duration_bars = strat_dd['max_dd_duration_bars']
        if self._bar_seconds > 0:
            self.dd_duration_days = (self.dd_duration_bars * self._bar_seconds) / 86400.0
        else:
            self.dd_duration_days = 0.0
        self.time_under_water_pct = strat_dd['time_under_water_pct']
        self.cagr = _cagr(eq, self.period_days) if self.period_days > 0 else 0.0
        strat_max_dd = strat_dd['max_drawdown_pct'] / 100.0
        self.calmar_ratio = (self.cagr / strat_max_dd) if strat_max_dd > 0 else 0.0

        # Per-bar (equity-curve) Sharpe — comparable to the benchmark Sharpe.
        # Note: this is distinct from `sharpe_ratio`, which is per-trade based.
        if len(eq) >= 2 and self._bar_seconds > 0:
            eq_arr = np.asarray(eq, dtype=float)
            with np.errstate(divide='ignore', invalid='ignore'):
                sr = np.where(eq_arr[:-1] != 0, eq_arr[1:] / eq_arr[:-1] - 1.0, 0.0)
            sr = np.nan_to_num(sr)
            ppy = 86400.0 * 365.0 / max(self._bar_seconds, 1.0)
            self.sharpe_ratio_bars = _annualized_sharpe(sr, ppy)
        else:
            self.sharpe_ratio_bars = 0.0

        # --- BTC buy-and-hold benchmark over the same period ---
        self.benchmark = compute_benchmark(
            closes, ts, initial_balance=self.config.initial_balance)
        bench_max_dd = self.benchmark.get('max_drawdown_pct', 0.0) / 100.0
        bench_cagr = self.benchmark.get('cagr', 0.0)
        self.benchmark['calmar_ratio'] = (
            bench_cagr / bench_max_dd) if bench_max_dd > 0 else 0.0

        # --- Bull / bear / sideways classification + conditional metrics ---
        self.regimes = classify_regimes(closes, ts)
        self.conditional_metrics = conditional_metrics(
            eq, ts, self.regimes, bar_seconds=self._bar_seconds)
        # Convenience counts of how the period itself was classified.
        self.regime_bar_counts = {
            r: sum(1 for x in self.regimes if x == r)
            for r in ('bull', 'bear', 'sideways')
        }

        # --- PnL concentration (the "few lucky periods" overfitting flag) ---
        # Top-N week/month share of total PnL + % of positive periods.  A high
        # top-N share with a low %-positive is the signature of an apparent edge
        # that is really a handful of lucky periods.
        self.pnl_concentration = period_concentration(eq, ts, top_n=5)

    def _finalize_alpha(self):
        """Strategy-vs-benchmark alpha.  Needs sharpe_ratio to already be set."""
        bench_ret = self.benchmark.get('total_return_pct', 0.0)
        strat_ret = getattr(self, 'total_roi', 0.0)
        self.alpha_vs_benchmark_pct = strat_ret - bench_ret
        # Risk-adjusted alpha: difference in (annualized) Sharpe.  The
        # benchmark Sharpe uses per-bar returns of the buy-and-hold curve.
        bench_eq = self.benchmark.get('equity_curve', [])
        if len(bench_eq) >= 2 and self._bar_seconds > 0:
            bench_eq_arr = np.asarray(bench_eq, dtype=float)
            with np.errstate(divide='ignore', invalid='ignore'):
                br = np.where(bench_eq_arr[:-1] != 0,
                              bench_eq_arr[1:] / bench_eq_arr[:-1] - 1.0, 0.0)
            br = np.nan_to_num(br)
            ppy = 86400.0 * 365.0 / max(self._bar_seconds, 1.0)
            self.benchmark_sharpe = _annualized_sharpe(br, ppy)
        else:
            self.benchmark_sharpe = 0.0
        # Risk-adjusted alpha uses the per-bar (equity) Sharpe on both sides so
        # the comparison is like-for-like.
        self.alpha_sharpe = getattr(self, 'sharpe_ratio_bars', 0.0) - self.benchmark_sharpe

    def _compute_indicator_accuracy(self) -> Dict[str, Dict]:
        """For each indicator, check if its vote direction matched trade outcome."""
        accuracy = {}
        for trade in self.trades:
            if not trade.indicators:
                continue
            profitable = trade.pnl > 0
            for name, score in trade.indicators.items():
                if name not in accuracy:
                    accuracy[name] = {'correct': 0, 'incorrect': 0, 'neutral': 0}
                if abs(score) < 0.3:
                    accuracy[name]['neutral'] += 1
                elif (score > 0 and trade.side == "buy") or (score < 0 and trade.side == "sell"):
                    # Indicator agreed with trade direction
                    if profitable:
                        accuracy[name]['correct'] += 1
                    else:
                        accuracy[name]['incorrect'] += 1
                else:
                    # Indicator disagreed
                    if profitable:
                        accuracy[name]['incorrect'] += 1
                    else:
                        accuracy[name]['correct'] += 1

        # Compute rates
        for name in accuracy:
            total = accuracy[name]['correct'] + accuracy[name]['incorrect']
            accuracy[name]['accuracy'] = accuracy[name]['correct'] / total if total > 0 else 0.0
            accuracy[name]['total_votes'] = total

        return accuracy

    def _compute_regime_metrics(self) -> Dict[str, Dict]:
        """Compute per-regime trade breakdown."""
        by_regime: Dict[str, List[BacktestTrade]] = {}
        for t in self.trades:
            by_regime.setdefault(t.regime, []).append(t)

        metrics = {}
        for regime, regime_trades in sorted(by_regime.items()):
            wins = [t for t in regime_trades if t.pnl > 0]
            losses = [t for t in regime_trades if t.pnl <= 0]
            gross_profit = sum(t.pnl for t in wins)
            gross_loss = abs(sum(t.pnl for t in losses))
            metrics[regime] = {
                'trades': len(regime_trades),
                'win_rate': len(wins) / len(regime_trades),
                'profit_factor': gross_profit / gross_loss if gross_loss > 0 else float('inf'),
                'total_pnl': sum(t.pnl for t in regime_trades),
                'avg_hold_bars': (
                    sum(t.bars_held for t in regime_trades) / len(regime_trades)),
            }
        return metrics

    def summary(self) -> str:
        """Human-readable summary"""
        lines = [
            "=" * 60,
            "BACKTEST RESULTS",
            "=" * 60,
            f"Initial Balance:  ${self.config.initial_balance:,.2f}",
            f"Final Balance:    ${self.equity_curve[-1]:,.2f}" if self.equity_curve else "",
            f"Total P&L:        ${self.total_pnl:,.2f} ({self.total_roi:+.2f}%)",
            "",
            f"Total Trades:     {self.total_trades}",
            f"  Long:           {self.long_trades}",
            f"  Short:          {self.short_trades}",
            f"Win Rate:         {self.win_rate:.1%}",
            f"Avg Win:          ${self.avg_win:,.2f}",
            f"Avg Loss:         ${self.avg_loss:,.2f}",
            f"Profit Factor:    {self.profit_factor:.2f}",
            "",
            f"Max Drawdown:     {self.max_drawdown_pct:.2f}%",
            f"Sharpe Ratio:     {self.sharpe_ratio:.2f}",
            f"Sharpe (per-bar): {self.sharpe_ratio_bars:.2f}",
            f"CAGR:             {self.cagr * 100:+.2f}%",
            f"Calmar Ratio:     {self.calmar_ratio:.2f}",
            f"DD Duration:      {self.dd_duration_bars} bars "
            f"({self.dd_duration_days:.1f}d)",
            f"Time Under Water: {self.time_under_water_pct:.1f}%",
        ]

        # PnL-concentration line (few-lucky-periods flag).
        pc = getattr(self, 'pnl_concentration', {}) or {}
        _ws = pc.get('top_n_week_share_pct')
        _ws_str = f"{_ws:.0f}%" if _ws is not None else "n/a (net<=0)"
        lines.append(
            f"PnL Concentration: top-{pc.get('top_n', 5)} wks {_ws_str} of net "
            f"({pc.get('top_n_week_gain_share_pct', 0.0):.0f}% of gains)  |  "
            f"{pc.get('pct_positive_weeks', 0.0):.0f}% weeks +, "
            f"{pc.get('pct_positive_months', 0.0):.0f}% months +")

        # Benchmark + alpha block
        b = self.benchmark
        bull_cnt = self.regime_bar_counts.get('bull', 0)
        bear_cnt = self.regime_bar_counts.get('bear', 0)
        side_cnt = self.regime_bar_counts.get('sideways', 0)
        lines += [
            "",
            "BTC Buy-and-Hold Benchmark (same period):",
            f"  Total Return:   {b.get('total_return_pct', 0.0):+.2f}%",
            f"  Max Drawdown:   {b.get('max_drawdown_pct', 0.0):.2f}%",
            f"  DD Duration:    {b.get('max_dd_duration_bars', 0)} bars",
            f"  Time U/Water:   {b.get('time_under_water_pct', 0.0):.1f}%",
            f"  Calmar:         {b.get('calmar_ratio', 0.0):.2f}",
            "",
            f"Alpha vs B&H:     {self.alpha_vs_benchmark_pct:+.2f}% return  "
            f"({self.alpha_sharpe:+.2f} Sharpe diff, per-bar)",
            "",
            f"Period regime mix: bull {bull_cnt} / bear {bear_cnt} / "
            f"sideways {side_cnt} bars",
            "Strategy by market regime:",
        ]
        for label in ('bull', 'bear', 'sideways'):
            cm = self.conditional_metrics.get(label, {})
            lines.append(
                f"  {label:9s}  {cm.get('bars', 0):5d} bars  "
                f"ret {cm.get('total_return_pct', 0.0):+7.2f}%  "
                f"Sharpe {cm.get('sharpe', 0.0):+6.2f}  "
                f"maxDD {cm.get('max_drawdown_pct', 0.0):5.2f}%")

        if self.regime_metrics:
            lines.append("")
            lines.append("Per-Regime Breakdown:")
            for regime, rm in self.regime_metrics.items():
                pf_str = f"{rm['profit_factor']:.2f}" if rm['profit_factor'] != float('inf') else "inf"
                lines.append(
                    f"  {regime:14s}  {rm['trades']:3d} trades  "
                    f"WR {rm['win_rate']:.0%}  PF {pf_str}  "
                    f"PnL ${rm['total_pnl']:+.2f}  "
                    f"Avg hold {rm['avg_hold_bars']:.1f} bars")

        if self.indicator_accuracy:
            lines.append("")
            lines.append("Indicator Accuracy:")
            for name, stats in sorted(self.indicator_accuracy.items()):
                lines.append(f"  {name:12s}  {stats['accuracy']:.1%} "
                           f"({stats['total_votes']} votes)")

        lines.append("=" * 60)
        return "\n".join(lines)

    def to_dict(self) -> Dict:
        """Serialize metrics to dictionary"""
        return {
            'total_trades': self.total_trades,
            'long_trades': self.long_trades,
            'short_trades': self.short_trades,
            'win_rate': self.win_rate,
            'avg_win': self.avg_win,
            'avg_loss': self.avg_loss,
            'profit_factor': self.profit_factor,
            'total_pnl': self.total_pnl,
            'total_roi': self.total_roi,
            'total_return_pct': self.total_roi,  # alias, mirrors benchmark.total_return_pct
            'max_drawdown_pct': self.max_drawdown_pct,
            'sharpe_ratio': self.sharpe_ratio,
            'sharpe_ratio_bars': self.sharpe_ratio_bars,
            'cagr': self.cagr,
            'calmar_ratio': self.calmar_ratio,
            'dd_duration_bars': self.dd_duration_bars,
            'dd_duration_days': self.dd_duration_days,
            'time_under_water_pct': self.time_under_water_pct,
            'period_days': self.period_days,
            'initial_balance': self.config.initial_balance,
            'final_balance': self.equity_curve[-1] if self.equity_curve else self.config.initial_balance,
            'regime_metrics': self.regime_metrics,
            # Fase 1: BTC buy-and-hold benchmark + alpha + conditional metrics
            'benchmark': {
                'total_return_pct': self.benchmark.get('total_return_pct', 0.0),
                'max_drawdown_pct': self.benchmark.get('max_drawdown_pct', 0.0),
                'max_dd_duration_bars': self.benchmark.get('max_dd_duration_bars', 0),
                'time_under_water_pct': self.benchmark.get('time_under_water_pct', 0.0),
                'cagr': self.benchmark.get('cagr', 0.0),
                'calmar_ratio': self.benchmark.get('calmar_ratio', 0.0),
                'final_balance': self.benchmark.get('final_balance',
                                                    self.config.initial_balance),
                # Equity curve points: kept out of to_dict() to avoid bloating
                # serialized output — use equity_series() for charting (it pairs
                # strategy + benchmark + regime per bar and can be downsampled).
                'equity_curve_len': len(self.benchmark.get('equity_curve', [])),
            },
            'benchmark_sharpe': self.benchmark_sharpe,
            'alpha_vs_benchmark_pct': self.alpha_vs_benchmark_pct,
            'alpha_sharpe': self.alpha_sharpe,
            'regime_bar_counts': self.regime_bar_counts,
            'conditional_metrics': self.conditional_metrics,
            'pnl_concentration': self.pnl_concentration,
        }

    def equity_series(self) -> List[Dict]:
        """Equity curve as a list of {ts, equity, benchmark, regime} points.

        Convenience for the dashboard — pairs the strategy equity with the BTC
        buy-and-hold benchmark and the bull/bear/sideways label per bar.
        """
        bench = self.benchmark.get('equity_curve', []) if self.benchmark else []
        out = []
        for i, eq in enumerate(self.equity_curve or []):
            ts = self.timestamps[i] if i < len(self.timestamps) else None
            out.append({
                'ts': ts.isoformat() if hasattr(ts, 'isoformat') else ts,
                'equity': float(eq),
                'benchmark': float(bench[i]) if i < len(bench) else None,
                'regime': self.regimes[i] if i < len(self.regimes) else None,
            })
        return out


class Backtester:
    """Replays historical data through strategy and simulates trading"""

    def __init__(self, strategy: TradingStrategy, config: BacktestConfig,
                 htf_sync: HTFCandleSync = None):
        self.strategy = strategy
        self.config = config
        self.htf_sync = htf_sync

    def run(self, candles_df: pd.DataFrame) -> BacktestResult:
        """Run backtest over the given candle data."""
        # Convert DataFrame to list of candle arrays (matching BloFin format)
        candles = []
        for _, row in candles_df.iterrows():
            candles.append([
                int(row['timestamp'].timestamp() * 1000) if hasattr(row['timestamp'], 'timestamp') else row['timestamp'],
                row['open'], row['high'], row['low'], row['close'], row['volume'],
                0, 0, 0  # Padding to match BloFin format
            ])

        balance = self.config.initial_balance
        position = None
        trades = []
        equity_curve = [balance]
        timestamps = [candles_df['timestamp'].iloc[0]]
        # BTC close aligned to each equity-curve / timestamp point — used by
        # BacktestResult for the buy-and-hold benchmark and regime detection.
        # The first point is the first candle's close (matches equity[0] which
        # is the initial balance at that timestamp).
        closes = [float(candles[0][4])]

        lookback = self.config.lookback_candles

        for i in range(lookback, len(candles)):
            window = candles[i - lookback:i]
            current = candles[i]
            current_price = float(current[4])  # Close
            current_high = float(current[2])
            current_low = float(current[3])
            current_time = candles_df['timestamp'].iloc[i]

            # 1. Check exits on open position
            if position is not None:
                position['bars_held'] = position.get('bars_held', 0) + 1
                exit_reason = self._check_exit(
                    position, current_high, current_low, current_price)
                if exit_reason:
                    exit_price = self._get_exit_price(
                        position, exit_reason, current_high, current_low,
                        current_price)
                    trade = self._close_position(
                        position, exit_price, current_time, exit_reason)
                    trades.append(trade)
                    balance += trade.pnl
                    position = None

            # 2. Check for new entry
            if position is None:
                # Inject HTF candles if available (eliminates lookahead)
                if self.htf_sync and hasattr(self.strategy, 'set_htf_candles'):
                    current_ts_ms = int(current[0]) if isinstance(current[0], (int, float)) else int(current_time.timestamp() * 1000)
                    htf_data = {}
                    for tf in self.htf_sync.available_timeframes:
                        closed = self.htf_sync.get_closed_candles(
                            tf, current_ts_ms, max_candles=200)
                        if closed:
                            htf_data[tf] = closed
                    self.strategy.set_htf_candles(htf_data)

                signal = self.strategy.analyze(window, current_price)
                if signal.action != "hold" and signal.confidence >= self.config.min_confidence:
                    if signal.action == "sell" and not self.config.allow_shorts:
                        pass  # Skip short signals
                    elif signal.stop_loss is not None and signal.take_profit is not None:
                        risk_mult = 1.0
                        if self.config.use_risk_multiplier:
                            risk_mult = max(float(
                                getattr(signal, 'risk_multiplier', 1.0) or 0.0), 0.0)
                        if risk_mult <= 0:
                            pass  # Skip — regime blocks trade
                        else:
                            size = self._calculate_size(
                                balance, current_price,
                                signal.stop_loss, risk_mult)
                            if size >= 0.1:
                                indicators = {}
                                if hasattr(signal, 'indicators') and signal.indicators:
                                    indicators = dict(signal.indicators)

                                # Apply slippage to entry: buy higher, sell lower
                                slip = self.config.slippage_pct / 100.0
                                if signal.action == 'buy':
                                    entry_price = current_price * (1 + slip)
                                else:
                                    entry_price = current_price * (1 - slip)

                                max_hold = 0
                                if self.config.use_time_exits:
                                    max_hold = int(
                                        getattr(signal, 'max_hold_bars', 0) or 0)

                                atr = float(getattr(signal, 'atr', 0.0) or 0.0)
                                regime = str(
                                    getattr(signal, 'regime', 'unknown') or 'unknown')

                                position = {
                                    'side': signal.action,
                                    'entry_price': entry_price,
                                    'size': size,
                                    'stop_loss': signal.stop_loss,
                                    'take_profit': signal.take_profit,
                                    'entry_time': current_time,
                                    'confidence': signal.confidence,
                                    'indicators': indicators,
                                    'bars_held': 0,
                                    'max_hold_bars': max_hold,
                                    'atr': atr,
                                    'peak_progress': 0.0,
                                    'regime': regime,
                                    'risk_score': getattr(signal, 'risk_score', None),
                                    'risk_score_components': dict(
                                        getattr(signal, 'risk_score_components', {}) or {}),
                                    'bear_check': dict(
                                        getattr(signal, 'bear_check', {}) or {}),
                                }

            # 3. Track equity
            unrealized = 0.0
            if position is not None:
                unrealized = self._unrealized_pnl(position, current_price)
            equity_curve.append(balance + unrealized)
            timestamps.append(current_time)
            closes.append(current_price)

        # Close any remaining position at last price
        if position is not None:
            last_price = float(candles[-1][4])
            last_time = candles_df['timestamp'].iloc[-1]
            exit_price = self._get_exit_price(
                position, "end_of_data", float(candles[-1][2]),
                float(candles[-1][3]), last_price)
            trade = self._close_position(
                position, exit_price, last_time, "end_of_data")
            trades.append(trade)
            balance += trade.pnl
            equity_curve[-1] = balance

        return BacktestResult(trades, equity_curve, timestamps, self.config,
                              closes=closes)

    def _check_exit(self, position: dict, candle_high: float,
                    candle_low: float, current_price: float) -> Optional[str]:
        """Check if candle triggers SL, TP, or time-based exit."""
        sl = position['stop_loss']
        tp = position['take_profit']

        if position['side'] == 'buy':
            sl_hit = candle_low <= sl
            tp_hit = candle_high >= tp
        else:
            sl_hit = candle_high >= sl
            tp_hit = candle_low <= tp

        # Conservative: if both hit in same candle, assume SL first
        if sl_hit and tp_hit:
            return "stop_loss"
        if sl_hit:
            return "stop_loss"
        if tp_hit:
            return "take_profit"

        # Time-based exits (matching live bot behavior)
        if self.config.use_time_exits:
            max_hold = position.get('max_hold_bars', 0)
            bars = position.get('bars_held', 0)
            if max_hold > 0 and bars >= max_hold:
                return "max_hold_bars"

            # Stale trade detection
            atr = position.get('atr', 0.0)
            if atr > 0 and bars >= 6:
                entry = position['entry_price']
                if position['side'] == 'buy':
                    progress = (current_price - entry) / atr
                else:
                    progress = (entry - current_price) / atr
                position['peak_progress'] = max(
                    position.get('peak_progress', 0.0), progress)
                if position['peak_progress'] < self.config.stale_trade_atr_progress:
                    return "stale_trade"

        return None

    def _get_exit_price(self, position: dict, exit_reason: str,
                        candle_high: float, candle_low: float,
                        current_price: float) -> float:
        """Determine exit price based on reason, with slippage."""
        slip = self.config.slippage_pct / 100.0

        if exit_reason == "stop_loss":
            base = position['stop_loss']
        elif exit_reason == "take_profit":
            base = position['take_profit']
        elif exit_reason in ("max_hold_bars", "stale_trade", "end_of_data"):
            base = current_price
        else:
            base = current_price

        # Apply slippage: closing a long = selling (lower), closing a short = buying (higher)
        if position['side'] == 'buy':
            return base * (1 - slip)
        else:
            return base * (1 + slip)

    def _close_position(self, position: dict, exit_price: float,
                        exit_time: datetime, exit_reason: str) -> BacktestTrade:
        """Close position and create trade record."""
        pnl = self._calculate_pnl(position, exit_price)
        entry_value = position['size'] * self.config.contract_value * position['entry_price']
        pnl_pct = (pnl / entry_value * 100) if entry_value > 0 else 0.0

        return BacktestTrade(
            entry_time=position['entry_time'],
            exit_time=exit_time,
            side=position['side'],
            entry_price=position['entry_price'],
            exit_price=exit_price,
            size=position['size'],
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=exit_reason,
            confidence=position['confidence'],
            indicators=position.get('indicators', {}),
            regime=position.get('regime', 'unknown'),
            bars_held=position.get('bars_held', 0),
            risk_score=position.get('risk_score'),
            risk_score_components=dict(position.get('risk_score_components', {}) or {}),
            bear_check=dict(position.get('bear_check', {}) or {}),
        )

    def _calculate_pnl(self, position: dict, exit_price: float) -> float:
        """Calculate P&L including fees."""
        size = position['size']
        entry = position['entry_price']
        cv = self.config.contract_value

        if position['side'] == 'buy':
            gross_pnl = (exit_price - entry) * size * cv
        else:
            gross_pnl = (entry - exit_price) * size * cv

        # Fees: fee_rate * notional on both entry and exit
        entry_fee = self.config.fee_rate * size * cv * entry
        exit_fee = self.config.fee_rate * size * cv * exit_price
        net_pnl = gross_pnl - entry_fee - exit_fee

        return net_pnl

    def _unrealized_pnl(self, position: dict, current_price: float) -> float:
        """Calculate unrealized P&L for equity curve."""
        return self._calculate_pnl(position, current_price)

    def _calculate_size(self, balance: float, price: float,
                        stop_loss: float,
                        risk_multiplier: float = 1.0) -> float:
        """Calculate position size using SL-based sizing (matches live bot)."""
        result = calculate_risk_position_size(
            balance=balance,
            entry_price=price,
            stop_loss=stop_loss,
            risk_percent=self.config.risk_per_trade_pct * risk_multiplier,
            contract_size=self.config.contract_value,
            contract_step=0.1,
            min_contracts=0.1,
            leverage=1.0,
            max_position_notional_pct=100.0,
            slippage_buffer_pct=self.config.slippage_pct,
        )
        return result.contracts

    @classmethod
    def from_multi_timeframe(
        cls,
        strategy: TradingStrategy,
        config: BacktestConfig,
        htf_datasets: Dict[str, pd.DataFrame],
    ) -> "Backtester":
        """Create a Backtester with real HTF candle data.

        Usage:
            datasets = collector.get_multi_timeframe_data('BTC-USDT', '5m',
                           higher_tfs=['15m', '1H', '4H'], days=30)
            bt = Backtester.from_multi_timeframe(strategy, config, datasets)
            result = bt.run(datasets['5m'])
        """
        sync = HTFCandleSync(htf_datasets)
        return cls(strategy, config, htf_sync=sync)
