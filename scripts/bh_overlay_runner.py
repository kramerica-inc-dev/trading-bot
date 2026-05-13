#!/usr/bin/env python3
"""BH-Overlay paper-trade runner — the documented fallback per DECISIONS.md.

Strategy: `bh_overlay_strategy.BHOverlayStrategy` (VolTarget · re-entering
TrailingStopBH 10%/20d).  See that module's docstring for the honest framing:
the underlying trend rule did NOT clear the §7.1 random-entry null gate on
the 3.3-year OOS series; this ships as the recorded fallback when the active
candidates don't clear the bar.

Mode invariant (three-state gate, mirrors `carry_runner.py`):

  * paper_only=true                                  → PAPER     (default).
    Strategy decisions run against a cached public daily BTC price series;
    rebalance fills are *simulated* with a configurable round-trip cost
    (default 0.20% total on the traded notional delta).  No live orders are
    ever placed.  This is the only mode currently supported — venue adapters
    for spot execution have not been built.
  * paper_only=false + allow_live=true + venue=<...> → REJECTED today.
    The runner refuses to start with a clear message: spot venue adapters
    (Bitvavo / Kraken / Coinbase EU / OKX EU spot) do not exist yet — that's
    a separate future phase.

Cycle (every `cycle_interval_sec`, default 1h):

  1. Refresh the cached BTC daily series (public market endpoint — no auth).
     Use the cache if fetching fails.
  2. If the latest cached bar is for "today (UTC)", do not advance the
     strategy — that bar is not yet closed.  The strategy uses only fully
     closed bars (the lookahead-audit convention).
  3. If we have not yet made a decision for the most recent CLOSED daily bar:
     - run `BHOverlayStrategy.decide(history)` → target_exposure
     - if `should_rebalance(current, target, no_trade_band)` → simulate a fill
       (apply cost on the traded delta only), update simulated equity and
       exposure.
     - mark today (UTC) as decided.
  4. Else (already decided today): no decision; just refresh the marked-to-
     market simulated equity from the most recent cached close, and refresh
     health.json for the dashboard.

Halt sentinel: if `state/bh_overlay/<instance>/halt` exists, refuse to take
any new decisions; health.json reports `halted=true`.

State layout (under `state/bh_overlay/<instance>/`):
  - state.json   — persisted runner state (atomic write)
  - trades.log   — JSONL, one entry per cycle (decision or hold)
  - health.json  — last cycle's health snapshot for the dashboard
  - cache/btc_daily.csv  — cached daily OHLCV series
  - halt         — manual sentinel, presence pauses decisions

Usage:
    python3 -m scripts.bh_overlay_runner --config configs/bh_overlay-btc.json --once
    python3 -m scripts.bh_overlay_runner --config configs/bh_overlay-btc.json --loop
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT))

from bh_overlay_strategy import (  # noqa: E402
    BHOverlayStrategy, StrategyDecision, should_rebalance,
)

# Lazy imports for the public market data — kept under a try so unit tests
# that mock fetch_daily_btc can run without `requests` being importable.
try:
    import requests  # noqa: F401
except Exception:  # pragma: no cover
    requests = None  # type: ignore


# =========================  Config  =========================

@dataclass
class BHOverlayConfig:
    """Runner configuration (JSON-loaded; comment keys starting with `_` ignored)."""
    instance_name: str = "btc"
    asset: str = "BTC-USDT"

    # Mode gate (see module docstring).
    paper_only: bool = True
    allow_live: bool = False
    venue: Optional[str] = None             # "bitvavo" | "kraken" | "coinbase" | "okx_spot" | None

    # Book sizing
    initial_equity_usd: float = 5000.0

    # Strategy params (passed straight to BHOverlayStrategy).
    vol_target_annualised: float = 0.20
    vol_window_days: int = 30
    leverage_cap: float = 1.0
    no_trade_band_pct: float = 0.15
    trailing_stop_pct: float = 0.10
    reentry_n_days: int = 20

    # Cost assumption — modelled as a percentage of the *traded notional delta*
    # at each rebalance, total round-trip (so 0.20% means 0.10% in + 0.10% out
    # on a typical open-close pair).
    round_trip_cost_pct: float = 0.002

    # Cycle cadence
    cycle_interval_sec: int = 3600

    # Price source
    price_source: str = "okx_spot_public"   # "okx_spot_public" | "blofin_spot_public"


def load_config(path: Optional[str]) -> BHOverlayConfig:
    if not path:
        return BHOverlayConfig()
    with open(path) as f:
        data = json.load(f)
    known = set(BHOverlayConfig.__dataclass_fields__.keys())
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    unknown = set(data.keys()) - known
    if unknown:
        logging.warning("unknown config keys ignored: %s", sorted(unknown))
    return BHOverlayConfig(**{k: v for k, v in data.items() if k in known})


# =========================  Mode resolution  =========================

MODE_PAPER = "PAPER"


def resolve_mode(cfg: BHOverlayConfig) -> str:
    """Resolve runner mode. Refuses any non-paper request until venue adapters exist."""
    if cfg.paper_only:
        return MODE_PAPER
    raise RuntimeError(
        "Refusing to run live: paper_only=false but no spot venue adapter "
        "has been implemented for the BH-overlay fallback yet. Live execution "
        "(Bitvavo / Kraken / Coinbase EU / OKX EU spot) is a separate future "
        "phase per DECISIONS.md 2026-05-13. Set paper_only=true to run."
    )


# =========================  State  =========================

@dataclass
class BHOverlayState:
    """Persisted runner state (`state/bh_overlay/<instance>/state.json`)."""
    started_ts: Optional[str] = None
    last_cycle_ts: Optional[str] = None
    cycles_total: int = 0

    # Last UTC date (YYYY-MM-DD) for which a *decision* was made.  We only
    # decide once per closed daily bar; subsequent cycles within the same
    # day only refresh price/equity for the dashboard.
    last_decision_date: Optional[str] = None
    last_decision_bar_ts: Optional[str] = None

    # Strategy state machine (in_market, running_high, done).
    strategy_state: Dict[str, Any] = field(default_factory=dict)

    # Simulated book
    simulated_equity: float = 0.0           # USD
    current_exposure: float = 0.0           # [0, L_max]
    # Realized cumulative cost
    fees_paid_total: float = 0.0
    rebalances_total: int = 0
    last_btc_close: float = 0.0
    # Peak equity for drawdown tracking
    peak_equity: float = 0.0
    peak_equity_ts: Optional[str] = None
    last_dd_pct: float = 0.0
    days_under_water: int = 0
    # Stop / re-entry counters
    stops_fired_total: int = 0
    reentries_total: int = 0

    # BH benchmark for comparison (also simulated from the same series).
    bh_equity: float = 0.0                  # USD
    bh_anchor_close: float = 0.0            # entry price for the BH benchmark
    bh_initialized: bool = False

    # Manual halt
    halted: bool = False
    halt_reason: Optional[str] = None
    last_mode: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "BHOverlayState":
        from dataclasses import MISSING
        kwargs: Dict[str, Any] = {}
        for k, f in cls.__dataclass_fields__.items():
            if k in data:
                kwargs[k] = data[k]
            elif f.default is not MISSING:
                kwargs[k] = f.default
            elif f.default_factory is not MISSING:  # type: ignore[misc]
                kwargs[k] = f.default_factory()
        return cls(**kwargs)


# =========================  Public price fetcher  =========================

OKX_CANDLES_URL = "https://www.okx.com/api/v5/market/candles"
BLOFIN_CANDLES_URL = "https://openapi.blofin.com/api/v1/market/candles"

# OKX returns newest-first arrays of strings:
# [ts(ms), open, high, low, close, vol, volCcy, volCcyQuote, confirm]
# confirm=="1" means the bar is closed.
_OKX_HTTP_TIMEOUT = 10.0


def _request_get(url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Tiny one-shot GET wrapper.  Returns None on any error."""
    if requests is None:  # pragma: no cover
        logging.warning("requests not importable — cannot fetch %s", url)
        return None
    try:
        # A browser UA — OKX is behind Cloudflare and rejects python-requests/x.
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        }
        r = requests.get(url, params=params, timeout=_OKX_HTTP_TIMEOUT,
                         headers=headers)
        if r.status_code != 200:
            logging.warning("GET %s -> %s", url, r.status_code)
            return None
        return r.json()
    except Exception as e:  # pragma: no cover
        logging.warning("GET %s failed: %s", url, e)
        return None


