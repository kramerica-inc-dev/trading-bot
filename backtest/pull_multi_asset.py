#!/usr/bin/env python3
"""Plan E (ε) data prep: pull 12 months of 1h candles for a multi-asset universe.

Universe: top 10 Blofin perps by expected liquidity (BTC, ETH, SOL, XRP,
BNB, DOGE, ADA, AVAX, DOT, LINK). Any asset whose API call fails or whose
data is too short is dropped; we proceed with the rest if >= 6 remain.

Outputs: backtest/data/{SYMBOL}_1h.csv per asset.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT))

from blofin_api import BlofinAPI  # noqa: E402
from backtest.data_collector import DataCollector  # noqa: E402


UNIVERSE = [
    "BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "BNB-USDT",
    "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "DOT-USDT", "LINK-USDT",
]
DAYS = 365
BAR = "1H"


def main() -> int:
    api = BlofinAPI(
        api_key=os.getenv("BLOFIN_API_KEY", "public"),
        api_secret=os.getenv("BLOFIN_API_SECRET", "public"),
        passphrase=os.getenv("BLOFIN_PASSPHRASE", "public"),
    )
    dc = DataCollector(api)

    ok, failed, short = [], [], []
    for sym in UNIVERSE:
        try:
            print(f"\n--- {sym} ---")
            df = dc.get_data(inst_id=sym, bar=BAR, days=DAYS, force_refresh=True)
            print(f"  pulled {len(df)} rows, "
                  f"{df['timestamp'].min() if len(df) else '-'} → "
                  f"{df['timestamp'].max() if len(df) else '-'}")
            if len(df) < 6000:  # need roughly 250+ days of 1h = 6000 bars
                short.append((sym, len(df)))
            else:
                ok.append(sym)
        except Exception as e:
            print(f"  FAILED: {e}")
            failed.append((sym, str(e)))
        time.sleep(0.2)

    print(f"\n== Summary ==")
    print(f"OK ({len(ok)}): {ok}")
    print(f"SHORT ({len(short)}): {short}")
    print(f"FAILED ({len(failed)}): {failed}")

    if len(ok) < 6:
        print("\nFewer than 6 assets with good data. Aborting ε pipeline.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
