#!/usr/bin/env python3
"""OKX access-verification probe (M0 of the OKX strategy-sweep plan).

Determines, for a given OKX account, *what can actually be traded* — the
single fact the entire candidate menu of the broad sweep branches on. The
project's whole carry lane died on exactly this question in 2026-05: OKX EU
retail is capped at acctLv=1 (MiCA → spot-only, no perp short). This probe
re-asks it against the *current* credentials, on both the live and demo key
sets, and emits a structured verdict.

What it does
------------
For each available key set (live / demo):
  * Resolves the working host (EU keys 403/50119 on www.okx.com; global keys
    may 50119 on my.okx.com — so we try a candidate list and record which host
    authenticated).
  * Reads account config (acctLv, posMode), unified-margin status, a margin
    snapshot, per-leg live fees, and the effective leverage cap. (read-only)
  * DEMO ONLY: places one tiny non-marketable perp limit order and one tiny
    non-marketable spot limit order, then cancels them — the only way to
    *confirm* (not infer) that the derivatives/spot surface is tradable.
    The live key set is left strictly read-only; its acctLv is the governing
    fact for live deployability.
  * Best-effort options-listing check (OKX has no options client in this repo).

Outputs `state/okx_access/probe_<keyset>.json` per key set and prints a
decision-tree verdict mapping the result onto which strategy families are
(a) live-deployable, (b) demo-paper-only, or (c) research-only this round.

Credentials (env)
-----------------
  live: OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE
  demo: OKX_DEMO_API_KEY / OKX_DEMO_API_SECRET / OKX_DEMO_API_PASSPHRASE
A key set with missing creds is skipped with a clear message — this is the
one human dependency in the milestone.

Usage
-----
  python -m scripts.okx_access_probe                 # both key sets
  python -m scripts.okx_access_probe --keyset demo   # demo only
  python -m scripts.okx_access_probe --no-order      # skip the demo test orders
  python -m scripts.okx_access_probe --base-url https://my.okx.com
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT))

from okx_adapter import OkxAdapter, to_okx_symbol  # noqa: E402
from carry_runner import pull_live_fees, verify_leverage_cap  # noqa: E402


# Hosts to try, in order. EU-region keys live under my.okx.com / eea.okx.com;
# global keys under www.okx.com. We auto-resolve which one authenticates.
DEFAULT_HOSTS = [
    "https://my.okx.com",
    "https://www.okx.com",
    "https://eea.okx.com",
]

# acctLv mapping (OKX /account/config).
ACCT_LV_NAMES = {
    1: "Simple (spot only)",
    2: "Single-currency margin",
    3: "Multi-currency margin (UNIFIED)",
    4: "Portfolio margin",
}

# OKX error-code / message signals that an order was rejected because the
# *account is not permitted to trade derivatives* (the MiCA / acctLv block),
# as opposed to an economic rejection (balance/size/price) which still proves
# the trading surface is reachable.
PERMISSION_BLOCK_PATTERNS = (
    "not support", "does not support", "account mode", "account level",
    "operation is not supported", "not allowed", "no permission",
    "not authorized", "local laws", "regulation", "restricted",
)
# Signals that the engine *accepted* the request and rejected it on economics
# → the surface IS permitted.
ECONOMIC_OK_PATTERNS = (
    "insufficient", "balance", "minimum", "min size", "lot size", "tick",
    "exceeds", "too small", "too large", "price", "available",
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _creds_for(keyset: str) -> Optional[Dict[str, str]]:
    """Read the three-part credential triple for a key set from env.

    Returns None if any of the three is missing.
    """
    prefix = "OKX_DEMO_API_" if keyset == "demo" else "OKX_API_"
    key = os.environ.get(f"{prefix}KEY", "")
    secret = os.environ.get(f"{prefix}SECRET", "")
    passphrase = os.environ.get(f"{prefix}PASSPHRASE", "")
    if not (key and secret and passphrase):
        return None
    return {"api_key": key, "api_secret": secret, "passphrase": passphrase}


def _build_adapter(creds: Dict[str, str], *, demo: bool, base_url: str) -> OkxAdapter:
    return OkxAdapter({
        "api_key": creds["api_key"],
        "api_secret": creds["api_secret"],
        "passphrase": creds["passphrase"],
        "demo_mode": demo,
        "base_url": base_url,
    })


def _config_authenticated(resp: Any) -> bool:
    """True iff an /account/config response looks authenticated (code '0')."""
    return isinstance(resp, dict) and str(resp.get("code")) == "0" and bool(resp.get("data"))


def resolve_working_host(
    creds: Dict[str, str], *, demo: bool, hosts: List[str],
) -> Tuple[Optional[str], Optional[OkxAdapter], Dict[str, Any]]:
    """Try each host until /account/config authenticates. Records each attempt.

    Returns (working_host, adapter, attempts_log). On total failure the host
    and adapter are None and the log explains why (e.g. every host 50119).
    """
    attempts: List[Dict[str, Any]] = []
    for host in hosts:
        adapter = _build_adapter(creds, demo=demo, base_url=host)
        resp = adapter.get_account_config()
        code = str(resp.get("code")) if isinstance(resp, dict) else "?"
        msg = resp.get("msg") if isinstance(resp, dict) else str(resp)
        attempts.append({"host": host, "code": code, "msg": msg})
        if _config_authenticated(resp):
            return host, adapter, {"attempts": attempts, "config": resp}
    return None, None, {"attempts": attempts, "config": None}


def _get_instrument_min(adapter: OkxAdapter, inst_type: str, inst_id: str) -> Dict[str, Any]:
    """Public instrument metadata (minSz, lotSz, tickSz, ctVal, lever)."""
    api = adapter.api
    resp = api._request(
        "GET", "/api/v5/public/instruments",
        params={"instType": inst_type, "instId": inst_id}, auth=False,
    )
    out: Dict[str, Any] = {"raw_code": None, "minSz": None, "tickSz": None,
                           "lotSz": None, "ctVal": None, "lever": None}
    if isinstance(resp, dict):
        out["raw_code"] = str(resp.get("code"))
        data = resp.get("data") or []
        if data:
            row = data[0]
            for k in ("minSz", "tickSz", "lotSz", "ctVal", "lever"):
                out[k] = row.get(k)
    return out


def _classify_order_response(resp: Any, *, acct_lv: Optional[int]) -> Dict[str, Any]:
    """Classify a place-order response as permitted / blocked / unknown.

    OKX returns {code, msg, data:[{sCode, sMsg, ordId, ...}]}. The per-order
    detail in data[0] is the actionable result.
    """
    top_code = str(resp.get("code")) if isinstance(resp, dict) else "?"
    top_msg = resp.get("msg", "") if isinstance(resp, dict) else str(resp)
    s_code, s_msg, ord_id = None, None, None
    if isinstance(resp, dict) and resp.get("data"):
        row = resp["data"][0] if isinstance(resp["data"], list) and resp["data"] else {}
        if isinstance(row, dict):
            s_code = str(row.get("sCode")) if row.get("sCode") is not None else None
            s_msg = row.get("sMsg")
            ord_id = row.get("ordId") or None

    accepted = top_code == "0" and (s_code in (None, "0", ""))
    blob = " ".join(str(x).lower() for x in (top_msg, s_msg) if x)

    if accepted:
        verdict = "permitted"
    elif any(p in blob for p in PERMISSION_BLOCK_PATTERNS):
        verdict = "blocked"
    elif any(p in blob for p in ECONOMIC_OK_PATTERNS):
        verdict = "permitted"          # engine reached, rejected on economics
    else:
        # Fall back to the governing fact: Simple mode (acctLv 1) = no derivs.
        if acct_lv is not None and acct_lv <= 1:
            verdict = "blocked"
        else:
            verdict = "unknown"

    return {
        "verdict": verdict, "accepted": accepted, "ord_id": ord_id,
        "top_code": top_code, "top_msg": top_msg,
        "s_code": s_code, "s_msg": s_msg, "raw": resp,
    }


def _test_order(
    adapter: OkxAdapter, *, kind: str, spot_symbol: str, perp_symbol: str,
    acct_lv: Optional[int], pos_mode: Optional[str],
) -> Dict[str, Any]:
    """Place ONE tiny non-marketable order (perp or spot) and cancel it.

    DEMO-ONLY caller guarantee. Returns a classification record.
    """
    api = adapter.api
    if kind == "perp":
        inst_id = to_okx_symbol(perp_symbol)          # BTC-USDT-SWAP
        meta = _get_instrument_min(adapter, "SWAP", inst_id)
        size = meta.get("minSz") or meta.get("lotSz") or "0.1"
        tk = adapter.get_ticker(perp_symbol)
    else:
        inst_id = spot_symbol                          # BTC-USDT
        meta = _get_instrument_min(adapter, "SPOT", inst_id)
        size = meta.get("minSz") or "0.0001"
        tk = adapter.get_spot_ticker(spot_symbol)

    # Last price for a deliberately non-marketable resting price.
    last = None
    try:
        rows = tk.get("data") if isinstance(tk, dict) else None
        if rows:
            last = float(rows[0].get("last") or rows[0].get("idxPx") or 0) or None
    except (TypeError, ValueError, IndexError):
        last = None

    record: Dict[str, Any] = {"kind": kind, "inst_id": inst_id, "size": size,
                              "instrument_meta": meta, "last_price": last}

    if kind == "perp":
        # Sell limit well ABOVE market → rests, never fills.
        price = f"{last * 1.5:.1f}" if last else "200000"
        pos_side = "short" if (pos_mode == "long_short_mode") else None
        resp = api.place_order(
            inst_id=inst_id, side="sell", order_type="limit", size=str(size),
            price=price, margin_mode="isolated", position_side=pos_side,
        )
    else:
        # Buy limit well BELOW market → rests, never fills.
        price = f"{last * 0.5:.1f}" if last else "1000"
        resp = api.place_spot_order(
            inst_id=inst_id, side="buy", order_type="limit", size=str(size),
            price=price, td_mode="cash",
        )

    cls = _classify_order_response(resp, acct_lv=acct_lv)
    record.update(cls)

    # If it rested, cancel it so we leave no open orders behind.
    if cls.get("accepted") and cls.get("ord_id"):
        if kind == "perp":
            cancel = api.cancel_order(inst_id, cls["ord_id"])
        else:
            cancel = api.cancel_spot_order(inst_id, order_id=cls["ord_id"])
        record["cancel"] = {"code": str(cancel.get("code")) if isinstance(cancel, dict) else "?",
                            "msg": cancel.get("msg") if isinstance(cancel, dict) else str(cancel)}
    return record


def _options_listing(adapter: OkxAdapter, inst_family: str = "BTC-USD") -> Dict[str, Any]:
    """Best-effort: is an OKX options surface listed for this family?

    Public listing tells us OKX *offers* options; account authorization for
    options is gated by the same derivatives/acctLv rule as perps, so we infer
    account permission from the perp test rather than over-trusting this.
    """
    api = adapter.api
    resp = api._request(
        "GET", "/api/v5/public/instruments",
        params={"instType": "OPTION", "instFamily": inst_family}, auth=False,
    )
    listed = 0
    code = "?"
    if isinstance(resp, dict):
        code = str(resp.get("code"))
        listed = len(resp.get("data") or [])
    # Best-effort account-side probe (endpoint may not exist for all tiers).
    acct_resp = api._request(
        "GET", "/api/v5/account/instruments",
        params={"instType": "OPTION", "instFamily": inst_family}, auth=True,
    )
    acct_listed = len(acct_resp.get("data") or []) if isinstance(acct_resp, dict) else 0
    acct_code = str(acct_resp.get("code")) if isinstance(acct_resp, dict) else "?"
    return {
        "public_code": code, "public_listed": listed,
        "account_code": acct_code, "account_listed": acct_listed,
        "account_msg": acct_resp.get("msg") if isinstance(acct_resp, dict) else None,
    }


def probe_keyset(
    keyset: str, *, base_url_override: Optional[str], symbol: str,
    place_orders: bool,
) -> Dict[str, Any]:
    """Run the full probe for one key set. Returns a structured result."""
    result: Dict[str, Any] = {
        "keyset": keyset, "ts": _iso_now(), "available": False,
        "working_host": None, "host_attempts": [],
        "acct_lv": None, "acct_lv_name": None, "pos_mode": None,
        "unified_margin": None, "margin_snapshot": None,
        "fees": None, "leverage": None,
        "perp_order_test": None, "spot_order_test": None,
        "options": None, "notes": [],
    }

    creds = _creds_for(keyset)
    if creds is None:
        result["notes"].append(
            f"credentials missing — set {'OKX_DEMO_API_*' if keyset == 'demo' else 'OKX_API_*'} "
            "(KEY/SECRET/PASSPHRASE) in the environment to run this key set."
        )
        return result

    demo = keyset == "demo"
    hosts = ([base_url_override] if base_url_override else []) + [
        h for h in DEFAULT_HOSTS if h != base_url_override
    ]
    host, adapter, hostlog = resolve_working_host(creds, demo=demo, hosts=hosts)
    result["host_attempts"] = hostlog["attempts"]
    if adapter is None:
        result["notes"].append(
            "no host authenticated — check the key/secret/passphrase, IP "
            "allowlist, and that the key matches the region (EU vs global). "
            "Codes seen: " + ", ".join(f"{a['host']}={a['code']}" for a in hostlog["attempts"])
        )
        return result

    result["available"] = True
    result["working_host"] = host

    cfg = hostlog["config"]
    acct_lv = adapter.get_account_level()
    result["acct_lv"] = acct_lv
    result["acct_lv_name"] = ACCT_LV_NAMES.get(acct_lv or -1, "unknown")
    try:
        result["pos_mode"] = cfg["data"][0].get("posMode")
    except (KeyError, IndexError, TypeError):
        result["pos_mode"] = None

    result["unified_margin"] = adapter.assert_unified_margin()
    result["margin_snapshot"] = {
        k: v for k, v in adapter.get_margin_snapshot(perp_inst_id=symbol).items()
        if k not in ("raw_balance", "raw_positions")     # keep the file small
    }
    result["fees"] = pull_live_fees(adapter, spot_inst=symbol, perp_inst=symbol)
    result["leverage"] = verify_leverage_cap(adapter, configured_cap=2.0, perp_inst=symbol)
    result["options"] = _options_listing(adapter)

    if place_orders and demo:
        result["perp_order_test"] = _test_order(
            adapter, kind="perp", spot_symbol=symbol, perp_symbol=symbol,
            acct_lv=acct_lv, pos_mode=result["pos_mode"],
        )
        result["spot_order_test"] = _test_order(
            adapter, kind="spot", spot_symbol=symbol, perp_symbol=symbol,
            acct_lv=acct_lv, pos_mode=result["pos_mode"],
        )
    elif place_orders and not demo:
        result["notes"].append(
            "live key set is kept read-only by design — no test order placed; "
            "live acctLv is the governing fact for live deployability."
        )
    return result


def _perp_permitted(res: Dict[str, Any]) -> Optional[bool]:
    """Did the demo perp order confirm the perp surface? None if untested."""
    test = res.get("perp_order_test")
    if test:
        v = test.get("verdict")
        if v == "permitted":
            return True
        if v == "blocked":
            return False
    # Untested or unknown → infer from acctLv (>=2 = derivatives-enabled mode).
    lv = res.get("acct_lv")
    if lv is None:
        return None
    return lv >= 2


def synthesize(results: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Map the per-keyset probes onto the strategy-family decision tree."""
    live = results.get("live", {})
    demo = results.get("demo", {})

    live_lv = live.get("acct_lv") if live.get("available") else None
    demo_lv = demo.get("acct_lv") if demo.get("available") else None
    live_perp = live_lv is not None and live_lv >= 2
    demo_perp = _perp_permitted(demo) if demo.get("available") else None

    live_carry = live_lv is not None and live_lv >= 3
    demo_carry = demo_lv is not None and demo_lv >= 3 and bool(demo_perp)

    # Deployment surface this round is PAPER on demo; live acctLv tells us
    # whether anything we validate could *ever* go live on this account.
    if demo_perp:
        paper_surface = "PERP_UNLOCKED (demo)"
    elif demo.get("available"):
        paper_surface = "SPOT_ONLY (demo)"
    else:
        paper_surface = "UNKNOWN (no demo creds)"

    if live_perp:
        live_surface = "PERP_UNLOCKED"
    elif live.get("available"):
        live_surface = "SPOT_ONLY (acctLv=%s)" % live_lv
    else:
        live_surface = "UNKNOWN (no live creds)"

    # Family routing.
    families: Dict[str, str] = {}

    def route(name: str, needs_perp: bool, needs_unified: bool = False) -> str:
        if needs_unified:
            if demo_carry:
                return "demo-paper-OK" + ("" if live_carry else " (NOT live-deployable on this account)")
            return "BLOCKED (needs acctLv>=3 + perp)"
        if not needs_perp:
            return "demo-paper-OK" + ("" if live.get("available") else "")
        if demo_perp:
            return "demo-paper-OK" + ("" if live_perp else " (NOT live-deployable on this account)")
        if demo_perp is None:
            return "UNKNOWN (supply demo creds / run --no-order=false)"
        return "BLOCKED on demo (spot-only)"

    families["B1 cash-and-carry"] = route("carry", needs_perp=True, needs_unified=True)
    families["B2 perp funding-timing"] = route("b2", needs_perp=True)
    families["B3 x-sectional funding carry"] = route("b3", needs_perp=True)
    families["B4 x-sectional momentum (perp)"] = route("b4", needs_perp=True)
    families["B5 spot directional"] = route("b5", needs_perp=False) + " — but already failed null; skip unchanged"
    families["B6 cross-venue spot basis"] = route("b6", needs_perp=False) + " (needs non-OKX venue adapters)"
    families["B7 variance-risk-premium (options)"] = (
        "options listed publicly; account-authorization gated like perp — "
        + ("likely OK" if demo_perp else "likely BLOCKED")
        + "; heaviest build (no options client yet)"
    )
    families["B8 staking/earn + overlay"] = route("b8", needs_perp=False) + " (Earn is separate from trade API)"
    families["B9 stat-arb/pairs (spot)"] = route("b9", needs_perp=False)
    families["B10 maker-rebate MM (spot)"] = route("b10", needs_perp=False) + " — only honest via live paper-fill measurement"

    return {
        "ts": _iso_now(),
        "live_surface": live_surface,
        "paper_surface_this_round": paper_surface,
        "live_acct_lv": live_lv,
        "demo_acct_lv": demo_lv,
        "live_perp_enabled": live_perp,
        "demo_perp_confirmed": demo_perp,
        "carry_revivable_live": live_carry,
        "carry_paperable_demo": demo_carry,
        "family_routing": families,
        "headline": _headline(live_surface, paper_surface, demo_perp, live_carry),
    }


