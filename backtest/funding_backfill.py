#!/usr/bin/env python3
"""Historical funding-rate backfill for Blofin perpetuals.

One-shot script that pulls historical funding data via the Blofin public
funding-rate-history endpoint and writes it to a CSV suitable for merging
into backtest runs. No authentication required.

Usage:
    python -m backtest.funding_backfill --inst BTC-USDT --days 365
    python -m backtest.funding_backfill --inst BTC-USDT --days 365 --output backtest/data/funding_BTC-USDT.csv
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

import pandas as pd

# Make the scripts/ dir importable for BlofinAPI
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from blofin_api import BlofinAPI  # noqa: E402


# Blofin funding-rate-history per-request cap
PAGE_LIMIT = 100
# Polite delay between paginated requests (the API client also rate-limits)
REQUEST_SPACING_SEC = 0.15


def _parse_row(row: dict) -> Optional[dict]:
    """Normalise a Blofin funding history row to a flat dict.

    Blofin returns strings for fundingRate and timestamps; coerce to the
    types we actually want to store.
    """
    try:
        ft = row.get("fundingTime") or row.get("fundingRateTime")
        rate = row.get("fundingRate")
        if ft is None or rate is None:
            return None
        return {
            "fundingTime": int(ft),
            "fundingRate": float(rate),
            "instId": row.get("instId"),
        }
    except (TypeError, ValueError):
        return None


def backfill_funding(api: BlofinAPI, inst_id: str, days: int) -> pd.DataFrame:
    """Paginate the funding-rate-history endpoint backward from now.

    Blofin pagination (consistent with get_candles): ``after=T`` returns
    records OLDER than fundingTime T. We start with no cursor to get the
    most recent page, then feed the oldest fundingTime back in as the
    ``after`` cursor, stopping when we run past the requested window or
    the server returns an empty page.
    """
    end_ts_ms = int(time.time() * 1000)
    min_ts_ms = end_ts_ms - days * 24 * 60 * 60 * 1000

    rows: List[dict] = []
    after_cursor: Optional[str] = None
    pages = 0
    max_pages = (days * 3) // PAGE_LIMIT + 4  # 3 settlements/day + slack

    print(f"Fetching {days} days of funding for {inst_id} "
          f"(up to {max_pages} pages x {PAGE_LIMIT} records)...")

    while pages < max_pages:
        resp = api.get_funding_rate_history(
            inst_id=inst_id,
            after=after_cursor,
            limit=PAGE_LIMIT,
        )
        pages += 1

        if not isinstance(resp, dict) or resp.get("code") not in ("0", 0):
            msg = resp.get("msg") if isinstance(resp, dict) else str(resp)
            print(f"  page {pages}: API error: {msg}", file=sys.stderr)
            break

        data = resp.get("data") or []
        if not data:
            print(f"  page {pages}: empty, stopping.")
            break

        parsed = [r for r in (_parse_row(d) for d in data) if r is not None]
        rows.extend(parsed)

        oldest = min(r["fundingTime"] for r in parsed)
        print(f"  page {pages}: +{len(parsed)} rows "
              f"(oldest fundingTime={oldest})")

        if oldest <= min_ts_ms:
            # Got far enough into the past; stop.
            break

        # Next page: records OLDER than current oldest.
        after_cursor = str(oldest)
        time.sleep(REQUEST_SPACING_SEC)
    else:
        print(f"  reached max_pages ({max_pages}); output may be incomplete.",
              file=sys.stderr)

    if not rows:
        return pd.DataFrame(columns=["fundingTime", "fundingRate", "instId",
                                     "timestamp"])

    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["fundingTime"]).sort_values("fundingTime")
    # Trim to the requested window (backend sometimes returns older records
    # beyond our cutoff on the last page).
    df = df[df["fundingTime"] >= min_ts_ms].reset_index(drop=True)
    # Human-readable UTC timestamp alongside the raw ms value so candle CSVs
    # can be merged on either.
    df["timestamp"] = pd.to_datetime(df["fundingTime"], unit="ms", utc=True)
    return df


def default_output_path(inst_id: str) -> Path:
    return (Path(__file__).parent / "data" /
            f"funding_{inst_id}.csv").resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inst", default="BTC-USDT",
                        help="Instrument ID (default: BTC-USDT)")
    parser.add_argument("--days", type=int, default=365,
                        help="How far back to fetch (default: 365)")
    parser.add_argument("--output", default=None,
                        help="Output CSV path "
                             "(default: backtest/data/funding_<inst>.csv)")
    args = parser.parse_args()

    # Public endpoint — BlofinAPI will still sign requests (harmless) but
    # credentials are not required. Use empty strings if env vars missing.
    api = BlofinAPI(
        api_key=os.getenv("BLOFIN_API_KEY", "public"),
        api_secret=os.getenv("BLOFIN_API_SECRET", "public"),
        passphrase=os.getenv("BLOFIN_PASSPHRASE", "public"),
        demo=False,
    )

    df = backfill_funding(api, args.inst, args.days)
    if df.empty:
        print("No funding data fetched; aborting.", file=sys.stderr)
        return 1

    out = Path(args.output) if args.output else default_output_path(args.inst)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)

    print(f"\nWrote {len(df)} rows to {out}")
    print(f"  range: {df['timestamp'].min()} .. {df['timestamp'].max()}")
    print(f"  funding rate: min={df['fundingRate'].min():.6f} "
          f"max={df['fundingRate'].max():.6f} "
          f"mean={df['fundingRate'].mean():.6f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
