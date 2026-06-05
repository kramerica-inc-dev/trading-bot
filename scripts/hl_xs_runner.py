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

    # -- targets -----------------------------------------------------------
    def _targets(self, closes, equity) -> Dict[str, float]:
        w = compute_target_weights(closes, self.cfg.lookback_days, self.cfg.m)
        gross_book = equity * self.cfg.gross_exposure
        return {c: wt * gross_book for c, wt in w.items() if wt != 0.0}

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
        """Close every live position (drawdown halt / failed-rebalance safety)."""
        try:
            coins = list(self.adapter.positions().keys())
        except Exception as e:
            return [{"act": "flatten", "ok": False, "err": f"positions read: {e}"}]
        out = []
        for coin in coins:
            r = self.adapter.close(coin)
            out.append({"act": "flatten", "coin": coin, "ok": r.ok, "err": r.error})
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
                r = self.adapter.close(coin)
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
                r = self.adapter.market_order_usd(coin, is_buy, usd, slippage=self.cfg.slippage)
                orders.append({"act": "resize", "coin": coin, "side": int(np.sign(tgt)),
                               "delta": round(tgt - cur_notional, 2), "ok": r.ok, "err": r.error})
                continue
            r = self.adapter.market_order_usd(coin, tgt > 0, abs(tgt), slippage=self.cfg.slippage)
            orders.append({"act": "open", "coin": coin, "side": int(np.sign(tgt)),
                           "ok": r.ok, "filled": r.filled_sz, "err": r.error})
        # 3. verify the realized book; bounded-retry missing legs; else FLATTEN
        #    (never leave a one-legged / non-neutral book).
        try:
            ok, missing, detail = self._verify_book(targets)
            for _ in range(2):
                if ok:
                    break
                for coin, tgt in missing.items():
                    r = self.adapter.market_order_usd(coin, tgt > 0, abs(tgt),
                                                      slippage=self.cfg.slippage)
                    orders.append({"act": "retry", "coin": coin, "ok": r.ok, "err": r.error})
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
                s.last_reconcile_ok = False
                return {"ok": False, "errors": [f"book read failed: {e}"]}
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

    # -- one cycle ---------------------------------------------------------
    def run_once(self) -> dict:
        cfg = self.cfg
        s = self.load_state()
        now = _utcnow()
        s.cycles_total += 1
        s.last_cycle_ts = now.isoformat()

        closes = self.adapter.daily_closes(cfg.universe, cfg.lookback_days)
        if len(closes) < cfg.min_assets:
            s.skips_total += 1
            self.save_state(s)
            self.write_health(s, {"last_action": "skip", "reason": f"{len(closes)} assets"})
            return {"action": "skip", "reason": f"insufficient data ({len(closes)} assets)"}
        mids = self.adapter.all_mids()

        eq = self.equity(s, mids)
        if eq is None:                               # TRANSIENT account read — skip, never halt
            s.skips_total += 1
            self.save_state(s)
            self.write_health(s, {"last_action": "skip", "reason": "account read transient"})
            return {"action": "skip", "reason": "transient account read failure"}
        s.equity = eq
        if s.cycles_total == 1:                       # anchor peak to true starting equity
            s.peak_equity = s.equity
        if self.live_trading and s.equity < 5.0:      # live account not funded yet
            s.skips_total += 1
            self.save_state(s)
            self.write_health(s, {"last_action": "skip", "reason": "account unfunded"})
            return {"action": "skip",
                    "reason": f"account unfunded (eq={s.equity}); deposit USDC to {self.adapter.address}"}
        s.peak_equity = max(s.peak_equity, s.equity)
        dd = (s.peak_equity - s.equity) / s.peak_equity if s.peak_equity > 0 else 0.0

        # Circuit breaker. Two halt states: "halted" = drawdown breaker
        # (auto-recovers when dd falls well below the line); "op_halt" =
        # operational failure (failed rebalance / reconcile divergence — manual
        # clear only). On a NEW drawdown halt, FLATTEN the live book (de-risk).
        if dd >= cfg.halt_drawdown_pct and s.cb_state not in ("halted", "op_halt"):
            s.cb_state = "halted"
            flat = self.flatten_all() if self.live_trading else []
            self.log({"action": "circuit_breaker_halt", "drawdown_pct": round(dd * 100, 2),
                      "flattened": flat})
        if s.cb_state in ("halted", "op_halt"):
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
            if self.live_trading and s.cb_state == "normal":
                # safety net: a non-neutral / over-legged live book -> flatten + halt
                flat = self.flatten_all()
                s.cb_state = "op_halt"
                self.log({"action": "reconcile_halt", "flattened": flat, "errors": rec["errors"]})
        insight = self._build_insight(s, closes, mids, s.equity)
        self.save_state(s)
        self.write_health(s, {"last_action": result["action"], "n_assets": len(closes),
                              "reconcile_ok": rec["ok"], **insight})
        return {**result, "equity": round(s.equity, 2), "reconcile_ok": rec["ok"]}

    def run_loop(self, interval_sec: int = 3600) -> None:
        print(f"[hl_xs_runner] {self.cfg.instance_name} mode={self.mode} "
              f"live_trading={self.live_trading} lb={self.cfg.lookback_days} "
              f"rebal={self.cfg.rebal_days} m={self.cfg.m} universe={len(self.cfg.universe)}")
        while True:
            try:
                r = self.run_once()
                print(f"[{_utcnow().isoformat()}] {r.get('action')} eq={r.get('equity')} "
                      f"reconcile_ok={r.get('reconcile_ok')}")
            except Exception as e:
                print(f"[hl_xs_runner] cycle error: {e}", file=sys.stderr)
            import time
            time.sleep(interval_sec)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=str(PROJECT_ROOT / "configs" / "hl-xsectional-main.json"))
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval-sec", type=int, default=3600)
    args = ap.parse_args()
    cfg = load_config(args.config) if Path(args.config).exists() else HLXSConfig()
    runner = HLXSRunner(cfg)
    if args.loop:
        runner.run_loop(args.interval_sec)
        return 0
    print(json.dumps(runner.run_once(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
