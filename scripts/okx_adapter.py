#!/usr/bin/env python3
"""OKX adapter — wraps OkxAPI to fit the ExchangeAdapter Protocol.

Two responsibilities the raw client doesn't carry:

1. **Symbol normalization.** Plan E's runner uses `BTC-USDT` (BloFin
   convention). OKX perp futures use `BTC-USDT-SWAP`. The adapter
   normalizes inbound `inst_id` arguments and maps response `instId`
   fields back to the input convention so callers see a consistent
   namespace regardless of venue.

2. **Response shape harmonization** for fields that diverge between
   OKX and BloFin (notably balance accounting and order lookup),
   enough that the runner can swap one for the other after paper-PASS.

Live trading is gated on Plan E paper-PASS — the adapter is registered
in the factory but the runner default stays `blofin` until then.
"""

from typing import Any, Dict, List, Optional

from exchange_adapter import ExchangeAdapter
from okx_api import OkxAPI


def to_okx_symbol(inst_id: str) -> str:
    """`BTC-USDT` -> `BTC-USDT-SWAP`. Already-suffixed symbols pass through."""
    if not inst_id:
        return inst_id
    if inst_id.endswith("-SWAP") or inst_id.endswith("-PERP"):
        return inst_id
    return f"{inst_id}-SWAP"


def from_okx_symbol(inst_id: str) -> str:
    """`BTC-USDT-SWAP` -> `BTC-USDT`. Spot-style passes through."""
    if not inst_id:
        return inst_id
    if inst_id.endswith("-SWAP"):
        return inst_id[: -len("-SWAP")]
    if inst_id.endswith("-PERP"):
        return inst_id[: -len("-PERP")]
    return inst_id


