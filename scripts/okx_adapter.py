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