def _headline(live_surface: str, paper_surface: str, demo_perp: Optional[bool],
              live_carry: bool) -> str:
    if demo_perp and live_carry:
        return ("PERP + unified margin available on BOTH demo and live — the "
                "carry lane is genuinely revivable. This is the upside branch.")
    if demo_perp and not live_carry:
        return ("Perp tradable on DEMO but live account is capped (no unified "
                "margin / spot-only) — you can PAPER-validate perp/carry but it "
                "could not go live on this account as-is. Flag before any build.")
    if demo_perp is False:
        return ("Spot-only even on demo — perp/carry/x-sectional-perp are off "
                "the table for OKX execution; only spot-executable families "
                "(basis, pairs, staking-overlay, spot MM) are deployable.")
    return ("Inconclusive — supply both live and demo credentials and re-run; "
            "until then the family routing is inferred from acctLv only.")


def main() -> int:
    ap = argparse.ArgumentParser(description="OKX access-verification probe (M0)")
    ap.add_argument("--keyset", choices=["live", "demo", "both"], default="both")
    ap.add_argument("--symbol", default="BTC-USDT", help="spot/perp base symbol")
    ap.add_argument("--base-url", default=None,
                    help="force a host first (else auto-resolve my/www/eea)")
    ap.add_argument("--no-order", action="store_true",
                    help="skip the demo perp/spot test orders (read-only probe)")
    ap.add_argument("--out-dir", default=str(PROJECT_ROOT / "state" / "okx_access"))
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    keysets = ["live", "demo"] if args.keyset == "both" else [args.keyset]
    results: Dict[str, Dict[str, Any]] = {}
    for ks in keysets:
        res = probe_keyset(
            ks, base_url_override=args.base_url, symbol=args.symbol,
            place_orders=not args.no_order,
        )
        results[ks] = res
        path = out_dir / f"probe_{ks}.json"
        path.write_text(json.dumps(res, indent=2, default=str))
        print(f"[{ks}] wrote {path}")

    verdict = synthesize(results)
    vpath = out_dir / "verdict.json"
    vpath.write_text(json.dumps(verdict, indent=2, default=str))

    # Human-readable summary.
    print("\n" + "=" * 70)
    print("OKX ACCESS PROBE — VERDICT")
    print("=" * 70)
    for ks in keysets:
        r = results[ks]
        if not r.get("available"):
            print(f"  {ks:5s}: UNAVAILABLE — {('; '.join(r.get('notes')) or 'no creds')}")
            continue
        lv = r.get("acct_lv")
        lev = (r.get("leverage") or {}).get("effective_max")
        print(f"  {ks:5s}: host={r['working_host']} acctLv={lv} "
              f"({r.get('acct_lv_name')}) eff_leverage={lev}")
        for tk in ("perp_order_test", "spot_order_test"):
            t = r.get(tk)
            if t:
                print(f"         {tk}: {t.get('verdict')} "
                      f"(code={t.get('top_code')}/{t.get('s_code')} "
                      f"{(t.get('s_msg') or t.get('top_msg') or '')[:60]})")
    print("-" * 70)
    print(f"  live surface : {verdict['live_surface']}")
    print(f"  paper surface: {verdict['paper_surface_this_round']}")
    print(f"  HEADLINE: {verdict['headline']}")
    print("-" * 70)
    print("  Family routing:")
    for fam, status in verdict["family_routing"].items():
        print(f"    - {fam}: {status}")
    print("=" * 70)
    print(f"verdict written to {vpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
