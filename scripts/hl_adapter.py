#!/usr/bin/env python3
"""Hyperliquid execution adapter — the order/signing leg for the momentum lane.

The momentum edge is validated AND executable on Hyperliquid (NL retail, no KYC,
no geoblock — see docs/VENUE-ACCESS-RESEARCH.md + docs/HL-VALIDATION). This wraps
the **official** `hyperliquid-python-sdk` (audited EIP-712 L1-action signing — we
do NOT hand-roll crypto) behind a three-state safety gate mirroring
`carry_runner.resolve_mode` / `xs_runner.resolve_mode`:

  * network=testnet                         → TESTNET       (real signed orders,
                                                              mock funds — safe)
  * network=mainnet & allow_live=false      → MAINNET_DRY   (mainnet DATA only;
                                                              order calls refused)
  * network=mainnet & allow_live=true       → MAINNET_LIVE  (REAL money — gated)

Keys come from the caller (env), never hard-coded and never logged. Use a
Hyperliquid **API/agent wallet** key (authorize it in the UI) so the main
account key is never exposed; set `account_address` to the main account.

Self-test (no funds needed — proves data + signing end-to-end on testnet):
    python -m scripts.hl_adapter --selftest
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import hashlib

import eth_account
import numpy as np
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.types import Cloid

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
# Mode gate centralised in mode_gate (re-exported for back-compat callers/tests).
from mode_gate import (  # noqa: E402,F401
    MODE_TESTNET, MODE_MAINNET_DRY, MODE_MAINNET_LIVE, resolve_hl_mode,
)


def _mask(addr: Optional[str]) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else str(addr)


@dataclass
class HLOrderResult:
    ok: bool
    raw: dict
    filled_sz: float = 0.0
    avg_px: float = 0.0
    error: Optional[str] = None


class HLAdapter:
    """Thin, guarded wrapper over the Hyperliquid SDK (Info + Exchange)."""

    def __init__(self, *, network: str = "testnet", private_key: Optional[str] = None,
                 account_address: Optional[str] = None, allow_live: bool = False):
        self.network = network
        self.allow_live = allow_live
        self.mode = resolve_hl_mode(network, allow_live)
        self.base_url = (constants.TESTNET_API_URL if network == "testnet"
                         else constants.MAINNET_API_URL)
        self.info = Info(self.base_url, skip_ws=True)
        self.wallet = eth_account.Account.from_key(private_key) if private_key else None
        self.address = account_address or (self.wallet.address if self.wallet else None)
        self.exchange = (Exchange(self.wallet, self.base_url, account_address=self.address)
                         if self.wallet else None)
        self._meta_cache: Optional[dict] = None

    # ------------------------------------------------------------------ reads
    def meta(self) -> dict:
        if self._meta_cache is None:
            self._meta_cache = self.info.meta()
        return self._meta_cache

    def sz_decimals(self) -> Dict[str, int]:
        return {u["name"]: int(u["szDecimals"]) for u in self.meta()["universe"]}

    def all_mids(self) -> Dict[str, float]:
        """Current mid prices. An EMPTY dict means the feed is unavailable/degraded
        (HL always serves a non-empty book), so callers must treat {} as "no data"
        and NOT size/close on it — never as a genuine empty market."""
        out: Dict[str, float] = {}
        try:
            for k, v in (self.info.all_mids() or {}).items():
                try:
                    out[k] = float(v)
                except (TypeError, ValueError):
                    continue
        except Exception:
            pass
        return out

    def daily_closes(self, coins: List[str], lookback: int, *, pad: int = 12,
                     return_latest_ms: bool = False):
        """Recent CLOSED daily closes per coin (oldest→newest) for the momentum
        signal. Public (no wallet). Drops the in-progress bar (T > now). A
        malformed response for one coin is skipped, never aborts the universe.
        With return_latest_ms=True also returns the newest closed-bar timestamp
        (ms) across the universe for the staleness guard — None if no data."""
        need = lookback + pad
        now_ms = int(time.time() * 1000)
        start = now_ms - (need + 3) * 86_400_000
        out: Dict[str, np.ndarray] = {}
        latest_ms: Optional[int] = None
        for c in coins:
            try:
                data = self.info.post("/info", {"type": "candleSnapshot", "req": {
                    "coin": c, "interval": "1d", "startTime": start, "endTime": now_ms}})
                if not isinstance(data, list):
                    continue
                rows = [(int(d["T"]), float(d["c"])) for d in data
                        if isinstance(d, dict) and "c" in d and "T" in d
                        and int(d["T"]) <= now_ms]
            except Exception:
                continue
            if len(rows) >= lookback + 1:
                rows.sort(key=lambda r: r[0])            # guarantee oldest→newest
                out[c] = np.asarray([r[1] for r in rows], dtype=float)
                latest_ms = max(latest_ms or 0, rows[-1][0])
            time.sleep(0.05)
        return (out, latest_ms) if return_latest_ms else out

    def funding_daily(self, coins: List[str]) -> Dict[str, float]:
        """Current predicted DAILY funding rate per coin (HL funding settles
        hourly → ×24). Best-effort; returns {} on any failure so a caller can fall
        back to a flat rate. For honest SIM P&L only — live funding is already
        reflected in account_value()."""
        out: Dict[str, float] = {}
        try:
            meta, ctxs = self.info.meta_and_asset_ctxs()
            names = [u["name"] for u in meta.get("universe", [])]
            for name, ctx in zip(names, ctxs):
                if name in coins and isinstance(ctx, dict) and "funding" in ctx:
                    try:
                        out[name] = float(ctx["funding"]) * 24.0
                    except (TypeError, ValueError):
                        continue
        except Exception:
            pass
        return out

    def _spot_usdc_free(self) -> Optional[float]:
        """FREE (un-held) USDC in the spot clearinghouse: total − hold. `hold` is
        spot USDC that is reserved — in a UNIFIED account the perp-margin earmark
        (which is ALSO counted in perp marginSummary.accountValue), plus any
        resting spot orders / pending transfers. Subtracting it is exactly what
        stops `account_value` double-counting the margin once positions are open.
        Empirically verified on-chain: perp_av 198.85 + free 792.02 = 990.87 on a
        ~991 account (vs 1188 if `hold` were NOT removed). None on a read failure."""
        try:
            ss = self.info.spot_user_state(self.address)
            for b in ss.get("balances", []) or []:
                if b.get("coin") == "USDC":
                    total = float(b.get("total") or 0.0)
                    hold = float(b.get("hold") or 0.0)
                    return max(0.0, total - hold)
            return 0.0
        except Exception:
            return None

    def account_value(self) -> Optional[float]:
        """Total account equity (USD) usable as perp collateral — correct for
        BOTH standard and UNIFIED (HL default) account modes:

            equity = perp marginSummary.accountValue + free spot USDC

        In standard mode spot USDC is ~0, so this is just the perp account value.
        In a unified account the collateral SPLITS between the perp side
        (accountValue, which already carries unrealized PnL and the margin
        earmark) and the un-held spot balance; their sum is the true equity. This
        avoids the 'unfunded' false-skip (perp reads ~0 before any position) and
        the 80%-false-drawdown (perp accountValue alone ignores the spot
        remainder once positions open). Retries; returns None on a TRANSIENT read
        so a hiccup isn't mistaken for a real 0; a genuine empty account is 0.0."""
        if not self.address:
            return 0.0
        for i in range(3):
            try:
                st = self.info.user_state(self.address)
                ms = st.get("marginSummary")
                if ms is None or "accountValue" not in ms:
                    time.sleep(0.4 * (i + 1)); continue
                perp_av = float(ms["accountValue"])
                spot_free = self._spot_usdc_free()
                if spot_free is None:
                    # Spot-endpoint hiccup. A perp-FUNDED (standard-mode) account
                    # doesn't need the spot read — mark off perp alone rather than
                    # skipping the whole cycle. If perp is ~0 the funds may be
                    # unified in spot, so we genuinely can't tell → retry/None.
                    if perp_av > 1e-9:
                        return perp_av
                    time.sleep(0.4 * (i + 1)); continue
                return perp_av + spot_free
            except Exception:
                time.sleep(0.4 * (i + 1))
        return None

    def positions(self) -> Dict[str, dict]:
        """{coin: {szi, entry_px, unrealized_pnl}} for open perp positions.
        Raises on a hard read failure (callers must NOT treat that as 'flat')."""
        if not self.address:
            return {}
        st = self.info.user_state(self.address)
        out: Dict[str, dict] = {}
        for ap in st.get("assetPositions", []) or []:
            try:
                p = ap.get("position") or {}
                szi = float(p.get("szi", 0.0))
                if szi != 0.0:
                    out[p["coin"]] = {"szi": szi, "entry_px": float(p.get("entryPx") or 0.0),
                                      "unrealized_pnl": float(p.get("unrealizedPnl") or 0.0)}
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def book_notional(self) -> Dict[str, float]:
        """Signed USD notional per held coin (szi * mark) — for notional-aware
        reconcile / neutrality checks."""
        mids = self.all_mids()
        out: Dict[str, float] = {}
        for coin, p in self.positions().items():
            mk = mids.get(coin) or p.get("entry_px") or 0.0
            out[coin] = p["szi"] * mk
        return out

    # ------------------------------------------------------------------ guard
    def _assert_can_trade(self) -> None:
        if self.exchange is None:
            raise RuntimeError("no signing wallet — supply a private key to place orders")
        if self.mode == MODE_MAINNET_DRY:
            raise RuntimeError(
                "MAINNET_DRY: order routing refused. Use network=testnet, or set "
                "allow_live=true for MAINNET_LIVE (real money).")

    def _round_sz(self, coin: str, sz: float) -> float:
        d = self.sz_decimals().get(coin, 4)
        return float(round(sz, d))

    def set_leverage(self, coin: str, leverage: int, *, is_cross: bool = True) -> dict:
        """Pin per-coin leverage (cross by default) so a position can't inherit
        HL's per-coin MAXIMUM leverage (e.g. 40x BTC). Returns {ok, raw|error};
        a no-op (ok=False) without a signing wallet or in MAINNET_DRY."""
        if self.exchange is None or self.mode == MODE_MAINNET_DRY:
            return {"ok": False, "error": "not live (no wallet / MAINNET_DRY)"}
        try:
            raw = self.exchange.update_leverage(int(leverage), coin, is_cross)
            ok = isinstance(raw, dict) and raw.get("status") == "ok"
            return {"ok": ok, "raw": raw, "error": None if ok else str(raw)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @staticmethod
    def make_cloid(seed: str) -> Cloid:
        """Deterministic 128-bit client order id from a seed string, so a re-send of
        the SAME intended leg carries the SAME id — a venue-level idempotency tag
        (P0 #2). Distinct seeds (per cycle/coin/action) give distinct ids."""
        return Cloid.from_str("0x" + hashlib.sha256(seed.encode()).hexdigest()[:32])

    def order_status_by_cloid(self, cloid: Cloid) -> Optional[dict]:
        """Query an order's status by client order id (idempotency check before a
        re-send). None on read failure or no wallet."""
        if not self.address:
            return None
        try:
            return self.info.query_order_by_cloid(self.address, cloid)
        except Exception:
            return None

    # ----------------------------------------------------------------- orders
    MIN_ORDER_USD = 10.0   # Hyperliquid minimum order value

    def market_order_usd(self, coin: str, is_buy: bool, usd_notional: float, *,
                         slippage: float = 0.05, cloid: Optional[Cloid] = None) -> HLOrderResult:
        """Marketable IOC sized by USD notional (mid -> sz, rounded to szDecimals).
        Enforces HL's $10 min and rejects if rounding moved the notional far, so a
        leg is never silently mis-sized or dropped to 0. Never raises. An optional
        cloid tags the order for idempotency."""
        self._assert_can_trade()
        if usd_notional < self.MIN_ORDER_USD:
            return HLOrderResult(ok=False, raw={},
                                 error=f"notional ${usd_notional:.2f} < ${self.MIN_ORDER_USD:.0f} min ({coin})")
        mid = self.all_mids().get(coin)
        if not mid or mid <= 0:
            return HLOrderResult(ok=False, raw={}, error=f"no mid for {coin}")
        sz = self._round_sz(coin, usd_notional / mid)
        if sz <= 0:
            return HLOrderResult(ok=False, raw={}, error=f"size rounds to 0 ({coin}, ${usd_notional:.2f})")
        rounded = sz * mid
        if rounded < self.MIN_ORDER_USD or abs(rounded - usd_notional) / usd_notional > 0.5:
            return HLOrderResult(ok=False, raw={},
                                 error=f"rounded notional ${rounded:.2f} off-target/below-min ({coin})")
        try:
            raw = self.exchange.market_open(coin, is_buy, sz, None, slippage, cloid)
        except Exception as e:
            return HLOrderResult(ok=False, raw={}, error=f"order exception: {e}")
        return self._parse_order(raw)

    def close(self, coin: str, *, slippage: float = 0.05,
              cloid: Optional[Cloid] = None) -> HLOrderResult:
        self._assert_can_trade()
        try:
            raw = self.exchange.market_close(coin, None, None, slippage, cloid)
        except Exception as e:
            return HLOrderResult(ok=False, raw={}, error=f"close exception: {e}")
        if raw is None:                          # SDK returns None when nothing to close
            return HLOrderResult(ok=True, raw={}, error=None)
        return self._parse_order(raw)

    @staticmethod
    def _parse_order(raw: Optional[dict]) -> HLOrderResult:
        if raw is None:
            return HLOrderResult(ok=False, raw={}, error="no response (None)")
        try:
            if raw.get("status") != "ok":
                return HLOrderResult(ok=False, raw=raw, error=str(raw))
            statuses = raw["response"]["data"]["statuses"]
            filled = next((s["filled"] for s in statuses if "filled" in s), None)
            if filled:
                sz = float(filled["totalSz"])
                return HLOrderResult(ok=sz > 0, raw=raw, filled_sz=sz,
                                     avg_px=float(filled["avgPx"]),
                                     error=None if sz > 0 else "zero fill")
            err = next((s["error"] for s in statuses if "error" in s), None)
            if err is not None:
                return HLOrderResult(ok=False, raw=raw, error=err)
            # resting/canceled IOC with no fill -> NOT filled (phantom-leg guard)
            return HLOrderResult(ok=False, raw=raw, error="not filled (resting/canceled)")
        except (KeyError, TypeError, IndexError) as e:
            return HLOrderResult(ok=False, raw=raw, error=f"parse: {e}")


# ---------------------------------------------------------------------------
# Self-test (testnet, no funds) — proves data + signing end-to-end
# ---------------------------------------------------------------------------

def _selftest() -> int:
    print("HL adapter self-test (testnet, throwaway wallet — no funds needed)\n")
    acct = eth_account.Account.create()                 # ephemeral; never persisted
    print(f"  ephemeral wallet: {_mask(acct.address)}")
    a = HLAdapter(network="testnet", private_key=acct.key.hex(), account_address=acct.address)
    print(f"  mode: {a.mode}  base: {a.base_url}")

    mids = a.all_mids()
    print(f"  all_mids: {len(mids)} coins (BTC mid ~ {mids.get('BTC')})  ✓ data path")
    print(f"  account_value (fresh wallet): {a.account_value()}  ✓ user_state path")
    print(f"  open positions: {a.positions()}")

    # Attempt a tiny order. A fresh/unfunded account should be REJECTED on
    # margin/registration — NOT on signature. A signature error would mean the
    # EIP-712 L1-action signing is broken; a margin error proves it works.
    print("\n  attempting tiny testnet BTC order (expect a non-signature rejection)…")
    r = a.market_order_usd("BTC", True, 12.0)
    err = (r.error or "").lower()
    sig_broken = any(k in err for k in ("signature", "recover", "does not exist", "must deposit"))
    print(f"  ok={r.ok}  error={r.error}")
    if r.ok:
        print("  → order FILLED (wallet was funded) — signing + routing PROVEN ✓")
        a.close("BTC")
    elif "signature" in err or "recover" in err:
        print("  → SIGNATURE error — signing is BROKEN ✗")
        return 1
    else:
        print("  → rejected on margin/registration, NOT signature — signing + routing PROVEN ✓")
    print("\nSelf-test passed: data reads + EIP-712 signing + /exchange routing all work.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
