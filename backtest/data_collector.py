#!/usr/bin/env python3
"""
Historical Data Collector for Backtesting
Fetches candle data from BloFin and caches locally as CSV.
"""

import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Optional

import pandas as pd
import numpy as np

# Add scripts/ to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from blofin_api import BlofinAPI


# Timeframe to minutes mapping
TIMEFRAME_MINUTES = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1H": 60, "4H": 240, "1D": 1440,
}


class DataCollector:
    """Fetches and caches historical candle data from BloFin"""

    def __init__(self, api: BlofinAPI, data_dir: str = None):
        if data_dir is None:
            data_dir = os.path.join(os.path.dirname(__file__), 'data')
        self.api = api
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def get_data(self, inst_id: str = "BTC-USDT", bar: str = "5m",
                 days: int = 30, force_refresh: bool = False) -> pd.DataFrame:
        """Main entry point: returns cached data or fetches fresh.

        Args:
            inst_id: Trading pair
            bar: Timeframe
            days: Number of days of history
            force_refresh: If True, always fetch fresh data
        """
        if not force_refresh:
            cached = self.load_from_csv(inst_id, bar)
            if cached is not None:
                # Check if cache covers the requested period
                min_ts = datetime.utcnow() - timedelta(days=days)
                if cached['timestamp'].min() <= min_ts:
                    return cached[cached['timestamp'] >= min_ts].reset_index(drop=True)

        df = self.fetch_candles(inst_id, bar, days)
        if len(df) > 0:
            self.save_to_csv(df, inst_id, bar)
        return df

    def fetch_candles(self, inst_id: str, bar: str, days: int) -> pd.DataFrame:
        """Fetch historical candle data, paginating as needed."""
        tf_minutes = TIMEFRAME_MINUTES.get(bar, 5)
        candles_per_request = 1440
        days_per_request = (candles_per_request * tf_minutes) / (60 * 24)

        end_ts = int(time.time() * 1000)
        start_ts = end_ts - (days * 24 * 60 * 60 * 1000)

        all_candles = []
        requests_made = 0
        max_requests = int(days / days_per_request) + 2

        print(f"Fetching {days} days of {bar} candles for {inst_id}...")

        # First request: get most recent candles (no pagination param)
        # Subsequent requests: use 'after' param to go backward in time
        # BloFin API: after=T returns candles OLDER than timestamp T
        current_after = None

        while requests_made < max_requests:
            if current_after is None:
                result = self.api.get_candles(
                    inst_id=inst_id, bar=bar, limit=candles_per_request
                )
            else:
                result = self.api.get_candles(
                    inst_id=inst_id, bar=bar, limit=candles_per_request,
                    after=str(current_after)
                )

            if result.get("code") == "error" or not result.get("data"):
                if requests_made == 0:
                    print(f"  API error: {result.get('msg', 'no data')}")
                break

            batch = result["data"]
            if not batch:
                break

            all_candles.extend(batch)
            requests_made += 1

            # Find oldest timestamp in batch
            oldest_ts = min(int(c[0]) for c in batch)
            print(f"  Batch {requests_made}: {len(batch)} candles "
                  f"(oldest: {datetime.utcfromtimestamp(oldest_ts/1000).strftime('%Y-%m-%d %H:%M')})")

            if oldest_ts <= start_ts:
                break

            current_after = oldest_ts
            time.sleep(0.2)  # Rate limiting

        if not all_candles:
            return pd.DataFrame(columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

        # Build DataFrame
        df = pd.DataFrame(all_candles)
        # BloFin candles: [ts, open, high, low, close, volume, ...]
        df = df.iloc[:, :6]
        df.columns = ['timestamp', 'open', 'high', 'low', 'close', 'volume']

        # Convert types
        df['timestamp'] = pd.to_datetime(df['timestamp'].astype(float), unit='ms', utc=True)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = df[col].astype(float)

        # Sort ascending, remove duplicates
        df = df.sort_values('timestamp').drop_duplicates(subset='timestamp').reset_index(drop=True)

        # Filter to requested period
        min_ts = datetime.utcnow().replace(tzinfo=df['timestamp'].dt.tz) - timedelta(days=days)
        df = df[df['timestamp'] >= min_ts].reset_index(drop=True)

        print(f"  Total: {len(df)} candles ({df['timestamp'].min()} to {df['timestamp'].max()})")
        return df

    def save_to_csv(self, df: pd.DataFrame, inst_id: str, bar: str) -> Path:
        """Save DataFrame to CSV file."""
        filename = f"{inst_id}_{bar}.csv"
        filepath = self.data_dir / filename
        df.to_csv(filepath, index=False)
        print(f"  Saved to {filepath}")
        return filepath

    def load_from_csv(self, inst_id: str, bar: str) -> Optional[pd.DataFrame]:
        """Load cached data from CSV if it exists."""
        filename = f"{inst_id}_{bar}.csv"
        filepath = self.data_dir / filename

        if not filepath.exists():
            return None

        df = pd.read_csv(filepath)
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
        return df


    def get_multi_timeframe_data(
        self,
        inst_id: str = "BTC-USDT",
        base_tf: str = "5m",
        higher_tfs: list = None,
        days: int = 30,
        force_refresh: bool = False,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch candle data for multiple timeframes independently.

        Returns a dict mapping timeframe string to its DataFrame.
        Each HTF dataset is fetched separately from the exchange,
        NOT resampled from the base timeframe.
        """
        if higher_tfs is None:
            higher_tfs = ["15m", "1H", "4H"]

        all_tfs = [base_tf] + [tf for tf in higher_tfs if tf != base_tf]
        result = {}

        for tf in all_tfs:
            print(f"Fetching {tf} data...")
            df = self.get_data(inst_id, tf, days, force_refresh)
            result[tf] = df
            print(f"  {tf}: {len(df)} candles")

        return result

    def save_multi_timeframe(
        self, datasets: Dict[str, pd.DataFrame], inst_id: str
    ) -> None:
        """Save all timeframe datasets to CSV."""
        for tf, df in datasets.items():
            self.save_to_csv(df, inst_id, tf)

    def load_multi_timeframe(
        self, inst_id: str, timeframes: list
    ) -> Dict[str, Optional[pd.DataFrame]]:
        """Load cached multi-timeframe data."""
        result = {}
        for tf in timeframes:
            result[tf] = self.load_from_csv(inst_id, tf)
        return result


def generate_synthetic_data(days: int = 90, bar: str = "5m",
                            start_price: float = 70000.0,
                            volatility: float = 0.02) -> pd.DataFrame:
    """Generate realistic synthetic BTC price data for backtesting without API access.

    Uses geometric Brownian motion with mean-reverting volatility clusters
    to simulate realistic crypto price action.
    """
    tf_minutes = TIMEFRAME_MINUTES.get(bar, 5)
    n_candles = int(days * 24 * 60 / tf_minutes)

    np.random.seed(42)

    # Generate returns with volatility clustering (GARCH-like)
    base_vol = volatility / np.sqrt(24 * 60 / tf_minutes)  # Per-candle volatility
    vol = np.full(n_candles, base_vol)
    for i in range(1, n_candles):
        vol[i] = 0.94 * vol[i-1] + 0.06 * base_vol * abs(np.random.normal())

    returns = np.random.normal(0.00001, vol)  # Slight upward drift

    # Build close prices
    closes = np.zeros(n_candles)
    closes[0] = start_price
    for i in range(1, n_candles):
        closes[i] = closes[i-1] * (1 + returns[i])

    # Generate OHLCV from closes
    timestamps = pd.date_range(
        end=datetime.utcnow(), periods=n_candles, freq=f"{tf_minutes}min", tz='UTC'
    )

    highs = closes * (1 + np.abs(np.random.normal(0, base_vol * 0.5, n_candles)))
    lows = closes * (1 - np.abs(np.random.normal(0, base_vol * 0.5, n_candles)))
    opens = np.roll(closes, 1)
    opens[0] = start_price

    # Volume: base + random + spike on big moves
    base_volume = 1000
    volumes = base_volume + np.abs(np.random.normal(0, 200, n_candles))
    big_moves = np.abs(returns) > 2 * base_vol
    volumes[big_moves] *= 2.5

    df = pd.DataFrame({
        'timestamp': timestamps,
        'open': opens,
        'high': highs,
        'low': lows,
        'close': closes,
        'volume': volumes
    })

    return df