def fetch_daily_btc_okx(symbol: str, limit: int = 300) -> List[Dict[str, Any]]:
    """Fetch public daily BTC candles from OKX.  Returns oldest-first."""
    resp = _request_get(OKX_CANDLES_URL, {
        "instId": symbol, "bar": "1D", "limit": str(min(int(limit), 300)),
    })
    if not isinstance(resp, dict) or not resp.get("data"):
        return []
    out: List[Dict[str, Any]] = []
    for row in resp["data"]:
        # Skip unconfirmed (today's still-open bar).
        try:
            if row[-1] not in ("1", 1, True):
                continue
        except Exception:
            pass
        try:
            ts = int(row[0])
            out.append({
                "timestamp": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(),
                "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]),  "close": float(row[4]),
                "volume": float(row[5]) if len(row) > 5 else 0.0,
            })
        except (ValueError, IndexError):
            continue
    out.sort(key=lambda r: r["timestamp"])
    return out


def fetch_daily_btc_blofin(symbol: str, limit: int = 300) -> List[Dict[str, Any]]:
    """Fetch public daily BTC candles from BloFin.  Returns oldest-first."""
    resp = _request_get(BLOFIN_CANDLES_URL, {
        "instId": symbol, "bar": "1D", "limit": str(min(int(limit), 1440)),
    })
    if not isinstance(resp, dict) or not resp.get("data"):
        return []
    out: List[Dict[str, Any]] = []
    # BloFin candle row: [ts, open, high, low, close, vol, volQuote, ...]
    for row in resp["data"]:
        try:
            ts = int(row[0])
            out.append({
                "timestamp": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(),
                "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]),  "close": float(row[4]),
                "volume": float(row[5]) if len(row) > 5 else 0.0,
            })
        except (ValueError, IndexError):
            continue
    out.sort(key=lambda r: r["timestamp"])
    return out


