#!/usr/bin/env python3
"""Hyperliquid carry shim — duck-types the OkxAdapter surface for carry_runner.

`scripts/carry_runner.py` was written against OKX envelopes. This shim lets the
SAME runner drive a Hyperliquid cash-and-carry (long spot UBTC/USDC + short BTC
perp) by translating the exact adapter surface the runner consumes (recon
2026-06-09, HL-CARRY-STUDY) into `HLAdapter` calls. The runner is byte-identical
for OKX; nothing here is imported unless `exchange == "hyperliquid"`.

Safety model (the lane must be impossible to run live accidentally):
  * DOUBLE GATE on every order path. The runner only calls `place_spot_order` /
    `place_order` when its carry mode != DRY_RUN (gate 1, `mode_gate.
    resolve_demo_mode`). Both methods then call `HLAdapter._assert_can_trade()`
    (gate 2, `mode_gate.resolve_hl_mode`): MAINNET_DRY or a missing signing
    wallet RAISES — it never returns a clean `{"code":"1"}` reject, so a runner
    bug can never mistake the refusal for an order that simply failed.
  * MAINNET_LIVE additionally requires `allow_live is True` (identity) AND the
    out-of-band env `HL_CONFIRM_LIVE=YES` — enforced inside `resolve_hl_mode`.
  * Funding cadence is NATIVE HOURLY. Samples are returned as per-settlement
    hourly rates; the runner annualises with `settlements_per_year` from config
    (8760 for HL). Do NOT aggregate hourly rates to 8h-equivalents.

Sub-account wiring (study recommendation: isolate the carry margin):
  the inner `HLAdapter` is built with `account_address=<sub>` so ALL Info reads
  (positions, margin, spot balances) target the sub-account, and the SDK
  `Exchange` gets `vault_address=<sub>` + `account_address=<master>` so the
  agent key's orders route into the sub (the SDK's agent-trades-sub pattern).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from hl_adapter import HLAdapter  # noqa: E402


# Conservative HL fee constants (base tier, no volume/staking discounts —
# 2026-06 schedule). HL has no per-account fee-probe API comparable to OKX's
# /account/trade-fee, so `carry_runner.pull_live_fees` reads these via
# `static_fee_schedule()`. Taker rates are what the carry's market legs pay.
HL_STATIC_FEES: Dict[str, float] = {
    "spot_maker": 0.0004,   # 4.0 bps
    "spot_taker": 0.0007,   # 7.0 bps
    "perp_maker": 0.00015,  # 1.5 bps
    "perp_taker": 0.00045,  # 4.5 bps
}

# OKX `mgnRatio` semantics are "big = safe" (the runner alarms when it drops
# BELOW margin_ratio_alarm). HL's margin_state() ratio is used/equity ("small =
# safe"), so we report the INVERSE (equity/used) and cap it instead of emitting
# infinity for a flat book (json-safe, still "maximally safe").
MARGIN_RATIO_CAP = 1e6

# A leg counts as FILLED only when filled_sz covers the requested qty within
# this relative tolerance (covers szDecimals rounding ~3e-4 at carry sizes and
# sub-second mid drift between the shim's sizing read and HLAdapter's own).
# Anything below is cached as 'partially_filled' so the runner's poll loop
# times the leg out and runs its abort/flatten path (review 2026-06-09 #3 —
# previously ANY filled_sz > 0 was reported as state='filled', so a partial
# IOC fill silently became net delta in the simulated book).
FILL_TOLERANCE = 2e-3


def _ok(data: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"code": "0", "msg": "", "data": data}


def _err(msg: str) -> Dict[str, Any]:
    return {"code": "1", "msg": str(msg), "data": []}


class _HLCarryApi:
    """Raw-API namespace consumed by carry_runner (funding reads only).

    Deliberately has NO `_request` attribute: the runner's startup probes
    (`pull_live_fees` / `verify_leverage_cap`) detect that and use the
    adapter-level `static_fee_schedule()` / `max_leverage()` instead of
    OKX REST paths.
    """

    def __init__(self, shim: "HLCarryAdapter") -> None:
        self._shim = shim

    def get_funding_rate(self, inst_id: Optional[str] = None) -> Dict[str, Any]:
        """Current PREDICTED hourly funding rate → OKX envelope.

        The runner appends "-SWAP" to its perp symbol before calling; we strip
        it and read the configured coin. Per-settlement = per-HOUR on HL.
        """
        shim = self._shim
        coin = shim._coin_from(inst_id)
        try:
            daily = shim.hl.funding_daily([coin])
        except Exception as e:
            return _err(f"funding read failed: {e}")
        if coin not in daily:
            return _err(f"no funding for {coin}")
        rate_hourly = float(daily[coin]) / 24.0
        # fundingTime = the NEXT hourly settlement boundary (HL settles at the
        # top of each hour), not the call time — so the envelope identifies
        # the settlement window the predicted rate belongs to.
        next_settle_ms = (int(time.time() * 1000) // 3_600_000 + 1) * 3_600_000
        return _ok([{"fundingRate": repr(rate_hourly),
                     "fundingTime": str(next_settle_ms)}])

    def get_funding_rate_history(self, inst_id: Optional[str] = None,
                                 limit: int = 100, **_: Any) -> Dict[str, Any]:
        """Last `limit` HOURLY funding settlements, NEWEST-FIRST (OKX order —
        the runner reverses to oldest-first). Pagination past the ~500-row
        server cap is handled inside `HLAdapter.funding_history`; the shim
        honors limits far above OKX's 100/page (e.g. 2160 = 90d hourly)."""
        shim = self._shim
        coin = shim._coin_from(inst_id)
        n = max(1, int(limit))
        # 24h slack on the window so clock skew / settlement lag can't shave
        # the oldest samples off a full seed.
        start_ms = int(time.time() * 1000) - (n + 24) * 3_600_000
        try:
            rows = shim.hl.funding_history(coin, start_ms)  # ascending, deduped
        except Exception as e:
            return _err(f"funding history read failed: {e}")
        data = [{"fundingRate": repr(float(r["rate"])),
                 "fundingTime": str(int(r["time_ms"]))}
                for r in reversed(rows[-n:])]
        return _ok(data)


