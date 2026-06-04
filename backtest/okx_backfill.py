#!/usr/bin/env python3
"""OKX-sourced historical backfill (M1 of the OKX strategy-sweep plan).

The whole repo's cached backtest data is BloFin-sourced. BloFin perp data is
a defensible *proxy* for price/momentum research, but it is NOT acceptable for
OKX-specific funding-carry or basis claims — those must come from OKX itself.
This one-shot script closes that gap using OKX's PUBLIC endpoints (no auth):

  * funding-rate-history  → backtest/data/okx/funding_<inst>.csv
  * history-candles       → backtest/data/okx/<inst>_<bar>.csv

Output schemas are byte-for-byte compatible with the existing loaders so the
backtest harnesses consume OKX data unchanged:
  funding: timestamp,funding_rate,funding_time,funding_interval_hours
           (see backtest/daily_backtester.py::load_funding_series + DEFAULT_FUNDING_PATH)
  candles: timestamp,open,high,low,close,volume
           (see backtest/daily_backtester.py::load_daily_btc)

OKX data is written to a *separate* backtest/data/okx/ directory so OKX and
BloFin series are never confused; the sweep points at these explicitly.

Usage:
    python -m backtest.okx_backfill --inst BTC-USDT --days 1300 --funding --bar 1D
    python -m backtest.okx_backfill --assets BTC-USDT,ETH-USDT,SOL-USDT --bar 1H --days 400
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from okx_api import OkxAPI  # noqa: E402

FUNDING_PAGE = 100          # OKX funding-rate-history hard cap
CANDLE_PAGE = 100           # OKX history-candles hard cap
REQUEST_SPACING_SEC = 0.12
OUT_DIR = (Path(__file__).parent / "data" / "okx").resolve()


def _to_swap(inst_id: str) -> str:
    return inst_id if inst_id.endswith("-SWAP") else f"{inst_id}-SWAP"


# ---------------------------------------------------------------------------
# Funding
# ---------------------------------------------------------------------------

def backfill_funding(api: OkxAPI, inst_id: str, days: int) -> pd.DataFrame:
    """Paginate OKX funding-rate-history backward from now.

    OKX `after=T` returns settlements OLDER than fundingTime T (same cursor
    convention as the BloFin backfill). Stop when we pass the window or a
    page comes back empty.
    """
    swap = _to_swap(inst_id)
    end_ms = int(time.time() * 1000)
    min_ms = end_ms - days * 86_400_000
    rows: List[Dict[str, Any]] = []
    after: Optional[str] = None
    pages = 0
    max_pages = (days * 3) // FUNDING_PAGE + 8     # 3 settlements/day + slack

    print(f"[funding] {swap}: up to {max_pages} pages x {FUNDING_PAGE}...")
    while pages < max_pages:
        resp = api.get_funding_rate_history(inst_id=swap, after=after, limit=FUNDING_PAGE)
        pages += 1
        if not isinstance(resp, dict) or str(resp.get("code")) != "0":
            print(f"  page {pages}: error {resp.get('msg') if isinstance(resp, dict) else resp}",
                  file=sys.stderr)
            break
        data = resp.get("data") or []
        if not data:
            print(f"  page {pages}: empty, stopping.")
            break
        for d in data:
            try:
                rows.append({
                    "funding_time": int(d["fundingTime"]),
                    "funding_rate": float(d["fundingRate"]),
                })
            except (KeyError, TypeError, ValueError):
                continue
        oldest = min(r["funding_time"] for r in rows[-len(data):])
        print(f"  page {pages}: +{len(data)} (oldest={oldest})")
        if oldest <= min_ms:
            break
        after = str(oldest)
        time.sleep(REQUEST_SPACING_SEC)

    if not rows:
        return pd.DataFrame(columns=["timestamp", "funding_rate", "funding_time",
                                     "funding_interval_hours"])
    df = pd.DataFrame(rows).drop_duplicates(subset=["funding_time"]).sort_values("funding_time")
    df = df[df["funding_time"] >= min_ms].reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["funding_time"], unit="ms", utc=True)
    # Interval from the median gap between settlements (8h for BTC perps).
    gaps_h = df["funding_time"].diff().dropna() / 3_600_000.0
    interval = round(float(gaps_h.median()), 2) if not gaps_h.empty else 8.0
    df["funding_interval_hours"] = interval
    return df[["timestamp", "funding_rate", "funding_time", "funding_interval_hours"]]


# ---------------------------------------------------------------------------
# Candles
# ---------------------------------------------------------------------------

def backfill_candles(api: OkxAPI, inst_id: str, bar: str, days: int,
                     *, spot: bool) -> pd.DataFrame:
    """Paginate OKX history-candles backward. Returns closed bars only.

    `/api/v5/market/history-candles` retains deep history (years) unlike the
    recent-only `/market/candles`. Rows are [ts,o,h,l,c,vol,volCcy,volCcyQuote,
    confirm], newest-first; `after=ts` pages toward the past.
    """
    target = inst_id if spot else _to_swap(inst_id)
    end_ms = int(time.time() * 1000)
    min_ms = end_ms - days * 86_400_000
    rows: List[Dict[str, Any]] = []
    after: Optional[str] = None
    pages = 0
    # bars/day estimate to bound pagination
    per_day = {"1D": 1, "1Dutc": 1, "1H": 24, "4H": 6, "5m": 288, "15m": 96}.get(bar, 24)
    max_pages = (days * per_day) // CANDLE_PAGE + 8

    print(f"[candles] {target} {bar}: up to {max_pages} pages x {CANDLE_PAGE}...")
    while pages < max_pages:
        params: Dict[str, Any] = {"instId": target, "bar": bar, "limit": str(CANDLE_PAGE)}
        if after:
            params["after"] = after
        resp = api._request("GET", "/api/v5/market/history-candles", params=params)
        pages += 1
        if not isinstance(resp, dict) or str(resp.get("code")) != "0":
            print(f"  page {pages}: error {resp.get('msg') if isinstance(resp, dict) else resp}",
                  file=sys.stderr)
            break
        data = resp.get("data") or []
        if not data:
            print(f"  page {pages}: empty, stopping.")
            break
        for r in data:
            try:
                # confirm flag is the last element ("1" closed, "0" in-progress)
                if str(r[-1]) not in ("1", "1.0"):
                    continue
                rows.append({
                    "ts": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                    "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
                })
            except (IndexError, TypeError, ValueError):
                continue
        oldest = min(int(r[0]) for r in data)
        print(f"  page {pages}: +{len(data)} (oldest={oldest})")
        if oldest <= min_ms:
            break
        after = str(oldest)
        time.sleep(REQUEST_SPACING_SEC)

    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows).drop_duplicates(subset=["ts"]).sort_values("ts")
    df = df[df["ts"] >= min_ms].reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


# ---------------------------------------------------------------------------
# Sanity-diff vs the BloFin proxy
# ---------------------------------------------------------------------------

def sanity_diff_funding(okx_df: pd.DataFrame) -> str:
    ref = Path(__file__).parent / "data" / "funding_btc_usdt.csv"
    if not ref.exists() or okx_df.empty:
        return "  (no BloFin funding reference to diff against)"
    b = pd.read_csv(ref)
    return ("  funding mean — OKX %.6f vs BloFin %.6f | rows OKX %d vs BloFin %d"
            % (okx_df["funding_rate"].mean(), b["funding_rate"].mean(),
               len(okx_df), len(b)))


def sanity_diff_daily(okx_df: pd.DataFrame) -> str:
    ref = Path(__file__).parent / "data" / "BTC-USDT_1d.csv"
    if not ref.exists() or okx_df.empty:
        return "  (no BloFin daily reference to diff against)"
    b = pd.read_csv(ref)
    return ("  daily last close — OKX %.1f vs BloFin %.1f | rows OKX %d vs BloFin %d"
            % (okx_df["close"].iloc[-1], float(b["close"].iloc[-1]),
               len(okx_df), len(b)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--inst", default="BTC-USDT")
    ap.add_argument("--assets", default=None,
                    help="comma list overriding --inst (multi-asset candles)")
    ap.add_argument("--days", type=int, default=1300)
    ap.add_argument("--bar", default="1D", help="OKX bar: 1D,4H,1H,15m,5m")
    ap.add_argument("--funding", action="store_true", help="also backfill funding")
    ap.add_argument("--no-candles", action="store_true",
                    help="skip candle fetch (e.g. refresh funding without "
                         "clobbering a longer candle history)")
    ap.add_argument("--spot", action="store_true",
                    help="fetch spot candles (default: perp/SWAP)")
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    api = OkxAPI()     # public market data — no credentials

    assets = [a.strip() for a in args.assets.split(",")] if args.assets else [args.inst]
    summary: List[str] = []

    if not args.no_candles:
        for inst in assets:
            cdf = backfill_candles(api, inst, args.bar, args.days, spot=args.spot)
            suffix = "_spot" if args.spot else ""
            cpath = out_dir / f"{inst}_{args.bar}{suffix}.csv"
            cdf.to_csv(cpath, index=False)
            line = f"{cpath.name}: {len(cdf)} bars"
            if not cdf.empty:
                line += f" ({cdf['timestamp'].min()} .. {cdf['timestamp'].max()})"
            summary.append(line)
            if inst == "BTC-USDT" and args.bar in ("1D", "1Dutc") and not args.spot:
                summary.append(sanity_diff_daily(cdf))

    if args.funding:
        for inst in assets:
            fdf = backfill_funding(api, inst, args.days)
            fpath = out_dir / f"funding_{inst}.csv"
            fdf.to_csv(fpath, index=False)
            line = f"{fpath.name}: {len(fdf)} settlements"
            if not fdf.empty:
                line += (f" ({fdf['timestamp'].min()} .. {fdf['timestamp'].max()}, "
                         f"mean={fdf['funding_rate'].mean():.6f})")
            summary.append(line)
            if inst == "BTC-USDT":
                summary.append(sanity_diff_funding(fdf))

    print("\n" + "=" * 60 + "\nOKX BACKFILL SUMMARY\n" + "=" * 60)
    for s in summary:
        print("  " + s)
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