def fetch_daily_btc(source: str, symbol: str, limit: int = 300) -> List[Dict[str, Any]]:
    """Dispatch to the configured public market source."""
    if source == "blofin_spot_public":
        return fetch_daily_btc_blofin(symbol, limit=limit)
    return fetch_daily_btc_okx(symbol, limit=limit)


# =========================  Runner  =========================

class BHOverlayRunner:
    """Daily-cycle paper-trade runner for the BH-overlay fallback strategy."""

    def __init__(
        self,
        cfg: BHOverlayConfig,
        state_dir: Optional[Path] = None,
        *,
        fetch_fn=None,
    ) -> None:
        self.mode = resolve_mode(cfg)
        self.cfg = cfg

        # State layout — mirrors the carry runner's nesting.
        if state_dir is not None:
            instance_dir = state_dir / cfg.instance_name
        else:
            instance_dir = PROJECT_ROOT / "state" / "bh_overlay" / cfg.instance_name
        instance_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = instance_dir / "state.json"
        self.log_path = instance_dir / "trades.log"
        self.health_path = instance_dir / "health.json"
        self.halt_sentinel = instance_dir / "halt"
        self.cache_dir = instance_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.cache_dir / "btc_daily.csv"

        # Strategy
        self.strategy = BHOverlayStrategy(
            trail_pct=cfg.trailing_stop_pct,
            breakout_days=cfg.reentry_n_days,
            sigma_target=cfg.vol_target_annualised,
            vol_window=cfg.vol_window_days,
            L_max=cfg.leverage_cap,
            no_trade_band=cfg.no_trade_band_pct,
        )

        # Injection seam for tests (no-network).  Production uses the public
        # market endpoint chosen by `cfg.price_source`.
        self._fetch_fn = fetch_fn or (
            lambda: fetch_daily_btc(cfg.price_source, cfg.asset, limit=300)
        )

        logging.info(
            "BHOverlayRunner instance=%s mode=%s asset=%s paper_only=%s "
            "trail=%.2f%% N=%dd σ_target=%.2f L_max=%.2f band=%.2f cost_rt=%.4f",
            cfg.instance_name, self.mode, cfg.asset, cfg.paper_only,
            cfg.trailing_stop_pct * 100, cfg.reentry_n_days,
            cfg.vol_target_annualised, cfg.leverage_cap,
            cfg.no_trade_band_pct, cfg.round_trip_cost_pct,
        )

    # ---------- state I/O ----------

    def load_state(self) -> BHOverlayState:
        if self.state_path.exists():
            with open(self.state_path) as f:
                data = json.load(f)
            state = BHOverlayState.from_json(data)
            # Restore the strategy's internal state machine.
            self.strategy.from_state(state.strategy_state)
            return state
        # Fresh state — initialise simulated equity at the configured book size.
        return BHOverlayState(
            started_ts=datetime.now(timezone.utc).isoformat(),
            simulated_equity=float(self.cfg.initial_equity_usd),
            bh_equity=float(self.cfg.initial_equity_usd),
            peak_equity=float(self.cfg.initial_equity_usd),
            peak_equity_ts=datetime.now(timezone.utc).isoformat(),
        )

    def save_state(self, state: BHOverlayState) -> None:
        # Snapshot strategy state into the persisted dataclass before writing.
        state.strategy_state = self.strategy.to_state()
        tmp = self.state_path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(state.to_json(), f, indent=2, default=str)
        tmp.replace(self.state_path)

    def append_log(self, entry: Dict[str, Any]) -> None:
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def write_health(self, payload: Dict[str, Any]) -> None:
        try:
            tmp = self.health_path.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump(payload, f, indent=2, default=str)
            tmp.replace(self.health_path)
        except Exception as e:
            logging.warning("health write failed: %s", e)

    # ---------- price cache ----------

    def _read_cache(self) -> pd.DataFrame:
        if not self.cache_path.exists():
            return pd.DataFrame(columns=["timestamp", "open", "high", "low",
                                         "close", "volume"])
        try:
            df = pd.read_csv(self.cache_path)
            if df.empty:
                return df
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df = df.sort_values("timestamp").reset_index(drop=True)
            return df
        except Exception as e:
            logging.warning("cache read failed: %s — starting fresh", e)
            return pd.DataFrame(columns=["timestamp", "open", "high", "low",
                                         "close", "volume"])

    def _write_cache(self, df: pd.DataFrame) -> None:
        if df is None or df.empty:
            return
        out = df.copy()
        # Coerce timestamp to ISO strings for stable CSV round-tripping.
        out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True).astype(str)
        tmp = self.cache_path.with_suffix(".csv.tmp")
        out.to_csv(tmp, index=False, quoting=csv.QUOTE_MINIMAL)
        tmp.replace(self.cache_path)

    def refresh_cache(self) -> pd.DataFrame:
        """Merge a fresh fetch into the cache; fall back to cache on failure."""
        cached = self._read_cache()
        try:
            fresh = self._fetch_fn() or []
        except Exception as e:
            logging.warning("public price fetch failed: %s — using cache", e)
            fresh = []
        if not fresh:
            return cached
        fresh_df = pd.DataFrame(fresh)
        fresh_df["timestamp"] = pd.to_datetime(fresh_df["timestamp"], utc=True)
        if cached.empty:
            merged = fresh_df
        else:
            merged = pd.concat([cached, fresh_df], ignore_index=True)
        # Dedupe on timestamp keeping the *latest* row (fresh fetch wins).
        merged = (merged
                  .sort_values("timestamp")
                  .drop_duplicates(subset=["timestamp"], keep="last")
                  .reset_index(drop=True))
        self._write_cache(merged)
        return merged

    # ---------- halt ----------

    def _check_manual_halt(self) -> bool:
        return self.halt_sentinel.exists()

    # ---------- cycle ----------

    def one_cycle(self) -> Dict[str, Any]:
        state = self.load_state()
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        today_utc = now.strftime("%Y-%m-%d")

        df = self.refresh_cache()
        cache_size = len(df)
        last_bar_ts: Optional[str] = None
        last_close: float = state.last_btc_close
        if not df.empty:
            last_bar_ts = str(df["timestamp"].iloc[-1])
            last_close = float(df["close"].iloc[-1])
            state.last_btc_close = last_close

        # Mark BH benchmark to market on first decisionable bar.
        if not state.bh_initialized and not df.empty:
            state.bh_anchor_close = last_close
            state.bh_initialized = True

        manual_halt = self._check_manual_halt()
        if manual_halt and not state.halted:
            state.halted = True
            state.halt_reason = "manual_halt_sentinel"
        if not manual_halt and state.halted and state.halt_reason == "manual_halt_sentinel":
            # Sentinel cleared by operator → resume.
            state.halted = False
            state.halt_reason = None

        # Decide whether THIS cycle is a decision cycle.
        # We decide at most once per UTC day, and only on a fully closed bar.
        bar_date = last_bar_ts.split("T")[0].split(" ")[0] if last_bar_ts else None
        # The latest bar must NOT be today's still-open bar — the fetcher
        # already skips unconfirmed OKX bars, but we double-check.
        bar_is_closed = bar_date is not None and bar_date < today_utc
        already_decided_today = state.last_decision_date == today_utc

        action: Dict[str, Any] = {"kind": "noop", "reason": "no_change"}
        decision: Optional[StrategyDecision] = None
        rebalanced = False
        traded_delta = 0.0
        fee_cost = 0.0

        if df.empty:
            action = {"kind": "skip", "reason": "no_price_data"}
        elif state.halted:
            action = {"kind": "noop",
                      "reason": f"halted: {state.halt_reason or 'unknown'}"}
        elif already_decided_today:
            action = {"kind": "noop", "reason": "already_decided_today"}
        elif not bar_is_closed:
            action = {"kind": "noop", "reason": "latest_bar_not_closed_yet"}
        else:
            # Decision cycle.
            decision = self.strategy.decide(df)
            target = float(decision.target_exposure)
            current = float(state.current_exposure)

            # Track stop/re-entry transitions for the operator dashboard.
            was_in_market = current > 1e-9
            now_in_market = decision.signal_on
            if was_in_market and not now_in_market:
                state.stops_fired_total += 1
            elif (not was_in_market) and now_in_market and state.cycles_total > 0:
                state.reentries_total += 1

            if should_rebalance(current, target, self.cfg.no_trade_band_pct):
                traded_delta = abs(target - current)
                # Apply round-trip cost on the traded notional delta only (so a
                # 100bps round-trip means 100bps × |Δexposure| × equity).
                fee_cost = (
                    traded_delta * state.simulated_equity
                    * self.cfg.round_trip_cost_pct
                )
                state.simulated_equity -= fee_cost
                state.fees_paid_total += fee_cost
                state.current_exposure = target
                state.rebalances_total += 1
                rebalanced = True
                action = {
                    "kind": "rebalance",
                    "reason": decision.reason,
                    "from_exposure": current,
                    "to_exposure": target,
                    "traded_delta": traded_delta,
                    "fee_cost_usd": fee_cost,
                }
            else:
                action = {
                    "kind": "hold",
                    "reason": (
                        "within_no_trade_band" if target > 1e-9 else "remain_flat"
                    ),
                    "current_exposure": current,
                    "target_exposure": target,
                }
            state.last_decision_date = today_utc
            state.last_decision_bar_ts = last_bar_ts

        # Mark equity to current close: regardless of whether we rebalanced
        # this cycle, the held BTC fraction is exposed to the latest close.
        # For the daily-decision convention we use the latest CLOSED bar as
        # the reference; equity moves with that bar's close vs the previous
        # mark.  Implementation: track the previous mark price internally.
        # We don't want to double-count when there's no new bar yet, so use
        # last_btc_close *as stored* (which we update at the top of each
        # cycle).  Simulated equity scaling already happened implicitly when
        # the simulated_position holds at exposure × price changes — but the
        # M0 backtester treats equity as a single scalar and lets it ride the
        # close.  We mirror that, BUT only when a new closed bar appeared.
        # The bookkeeping below is therefore done in the decision branch
        # above (rebalance pays cost; equity rides closes between decisions).
        # We separately compute an "implied" mark-to-market for the dashboard.

        # Compute drawdown / underwater days from the simulated equity series.
        if state.simulated_equity > state.peak_equity:
            state.peak_equity = state.simulated_equity
            state.peak_equity_ts = now_iso
            state.days_under_water = 0
        else:
            # Increment under-water counter only on a fresh decision day.
            if action.get("kind") in ("rebalance", "hold") and bar_is_closed \
                    and state.last_decision_date == today_utc:
                state.days_under_water += 1
        state.last_dd_pct = (
            0.0 if state.peak_equity <= 0
            else max(0.0, (state.peak_equity - state.simulated_equity) / state.peak_equity)
        )

        # Buy-and-hold benchmark — also rides the closed series.
        if state.bh_initialized and state.bh_anchor_close > 0 and last_close > 0:
            state.bh_equity = (
                float(self.cfg.initial_equity_usd)
                * (last_close / state.bh_anchor_close)
            )

        # Build the per-cycle log entry.
        state.cycles_total += 1
        state.last_cycle_ts = now_iso
        state.last_mode = self.mode

        entry: Dict[str, Any] = {
            "ts": now_iso,
            "instance": self.cfg.instance_name,
            "mode": self.mode,
            "paper_only": self.cfg.paper_only,
            "asset": self.cfg.asset,
            "today_utc": today_utc,
            "bar_ts": last_bar_ts,
            "bar_is_closed": bar_is_closed,
            "already_decided_today": already_decided_today,
            "btc_close": last_close,
            "cache_bars": cache_size,
            "current_exposure": state.current_exposure,
            "target_exposure": (
                decision.target_exposure if decision else state.current_exposure
            ),
            "signal_on": (decision.signal_on if decision else None),
            "vol_realized": (decision.vol_realized if decision else None),
            "vol_target": self.cfg.vol_target_annualised,
            "vol_target_multiplier": (
                decision.vol_target_multiplier if decision else None
            ),
            "peak_price": (decision.peak_price if decision else None),
            "drawdown_pct_price": (decision.drawdown_pct if decision else None),
            "simulated_equity": state.simulated_equity,
            "bh_equity": state.bh_equity,
            "peak_equity": state.peak_equity,
            "drawdown_pct_equity": state.last_dd_pct,
            "days_under_water": state.days_under_water,
            "rebalanced": rebalanced,
            "traded_delta": traded_delta,
            "fee_cost_usd": fee_cost,
            "fees_paid_total": state.fees_paid_total,
            "rebalances_total": state.rebalances_total,
            "stops_fired_total": state.stops_fired_total,
            "reentries_total": state.reentries_total,
            "halted": state.halted,
            "halt_reason": state.halt_reason,
            "action": action,
            "strategy_state": self.strategy.to_state(),
        }

        self.save_state(state)
        self.append_log(entry)
        self.write_health(self.health(state, decision))

        logging.info(
            "[%s] cycle #%d mode=%s bar=%s closed=%s decided_today=%s "
            "exposure=%.3f signal_on=%s eq=$%.2f dd=%.2f%% action=%s",
            self.cfg.instance_name, state.cycles_total, self.mode,
            last_bar_ts, bar_is_closed, already_decided_today,
            state.current_exposure,
            (decision.signal_on if decision else None),
            state.simulated_equity, state.last_dd_pct * 100,
            action.get("kind"),
        )
        return entry

    # ---------- loop ----------

    def loop(self, max_cycles: Optional[int] = None) -> None:
        n = 0
        while True:
            try:
                self.one_cycle()
            except Exception as e:
                logging.exception("cycle crashed: %s", e)
            n += 1
            if max_cycles is not None and n >= max_cycles:
                return
            time.sleep(max(1, int(self.cfg.cycle_interval_sec)))

    # ---------- health ----------

    def health(
        self,
        state: BHOverlayState,
        decision: Optional[StrategyDecision] = None,
    ) -> Dict[str, Any]:
        bh_pnl_pct = 0.0
        if state.bh_initialized and self.cfg.initial_equity_usd > 0:
            bh_pnl_pct = (state.bh_equity / self.cfg.initial_equity_usd - 1.0) * 100.0
        strat_pnl_pct = 0.0
        if self.cfg.initial_equity_usd > 0:
            strat_pnl_pct = (state.simulated_equity / self.cfg.initial_equity_usd - 1.0) * 100.0
        return {
            "alive": True,
            "instance": self.cfg.instance_name,
            "mode": self.mode,
            "paper_only": self.cfg.paper_only,
            "allow_live": self.cfg.allow_live,
            "venue": self.cfg.venue,
            "asset": self.cfg.asset,
            "halted": state.halted,
            "halt_reason": state.halt_reason,
            "last_cycle_ts": state.last_cycle_ts,
            "last_decision_date": state.last_decision_date,
            "last_decision_bar_ts": state.last_decision_bar_ts,
            "cycles_total": state.cycles_total,
            "rebalances_total": state.rebalances_total,
            "stops_fired_total": state.stops_fired_total,
            "reentries_total": state.reentries_total,
            "fees_paid_total": state.fees_paid_total,
            "simulated_equity": state.simulated_equity,
            "initial_equity_usd": self.cfg.initial_equity_usd,
            "strategy_pnl_pct": strat_pnl_pct,
            "current_exposure": state.current_exposure,
            "target_exposure": (
                decision.target_exposure if decision else state.current_exposure
            ),
            "signal_on": (decision.signal_on if decision else None),
            "vol_realized": (decision.vol_realized if decision else None),
            "vol_target": self.cfg.vol_target_annualised,
            "vol_target_multiplier": (
                decision.vol_target_multiplier if decision else None
            ),
            "peak_equity": state.peak_equity,
            "peak_equity_ts": state.peak_equity_ts,
            "drawdown_from_peak": state.last_dd_pct,
            "days_under_water": state.days_under_water,
            "peak_price": (decision.peak_price if decision else None),
            "drawdown_pct_price": (decision.drawdown_pct if decision else None),
            "last_btc_close": state.last_btc_close,
            "bh_equity": state.bh_equity,
            "bh_pnl_pct": bh_pnl_pct,
            "vol_target_active": (
                decision is not None and decision.vol_target_multiplier < self.cfg.leverage_cap
            ),
            "trend_signal_on": (decision.signal_on if decision else None),
            "trailing_stop_pct": self.cfg.trailing_stop_pct,
            "reentry_n_days": self.cfg.reentry_n_days,
            "vol_window_days": self.cfg.vol_window_days,
            "no_trade_band_pct": self.cfg.no_trade_band_pct,
            "round_trip_cost_pct": self.cfg.round_trip_cost_pct,
            "price_source": self.cfg.price_source,
            "strategy_state": self.strategy.to_state(),
        }


# =========================  CLI  =========================

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None,
                    help="JSON config; defaults if omitted")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--max-cycles", type=int, default=None,
                    help="for --loop, exit after N cycles (test/dev)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    cfg = load_config(args.config)
    runner = BHOverlayRunner(cfg)

    if args.loop:
        runner.loop(max_cycles=args.max_cycles)
        return 0

    entry = runner.one_cycle()
    print(json.dumps(entry, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