def _renormalize_data(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Map any `instId` field on each item back to the BloFin-style symbol."""
    out = []
    for it in items or []:
        if isinstance(it, dict) and "instId" in it:
            it = {**it, "instId": from_okx_symbol(it.get("instId", ""))}
        out.append(it)
    return out


class OkxAdapter(ExchangeAdapter):
    """OKX adapter conforming to ExchangeAdapter."""

    def __init__(self, config: Dict):
        super().__init__(config)
        self.api = OkxAPI(
            api_key=config.get("api_key", ""),
            api_secret=config.get("api_secret", ""),
            passphrase=config.get("passphrase", ""),
            demo=config.get("demo_mode", False),
            base_url=config.get("base_url"),
        )

    # ----- market data -----

    def get_ticker(self, inst_id: str = "BTC-USDT") -> Dict:
        resp = self.api.get_ticker(to_okx_symbol(inst_id))
        if isinstance(resp, dict) and resp.get("data"):
            resp = {**resp, "data": _renormalize_data(resp["data"])}
        return resp

    def get_candles(self, inst_id: str = "BTC-USDT", bar: str = "5m",
                    limit: int = 100, before: Optional[str] = None,
                    after: Optional[str] = None) -> List:
        resp = self.api.get_candles(to_okx_symbol(inst_id), bar, limit,
                                    before=before, after=after)
        # Mirror BlofinAdapter behavior: callers expect `data` array of rows,
        # not the full envelope. OKX rows have the same column order as
        # BloFin: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm].
        if isinstance(resp, dict) and "data" in resp:
            return resp["data"] or []
        if isinstance(resp, list):
            return resp
        raise ValueError(f"unexpected candles response: {type(resp)}")

    # ----- account -----

    def get_balance(self, account_type: str = "futures",
                    currency: Optional[str] = None) -> Dict:
        resp = self.api.get_balance(account_type, currency)
        # Normalize OKX's nested `details` array to a flat BloFin-like list.
        if isinstance(resp, dict) and resp.get("data"):
            flat = []
            for acc in resp["data"]:
                for d in (acc.get("details") or []):
                    if currency and d.get("ccy") != currency:
                        continue
                    flat.append({
                        "currency": d.get("ccy"),
                        "available": d.get("availBal") or d.get("availEq", "0"),
                        "frozen": d.get("frozenBal", "0"),
                    })
            resp = {**resp, "data": flat}
        return resp

    def get_positions(self, inst_id: Optional[str] = None) -> Dict:
        resp = self.api.get_positions(to_okx_symbol(inst_id) if inst_id else None)
        if isinstance(resp, dict) and resp.get("data"):
            resp = {**resp, "data": _renormalize_data(resp["data"])}
        return resp

    def get_position_mode(self) -> Dict:
        return self.api.get_position_mode()

    # ----- trading -----

    def place_order(self, inst_id: str, side: str, order_type: str,
                    size: str, price: Optional[str] = None,
                    margin_mode: str = "isolated", **kwargs: Any) -> Dict:
        return self.api.place_order(
            inst_id=to_okx_symbol(inst_id), side=side, order_type=order_type,
            size=size, price=price, margin_mode=margin_mode, **kwargs,
        )

    def cancel_order(self, inst_id: str, order_id: str) -> Dict:
        return self.api.cancel_order(to_okx_symbol(inst_id), order_id)

    def get_orders(self, inst_id: Optional[str] = None,
                   state: Optional[str] = None) -> Dict:
        resp = self.api.get_orders(
            to_okx_symbol(inst_id) if inst_id else None, state)
        if isinstance(resp, dict) and resp.get("data"):
            resp = {**resp, "data": _renormalize_data(resp["data"])}
        return resp

    def get_active_orders(self, inst_id: Optional[str] = None) -> Dict:
        resp = self.api.get_active_orders(
            to_okx_symbol(inst_id) if inst_id else None)
        if isinstance(resp, dict) and resp.get("data"):
            resp = {**resp, "data": _renormalize_data(resp["data"])}
        return resp

    def get_order_detail(self, inst_id: str, order_id: Optional[str] = None,
                         client_order_id: Optional[str] = None) -> Dict:
        resp = self.api.get_order_detail(
            to_okx_symbol(inst_id), order_id=order_id,
            client_order_id=client_order_id)
        if isinstance(resp, dict) and resp.get("data"):
            resp = {**resp, "data": _renormalize_data(resp["data"])}
        return resp

    # ----- TP/SL -----

    def place_tpsl_order(self, *, inst_id: str, margin_mode: str,
                         position_side: str, side: str, size: str,
                         **kwargs: Any) -> Dict:
        return self.api.place_tpsl_order(
            inst_id=to_okx_symbol(inst_id), margin_mode=margin_mode,
            position_side=position_side, side=side, size=size, **kwargs)

    def get_active_tpsl_orders(self, inst_id: Optional[str] = None) -> Dict:
        resp = self.api.get_active_tpsl_orders(
            to_okx_symbol(inst_id) if inst_id else None)
        if isinstance(resp, dict) and resp.get("data"):
            resp = {**resp, "data": _renormalize_data(resp["data"])}
        return resp

    def cancel_tpsl_orders(self, orders: List[Dict]) -> Dict:
        # Each item carries instId — translate before forwarding.
        translated = [
            {**o, "instId": to_okx_symbol(o.get("instId", ""))}
            for o in (orders or [])
        ]
        return self.api.cancel_tpsl_orders(translated)

    def get_orders_history(self, inst_id: Optional[str] = None, **kwargs) -> Dict:
        resp = self.api.get_orders_history(
            inst_id=to_okx_symbol(inst_id) if inst_id else None, **kwargs)
        if isinstance(resp, dict) and resp.get("data"):
            resp = {**resp, "data": _renormalize_data(resp["data"])}
        return resp

    def get_fills_history(self, inst_id: Optional[str] = None, **kwargs) -> Dict:
        resp = self.api.get_fills_history(
            inst_id=to_okx_symbol(inst_id) if inst_id else None, **kwargs)
        if isinstance(resp, dict) and resp.get("data"):
            resp = {**resp, "data": _renormalize_data(resp["data"])}
        return resp

    def get_positions_history(self, inst_id: Optional[str] = None, **kwargs) -> Dict:
        resp = self.api.get_positions_history(
            inst_id=to_okx_symbol(inst_id) if inst_id else None, **kwargs)
        if isinstance(resp, dict) and resp.get("data"):
            resp = {**resp, "data": _renormalize_data(resp["data"])}
        return resp

    # =====================  SPOT (carry P1)  =====================
    #
    # The spot leg of the carry trade. Spot symbols on OKX are plain
    # `BTC-USDT` (no -SWAP suffix); pass-through here without
    # `to_okx_symbol()`. We expose a dedicated `*_spot` surface so the
    # carry runner can place spot orders without colliding with the perp
    # path used by Plan E.

    def get_spot_ticker(self, inst_id: str = "BTC-USDT") -> Dict:
        """Spot ticker (no symbol munging — spot symbols are native)."""
        return self.api.get_spot_ticker(inst_id)

    def get_spot_instrument(self, inst_id: str = "BTC-USDT") -> Dict:
        """Single-symbol spot instrument metadata (minSz, tickSz, lotSz)."""
        return self.api.get_spot_instruments(inst_id)

    def get_spot_min_size(self, inst_id: str = "BTC-USDT") -> Optional[float]:
        """Convenience: parse minSz from the spot instrument metadata.

        Returns None on failure (network error, unexpected response shape).
        Used by the carry runner to validate target leg size before any
        order placement attempt.
        """
        resp = self.api.get_spot_instruments(inst_id)
        if not isinstance(resp, dict) or not resp.get("data"):
            return None
        try:
            return float(resp["data"][0].get("minSz", "0") or "0")
        except (TypeError, ValueError, IndexError):
            return None

    def place_spot_order(
        self, inst_id: str, side: str, order_type: str, size: str,
        price: Optional[str] = None,
        td_mode: str = "cash",
        client_order_id: Optional[str] = None,
        target_currency: Optional[str] = None,
    ) -> Dict:
        """Place a spot order. See `OkxAPI.place_spot_order` for arg semantics.

        Note: this is the *only* method on the adapter that does NOT go
        through `to_okx_symbol` — spot symbols are passed as-is.
        """
        return self.api.place_spot_order(
            inst_id=inst_id, side=side, order_type=order_type, size=size,
            price=price, td_mode=td_mode, client_order_id=client_order_id,
            target_currency=target_currency,
        )

    def cancel_spot_order(self, inst_id: str,
                          order_id: Optional[str] = None,
                          client_order_id: Optional[str] = None) -> Dict:
        return self.api.cancel_spot_order(
            inst_id, order_id=order_id, client_order_id=client_order_id,
        )

    def get_spot_order_detail(self, inst_id: str,
                              order_id: Optional[str] = None,
                              client_order_id: Optional[str] = None) -> Dict:
        return self.api.get_spot_order_detail(
            inst_id, order_id=order_id, client_order_id=client_order_id,
        )

    def get_spot_active_orders(self, inst_id: Optional[str] = None) -> Dict:
        return self.api.get_spot_active_orders(inst_id)

    def get_spot_fills(self, inst_id: Optional[str] = None,
                       order_id: Optional[str] = None,
                       begin: Optional[str] = None,
                       end: Optional[str] = None,
                       limit: int = 100) -> Dict:
        return self.api.get_spot_fills(
            inst_id=inst_id, order_id=order_id,
            begin=begin, end=end, limit=limit,
        )

    def get_spot_balance(self, currency: Optional[str] = None) -> Dict:
        """Spot/coin balance, flattened to BloFin-like rows.

        On OKX unified-margin (acctLv >= 2) there is one `/account/balance`
        endpoint that returns all currencies regardless of trading mode —
        spot BTC sits in the same `details` list as USDT. We re-use
        `get_balance` here and just filter to the requested currency.
        """
        return self.get_balance("spot", currency=currency)

    # =====================  Unified-margin awareness  =====================

    def get_account_config(self) -> Dict:
        """Raw account config (acctLv, posMode, autoLoan, ip whitelist)."""
        return self.api.get_account_config()

    def get_account_level(self) -> Optional[int]:
        """Convenience: parse the integer `acctLv` from the account config.

        Returns:
            1 = Simple, 2 = Single-currency margin,
            3 = Multi-currency margin (UNIFIED), 4 = Portfolio margin.
            None if the call failed or response was malformed.

        The carry runner uses this to assert that the account is in mode
        3 or 4 before deploying (so spot BTC can collateralize the perp
        short). It does NOT auto-change the mode — that's a one-time
        human action in the OKX UI/API; surprises from auto-flipping
        account-wide settings are not acceptable in this build.
        """
        resp = self.get_account_config()
        if not isinstance(resp, dict) or not resp.get("data"):
            return None
        try:
            return int(resp["data"][0].get("acctLv", "0"))
        except (TypeError, ValueError, IndexError):
            return None

    def assert_unified_margin(self) -> Dict[str, Any]:
        """Hard check: return a structured result the runner can log/abort on.

        Returns:
            {"ok": bool, "acct_lv": Optional[int], "message": str}
        """
        lv = self.get_account_level()
        if lv is None:
            return {
                "ok": False, "acct_lv": None,
                "message": "could not read account config (auth / network?)",
            }
        if lv >= 3:
            return {
                "ok": True, "acct_lv": lv,
                "message": (f"unified-margin OK (acctLv={lv}) — spot BTC "
                            "collateralizes the perp short"),
            }
        return {
            "ok": False, "acct_lv": lv,
            "message": (f"account is acctLv={lv}; carry requires acctLv>=3 "
                        "(multi-currency margin / unified). Switch the mode "
                        "in the OKX UI: Account → Account Mode. We do NOT "
                        "auto-flip this — it affects every product on the "
                        "account."),
        }

    def get_margin_snapshot(self, *, perp_inst_id: str = "BTC-USDT",
                            quote_ccy: str = "USDT") -> Dict[str, Any]:
        """Joint margin/equity snapshot for the carry runner's monitor.

        Returns a flat dict with:
            total_eq_usd: total account equity (USD) — joint across spot+perp
            avail_eq_usd: free margin
            unrealized_perp_usd: unrealized PnL on the short perp leg
            margin_ratio: account margin ratio (1/leverage proxy)
            short_perp_qty: signed perp position (negative = short)
            spot_btc_qty: spot BTC balance (raw, not collateralized fraction)
            raw_balance: full balance dict for debugging
            raw_positions: positions list for debugging

        Best-effort: any missing field is left None rather than raising,
        because the runner needs to *report* anomalies, not crash on them.
        """
        out: Dict[str, Any] = {
            "total_eq_usd": None, "avail_eq_usd": None,
            "unrealized_perp_usd": None, "margin_ratio": None,
            "short_perp_qty": None, "spot_btc_qty": None,
            "raw_balance": None, "raw_positions": None,
            "errors": [],
        }

        try:
            bal = self.api.get_balance("unified", None)
            out["raw_balance"] = bal
            data = bal.get("data") if isinstance(bal, dict) else None
            if data:
                acct = data[0]
                # OKX returns totalEq / availEq as strings.
                te = acct.get("totalEq")
                ae = acct.get("availEq") or acct.get("availBal")
                mr = acct.get("mgnRatio")
                try:
                    out["total_eq_usd"] = float(te) if te is not None else None
                except (TypeError, ValueError):
                    pass
                try:
                    out["avail_eq_usd"] = float(ae) if ae is not None else None
                except (TypeError, ValueError):
                    pass
                try:
                    out["margin_ratio"] = float(mr) if mr is not None else None
                except (TypeError, ValueError):
                    pass
                for d in (acct.get("details") or []):
                    if d.get("ccy") == "BTC":
                        try:
                            out["spot_btc_qty"] = float(d.get("eq") or "0")
                        except (TypeError, ValueError):
                            pass
        except Exception as e:
            out["errors"].append({"step": "balance", "error": str(e)})

        try:
            pos = self.api.get_positions(to_okx_symbol(perp_inst_id))
            out["raw_positions"] = pos
            data = pos.get("data") if isinstance(pos, dict) else None
            if data:
                for p in data:
                    if not isinstance(p, dict):
                        continue
                    # OKX `pos` is signed — net qty; for short hedge it's
                    # negative under long/short mode or positive with
                    # posSide=short. Take the abs if signed-negative,
                    # combined with posSide tag, to surface signed qty.
                    try:
                        qty_raw = float(p.get("pos") or "0")
                    except (TypeError, ValueError):
                        qty_raw = 0.0
                    pos_side = p.get("posSide") or "net"
                    if pos_side == "short":
                        signed = -abs(qty_raw)
                    elif pos_side == "long":
                        signed = abs(qty_raw)
                    else:  # net mode: sign is already in `pos`
                        signed = qty_raw
                    out["short_perp_qty"] = signed
                    try:
                        out["unrealized_perp_usd"] = float(p.get("upl") or "0")
                    except (TypeError, ValueError):
                        pass
                    break  # only first matching position
        except Exception as e:
            out["errors"].append({"step": "positions", "error": str(e)})

        return out
