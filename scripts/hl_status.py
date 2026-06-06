#!/usr/bin/env python3
"""Read-only Hyperliquid account monitor — balance, positions, PnL.

Hits the PUBLIC Hyperliquid info API (no wallet / no keys), so it is safe to run
anywhere (laptop, LXC, cron). Mirrors hl_adapter.account_value()'s equity model:
unified-account equity = perp marginSummary.accountValue + free spot USDC
(spot total - hold), so the number matches what hl_xs_runner sizes the book on.

Usage:
    python -m scripts.hl_status                      # pretty, default address
    python -m scripts.hl_status --address 0x..       # another account
    python -m scripts.hl_status --json               # machine-readable (dashboard/cron)
    python -m scripts.hl_status --loop 30            # refresh every 30s
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import urllib.request

INFO_URL = "https://api.hyperliquid.xyz/info"
DEFAULT_ADDRESS = "0x70Cbb988e66E93b00c0A3CC170fF71C149E64c89"


def _ssl_context():
    """Verified TLS context. Prefer certifi's CA bundle (works on macOS Python
    builds that ship without system roots); fall back to the platform default."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_CTX = _ssl_context()


def _post(body: dict) -> dict:
    req = urllib.request.Request(
        INFO_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15, context=_CTX) as r:
        return json.load(r)


def fetch(address: str) -> dict:
    """Return a normalized snapshot of the account."""
    perp = _post({"type": "clearinghouseState", "user": address})
    spot = _post({"type": "spotClearinghouseState", "user": address})

    ms = perp.get("marginSummary", {}) or {}
    perp_av = float(ms.get("accountValue") or 0.0)
    maint = float(perp.get("crossMaintenanceMarginUsed") or 0.0)
    withdrawable = float(perp.get("withdrawable") or 0.0)

    usdc_total = usdc_hold = 0.0
    for b in spot.get("balances", []) or []:
        if b.get("coin") == "USDC":
            usdc_total = float(b.get("total") or 0.0)
            usdc_hold = float(b.get("hold") or 0.0)
            break
    spot_free = max(0.0, usdc_total - usdc_hold)
    equity = perp_av + spot_free  # matches hl_adapter.account_value()

    positions = []
    gross = net_upnl = 0.0
    for ap in perp.get("assetPositions", []) or []:
        p = ap.get("position", {}) or {}
        szi = float(p.get("szi") or 0.0)
        if szi == 0.0:
            continue
        notional = float(p.get("positionValue") or 0.0)
        upnl = float(p.get("unrealizedPnl") or 0.0)
        gross += notional
        net_upnl += upnl
        positions.append({
            "coin": p.get("coin"),
            "side": "LONG" if szi > 0 else "SHORT",
            "size": szi,
            "notional": notional,
            "entry": float(p.get("entryPx") or 0.0),
            "upnl": upnl,
            "lev": (p.get("leverage") or {}).get("value"),
            "liq": p.get("liquidationPx"),
        })
    positions.sort(key=lambda x: (x["side"], x["coin"]))

    return {
        "address": address,
        "equity": equity,
        "perp_account_value": perp_av,
        "spot_usdc_total": usdc_total,
        "spot_usdc_hold": usdc_hold,
        "spot_usdc_free": spot_free,
        "withdrawable": withdrawable,
        "maintenance_margin": maint,
        "gross_notional": gross,
        "net_upnl": net_upnl,
        "gross_leverage_on_equity": (gross / equity) if equity else 0.0,
        "maintenance_buffer": perp_av - maint,
        "num_positions": len(positions),
        "positions": positions,
    }


def render(s: dict) -> str:
    L = []
    L.append(f"Hyperliquid  {s['address']}")
    L.append("=" * 64)
    L.append(f"  Equity (perp+spot) : ${s['equity']:>12,.2f}")
    L.append(f"    perp accountValue: ${s['perp_account_value']:>12,.2f}")
    L.append(f"    spot USDC free   : ${s['spot_usdc_free']:>12,.2f}"
             f"   (total ${s['spot_usdc_total']:,.2f}, hold ${s['spot_usdc_hold']:,.2f})")
    L.append(f"  Gross notional     : ${s['gross_notional']:>12,.2f}"
             f"   ({s['gross_leverage_on_equity']:.2f}x of equity)")
    sign = "+" if s["net_upnl"] >= 0 else "-"
    L.append(f"  Net unrealized PnL : {sign}${abs(s['net_upnl']):>11,.2f}")
    L.append(f"  Maint. margin used : ${s['maintenance_margin']:>12,.2f}"
             f"   (buffer ${s['maintenance_buffer']:,.2f})")
    if s["positions"]:
        L.append("-" * 64)
        L.append(f"  {'COIN':<6}{'SIDE':<6}{'NOTIONAL':>12}{'uPnL':>11}{'LIQ':>14}")
        for p in s["positions"]:
            liq = "—" if p["liq"] in (None, "null") else f"{float(p['liq']):,.4g}"
            L.append(f"  {p['coin']:<6}{p['side']:<6}"
                     f"${p['notional']:>10,.2f}{p['upnl']:>+11.2f}{liq:>14}")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only Hyperliquid account monitor")
    ap.add_argument("--address", default=DEFAULT_ADDRESS)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    ap.add_argument("--loop", type=float, metavar="SECONDS",
                    help="refresh continuously every SECONDS")
    a = ap.parse_args(argv)

    while True:
        try:
            snap = fetch(a.address)
            print(json.dumps(snap) if a.json else render(snap), flush=True)
        except Exception as e:  # noqa: BLE001 — a monitor must not crash on a hiccup
            print(f"hl_status: fetch failed: {e}", file=sys.stderr, flush=True)
        if not a.loop:
            return 0
        time.sleep(a.loop)


if __name__ == "__main__":
    raise SystemExit(main())
