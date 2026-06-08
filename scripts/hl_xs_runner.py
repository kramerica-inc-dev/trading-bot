#!/usr/bin/env python3
"""Cross-sectional momentum runner on HYPERLIQUID — the executable venue.

Same strategy as scripts/xs_runner.py (long top-m / short bottom-m of the OKX-
validated basket, dollar-neutral, rebalanced every `rebal` days) but data +
execution come from Hyperliquid, the one venue an NL retail user can actually
trade perps on (docs/VENUE-ACCESS-RESEARCH.md). The momentum edge was confirmed
to survive on Hyperliquid's own data (docs/VENUE-ACCESS-RESEARCH.md → HL
validation: ADVANCE, net +252%, null 99.9th).

Execution rides scripts/hl_adapter.py (official SDK, EIP-712 signing). The
adapter's three-state gate governs behaviour:
  * network=testnet                    → TESTNET      real signed orders, mock funds
  * network=mainnet & allow_live=false → MAINNET_DRY  data only; SIMULATED fills,
                                                       no orders, no wallet needed
  * network=mainnet & allow_live=true  → MAINNET_LIVE real money (hard-gated)

So MAINNET_DRY forward-papers on real mainnet prices+funding with no wallet and
no orders — the safe default for validating the loop. Credentials (an API/agent
wallet key) come from env: HL_PRIVATE_KEY (+ optional HL_ACCOUNT_ADDRESS).

Usage:
    python -m scripts.hl_xs_runner --config configs/hl-xsectional.json --once
    python -m scripts.hl_xs_runner --config configs/hl-xsectional.json --loop
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from xs_runner import XSState, Position, compute_target_weights  # noqa: E402
from hl_adapter import (  # noqa: E402
    HLAdapter, MODE_TESTNET, MODE_MAINNET_DRY, MODE_MAINNET_LIVE, _mask,
)

PROJECT_ROOT = HERE.parent
STATE_ROOT = PROJECT_ROOT / "state" / "hl_xsectional"


@dataclass
class HLXSConfig:
    instance_name: str = "main"
    universe: List[str] = field(default_factory=lambda: [
        "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "DOT", "LINK"])
    lookback_days: int = 120
    rebal_days: int = 5
    m: int = 3
    initial_capital: float = 5000.0     # notional used in sim (no-wallet) modes
    gross_exposure: float = 1.0
    cost_rate: float = 0.00045          # HL taker, for sim accounting
    flat_funding_annual: float = 0.06   # HL-calibrated headwind (sim)
    slippage: float = 0.05              # marketable-IOC slippage (live)
    resize_threshold: float = 0.10      # min drift (fraction of target $) to resize a held leg
    min_assets: int = 6
    halt_drawdown_pct: float = 0.25
    # catastrophe backstop: a TERMINAL, non-auto-resuming halt distinct from the
    # 25% auto-resuming "halted" — fires on a deep drawdown-from-peak OR a single
    # settled-equity drop in one cycle, flattens the live book, and clears only
    # via the manual op_halt path.
    catastrophe_drawdown_pct: float = 0.12
    catastrophe_intracycle_pct: float = 0.08
    # fast safety cadence: how often run_safety_once() checks equity/CB/reconcile
    # between the (slow) rebalances — so a live book isn't only seen hourly. 180s
    # tightens the window in which a fast move is caught (P0 #5); the cost is a
    # cheap account read, not a rebalance.
    safety_interval_sec: int = 180
    # --- P0 safety caps (live) ---
    # Pin per-coin cross leverage so a position can't inherit HL's per-coin MAX
    # (e.g. 40x BTC). The neutral basket only needs ~2x; 5x leaves headroom while
    # capping the liquidation tail.
    max_leverage: int = 5
    # Absolute USD ceilings on the basket, independent of the equity read — a guard
    # against an over-stated equity scaling the book unchecked. None = disabled
    # (set these in the live mainnet config to the real account's risk budget).
    max_gross_usd: Optional[float] = None    # cap on Σ|leg notional|
    max_net_usd: Optional[float] = None      # cap on |Σ leg notional| (net directional)
    # Stale/insufficient-data handling: skip the rebalance when the newest daily
    # bar is older than this; after this many consecutive bad-data cycles, flatten
    # an open live book rather than holding it blind.
    data_staleness_hours: int = 36
    max_data_outage_cycles: int = 3
    # Equity-jump guard: an account read this many times ABOVE the last settled
    # equity is treated as a likely misread (not a real gain) and not sized off —
    # a genuine deposit persists and is accepted after equity_jump_confirm_cycles
    # consecutive reads. Scales with the account automatically (no fixed cap).
    # 0 disables. This is the scale-free alternative to a static max_gross_usd.
    max_equity_jump_factor: float = 3.0
    equity_jump_confirm_cycles: int = 3
    network: str = "mainnet"            # 'mainnet' (DRY default) or 'testnet'
    allow_live: bool = False            # mainnet real-money gate


def load_config(path: str) -> HLXSConfig:
    raw = json.loads(Path(path).read_text())
    known = {f for f in HLXSConfig().__dataclass_fields__}
    cfg = HLXSConfig(**{k: v for k, v in raw.items() if k in known})
    if cfg.network not in ("testnet", "mainnet"):
        raise ValueError(f"network must be 'testnet' or 'mainnet', got {cfg.network!r}")
    if not isinstance(cfg.allow_live, bool):       # reject 'false'/0/1 strings — never silently go live
        raise ValueError(f"allow_live must be a JSON boolean, got {cfg.allow_live!r}")
    return cfg


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HLXSRunner:
    def __init__(self, cfg: HLXSConfig):
        self.cfg = cfg
        pk = os.environ.get("HL_PRIVATE_KEY") or None
        addr = os.environ.get("HL_ACCOUNT_ADDRESS") or None
        self.adapter = HLAdapter(network=cfg.network, private_key=pk,
                                 account_address=addr, allow_live=cfg.allow_live)
        self.mode = self.adapter.mode
        # We place real orders only with a wallet AND in a trade-enabled mode.
        self.live_trading = (self.mode in (MODE_TESTNET, MODE_MAINNET_LIVE)
                             and self.adapter.exchange is not None)
        self._force_rebalance = False        # set via --force-rebalance for a one-shot
        self._leverage_set = False           # per-process idempotent leverage pin
        self.dir = STATE_ROOT / cfg.instance_name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.trades_path = self.dir / "trades.log"
        self.health_path = self.dir / "health.json"

    # -- persistence (same shape as xs_runner) -----------------------------
    def load_state(self) -> XSState:
        if self.state_path.exists():
            return XSState.from_json(json.loads(self.state_path.read_text()))
        return XSState(cash=self.cfg.initial_capital, equity=self.cfg.initial_capital,
                       peak_equity=self.cfg.initial_capital,
                       started_ts=_utcnow().isoformat(), dry_run=not self.live_trading)

    def save_state(self, s: XSState) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(s.to_json(), indent=2))
        tmp.replace(self.state_path)

    def log(self, event: dict) -> None:
        # Rotate to <name>.1 at 10MB so the append-only log can't fill the LXC disk.
        try:
            if self.trades_path.exists() and self.trades_path.stat().st_size > 10 * 1024 * 1024:
                self.trades_path.replace(self.trades_path.parent / (self.trades_path.name + ".1"))
        except OSError:
            pass
        with open(self.trades_path, "a") as f:
            f.write(json.dumps({"ts": _utcnow().isoformat(), **event}) + "\n")

    # -- marking -----------------------------------------------------------
    def _sim_mark(self, s: XSState, mids: Dict[str, float]) -> float:
        upnl = 0.0
        for sym, p in s.positions.items():
            px = mids.get(sym)
            if px and p.entry_price > 0:
                upnl += p.side * p.notional * (px / p.entry_price - 1.0)
        return s.cash + upnl

    def equity(self, s: XSState, mids: Dict[str, float]) -> Optional[float]:
        """Live: real on-chain account value (None on a TRANSIENT read failure →
        caller skips, never halts). Dry/sim: the simulated mark. Gated on
        live_trading (NOT wallet presence) so a stray env key can't make DRY size
        off real money."""
        if self.live_trading:
            return self.adapter.account_value()      # float, 0.0 (unfunded), or None
        return self._sim_mark(s, mids)

    # -- leverage pin (live) -----------------------------------------------
    def _ensure_leverage(self, s: XSState) -> None:
        """Pin per-coin cross leverage once per process (idempotent), so a live
        position can't inherit HL's per-coin MAX leverage (P0 #1). Best-effort;
        logs failures but never blocks a cycle. No-op in sim/DRY."""
        if self._leverage_set or not self.live_trading or self.cfg.max_leverage <= 0:
            return
        results = {c: self.adapter.set_leverage(c, self.cfg.max_leverage).get("ok", False)
                   for c in self.cfg.universe}
        self._leverage_set = True
        self.log({"action": "set_leverage", "leverage": self.cfg.max_leverage,
                  "ok_count": sum(1 for v in results.values() if v),
                  "failed": [c for c, ok in results.items() if not ok]})

    # -- sim funding (paper honesty) ---------------------------------------
    def _accrue_sim_funding(self, s: XSState, now: datetime) -> float:
        """SIM-only: accrue per-asset funding (HL predicted rate, flat fallback) on
        the held paper legs since the last accrual, so MAINNET_DRY paper P&L isn't
        optimistic (P0 #6). No-op when live_trading — real funding is already in
        account_value()."""
        if self.live_trading or not s.positions:
            return 0.0
        ref = s.last_funding_ts or s.started_ts
        s.last_funding_ts = now.isoformat()
        if not ref:
            return 0.0
        try:
            elapsed_days = max(0.0, (now - datetime.fromisoformat(ref)).total_seconds() / 86400.0)
        except (TypeError, ValueError):
            return 0.0
        if elapsed_days <= 0:
            return 0.0
        rates = self.adapter.funding_daily(list(s.positions.keys()))
        flat_daily = self.cfg.flat_funding_annual / 365.0
        total = 0.0
        for sym, p in s.positions.items():
            rate = rates.get(sym)
            if rate is None:
                rate = flat_daily
            # long pays funding when rate>0, short receives → pnl = -side*notional*rate
            total += -p.side * p.notional * rate * elapsed_days
        s.cash += total
        s.funding_paid_total += -total
        return total

    # -- targets -----------------------------------------------------------
    def _targets(self, closes, equity) -> Dict[str, float]:
        w = compute_target_weights(closes, self.cfg.lookback_days, self.cfg.m)
        gross_book = equity * self.cfg.gross_exposure
        targets = {c: wt * gross_book for c, wt in w.items() if wt != 0.0}
        return self._apply_exposure_caps(targets)

    def _apply_exposure_caps(self, targets: Dict[str, float]) -> Dict[str, float]:
        """Scale the basket down to the absolute USD ceilings (P0 #1/#11) — a guard
        so an over-stated equity read can't size the book unchecked. Scaling is
        uniform, so dollar-neutrality is preserved. No-op when the caps are unset."""
        if not targets:
            return targets
        gross = sum(abs(v) for v in targets.values())
        net = abs(sum(targets.values()))
        scale = 1.0
        if self.cfg.max_gross_usd is not None and self.cfg.max_gross_usd > 0 and gross > self.cfg.max_gross_usd:
            scale = min(scale, self.cfg.max_gross_usd / gross)
        if self.cfg.max_net_usd is not None and self.cfg.max_net_usd > 0 and net > self.cfg.max_net_usd:
            scale = min(scale, self.cfg.max_net_usd / net)
        if scale < 1.0:
            targets = {c: v * scale for c, v in targets.items()}
            self.log({"action": "exposure_capped", "scale": round(scale, 4),
                      "gross_before": round(gross, 2), "net_before": round(net, 2),
                      "max_gross_usd": self.cfg.max_gross_usd,
                      "max_net_usd": self.cfg.max_net_usd})
        return targets

    # -- live book verification / remediation ------------------------------
    def _verify_book(self, targets: Dict[str, float]):
        """Does the live venue book == the intended dollar-neutral basket?
        Notional-aware (szi*mark). Returns (ok, missing_legs, detail)."""
        book = self.adapter.book_notional()          # {coin: signed usd}
        missing = {}
        for coin, tgt in targets.items():
            cur = book.get(coin, 0.0)
            if int(np.sign(cur)) != int(np.sign(tgt)) or abs(cur) < abs(tgt) * 0.5:
                missing[coin] = tgt
        extra = [c for c in book if c not in targets
                 and abs(book[c]) > self.adapter.MIN_ORDER_USD]
        ln = sum(v for v in book.values() if v > 0)
        sn = -sum(v for v in book.values() if v < 0)
        g = ln + sn
        neutral = (g <= 0) or (abs(ln - sn) / g <= 0.15)
        ok = (not missing) and (not extra) and neutral
        return ok, missing, f"missing={list(missing)} extra={extra} L/S={ln:.0f}/{sn:.0f}"

    def flatten_all(self) -> list:
        """Close every live position (drawdown halt / failed-rebalance safety).
        Hardened (P0 #8): retry the positions read, then VERIFY the book is flat and
        re-close any straggler, so a breaker can never report a benign-looking
        'flattened' while a read failure actually left real positions open."""
        coins = None
        last_err = None
        for i in range(3):
            try:
                coins = list(self.adapter.positions().keys())
                break
            except Exception as e:
                last_err = e
                time.sleep(0.4 * (i + 1))
        if coins is None:
            # could not even read the book — do NOT pretend we flattened
            return [{"act": "flatten", "ok": False, "verified_flat": False,
                     "err": f"positions read failed (UNCONFIRMED flatten): {last_err}"}]
        out = []
        for coin in coins:
            r = self.adapter.close(coin)
            out.append({"act": "flatten", "coin": coin, "ok": r.ok, "err": r.error})
        # verify flat; re-close any straggler once more, then report verified_flat
        try:
            remaining = list(self.adapter.positions().keys())
            for coin in remaining:
                r = self.adapter.close(coin)
                out.append({"act": "flatten_retry", "coin": coin, "ok": r.ok, "err": r.error})
            if remaining:
                remaining = list(self.adapter.positions().keys())
            out.append({"act": "flatten_verify", "verified_flat": not remaining,
                        "remaining": remaining})
        except Exception as e:
            out.append({"act": "flatten_verify", "verified_flat": False,
                        "err": f"verify read failed: {e}"})
        return out

    @staticmethod
    def _resize_order(cur_notional: float, tgt: float, threshold: float, min_order: float):
        """For a leg already held on the CORRECT side, the (is_buy, usd) order to
        move it toward the target notional — or None if the drift is within
        tolerance (< threshold·|tgt|) or below the venue minimum. `is_buy = delta>0`
        grows a long / trims a short and trims a long / grows a short symmetrically;
        because tgt is same-signed as cur here, |delta| < |cur| so a resize can
        never flip the side."""
        delta = tgt - cur_notional
        drift = abs(delta)
        if drift < max(min_order, threshold * abs(tgt)):
            return None
        return (delta > 0, drift)

    # -- execution: LIVE (testnet / mainnet-live) --------------------------
    def _execute_live(self, targets: Dict[str, float], now: datetime) -> dict:
        orders = []
        cid = now.isoformat()                        # stable cycle id → deterministic cloids
        inst = self.cfg.instance_name

        def cl(coin, action):                        # idempotency tag per cycle/coin/action
            return self.adapter.make_cloid(f"{inst}|{cid}|{coin}|{action}")

        try:
            cur = self.adapter.positions()           # single snapshot
        except Exception as e:
            return {"action": "rebalance", "mode": self.mode, "execution": "live",
                    "complete": False, "longs": [], "shorts": [],
                    "orders": [{"act": "abort", "err": f"positions read: {e}"}]}
        # 1. close legs leaving the basket or flipping side
        for coin, p in cur.items():
            tgt = targets.get(coin, 0.0)
            if tgt == 0.0 or int(np.sign(tgt)) != (1 if p["szi"] > 0 else -1):
                r = self.adapter.close(coin, cloid=cl(coin, "close"))
                orders.append({"act": "close", "coin": coin, "ok": r.ok, "err": r.error})
        # 2. establish target legs not held; RESIZE legs held on the correct side
        #    toward target notional (correct drift instead of silently skipping).
        try:
            held = self.adapter.positions()
        except Exception:
            held = cur
        mids = self.adapter.all_mids()
        for coin, tgt in targets.items():
            h = held.get(coin)
            if h and int(np.sign(h["szi"])) == int(np.sign(tgt)):
                mk = mids.get(coin) or h.get("entry_px") or 0.0
                cur_notional = h["szi"] * mk
                ro = self._resize_order(cur_notional, tgt, self.cfg.resize_threshold,
                                        self.adapter.MIN_ORDER_USD)
                if ro is None:
                    continue                         # drift within tolerance — leave it
                is_buy, usd = ro
                r = self.adapter.market_order_usd(coin, is_buy, usd, slippage=self.cfg.slippage,
                                                  cloid=cl(coin, "resize"))
                orders.append({"act": "resize", "coin": coin, "side": int(np.sign(tgt)),
                               "delta": round(tgt - cur_notional, 2), "ok": r.ok, "err": r.error})
                continue
            r = self.adapter.market_order_usd(coin, tgt > 0, abs(tgt), slippage=self.cfg.slippage,
                                              cloid=cl(coin, "open"))
            orders.append({"act": "open", "coin": coin, "side": int(np.sign(tgt)),
                           "ok": r.ok, "filled": r.filled_sz, "err": r.error})
        # 3. verify the realized book; bounded-retry only the REMAINING shortfall of
        #    each missing leg (partial-fill-aware → a re-send can't overshoot); else
        #    FLATTEN (never leave a one-legged / non-neutral book).
        try:
            ok, missing, detail = self._verify_book(targets)
            attempt = 0
            while not ok and attempt < 2:
                attempt += 1
                book = self.adapter.book_notional()
                for coin in list(missing):
                    tgt = targets[coin]
                    curn = book.get(coin, 0.0)
                    remaining = (tgt - curn) if int(np.sign(curn)) == int(np.sign(tgt)) else tgt
                    if abs(remaining) < self.adapter.MIN_ORDER_USD:
                        continue
                    r = self.adapter.market_order_usd(coin, remaining > 0, abs(remaining),
                                                      slippage=self.cfg.slippage,
                                                      cloid=cl(coin, f"retry{attempt}"))
                    orders.append({"act": "retry", "coin": coin, "remaining": round(remaining, 2),
                                   "ok": r.ok, "err": r.error})
                ok, missing, detail = self._verify_book(targets)
        except Exception as e:
            ok, detail = False, f"verify error: {e}"
        if not ok:
            orders += self.flatten_all()             # back to flat, not one-legged
            orders.append({"act": "rebalance_failed_flattened", "detail": detail})
        longs = sorted(c for c, t in targets.items() if t > 0)
        shorts = sorted(c for c, t in targets.items() if t < 0)
        return {"action": "rebalance", "mode": self.mode, "execution": "live",
                "complete": bool(ok), "longs": longs, "shorts": shorts,
                "orders": orders, "book": detail}

    # -- execution: SIM (mainnet-dry / no wallet) --------------------------
    def _execute_sim(self, s: XSState, targets: Dict[str, float],
                     mids: Dict[str, float], now: datetime) -> dict:
        traded = 0.0
        for sym in list(s.positions.keys()):
            p = s.positions[sym]
            tgt = targets.get(sym, 0.0)
            if tgt == 0.0 or int(np.sign(tgt)) != p.side:
                px = mids.get(sym)
                if px is None:
                    continue
                s.cash += p.side * p.notional * (px / p.entry_price - 1.0)
                traded += abs(p.notional)
                del s.positions[sym]
        for sym, tgt in targets.items():
            px = mids.get(sym)
            if px is None:
                continue
            cur = s.positions.get(sym)
            if cur and cur.side == int(np.sign(tgt)):
                continue
            traded += abs(tgt)
            s.positions[sym] = Position(side=int(np.sign(tgt)), notional=abs(tgt),
                                        entry_price=px, entered_ts=now.isoformat())
        fee = traded * self.cfg.cost_rate
        s.cash -= fee
        s.fees_paid_total += fee
        longs = sorted(c for c, t in targets.items() if t > 0)
        shorts = sorted(c for c, t in targets.items() if t < 0)
        return {"action": "rebalance", "mode": self.mode, "execution": "sim",
                "longs": longs, "shorts": shorts, "fee": round(fee, 2),
                "traded_notional": round(traded, 2)}

    # -- reconcile ---------------------------------------------------------
    def reconcile(self, s: XSState, targets: Optional[Dict[str, float]]) -> dict:
        errs = []
        if self.live_trading:
            # NOTIONAL-aware (szi*mark), not count-only: a count-balanced but
            # size-skewed book is exactly the loss of dollar-neutrality we guard.
            try:
                book = self.adapter.book_notional()
            except Exception as e:
                # A transient venue-read failure (5xx / network blip) is NOT a bad
                # book — flag it so callers HOLD + retry instead of flattening +
                # halting. A persistent outage is caught by the external watchdog
                # (health goes stale), not by churning the live book.
                s.last_reconcile_ok = False
                return {"ok": False, "transient": True, "errors": [f"book read failed: {e}"]}
            ln = sum(v for v in book.values() if v > 0)
            sn = -sum(v for v in book.values() if v < 0)
            g = ln + sn
            if g > 0 and abs(ln - sn) / g > 0.15:
                errs.append(f"venue not dollar-neutral: {ln:.0f}L/{sn:.0f}S")
            n_legs = len([c for c in book if abs(book[c]) > self.adapter.MIN_ORDER_USD])
            if n_legs > 2 * self.cfg.m:
                errs.append(f"venue {n_legs} legs > 2m={2*self.cfg.m}")
        else:
            ln = sum(p.notional for p in s.positions.values() if p.side > 0)
            sn = sum(p.notional for p in s.positions.values() if p.side < 0)
            g = ln + sn
            if g > 0 and abs(ln - sn) / g > 0.10:
                errs.append(f"sim not dollar-neutral: {ln:.0f}L/{sn:.0f}S")
        s.last_reconcile_ok = not errs
        return {"ok": not errs, "errors": errs}

    # -- health ------------------------------------------------------------
    def write_health(self, s: XSState, extra: dict) -> None:
        dd = (s.peak_equity - s.equity) / s.peak_equity if s.peak_equity > 0 else 0.0
        # In live mode the book lives on-venue (s.positions is only the sim dict),
        # so report the real held-leg count — the dashboard must not show 0 legs
        # while real money is deployed.
        n_positions = len(s.positions)
        if self.live_trading:
            try:
                n_positions = len([c for c, v in self.adapter.book_notional().items()
                                   if abs(v) > self.adapter.MIN_ORDER_USD])
            except Exception:
                pass
        h = {"ts": _utcnow().isoformat(), "instance": self.cfg.instance_name,
             "mode": self.mode, "venue": "hyperliquid", "live_trading": self.live_trading,
             "have_wallet": self.adapter.wallet is not None,
             "account": (_mask(self.adapter.address) if self.adapter.wallet else None),
             # Full PUBLIC address (never the key) so the dashboard can query the
             # venue's public API for a live account view. None in no-wallet modes.
             "account_full": (self.adapter.address if self.adapter.wallet else None),
             "cycles_total": s.cycles_total, "rebalances_total": s.rebalances_total,
             "skips_total": s.skips_total, "equity": round(s.equity, 2),
             "peak_equity": round(s.peak_equity, 2), "drawdown_pct": round(dd * 100, 2),
             "cb_state": s.cb_state, "n_positions": n_positions,
             "fees_paid_total": round(s.fees_paid_total, 2),
             "last_rebalance_ts": s.last_rebalance_ts, "last_reconcile_ok": s.last_reconcile_ok,
             "config": {"lookback_days": self.cfg.lookback_days, "rebal_days": self.cfg.rebal_days,
                        "m": self.cfg.m, "universe": self.cfg.universe},
             **extra}
        tmp = self.health_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(h, indent=2))
        tmp.replace(self.health_path)

    # -- insight (dashboard observability only; never affects trading) ------
    def _build_insight(self, s: XSState, closes: Dict[str, np.ndarray],
                       mids: Dict[str, float], equity: float) -> dict:
        """The momentum ranking that drives selection + the current basket book +
        a net-beta gauge, for the dashboard. Best-effort; pure observability,
        never affects trading.

        Computed in BOTH live and sim/DRY modes: the book is read from the live
        venue when trading for real, else from the simulated basket (s.positions).
        That keeps the pro-cyclical-tilt net-beta gauge visible during the
        MAINNET_DRY forward-paper window — exactly the period where the basket's
        regime / hidden-beta risk is being evaluated (docs/XS-BETA-STUDY.md)."""
        out: dict = {}
        lb = self.cfg.lookback_days
        mom = []
        for c, cl in closes.items():
            if len(cl) > lb and cl[-1 - lb] > 0:
                mom.append({"coin": c, "trail_ret_pct": round((float(cl[-1]) / float(cl[-1 - lb]) - 1) * 100, 1)})
        mom.sort(key=lambda x: x["trail_ret_pct"], reverse=True)
        out["momentum"] = mom
        try:
            book = self._book_snapshot(s, mids)
            if book:
                out["book"] = sorted(book, key=lambda x: -x["notional"])
                out["book_source"] = "venue" if self.live_trading else "sim"
                nb = self._net_beta(closes, out["book"], equity)
                if nb is not None:
                    out["net_beta"] = nb
        except Exception:
            pass
        return out

    def _book_snapshot(self, s: XSState, mids: Dict[str, float]) -> list:
        """Per-leg book as [{coin, side, notional(signed $), upnl}] — from the
        live venue when live_trading, else from the simulated basket (s.positions)
        so the dashboard book + net-beta gauge are populated in DRY/paper too."""
        book = []
        if self.live_trading:
            for coin, p in self.adapter.positions().items():
                mk = (mids or {}).get(coin) or p.get("entry_px") or 0.0
                book.append({"coin": coin, "side": int(np.sign(p["szi"])),
                             "notional": round(p["szi"] * mk, 2),
                             "upnl": round(float(p.get("unrealized_pnl", 0.0)), 2)})
        else:
            for sym, p in s.positions.items():
                mk = (mids or {}).get(sym) or p.entry_price or 0.0
                upnl = (p.side * p.notional * (mk / p.entry_price - 1.0)
                        if p.entry_price > 0 else 0.0)
                book.append({"coin": sym, "side": p.side,
                             "notional": round(p.side * p.notional, 2),
                             "upnl": round(upnl, 2)})
        return book

    def _net_beta(self, closes, book, equity: float, win: int = 90):
        """Realized net BTC-beta of the live book (Σ wᵢ·βᵢ, wᵢ=notionalᵢ/equity,
        βᵢ=90d rolling beta vs BTC) — the pro-cyclical-tilt risk gauge. None if no data."""
        if equity <= 0 or not book:
            return None
        btc = closes.get("BTC")
        if btc is None:
            btc = self.adapter.daily_closes(["BTC"], win + 5).get("BTC")
        if btc is None or len(btc) < win + 2:
            return None
        br = (btc[1:] / btc[:-1] - 1.0)[-win:]
        if float(np.var(br, ddof=1)) <= 1e-12:
            return None
        nb = 0.0
        for leg in book:
            cl = closes.get(leg["coin"]) if closes.get(leg["coin"]) is not None else \
                self.adapter.daily_closes([leg["coin"]], win + 5).get(leg["coin"])
            if cl is None or len(cl) < win + 2:
                continue
            r = (cl[1:] / cl[:-1] - 1.0)[-win:]
            n = min(len(r), len(br))
            beta = float(np.cov(r[-n:], br[-n:], ddof=1)[0, 1] / np.var(br[-n:], ddof=1))
            nb += (leg["notional"] / equity) * beta
        return round(nb, 3)

    def _should_rebalance(self, s: XSState, now: datetime) -> bool:
        if self._force_rebalance:          # one-shot operator override (--force-rebalance)
            return True
        if s.last_rebalance_ts is None:
            return True
        last = datetime.fromisoformat(s.last_rebalance_ts)
        return (now - last).total_seconds() >= self.cfg.rebal_days * 86400 - 3600

    @staticmethod
    def _accept_post_trade_equity(prev_equity: float, eq2: Optional[float],
                                  result: dict) -> bool:
        """Whether to record a freshly-read post-rebalance equity. Right after a
        CLEANLY-COMPLETED live rebalance the venue's spot/perp collateral split
        can momentarily under-report (settles in ~1s); a read <50% of the
        pre-rebalance equity is then a settlement artifact → reject it (keep the
        settled value). If the rebalance did NOT complete (a leg failed / the book
        was flattened) a low read is REAL → accept it so the drawdown breaker sees
        the loss. Non-rebalance cycles (no trade just happened) always accept.
        Note the breaker itself always re-reads settled top-of-cycle equity, so a
        suppressed read can only delay a halt by one cycle, never hide it."""
        if eq2 is None:
            return False
        completed_live = (result.get("action") == "rebalance"
                          and result.get("execution") == "live"
                          and result.get("complete") is True)
        if completed_live and prev_equity > 0 and eq2 < 0.5 * prev_equity:
            return False
        return True

    # -- shared safety: equity read + circuit breakers ---------------------
    #   Both run_once (full, with rebalance) and run_safety_once (fast, no
    #   rebalance) funnel through these so the money-path CB logic lives in ONE
    #   place. _read_equity returns (dd, short_circuit_result|None); a non-None
    #   result means the cycle must stop now (transient/unfunded skip). The
    #   helpers never place a venue order EXCEPT a flatten when a breaker trips.
    def _read_equity(self, s: XSState, mids: Dict[str, float]):
        """Read settled equity, apply the transient/unfunded guards, update
        peak + drawdown. Returns (dd, short_circuit_result|None)."""
        eq = self.equity(s, mids)
        if eq is None:                               # TRANSIENT account read — skip, never halt
            s.skips_total += 1
            self.save_state(s)
            self.write_health(s, {"last_action": "skip", "reason": "account read transient"})
            return None, {"action": "skip", "reason": "transient account read failure"}
        # Equity-jump guard (P0 #1/#11): a read absurdly ABOVE the last settled
        # equity is almost certainly a misread (unified perp/spot accounting glitch),
        # not a real gain — never size the book off it. A genuine DEPOSIT persists,
        # so accept once the elevated read is confirmed over consecutive cycles; a
        # one-off glitch never is. Scale-free — no fixed dollar cap to maintain.
        ref = s.last_settled_equity
        if (ref is not None and ref > 0 and self.cfg.max_equity_jump_factor > 0
                and eq > ref * self.cfg.max_equity_jump_factor):
            s.equity_jump_streak += 1
            if s.equity_jump_streak < self.cfg.equity_jump_confirm_cycles:
                s.skips_total += 1
                self.save_state(s)
                self.write_health(s, {"last_action": "skip",
                                      "reason": f"unconfirmed equity jump {eq:.0f} vs settled {ref:.0f}",
                                      "equity_jump_streak": s.equity_jump_streak})
                return None, {"action": "skip",
                              "reason": f"suspicious equity jump ({eq:.0f} vs settled {ref:.0f}); holding"}
        s.equity_jump_streak = 0
        s.equity = eq
        if s.cycles_total == 1:                       # anchor peak to true starting equity
            s.peak_equity = s.equity
        if self.live_trading and s.equity < 5.0:      # live account not funded yet
            s.skips_total += 1
            self.save_state(s)
            self.write_health(s, {"last_action": "skip", "reason": "account unfunded"})
            return None, {"action": "skip",
                          "reason": f"account unfunded (eq={s.equity}); deposit USDC to {self.adapter.address}"}
        s.peak_equity = max(s.peak_equity, s.equity)
        dd = (s.peak_equity - s.equity) / s.peak_equity if s.peak_equity > 0 else 0.0
        return dd, None

    def _apply_circuit_breaker(self, s: XSState, dd: float):
        """Evaluate every breaker on the freshly-settled equity. Returns a
        short-circuit result dict if the cycle must HALT now (no rebalance),
        else None to proceed. Trips, in priority order:

          * catastrophe  — TERMINAL. dd-from-peak >= catastrophe_drawdown_pct OR a
                            single-cycle settled-equity drop >= catastrophe_intracycle_pct.
                            Flattens + sets cb_state="catastrophe_halt"; does NOT
                            auto-resume (clears only via the manual op_halt path).
          * halted       — the existing 25% drawdown breaker; auto-recovers when
                            dd falls well below the line.
          * op_halt      — operational failure (set elsewhere); manual clear only.

        `s.last_settled_equity` carries the previous accepted settled equity for
        the intracycle delta; it is reused for the intracycle guard so a single
        transient/settling low read (already filtered to None / via
        _accept_post_trade_equity) can't fabricate a drop here."""
        cfg = self.cfg
        prev_settled = s.last_settled_equity
        intracycle_drop = 0.0
        if prev_settled is not None and prev_settled > 0:
            intracycle_drop = max(0.0, (prev_settled - s.equity) / prev_settled)

        # 1. catastrophe (terminal, non-auto-resuming) — checked first.
        if s.cb_state != "catastrophe_halt" and (
                dd >= cfg.catastrophe_drawdown_pct
                or intracycle_drop >= cfg.catastrophe_intracycle_pct):
            trigger = ("drawdown" if dd >= cfg.catastrophe_drawdown_pct else "intracycle")
            s.cb_state = "catastrophe_halt"
            flat = self.flatten_all() if self.live_trading else []
            self.log({"action": "catastrophe_halt", "trigger": trigger,
                      "drawdown_pct": round(dd * 100, 2),
                      "intracycle_drop_pct": round(intracycle_drop * 100, 2),
                      "flattened": flat})

        # 2. the auto-resuming 25% drawdown breaker. On a NEW halt, FLATTEN.
        if dd >= cfg.halt_drawdown_pct and s.cb_state not in (
                "halted", "op_halt", "catastrophe_halt"):
            s.cb_state = "halted"
            flat = self.flatten_all() if self.live_trading else []
            self.log({"action": "circuit_breaker_halt", "drawdown_pct": round(dd * 100, 2),
                      "flattened": flat})

        # 3. any active halt: auto-resume only the "halted" state; the terminal
        #    states (op_halt / catastrophe_halt) keep the book flat + short-circuit.
        if s.cb_state in ("halted", "op_halt", "catastrophe_halt"):
            if s.cb_state == "halted" and dd < cfg.halt_drawdown_pct * 0.5:
                s.cb_state = "normal"
                self.log({"action": "circuit_breaker_resume", "drawdown_pct": round(dd * 100, 2)})
            else:
                if self.live_trading:                 # keep the book flat while halted
                    self.flatten_all()
                self.save_state(s)
                self.write_health(s, {"last_action": "halted", "cb_state": s.cb_state,
                                      "drawdown_pct": round(dd * 100, 2)})
                return {"action": "halted", "cb_state": s.cb_state,
                        "drawdown_pct": round(dd * 100, 2)}
        s.last_settled_equity = s.equity              # record the accepted settled equity
        return None

    # -- single-instance lock ----------------------------------------------
    @contextmanager
    def _cycle_lock(self):
        """Exclusive per-cycle file lock (flock) so a manual run (e.g.
        --force-rebalance) can't interleave order placement with the daemon loop
        (P0 #2). Yields True if acquired, False if another process holds it.
        Best-effort: a platform without fcntl simply yields True."""
        f = None
        acquired = False
        try:
            import fcntl
            f = open(self.dir / "cycle.lock", "w")
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except (BlockingIOError, OSError):
            if f is not None:
                try:
                    f.close()
                except OSError:
                    pass
                f = None
        except Exception:
            acquired = True            # fcntl unavailable — don't block the cycle
        try:
            yield acquired
        finally:
            if f is not None:
                try:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_UN)
                except OSError:
                    pass
                try:
                    f.close()
                except OSError:
                    pass

    # -- fast safety cycle (no rebalance) ----------------------------------
    def run_safety_once(self) -> dict:
        """Lock wrapper: a manual run can't interleave with the daemon (P0 #2)."""
        with self._cycle_lock() as locked:
            if not locked:
                return {"action": "skip", "reason": "another cycle holds the lock"}
            return self._run_safety_once()

    def _run_safety_once(self) -> dict:
        """The fast cadence: equity-read → drawdown/catastrophe/terminal breaker
        → reconcile, WITHOUT rebalancing. Run every `safety_interval_sec` between
        the (slow) full rebalances so a live book is checked far more often than
        hourly. Places NO venue order except a flatten when a breaker trips."""
        cfg = self.cfg
        s = self.load_state()
        now = _utcnow()
        s.cycles_total += 1
        s.last_cycle_ts = now.isoformat()

        mids = self.adapter.all_mids()
        dd, sc = self._read_equity(s, mids)
        if sc is not None:
            return sc
        sc = self._apply_circuit_breaker(s, dd)
        if sc is not None:
            return sc

        rec = self.reconcile(s, None)
        if not rec["ok"]:
            self.log({"action": "reconcile", **rec})
            if self.live_trading and s.cb_state == "normal" and not rec.get("transient"):
                flat = self.flatten_all()
                s.cb_state = "op_halt"
                self.log({"action": "reconcile_halt", "flattened": flat, "errors": rec["errors"]})
        self.save_state(s)
        self.write_health(s, {"last_action": "safety", "reconcile_ok": rec["ok"]})
        return {"action": "safety", "equity": round(s.equity, 2), "reconcile_ok": rec["ok"]}

    # -- data-outage handling ---------------------------------------------
    def _handle_data_outage(self, s: XSState, closes: dict, stale_h: Optional[float],
                            mids: dict) -> dict:
        """Insufficient/stale data (or a degraded live mid feed). The equity read +
        circuit breaker have ALREADY run this cycle (B1), so the book stays
        monitored. Track a streak; after max_data_outage_cycles consecutive bad
        cycles, FLATTEN an open live book rather than holding it blind (P0 #4/#5);
        otherwise skip the rebalance."""
        s.data_outage_streak += 1
        n = len(closes)
        if stale_h is not None and stale_h > self.cfg.data_staleness_hours:
            reason = f"stale data ({stale_h:.1f}h)"
        elif self.live_trading and not mids:
            reason = "mid feed degraded (empty)"
        else:
            reason = f"insufficient data ({n} assets)"
        action = "skip"
        if (self.live_trading and s.cb_state == "normal"
                and s.data_outage_streak >= self.cfg.max_data_outage_cycles):
            try:
                has_book = any(abs(v) > self.adapter.MIN_ORDER_USD
                               for v in self.adapter.book_notional().values())
            except Exception:
                has_book = False
            if has_book:
                flat = self.flatten_all()
                s.cb_state = "op_halt"
                action = "data_outage_flatten"
                self.log({"action": action, "streak": s.data_outage_streak,
                          "reason": reason, "flattened": flat})
        s.skips_total += 1
        self.save_state(s)
        self.write_health(s, {"last_action": action, "reason": reason,
                              "data_outage_streak": s.data_outage_streak})
        return {"action": action, "reason": reason,
                "data_outage_streak": s.data_outage_streak}

    # -- one cycle ---------------------------------------------------------
    def run_once(self) -> dict:
        """Lock wrapper: a manual run can't interleave with the daemon (P0 #2)."""
        with self._cycle_lock() as locked:
            if not locked:
                return {"action": "skip", "reason": "another cycle holds the lock"}
            return self._run_once()

    def _run_once(self) -> dict:
        cfg = self.cfg
        s = self.load_state()
        now = _utcnow()
        s.cycles_total += 1
        s.last_cycle_ts = now.isoformat()

        # SIM-only funding accrual so paper P&L isn't optimistic (A1) — before the
        # equity read so the headwind flows into equity + drawdown.
        funding = self._accrue_sim_funding(s, now)

        # B1: monitor the live book FIRST. The equity read + circuit breaker run on
        # EVERY cycle, BEFORE any data-sufficiency gate, so a data outage can never
        # leave an open live book unmonitored by the breakers. Live equity comes
        # from the account (no mids needed); sim marks off mids.
        mids = self.adapter.all_mids()
        dd, sc = self._read_equity(s, mids)
        if sc is not None:
            return sc
        sc = self._apply_circuit_breaker(s, dd)
        if sc is not None:
            return sc

        # Pin leverage before any order can be placed (B2), then gate on data quality.
        self._ensure_leverage(s)
        closes, latest_ms = self.adapter.daily_closes(
            cfg.universe, cfg.lookback_days, return_latest_ms=True)
        stale_h = ((now.timestamp() * 1000 - latest_ms) / 3.6e6
                   if latest_ms is not None else None)
        data_bad = (len(closes) < cfg.min_assets
                    or (stale_h is not None and stale_h > cfg.data_staleness_hours)
                    or (self.live_trading and not mids))      # A2: degraded mid feed
        if data_bad:
            return self._handle_data_outage(s, closes, stale_h, mids)
        s.data_outage_streak = 0

        result = {"action": "noop"}
        targets = None
        if self._should_rebalance(s, now):
            targets = self._targets(closes, s.equity)
            if not targets:
                result = {"action": "skip", "reason": "no target weights"}
                s.skips_total += 1
            else:
                result = (self._execute_live(targets, now) if self.live_trading
                          else self._execute_sim(s, targets, mids, now))
                s.rebalances_total += 1
                if self.live_trading and not result.get("complete", True):
                    # rebalance could not be completed — _execute_live already
                    # flattened the book; halt for an operator (no churn loop).
                    s.cb_state = "op_halt"
                    self.log({"action": "rebalance_halt", "book": result.get("book")})
                elif result.get("complete", True):
                    s.last_rebalance_ts = now.isoformat()   # advance only on a complete rebalance
            self.log(result)

        eq2 = self.equity(s, mids)
        if self._accept_post_trade_equity(s.equity, eq2, result):
            s.equity = eq2
            s.peak_equity = max(s.peak_equity, s.equity)
        rec = self.reconcile(s, targets)
        if not rec["ok"]:
            self.log({"action": "reconcile", **rec})
            if self.live_trading and s.cb_state == "normal" and not rec.get("transient"):
                # safety net: a CONFIRMED non-neutral / over-legged live book -> flatten
                # + halt. A transient book-read failure is NOT a bad book (hold + retry).
                flat = self.flatten_all()
                s.cb_state = "op_halt"
                self.log({"action": "reconcile_halt", "flattened": flat, "errors": rec["errors"]})
        insight = self._build_insight(s, closes, mids, s.equity)
        self.save_state(s)
        self.write_health(s, {"last_action": result["action"], "n_assets": len(closes),
                              "reconcile_ok": rec["ok"], "funding": round(funding, 4), **insight})
        return {**result, "equity": round(s.equity, 2), "reconcile_ok": rec["ok"]}

    def run_loop(self, interval_sec: int = 3600) -> None:
        """Decoupled cadence: the FULL run_once (equity + breaker + reconcile +
        rebalance) fires every `interval_sec`; the fast run_safety_once (equity +
        breaker + reconcile, NO rebalance) fires every `safety_interval_sec` in
        between, so a live book is checked far more often than the slow rebalance
        cadence. run_once advances the rebalance clock itself (_should_rebalance),
        so a full tick whose rebalance isn't due is just a no-trade full cycle."""
        safety_sec = max(1, int(self.cfg.safety_interval_sec))
        full_sec = max(safety_sec, int(interval_sec))
        print(f"[hl_xs_runner] {self.cfg.instance_name} mode={self.mode} "
              f"live_trading={self.live_trading} lb={self.cfg.lookback_days} "
              f"rebal={self.cfg.rebal_days} m={self.cfg.m} universe={len(self.cfg.universe)} "
              f"full={full_sec}s safety={safety_sec}s")
        import time
        next_full = 0.0                               # run a full cycle immediately on start
        while True:
            try:
                if time.monotonic() >= next_full:
                    r = self.run_once()
                    next_full = time.monotonic() + full_sec
                else:
                    r = self.run_safety_once()
                print(f"[{_utcnow().isoformat()}] {r.get('action')} eq={r.get('equity')} "
                      f"reconcile_ok={r.get('reconcile_ok')}")
            except Exception as e:
                print(f"[hl_xs_runner] cycle error: {e}", file=sys.stderr)
            time.sleep(safety_sec)


def main() -> int:
    # Guarantee live journald output regardless of PYTHONUNBUFFERED in the unit.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(PROJECT_ROOT / "configs" / "hl-xsectional-main.json"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval-sec", type=int, default=3600)
    ap.add_argument("--force-rebalance", action="store_true",
                    help="force a one-shot rebalance now, ignoring the rebal_days "
                         "timer (operator override; use with --once)")
    args = ap.parse_args()
    cfg = load_config(args.config) if Path(args.config).exists() else HLXSConfig()
    runner = HLXSRunner(cfg)
    runner._force_rebalance = args.force_rebalance
    if args.loop:
        runner.run_loop(args.interval_sec)
        return 0
    print(json.dumps(runner.run_once(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
