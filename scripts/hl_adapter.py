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
import functools
import sys
import time
from collections import deque
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


# (connect, read) seconds passed straight to requests via the SDK. A bounded
# read timeout converts an INDEFINITE hang — the 2026-06-20 outage mechanism: a
# blocking POST during a gateway outage freezes the loop → health.json goes
# stale → the watchdog emergency-flattens on recovery — into a catchable
# exception that the transient-skip read paths (account_value→None, all_mids→{})
# already handle as "no data, skip this cycle". None reverts to the SDK default
# (no timeout); do NOT run production with None.
_DEFAULT_TIMEOUT = (5.0, 10.0)


def _mask(addr: Optional[str]) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else str(addr)


@dataclass
class HLOrderResult:
    ok: bool
    raw: dict
    filled_sz: float = 0.0
    avg_px: float = 0.0
    error: Optional[str] = None


class _LatencyRing:
    """Bounded in-memory ring of recent network-call latencies, split read
    (via /info) vs write (via /exchange). Pure observability substrate: every
    SDK read/write funnels through a single API.post chokepoint, so wrapping
    that one method captures the whole latency picture. Single-process, GIL-
    bound appends; read with samples()/summary(). Cheap enough to leave on in
    production health, and the baseline contract for the latency probe."""

    __slots__ = ("_buf",)

    def __init__(self, maxlen: int = 4096):
        self._buf: "deque[dict]" = deque(maxlen=maxlen)

    def record(self, side: str, url_path: str, elapsed_ms: float, ok: bool, t_wall: float) -> None:
        self._buf.append({"t": t_wall, "side": side, "path": url_path,
                          "ms": round(elapsed_ms, 3), "ok": ok})

    def samples(self) -> List[dict]:
        return list(self._buf)

    def summary(self) -> Dict[str, dict]:
        samples = list(self._buf)
        out: Dict[str, dict] = {}
        for side in ("read", "write"):
            xs = [s["ms"] for s in samples if s["side"] == side]
            if not xs:
                out[side] = {"n": 0}
                continue
            a = np.asarray(xs, dtype=float)
            out[side] = {
                "n": len(xs),
                "p50": round(float(np.percentile(a, 50)), 3),
                "p95": round(float(np.percentile(a, 95)), 3),
                "p99": round(float(np.percentile(a, 99)), 3),
                "max": round(float(a.max()), 3),
                "errs": sum(1 for s in samples if s["side"] == side and not s["ok"]),
            }
        return out


