#!/usr/bin/env python3
"""Build backtest/data/BTC-USDT_1d.csv.

Two modes:
  * ``--source venue`` (default) — fetch native 1D candles straight from the
    BloFin market endpoint (the same paginated endpoint `data_collector.py`
    uses); this reaches back as far as the venue allows (~2023-01 for
    BTC-USDT, i.e. ~3.3 years), which is the longest history we can honestly
    obtain.  The last bar is dropped if it is the still-forming current UTC
    day, so every emitted row is a fully-closed daily bar.
  * ``--source 5m|1H`` — the old behaviour: resample an existing higher-freq
    CSV into daily bars (only ~1 year, the span of those CSVs).

Daily OHLCV bars are keyed by the candle *open* time (UTC midnight) — the same
"timestamp = candle-open" convention the rest of the backtest data uses.

Usage:
    python -m backtest.build_daily_csv               # venue fetch (longest)
    python -m backtest.build_daily_csv --source 5m   # resample local 5m CSV
"""

import argparse
import os
import sys
from datetime import datetime, timezone

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


def fetch_from_venue(inst_id: str = "BTC-USDT", days: int = 1500) -> pd.DataFrame:
    """Fetch native 1D candles from BloFin via `DataCollector.fetch_candles`.

    Market data is public, so dummy API credentials are fine.  Returns a
    DataFrame with the standard columns sorted ascending, with the still-forming
    current UTC day stripped off so every row is a fully-closed daily bar.
    """
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    sys.path.insert(0, os.path.join(ROOT, "backtest"))
    from blofin_api import BlofinAPI  # noqa: E402
    from data_collector import DataCollector  # noqa: E402

    api = BlofinAPI("public", "public", "public")
    dc = DataCollector(api)
    df = dc.fetch_candles(inst_id, "1D", days=days)
    if df.empty:
        return df
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp").drop_duplicates(subset="timestamp").reset_index(drop=True)
    # Strip the current (still forming) UTC day: keep only bars strictly older
    # than today's UTC midnight.
    today_midnight = pd.Timestamp(datetime.now(timezone.utc).date(), tz="UTC")
    df = df[df["timestamp"] < today_midnight].reset_index(drop=True)
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the daily BTC CSV")
    parser.add_argument("--source", default="venue", choices=["venue", "5m", "1H"],
                        help="'venue' = fetch native 1D candles from BloFin "
                             "(longest history); '5m'/'1H' = resample a local CSV")
    parser.add_argument("--inst", default="BTC-USDT", help="instrument id (venue mode)")
    parser.add_argument("--days", type=int, default=1500,
                        help="how far back to request (venue mode); the venue caps "
                             "BTC-USDT 1D at ~3.3y so anything ≥1300 gets everything")
    args = parser.parse_args()
    if args.source == "venue":
        daily = fetch_from_venue(args.inst, args.days)
        src_desc = f"venue ({args.inst} 1D)"
    else:
        daily = build(args.source)
        src_desc = args.source
    if daily.empty:
        print("No daily bars produced — aborting (CSV left untouched).")
        return 1
    daily.to_csv(OUT_PATH, index=False)
    print(f"Wrote {len(daily)} daily bars to {OUT_PATH} "
          f"({daily['timestamp'].iloc[0]} .. {daily['timestamp'].iloc[-1]}) "
          f"from {src_desc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
