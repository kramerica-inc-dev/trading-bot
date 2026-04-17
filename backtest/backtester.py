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
                 timestamps: List[datetime], config: BacktestConfig):
        self.trades = trades
        self.equity_curve = equity_curve
        self.timestamps = timestamps
        self.config = config
        self._compute_metrics()

    def _compute_metrics(self):
        self.total_trades = len(self.trades)

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
        ]

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
            'max_drawdown_pct': self.max_drawdown_pct,
            'sharpe_ratio': self.sharpe_ratio,
            'initial_balance': self.config.initial_balance,
            'final_balance': self.equity_curve[-1] if self.equity_curve else self.config.initial_balance,
            'regime_metrics': self.regime_metrics,
        }


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
                                }

            # 3. Track equity
            unrealized = 0.0
            if position is not None:
                unrealized = self._unrealized_pnl(position, current_price)
            equity_curve.append(balance + unrealized)
            timestamps.append(current_time)

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

        return BacktestResult(trades, equity_curve, timestamps, self.config)

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
