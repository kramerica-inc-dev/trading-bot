#!/usr/bin/env python3
"""Deribit DVOL (BTC implied-volatility index) backfill — for the VRP lane.

DVOL is Deribit's 30-day forward implied-vol index for BTC (the crypto VIX).
This one-shot fetcher paginates the public `get_volatility_index_data` endpoint
(no auth) back to the index's inception (~2021-03) and writes a daily CSV.

The variance-risk-premium backtest (backtest/sweep/vrp.py) pairs this implied
vol against *realized* vol computed from BTC daily closes (the OKX series we
already have), so we don't depend on Deribit's short realized-vol history.

Usage:
    python -m backtest.deribit_dvol --currency BTC
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List

import pandas as pd
import requests

BASE = "https://www.deribit.com/api/v2/public"
OUT_DIR = (Path(__file__).parent / "data").resolve()
DAY_MS = 86_400_000


def fetch_dvol(currency: str = "BTC", *, start_ms: int = 1_614_556_800_000,
               end_ms: int | None = None) -> pd.DataFrame:
    """Paginate DVOL daily candles backward to `start_ms` (default 2021-03-01)."""
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
    # end_ms must be provided by the caller (Date.now is avoided here); default to
    # a far-future bound so we get everything up to now.
    end = end_ms if end_ms is not None else 1_800_000_000_000
    rows: List[list] = []
    seen_earliest = None
    for _ in range(20):
        r = s.get(f"{BASE}/get_volatility_index_data",
                  params={"currency": currency, "start_timestamp": start_ms,
                          "end_timestamp": end, "resolution": "1D"}, timeout=20)
        data = (r.json().get("result") or {}).get("data") or []
        if not data:
            break
        rows.extend(data)
        earliest = min(d[0] for d in data)
        print(f"  +{len(data)} (earliest {pd.to_datetime(earliest, unit='ms').date()})")
        if seen_earliest is not None and earliest >= seen_earliest:
            break               # no further progress
        seen_earliest = earliest
        if earliest <= start_ms:
            break
        end = earliest - DAY_MS
        time.sleep(0.15)

    if not rows:
        return pd.DataFrame(columns=["timestamp", "dvol"])
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
    df = df.drop_duplicates(subset=["ts"]).sort_values("ts")
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.rename(columns={"close": "dvol"})
    return df[["timestamp", "dvol"]].reset_index(drop=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--currency", default="BTC")
    ap.add_argument("--end-ms", type=int, default=None,
                    help="upper timestamp bound (ms); default far-future")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Fetching Deribit DVOL ({args.currency})...")
    df = fetch_dvol(args.currency, end_ms=args.end_ms)
    if df.empty:
        print("No DVOL data fetched.", file=sys.stderr)
        return 1
    out = OUT_DIR / f"deribit_dvol_{args.currency}.csv"
    df.to_csv(out, index=False)
    print(f"\nWrote {len(df)} rows to {out}")
    print(f"  range: {df['timestamp'].min().date()} .. {df['timestamp'].max().date()}")
    print(f"  DVOL: min={df['dvol'].min():.1f} max={df['dvol'].max():.1f} "
          f"mean={df['dvol'].mean():.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