class HLCarryAdapter:
    """OkxAdapter-shaped shim over HLAdapter for the carry lane.

    Constructor cfg keys (all optional except none — safe defaults):
        network              "mainnet" (default) | "testnet"
        private_key          agent-wallet key (env-sourced by the runner); None
                             → data-only adapter, every order path raises
        account_address      master account 0x… (Exchange.account_address when
                             a sub-account is used; Info reads otherwise)
        sub_account_address  sub-account 0x… holding the carry (None = master)
        coin                 perp coin, default "BTC"
        spot_pair            composed spot name, default "UBTC/USDC"
        allow_live           must be the bool True (identity) for MAINNET_LIVE;
                             even then env HL_CONFIRM_LIVE=YES is required

    `hl=` injects a pre-built/fake HLAdapter (tests). Order results resolve
    synchronously (HL market legs are IOC), so order-detail polling is served
    from an internal cache keyed by the synthetic ordId we hand the runner.
    """

    def __init__(self, cfg: Dict[str, Any], *, hl: Optional[HLAdapter] = None) -> None:
        cfg = dict(cfg or {})
        self.network: str = cfg.get("network") or "mainnet"
        self.coin: str = cfg.get("coin") or "BTC"
        self.spot_pair: str = cfg.get("spot_pair") or "UBTC/USDC"
        self.allow_live: bool = cfg.get("allow_live") is True
        self.account_address: Optional[str] = cfg.get("account_address") or None
        self.sub_account_address: Optional[str] = cfg.get("sub_account_address") or None
        private_key = cfg.get("private_key") or None

        if hl is None:
            hl = HLAdapter(
                network=self.network,
                private_key=private_key,
                # Info reads must target the account that HOLDS the carry.
                account_address=self.sub_account_address or self.account_address,
                allow_live=self.allow_live,
            )
        self.hl = hl

        # Agent-trades-sub wiring (SDK pattern): orders carry the sub as
        # vaultAddress while the signing payload references the master.
        if self.sub_account_address and getattr(self.hl, "exchange", None) is not None:
            self.hl.exchange.vault_address = self.sub_account_address
            if self.account_address:
                self.hl.exchange.account_address = self.account_address

        self.mode = getattr(self.hl, "mode", None)
        self.api = _HLCarryApi(self)
        self._order_cache: Dict[str, Dict[str, Any]] = {}
        self._order_seq = 0

    # ------------------------------------------------------------- helpers

    def _coin_from(self, inst_id: Optional[str]) -> str:
        """Perp coin from a runner-supplied inst_id ("BTC" or "BTC-SWAP")."""
        c = inst_id or self.coin
        if isinstance(c, str) and c.endswith("-SWAP"):
            c = c[:-5]
        return c or self.coin

    def _next_oid(self) -> str:
        self._order_seq += 1
        return f"hl{self._order_seq}"

    def _cache_fill(self, oid: str, state: str, fill_sz: float) -> None:
        self._order_cache[oid] = {
            "ordId": oid, "state": state, "accFillSz": f"{max(0.0, fill_sz):.8f}",
        }
        # Bound the cache — the runner only ever polls the freshest legs.
        if len(self._order_cache) > 64:
            for k in list(self._order_cache)[:-64]:
                del self._order_cache[k]

    def _envelope_from_result(self, r: Any, *, requested_qty: float) -> Dict[str, Any]:
        """HLOrderResult → OKX order envelope (+ cached synchronous detail).

        state='filled' ONLY when the fill covers the requested qty (within
        FILL_TOLERANCE); a partial IOC fill is cached as 'partially_filled'
        with the ACTUAL accFillSz, which the runner's _wait_for_fill treats as
        not-settled → leg timeout → abort/flatten on the real fill (the
        OkxAdapter return-shape contract the runner was written against)."""
        if getattr(r, "ok", False):
            filled = float(getattr(r, "filled_sz", 0.0) or 0.0)
            if filled <= 0.0:
                # close() returns ok with no fill detail when there was nothing
                # to close — report the requested qty so the book zeroes out.
                filled = float(requested_qty)
            full = filled >= float(requested_qty) * (1.0 - FILL_TOLERANCE)
            oid = self._next_oid()
            self._cache_fill(oid, "filled" if full else "partially_filled", filled)
            return _ok([{"ordId": oid}])
        return _err(getattr(r, "error", None) or "order failed")

    # ------------------------------------------------------- startup probes

    def static_fee_schedule(self) -> Dict[str, float]:
        """Documented HL base-tier fees (no per-account probe API on HL);
        consumed by carry_runner.pull_live_fees instead of OKX REST."""
        return dict(HL_STATIC_FEES)

    def max_leverage(self, perp_inst: Optional[str] = None) -> Optional[float]:
        """Venue max leverage for the carry coin from perp meta (BTC = 40 on
        HL). None on read failure — the runner keeps the configured cap."""
        coin = self._coin_from(perp_inst)
        try:
            for u in (self.hl.meta() or {}).get("universe", []) or []:
                if u.get("name") == coin and u.get("maxLeverage") is not None:
                    return float(u["maxLeverage"])
        except Exception:
            return None
        return None

    # ---------------------------------------------------------- market data

    def get_spot_ticker(self, inst_id: Optional[str] = None) -> Dict[str, Any]:
        """Spot mid (midPx, markPx fallback) for the configured pair."""
        pair = inst_id or self.spot_pair
        px = (self.hl.spot_mids() or {}).get(pair)
        if not px or px <= 0:
            return _err(f"no spot mid for {pair}")
        return _ok([{"last": repr(float(px))}])

    def get_ticker(self, inst_id: Optional[str] = None) -> Dict[str, Any]:
        """Perp mid from all_mids ({} = degraded feed → error envelope)."""
        coin = self._coin_from(inst_id)
        px = (self.hl.all_mids() or {}).get(coin)
        if not px or px <= 0:
            return _err(f"no perp mid for {coin}")
        return _ok([{"last": repr(float(px))}])

    # -------------------------------------------------------------- account

    def assert_unified_margin(self) -> Dict[str, Any]:
        """HL has no acctLv tiers — spot USDC and perp margin already share one
        account (and the sub-account isolates the carry book). Report acct_lv=3
        so the runner's C6 unified-margin check passes without modification."""
        return {
            "ok": True,
            "acct_lv": 3,
            "message": "HL unified (spot USDC + perp margin share the account; "
                       "sub-account isolates the carry)",
        }

    def get_margin_snapshot(self, *, perp_inst_id: Optional[str] = None,
                            **_: Any) -> Dict[str, Any]:
        """Flat snapshot in the OkxAdapter shape.

        margin_ratio is INVERTED to OKX semantics (equity/used, "big = safe",
        capped at MARGIN_RATIO_CAP for a flat book) so the runner's existing
        `margin_ratio < margin_ratio_alarm` check keeps its meaning.
        """
        coin = self._coin_from(perp_inst_id)
        out: Dict[str, Any] = {
            "total_eq_usd": None, "avail_eq_usd": None,
            "unrealized_perp_usd": None, "margin_ratio": None,
            "short_perp_qty": None, "spot_btc_qty": None,
            "raw_balance": None, "raw_positions": None,
            "errors": [],
        }

        ms = self.hl.margin_state()
        if ms is None:
            out["errors"].append({"step": "margin_state",
                                  "error": "margin_state unavailable"})
        else:
            out["raw_balance"] = ms
            out["total_eq_usd"] = float(ms.get("account_value") or 0.0)
            out["avail_eq_usd"] = float(ms.get("withdrawable") or 0.0)
            used = float(ms.get("total_margin_used") or 0.0)
            equity = float(ms.get("account_value") or 0.0)
            if used <= 0.0:
                out["margin_ratio"] = MARGIN_RATIO_CAP      # flat book = safe
            else:
                out["margin_ratio"] = min(equity / used, MARGIN_RATIO_CAP)

        try:
            pos = self.hl.positions()
            out["raw_positions"] = pos
            p = pos.get(coin)
            out["short_perp_qty"] = float(p["szi"]) if p else 0.0
            out["unrealized_perp_usd"] = float(p.get("unrealized_pnl", 0.0)) if p else 0.0
        except Exception as e:
            out["errors"].append({"step": "positions", "error": str(e)})

        rec = self.hl.resolve_spot_pair(self.spot_pair)
        base = rec["base"] if rec else self.spot_pair.split("/")[0]
        bals = self.hl.spot_balances()
        if bals is None:
            out["errors"].append({"step": "spot_balances",
                                  "error": "spot balances unavailable"})
        else:
            out["spot_btc_qty"] = float((bals.get(base) or {}).get("total", 0.0))

        return out

    # --------------------------------------------------------------- orders

    def place_spot_order(self, inst_id: Optional[str] = None, side: str = "buy",
                         order_type: str = "market", size: str = "0",
                         price: Optional[str] = None, td_mode: str = "cash",
                         target_currency: str = "base_ccy",
                         client_order_id: Optional[str] = None,
                         **_: Any) -> Dict[str, Any]:
        """Spot leg (BASE-unit sized, like the runner sends) → HL spot IOC.

        DOUBLE GATE: only reachable when the carry mode != DRY_RUN, and the
        explicit `_assert_can_trade()` below RAISES in MAINNET_DRY / without a
        signing wallet (never a clean error envelope). The guarded
        `HLAdapter.spot_market_order_usd` asserts again on its own path.
        """
        self.hl._assert_can_trade()
        qty = float(size)
        mid = (self.hl.spot_mids() or {}).get(self.spot_pair)
        if not mid or mid <= 0:
            return _err(f"no spot mid for {self.spot_pair}")
        cloid = self.hl.make_cloid(
            f"carry-spot:{self.spot_pair}:{side}:{qty:.8f}:{self._order_seq}")
        r = self.hl.spot_market_order_usd(
            self.spot_pair, side == "buy", qty * float(mid), cloid=cloid)
        return self._envelope_from_result(r, requested_qty=qty)

    def place_order(self, inst_id: Optional[str] = None, side: str = "sell",
                    order_type: str = "market", size: str = "0",
                    price: Optional[str] = None, margin_mode: str = "isolated",
                    reduce_only: bool = False, **_: Any) -> Dict[str, Any]:
        """Perp leg → HL perp IOC (open) or market_close (reduce_only unwind).
        The unwind passes the REQUESTED qty to close() — never sz=None — so a
        reduce-only leg can only ever reduce the carry's own book; on a shared
        account, close-all would flatten another lane's position in the same
        coin (review 2026-06-09 #2). close() still can never flip the book the
        way a plain buy could. Same double gate as the spot leg.
        """
        self.hl._assert_can_trade()
        coin = self._coin_from(inst_id)
        qty = float(size)
        cloid = self.hl.make_cloid(
            f"carry-perp:{coin}:{side}:{qty:.8f}:ro={bool(reduce_only)}:{self._order_seq}")
        if reduce_only:
            r = self.hl.close(coin, sz=qty, cloid=cloid)
        else:
            mid = (self.hl.all_mids() or {}).get(coin)
            if not mid or mid <= 0:
                return _err(f"no perp mid for {coin}")
            r = self.hl.market_order_usd(coin, side == "buy", qty * float(mid),
                                         cloid=cloid)
        return self._envelope_from_result(r, requested_qty=qty)

    # --------------------------------------------------------- order detail

    def get_spot_order_detail(self, inst_id: Optional[str] = None,
                              order_id: Optional[str] = None,
                              **_: Any) -> Dict[str, Any]:
        """HL IOC legs settle synchronously — detail is served from the cache
        written at placement. Unknown ids return an empty envelope so the
        runner's poll loop times the leg out (fail closed) instead of
        inventing a fill."""
        det = self._order_cache.get(str(order_id))
        if det is None:
            return {"code": "1", "msg": f"unknown order id {order_id}", "data": []}
        return _ok([dict(det)])

    def get_order_detail(self, inst_id: Optional[str] = None,
                         order_id: Optional[str] = None,
                         **_: Any) -> Dict[str, Any]:
        return self.get_spot_order_detail(inst_id, order_id=order_id)

    # ------------------------------------------------------------ ops extras
    # set_leverage/get_leverage are consumed by carry_runner._pin_hl_leverage
    # (first non-DRY startup probes: pin leverage_cap + read-back, review
    # 2026-06-09 #5); the rest are operator/top-up helpers. All order-shaped
    # paths route through HLAdapter's own guards.

    def set_leverage(self, leverage: int, *, is_cross: bool = True) -> Dict[str, Any]:
        return self.hl.set_leverage(self.coin, int(leverage), is_cross=is_cross)

    def get_leverage(self) -> Optional[Dict[str, Any]]:
        """Venue-side leverage read-back for the carry coin ({type, value});
        None on read failure — the runner's pin verification fails closed."""
        return self.hl.get_leverage(self.coin)

    def usd_class_transfer(self, amount_usd: float, to_perp: bool) -> Dict[str, Any]:
        return self.hl.usd_class_transfer(amount_usd, to_perp)

    def spot_balance(self, token: Optional[str] = None) -> Optional[Dict[str, Any]]:
        bals = self.hl.spot_balances()
        if bals is None:
            return None
        if token is None:
            rec = self.hl.resolve_spot_pair(self.spot_pair)
            token = rec["base"] if rec else self.spot_pair.split("/")[0]
        return bals.get(token, {"total": 0.0, "hold": 0.0, "free": 0.0})