class HLAdapter:
    """Thin, guarded wrapper over the Hyperliquid SDK (Info + Exchange)."""

    def __init__(self, *, network: str = "testnet", private_key: Optional[str] = None,
                 account_address: Optional[str] = None, allow_live: bool = False,
                 timeout: Optional[tuple] = _DEFAULT_TIMEOUT):
        self.network = network
        self.allow_live = allow_live
        self.timeout = timeout
        self.mode = resolve_hl_mode(network, allow_live)
        self.base_url = (constants.TESTNET_API_URL if network == "testnet"
                         else constants.MAINNET_API_URL)
        self.info = Info(self.base_url, skip_ws=True, timeout=timeout)
        self.wallet = eth_account.Account.from_key(private_key) if private_key else None
        self.address = account_address or (self.wallet.address if self.wallet else None)
        self.exchange = (Exchange(self.wallet, self.base_url, account_address=self.address,
                                  timeout=timeout)
                         if self.wallet else None)
        self._meta_cache: Optional[dict] = None
        self._spot_meta_cache: Optional[dict] = None
        self._last_av_time_ms: Optional[int] = None   # chain `time` from the last account_value read (staleness gate)
        self._latency = _LatencyRing()
        self._instrument_latency()

    # --------------------------------------------------------- instrumentation
    def _instrument_latency(self) -> None:
        """Wrap the single API.post chokepoint on the Info (reads, /info) and
        Exchange (writes, /exchange) instances to time every call into the
        latency ring. Pure instrumentation: the wrapper returns the original
        result and re-raises the original exception unchanged — only timing is
        added. Idempotent guard via the `_lat_wrapped` marker so a re-init can't
        double-wrap and inflate counts."""
        def _wrap(obj, side: str) -> None:
            if obj is None or getattr(obj, "_lat_wrapped", False):
                return
            original = obj.post
            ring = self._latency

            @functools.wraps(original)
            def post(url_path, payload=None):
                t_wall = time.time()
                t0 = time.perf_counter()
                ok = True
                try:
                    return original(url_path, payload)
                except Exception:
                    ok = False
                    raise
                finally:
                    ring.record(side, url_path, (time.perf_counter() - t0) * 1000.0, ok, t_wall)

            obj.post = post
            obj._lat_wrapped = True

        _wrap(self.info, "read")
        _wrap(self.exchange, "write")

    def latency_samples(self) -> List[dict]:
        """Recent per-call network latencies (read via /info, write via
        /exchange) as raw event dicts — for health export / JSONL dumps."""
        return self._latency.samples()

    def latency_summary(self) -> Dict[str, dict]:
        """p50/p95/p99/max (ms) over the latency ring, split read vs write.
        Read-side ≈ market-data freshness cost; write-side ≈ HL order-inclusion
        floor (~200ms+). The before/after contract for the WS migration."""
        return self._latency.summary()

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

    def funding_history(self, coin: str, start_ms: int, end_ms: Optional[int] = None,
                        *, max_pages: int = 50) -> List[dict]:
        """HOURLY funding rows for `coin` (perp name, e.g. "BTC") within
        [start_ms, end_ms] as [{time_ms, rate}], sorted ASCENDING and deduped on
        time. The server caps each response (~500 rows), so this pages forward on
        the last seen timestamp until the window is exhausted (90d hourly ≈ 2160
        rows ≈ 5 pages). Raises on a hard read failure — callers must NOT treat
        that as 'no funding'. `max_pages` only bounds a misbehaving server (no
        normal window hits it)."""
        out: Dict[int, float] = {}
        cursor = int(start_ms)
        for _ in range(max_pages):
            rows = self.info.funding_history(coin, cursor, end_ms)
            if not isinstance(rows, list) or not rows:
                break
            page_max = cursor - 1
            for r in rows:
                try:
                    t = int(r["time"])
                    rate = float(r["fundingRate"])
                except (KeyError, TypeError, ValueError):
                    continue
                page_max = max(page_max, t)
                if t < start_ms or (end_ms is not None and t > end_ms):
                    continue
                out[t] = rate
            if page_max < cursor:          # no forward progress → final/garbage page
                break
            cursor = page_max + 1
            if end_ms is not None and cursor > end_ms:
                break
        return [{"time_ms": t, "rate": out[t]} for t in sorted(out)]

    # ------------------------------------------------------------- spot reads
    def spot_meta(self) -> dict:
        """Cached spot metadata ({universe, tokens}) — same caching discipline
        as meta(); refreshed only on a new adapter instance."""
        if self._spot_meta_cache is None:
            self._spot_meta_cache = self.info.spot_meta()
        return self._spot_meta_cache

    def spot_pairs(self) -> Dict[str, dict]:
        """Every listed spot pair keyed by the composed "BASE/QUOTE" name (e.g.
        the Unit-bridged "UBTC/USDC") → {coin, index, base, quote, sz_decimals}.
        `coin` is the SDK's spot-universe name ("PURR/USDC" for canonical pairs,
        "@<index>" otherwise) — the name the SDK expects for orders and ctx
        lookups; `sz_decimals` is the BASE token's szDecimals (spot order sizing
        rounds to it, NOT the perp coin's). This is also the map of which coins
        have a spot pair at all. {} on a read failure — never a real empty
        listing."""
        try:
            sm = self.spot_meta()
            tok = {t["index"]: t for t in sm.get("tokens", []) or []}
            out: Dict[str, dict] = {}
            for u in sm.get("universe", []) or []:
                try:
                    base, quote = u["tokens"]
                    name = f'{tok[base]["name"]}/{tok[quote]["name"]}'
                    out[name] = {"coin": u["name"], "index": int(u["index"]),
                                 "base": tok[base]["name"], "quote": tok[quote]["name"],
                                 "sz_decimals": int(tok[base]["szDecimals"])}
                except (KeyError, TypeError, ValueError):
                    continue
            return out
        except Exception:
            return {}

    def resolve_spot_pair(self, pair: str) -> Optional[dict]:
        """Resolve a composed "UBTC/USDC" name (or a raw universe name like
        "@142") to its spot_pairs() record. None when unknown/unreadable —
        callers must treat None as 'cannot trade this pair', never default."""
        pairs = self.spot_pairs()
        rec = pairs.get(pair)
        if rec is not None:
            return rec
        for rec in pairs.values():
            if rec["coin"] == pair:
                return rec
        return None

    def spot_mids(self) -> Dict[str, float]:
        """Current spot mids keyed by BOTH the composed "BASE/QUOTE" name and
        the SDK universe name ("@N"/"PURR/USDC"). Prefers midPx, falls back to
        markPx (thin books often have no mid). Like all_mids(): an EMPTY dict
        means the feed is unavailable/degraded — callers must treat {} as
        "no data", never as a genuine empty market.

        Ctx association is by the ctx's own `coin` field (fallback: the
        universe entry's `index` into the ctx list) — NEVER positional. The
        spot universe list has gaps from delisted pairs, so a positional zip
        pairs an entry with another pair's ctx (mainnet 2026-07-20: position
        140 = @142 UBTC/USDC, but ctxs[140] belongs to @140 at $0.000068 —
        the carry lane read that as its spot mid and alarmed basis-blowout
        every cycle; testnet's gapless universe masked it)."""
        out: Dict[str, float] = {}
        try:
            sm, ctxs = self.info.spot_meta_and_asset_ctxs()
            ctxs = ctxs or []
            tok = {t["index"]: t for t in sm.get("tokens", []) or []}
            ctx_by_coin = {c.get("coin"): c for c in ctxs
                           if isinstance(c, dict)}
            for u in sm.get("universe", []) or []:
                ctx = ctx_by_coin.get(u.get("name"))
                if ctx is None:
                    try:
                        idx = int(u["index"])
                        ctx = ctxs[idx] if 0 <= idx < len(ctxs) else None
                    except (KeyError, TypeError, ValueError):
                        ctx = None
                if not isinstance(ctx, dict):
                    continue
                try:
                    px = float(ctx.get("midPx") or ctx.get("markPx") or 0.0)
                except (TypeError, ValueError):
                    continue
                if px <= 0:
                    continue
                out[u["name"]] = px
                try:
                    base, quote = u["tokens"]
                    out[f'{tok[base]["name"]}/{tok[quote]["name"]}'] = px
                except (KeyError, TypeError, ValueError):
                    continue
        except Exception:
            pass
        return out

    def spot_balances(self) -> Optional[Dict[str, dict]]:
        """ALL spot-clearinghouse balances {token: {total, hold, free}} — the
        full-list sibling of _spot_usdc_free (free = total − hold; see there for
        why `hold` is removed). {} for a keyless/empty account, None on a read
        failure — callers treat None as 'unknown', never as flat."""
        if not self.address:
            return {}
        try:
            ss = self.info.spot_user_state(self.address)
            out: Dict[str, dict] = {}
            for b in ss.get("balances", []) or []:
                try:
                    total = float(b.get("total") or 0.0)
                    hold = float(b.get("hold") or 0.0)
                    out[b["coin"]] = {"total": total, "hold": hold,
                                      "free": max(0.0, total - hold)}
                except (KeyError, TypeError, ValueError):
                    continue
            return out
        except Exception:
            return None

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
                t = st.get("time")                    # L1 block time (ms) — for the staleness gate
                if t is not None:
                    try:
                        self._last_av_time_ms = int(t)
                    except (TypeError, ValueError):
                        pass
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

    def last_account_age_s(self) -> Optional[float]:
        """Wall-clock age (s) of the chain `time` carried by the last
        account_value() read, or None if never read. HL's clearinghouseState
        carries the L1 block `time`; during a network upgrade the chain halts and
        this stops advancing, so a large age flags a stale/halted read even when
        the returned numbers look plausible. Clamped at 0 (the server clock can
        lead the local clock by a few hundred ms)."""
        if self._last_av_time_ms is None:
            return None
        return max(0.0, time.time() - self._last_av_time_ms / 1000.0)

    def exchange_status(self) -> Optional[dict]:
        """The venue's operational status: {"specialStatuses": <marker|null>,
        "time": <ms>}. `specialStatuses` is null in normal operation and carries a
        marker during a restricted window (e.g. the post-only window right after a
        network upgrade). Best-effort, weight-2 read: returns None on a read
        failure so the caller treats 'unknown' as 'not restricted'."""
        try:
            return self.info.post("/info", {"type": "exchangeStatus"})
        except Exception:
            return None

    @staticmethod
    def is_upgrade_reject(err: Optional[str]) -> bool:
        """True if an order error is HL's post-only-only window right after a
        network upgrade ("Only post-only orders allowed immediately after a
        network upgrade") — a transient venue-restricted state, NOT a bad order."""
        if not err:
            return False
        e = err.lower()
        return ("post-only" in e or "post only" in e) and "upgrade" in e

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

    def get_leverage(self, coin: str) -> Optional[dict]:
        """The venue-side leverage setting for `coin` ({type, value}) via the
        activeAssetData info endpoint — the read-back for verifying the
        set_leverage pin actually took. None on read failure / no address."""
        if not self.address:
            return None
        try:
            d = self.info.post("/info", {"type": "activeAssetData",
                                         "user": self.address, "coin": coin})
            lev = (d or {}).get("leverage") or {}
            if "value" not in lev:
                return None
            return {"type": lev.get("type"), "value": int(lev["value"])}
        except Exception:
            return None

    def margin_state(self) -> Optional[dict]:
        """Perp margin snapshot for risk observability: {account_value,
        total_margin_used, total_ntl_pos, withdrawable, margin_ratio}.
        margin_ratio = totalMarginUsed / TOTAL equity (perp accountValue +
        free spot USDC) — in a UNIFIED account the perp accountValue is mostly
        just the margin earmark itself (the rest of the collateral sits free
        on the spot side), so a perp-only ratio would read ~1.0 on a perfectly
        healthy book. Falls back to the perp-only (conservative, overstated)
        ratio when the spot read fails. None on a read failure / no address —
        callers treat that as 'unknown', never as safe."""
        if not self.address:
            return None
        try:
            st = self.info.user_state(self.address)
            ms = st.get("marginSummary") or {}
            if "accountValue" not in ms:
                return None
            av = float(ms["accountValue"])
            used = float(ms.get("totalMarginUsed") or 0.0)
            equity = av + (self._spot_usdc_free() or 0.0)
            return {"account_value": equity,
                    "total_margin_used": used,
                    "total_ntl_pos": float(ms.get("totalNtlPos") or 0.0),
                    "withdrawable": float(st.get("withdrawable") or 0.0),
                    "margin_ratio": (used / equity) if equity > 0 else 0.0}
        except Exception:
            return None

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

    def spot_market_order_usd(self, pair: str, is_buy: bool, usd_notional: float, *,
                              slippage: float = 0.05, cloid: Optional[Cloid] = None) -> HLOrderResult:
        """Spot marketable IOC sized by USD notional (spot mid → BASE sz, rounded
        to the BASE token's szDecimals — NOT the perp coin's). Mirrors
        market_order_usd: enforces HL's $10 min and rejects if rounding moved the
        notional far, so a leg is never silently mis-sized or dropped to 0. Never
        raises past the trade gate. `pair` is the composed "UBTC/USDC" name (or a
        raw universe name); an optional cloid tags the order for idempotency."""
        self._assert_can_trade()
        rec = self.resolve_spot_pair(pair)
        if rec is None:
            return HLOrderResult(ok=False, raw={}, error=f"unknown spot pair {pair!r}")
        if usd_notional < self.MIN_ORDER_USD:
            return HLOrderResult(ok=False, raw={},
                                 error=f"notional ${usd_notional:.2f} < ${self.MIN_ORDER_USD:.0f} min ({pair})")
        mid = self.spot_mids().get(rec["coin"])
        if not mid or mid <= 0:
            return HLOrderResult(ok=False, raw={}, error=f"no spot mid for {pair}")
        sz = float(round(usd_notional / mid, rec["sz_decimals"]))
        if sz <= 0:
            return HLOrderResult(ok=False, raw={}, error=f"size rounds to 0 ({pair}, ${usd_notional:.2f})")
        rounded = sz * mid
        if rounded < self.MIN_ORDER_USD or abs(rounded - usd_notional) / usd_notional > 0.5:
            return HLOrderResult(ok=False, raw={},
                                 error=f"rounded notional ${rounded:.2f} off-target/below-min ({pair})")
        try:
            raw = self.exchange.market_open(rec["coin"], is_buy, sz, None, slippage, cloid)
        except Exception as e:
            return HLOrderResult(ok=False, raw={}, error=f"spot order exception: {e}")
        return self._parse_order(raw)

    def usd_class_transfer(self, amount_usd: float, to_perp: bool) -> dict:
        """Move USDC between the spot and perp clearinghouses (to_perp=True →
        spot→perp). Routed through _assert_can_trade — refused in MAINNET_DRY /
        without a signing wallet — because a transfer MUTATES the live account
        even though it places no order. Returns {ok, raw, error}. NOTE: this is
        a USER-SIGNED action (not L1) — whether an AGENT key may perform it is
        unverified; prove on testnet before relying on it (fallback:
        sub_account_transfer, which is L1-signed)."""
        self._assert_can_trade()
        if not amount_usd or amount_usd <= 0:
            return {"ok": False, "raw": {}, "error": f"amount ${amount_usd} must be > 0"}
        try:
            raw = self.exchange.usd_class_transfer(float(amount_usd), bool(to_perp))
            ok = isinstance(raw, dict) and raw.get("status") == "ok"
            return {"ok": ok, "raw": raw, "error": None if ok else str(raw)}
        except Exception as e:
            return {"ok": False, "raw": {}, "error": str(e)}

    def close(self, coin: str, *, sz: Optional[float] = None, slippage: float = 0.05,
              cloid: Optional[Cloid] = None) -> HLOrderResult:
        """Reduce/close a perp position. sz=None closes the ENTIRE position of
        whatever account the Exchange routes to — callers that manage only a
        SUB-book (e.g. the carry lane on a shared account) MUST pass their own
        qty so a reduce-only leg can never flatten another lane's position
        (review 2026-06-09 #2). sz is rounded to the coin's szDecimals; a size
        that rounds to 0 is rejected (never silently escalated to close-all)."""
        self._assert_can_trade()
        sz_rounded: Optional[float] = None
        if sz is not None:
            sz_rounded = self._round_sz(coin, float(sz))
            if sz_rounded <= 0:
                return HLOrderResult(ok=False, raw={},
                                     error=f"close size {sz} rounds to 0 ({coin})")
        try:
            raw = self.exchange.market_close(coin, sz_rounded, None, slippage, cloid)
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
