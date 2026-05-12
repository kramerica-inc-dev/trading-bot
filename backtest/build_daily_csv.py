#!/usr/bin/env python3
"""Build backtest/data/BTC-USDT_1d.csv by resampling the 5m (or 1H) candle CSV.

Daily OHLCV bars keyed by the candle *open* time (UTC midnight) — the same
"timestamp = candle-open" convention the rest of the backtest data uses.  The
first/last day are dropped if they are partial (the 5m series does not start at
exactly 00:00), so every emitted bar is a full UTC day.

Usage:
    python -m backtest.build_daily_csv [--source 5m|1H]
"""

import argparse
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "backtest", "data")
OUT_PATH = os.path.join(DATA_DIR, "BTC-USDT_1d.csv")


def build(source: str = "5m") -> pd.DataFrame:
    src_path = os.path.join(DATA_DIR, f"BTC-USDT_{source}.csv")
    if not os.path.exists(src_path):
        raise FileNotFoundError(
            f"{src_path} not found — regenerate it via backtest/data_collector.py")
    df = pd.read_csv(src_path)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()
    daily = (
        df.resample("1D", label="left", closed="left")
        .agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
             close=("close", "last"), volume=("volume", "sum"))
        .dropna()
    )
    daily = daily.reset_index()

    # Drop a partial first day: if the source doesn't start at 00:00 UTC the
    # first daily bar aggregates only a fraction of the day.
    src_start = df.index[0]
    if src_start.hour != 0 or src_start.minute != 0 or src_start.second != 0:
        daily = daily.iloc[1:].reset_index(drop=True)
    return daily


def main() -> int:
    parser = argparse.ArgumentParser(description="Resample BTC candles to daily")
    parser.add_argument("--source", default="5m", choices=["5m", "1H"],
                        help="Which higher-frequency CSV to resample from")
    args = parser.parse_args()
    daily = build(args.source)
    daily.to_csv(OUT_PATH, index=False)
    print(f"Wrote {len(daily)} daily bars to {OUT_PATH} "
          f"({daily['timestamp'].iloc[0]} .. {daily['timestamp'].iloc[-1]}) "
          f"from {args.source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
