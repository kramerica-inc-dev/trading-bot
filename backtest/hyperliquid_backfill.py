#!/usr/bin/env python3
"""Hyperliquid public-data backfill — for validating the momentum lead on the
venue where it can actually execute (NL retail, no KYC, no geoblock).

Hyperliquid exposes everything we need from one public POST endpoint
(https://api.hyperliquid.xyz/info, no auth):
  * candleSnapshot  → daily OHLC per coin
  * fundingHistory  → hourly funding per coin (we aggregate to daily)

Writes xsectional-compatible CSVs so backtest/sweep/xsectional.py can run on it
unchanged:
  backtest/data/hyperliquid/<COIN>_1d.csv         (timestamp,open,high,low,close,volume)
  backtest/data/hyperliquid/funding_<COIN>.csv    (timestamp,funding_rate)

Usage:
    python -m backtest.hyperliquid_backfill
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import List

import pandas as pd
import requests

URL = "https://api.hyperliquid.xyz/info"
OUT = (Path(__file__).parent / "data" / "hyperliquid").resolve()
ASSETS = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "DOT", "LINK"]
# Fixed ms bounds (Date.now is unavailable here): 2023-01-01 .. ~2026-05-29.
START_MS = 1_672_531_200_000
END_MS = 1_780_000_000_000
DAY_MS = 86_400_000
HOUR_MS = 3_600_000

_S = requests.Session()
_S.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                   "Content-Type": "application/json"})


def _post(body: dict, *, retries: int = 4):
    for i in range(retries):
        try:
            r = _S.post(URL, data=json.dumps(body), timeout=25)
            if r.status_code == 200 and r.text.strip():
                return r.json()
        except (requests.RequestException, ValueError):
            pass
        time.sleep(0.6 * (i + 1))
    return None


def fetch_candles(coin: str, *, start=START_MS, end=END_MS) -> pd.DataFrame:
    """Daily candles, paginated forward (HL caps ~5000 rows/req; daily is small)."""
    rows = []
    cur = start
    for _ in range(40):
        data = _post({"type": "candleSnapshot",
                      "req": {"coin": coin, "interval": "1d",
                              "startTime": cur, "endTime": end}})
        if not isinstance(data, list) or not data:
            break
        for c in data:
            rows.append({"ts": int(c["t"]), "open": float(c["o"]), "high": float(c["h"]),
                         "low": float(c["l"]), "close": float(c["c"]), "volume": float(c["v"])})
        newest = max(int(c["t"]) for c in data)
        if newest <= cur or newest >= end - DAY_MS:
            break
        cur = newest + DAY_MS
        time.sleep(0.25)
    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows).drop_duplicates(subset=["ts"]).sort_values("ts")
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def fetch_funding(coin: str, *, start=START_MS, end=END_MS) -> pd.DataFrame:
    """Hourly funding, paginated forward (HL caps 500 rows/req)."""
    rows = []
    cur = start
    for _ in range(400):
        # NB: fundingHistory takes coin/startTime/endTime at the TOP LEVEL
        # (unlike candleSnapshot which nests them under "req").
        data = _post({"type": "fundingHistory",
                      "coin": coin, "startTime": cur, "endTime": end})
        if not isinstance(data, list) or not data:
            break
        for f in data:
            rows.append({"ts": int(f["time"]), "funding_rate": float(f["fundingRate"])})
        newest = max(int(f["time"]) for f in data)
        if newest <= cur or len(data) < 500:
            break
        cur = newest + HOUR_MS
        time.sleep(0.2)
    if not rows:
        return pd.DataFrame(columns=["timestamp", "funding_rate"])
    df = pd.DataFrame(rows).drop_duplicates(subset=["ts"]).sort_values("ts")
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df[["timestamp", "funding_rate"]].reset_index(drop=True)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"Hyperliquid backfill → {OUT}")
    summary = []
    for coin in ASSETS:
        cdf = fetch_candles(coin)
        if not cdf.empty:
            cdf.to_csv(OUT / f"{coin}_1d.csv", index=False)
        fdf = fetch_funding(coin)
        if not fdf.empty:
            fdf.to_csv(OUT / f"funding_{coin}.csv", index=False)
        crange = (f"{cdf['timestamp'].min().date()}..{cdf['timestamp'].max().date()}"
                  if not cdf.empty else "none")
        frange = (f"{fdf['timestamp'].min().date()}..{fdf['timestamp'].max().date()}"
                  if not fdf.empty else "none")
        print(f"  {coin:5s} candles={len(cdf):5d} [{crange}]  funding={len(fdf):6d} [{frange}]")
        summary.append((coin, len(cdf), len(fdf)))
        time.sleep(0.3)
    ok = sum(1 for _, c, _ in summary if c > 0)
    print(f"\n{ok}/{len(ASSETS)} assets with candles.")
    return 0 if ok >= 6 else 1


if __name__ == "__main__":
    sys.exit(main())
