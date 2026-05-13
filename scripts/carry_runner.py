#!/usr/bin/env python3
"""Funding/basis carry runner — P1 dry-run on OKX EU.

Modeled on `plan_e_runner.py`:
  * config-driven (JSON), state in `state/<instance>/`
  * structured JSONL trade log at `state/<instance>/trades.log`
  * periodic cycle: read account+market state, compute target, reconcile
  * does NOT place orders in P1 (dry_run=true is the only supported mode)

Per the 2026-05-13 DECISIONS.md entry, this is the *first* phase of the
carry build. Even though current trailing-90d funding is compressed
(2026 YTD ≈ -0.9%/yr), we ship the integration so the runner is ready
when funding normalises.

Green-button rule (also from DECISIONS.md):
    if trailing_90d_annualised_funding > +5%/yr  →  ON, target_notional > 0
    else                                          →  OFF, target_notional = 0
                                                     (hold cash; basis & funding only)

Usage:
    # market-data-only (no credentials needed):
    python3 -m scripts.carry_runner --config configs/carry.json --once

    # once with credentials (paper account / demo):
    OKX_API_KEY=... OKX_API_SECRET=... OKX_API_PASSPHRASE=... \\
        python3 -m scripts.carry_runner --config configs/carry.json --once

    # systemd-style loop (P2 will graduate this to paper-account orders):
    python3 -m scripts.carry_runner --config configs/carry.json --loop

P1 hard rule: `dry_run=false` is rejected. The runner ASSERTs no real
order is ever placed in P1. P2 will switch this behind a paper-account
gate.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT))

from okx_adapter import OkxAdapter  # noqa: E402
from carry_position import (  # noqa: E402
    CarryPosition, DriftReport, annualize_funding,
    delta_neutral_drift, funding_accrual_step,
    green_button_on, projected_next_funding_usd,
    target_position_for,
)


# =========================  Config  =========================

@dataclass
class CarryRunnerConfig:
    """Carry runner config (JSON-loaded).

    A small surface so the file is reviewable end-to-end. All risk
    parameters live here; the runner reads them, never hard-codes.
    """
    instance_name: str = "carry"
    # exchange + symbol
    exchange: str = "okx"                  # only "okx" supported in P1
    spot_symbol: str = "BTC-USDT"          # OKX spot
    perp_symbol: str = "BTC-USDT"          # passed to OkxAdapter — gets -SWAP suffix
    # sizing
    initial_notional_usd: float = 5000.0   # book size (for projection only in dry-run)
    target_dn_notional_fraction: float = 0.6   # fraction of book deployed delta-neutral
    leverage_cap: float = 2.0              # perp short leverage (≤2× per spec §6)
    # green-button gate
    funding_on_threshold_annualised: float = 0.05   # +5%/yr per DECISIONS 2026-05-13
    trailing_window_samples: int = 270     # ~90d of 8h settlements
    # risk controls
    basis_kill_pct: float = 0.01           # flatten if |basis|/spot > 1%
    margin_ratio_alarm: float = 1.5        # warn if account margin_ratio < this
    # cycle
    cycle_interval_sec: int = 300          # 5 min default
    # dry-run guard
    dry_run: bool = True                   # P1: must be true. Asserted at startup.
    # OKX base URL override (EU region or testnet)
    okx_base_url: Optional[str] = None
    # OKX demo trading flag (uses public host + x-simulated-trading header)
    okx_demo: bool = False


def load_config(path: Optional[str]) -> CarryRunnerConfig:
    if not path:
        return CarryRunnerConfig()
    with open(path) as f:
        data = json.load(f)
    # Ignore unknown keys with a warning rather than crashing — keeps
    # configs forward-compatible as we add P2/P3 knobs.
    known = set(CarryRunnerConfig.__dataclass_fields__.keys())
    unknown = set(data.keys()) - known
    if unknown:
        logging.warning("unknown config keys ignored: %s", sorted(unknown))
    return CarryRunnerConfig(**{k: v for k, v in data.items() if k in known})


# =========================  Persistent runner state  =========================

@dataclass
class CarryRunnerState:
    """Persistent state in `state/<instance>/state.json`.

    Mirrors PortfolioState's discipline: load on cycle, mutate, persist
    atomically.

    `simulated_position` holds the runner's *intended* delta-neutral
    book — in dry-run mode this is what we would have if we placed the
    orders we logged. P2 will reconcile this against real fills.
    """
    started_ts: Optional[str] = None
    last_cycle_ts: Optional[str] = None
    cycles_total: int = 0
    funding_samples: List[float] = field(default_factory=list)  # rolling-90d cache
    funding_samples_ts: List[str] = field(default_factory=list)
    simulated_position: Dict[str, Any] = field(default_factory=lambda: asdict(CarryPosition()))
    simulated_equity: float = 0.0
    last_funding_rate: float = 0.0
    last_basis_usd: float = 0.0
    last_spot_price: float = 0.0
    last_perp_price: float = 0.0
    last_account_check: Optional[Dict[str, Any]] = None
    last_reconcile_ok: bool = True
    last_reconcile_errors: List[str] = field(default_factory=list)
    # Persisted dry-run flag - sanity check on reload.
    dry_run: bool = True

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "CarryRunnerState":
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


# =========================  Reconciliation (P1)  =========================

@dataclass
class CarryReconcileResult:
    ok: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    rules_evaluated: int = 0
    ts: str = ""

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    def add_error(self, code: str, msg: str) -> None:
        self.errors.append(f"{code}: {msg}")
        self.ok = False

    def add_warning(self, code: str, msg: str) -> None:
        self.warnings.append(f"{code}: {msg}")


def reconcile_carry_state(
    state: CarryRunnerState,
    cfg: CarryRunnerConfig,
    *,
    account_snapshot: Optional[Dict[str, Any]] = None,
    now_iso: Optional[str] = None,
) -> CarryReconcileResult:
    """Self-consistency + (if account_snapshot given) light remote check.

    Rules:
      C1: simulated_position has finite numeric fields.
      C2: spot_qty >= 0, perp_qty <= 0 (carry sign convention).
      C3: |spot_qty + perp_qty| <= 1e-6 * max(|spot_qty|, |perp_qty|, 1) — drift
          within the runner's own book should be ~0 by construction in P1
          (we never place orders, so the simulated_position only ever
          mutates as one matched pair).
      C4: dry_run flag matches between state and cfg.
      C5 (warning): if account_snapshot given AND we have a simulated
          non-zero position, warn that P1 should never have a real
          position. (Sanity tripwire: someone enabled live mode by accident.)
      C6 (warning): if account_snapshot.acct_lv < 3, warn — unified margin
          is not enabled.

    Does NOT mutate state. Returns a structured result; the runner logs it.
    """
    res = CarryReconcileResult(
        ts=now_iso or datetime.now(timezone.utc).isoformat(),
    )

    pos_d = state.simulated_position or {}
    # C1
    res.rules_evaluated += 1
    for k in ("spot_qty", "perp_qty", "entry_spot_price", "entry_perp_price"):
        v = pos_d.get(k, 0.0)
        try:
            fv = float(v)
        except (TypeError, ValueError):
            res.add_error("C1", f"simulated_position.{k} not numeric: {v!r}")
            continue
        if fv != fv:  # NaN
            res.add_error("C1", f"simulated_position.{k} is NaN")
        if fv == float("inf") or fv == float("-inf"):
            res.add_error("C1", f"simulated_position.{k} is infinite")

    spot_q = float(pos_d.get("spot_qty", 0.0) or 0.0)
    perp_q = float(pos_d.get("perp_qty", 0.0) or 0.0)
    # C2
    res.rules_evaluated += 1
    if spot_q < -1e-9:
        res.add_error("C2", f"spot_qty < 0 ({spot_q}); carry requires long spot")
    if perp_q > 1e-9:
        res.add_error("C2", f"perp_qty > 0 ({perp_q}); carry requires short perp")

    # C3
    res.rules_evaluated += 1
    drift = spot_q + perp_q
    scale = max(abs(spot_q), abs(perp_q), 1.0)
    if abs(drift) > 1e-6 * scale:
        res.add_error(
            "C3",
            f"simulated drift {drift:+.9f} BTC > tolerance "
            f"(spot={spot_q}, perp={perp_q})",
        )

    # C4
    res.rules_evaluated += 1
    if bool(state.dry_run) != bool(cfg.dry_run):
        res.add_error(
            "C4",
            f"state.dry_run={state.dry_run} != cfg.dry_run={cfg.dry_run}",
        )

    # C5 / C6 — soft, only relevant once an account snapshot is available.
    if account_snapshot:
        res.rules_evaluated += 1
        acct_lv = account_snapshot.get("acct_lv")
        if acct_lv is not None and acct_lv < 3:
            res.add_warning(
                "C6",
                f"account acctLv={acct_lv} (need ≥3 for unified margin)",
            )
        # Tripwire: spot_qty/perp_qty != 0 means we'd be holding a real
        # position. In P1 we never do. If a future P2 runner ever loads
        # this state file with a non-zero simulated_position and tries
        # to reconcile against the exchange, this rule fires the warning.
        live_qty = account_snapshot.get("short_perp_qty")
        if live_qty is not None and abs(float(live_qty)) > 1e-9:
            res.add_warning(
                "C5",
                f"exchange has live perp position ({live_qty} BTC); "
                "P1 expects no live trades",
            )
    return res


# =========================  Runner  =========================

class CarryRunner:
    """Dry-run carry runner.

    Lifecycle:
        * __init__: build OkxAdapter (creds optional), set up state path,
          assert dry_run=True for P1.
        * one_cycle(): runs the full read/compute/log/reconcile pass.
        * loop(): cycles at config.cycle_interval_sec.
        * health(): a small dict for the dashboard.
    """

    def __init__(
        self,
        cfg: CarryRunnerConfig,
        state_dir: Optional[Path] = None,
    ) -> None:
        # ---------- P1 HARD GUARD ----------
        if not cfg.dry_run:
            raise RuntimeError(
                "CarryRunner P1 only supports dry_run=true. "
                "Live orders are blocked until P2 (paper) clears its gate."
            )

        self.cfg = cfg
        instance_dir = (state_dir or PROJECT_ROOT / "state") / cfg.instance_name
        instance_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = instance_dir / "state.json"
        self.log_path = instance_dir / "trades.log"
        self.health_path = instance_dir / "health.json"

        # Credentials are *optional*. With no key/secret/passphrase the
        # adapter can still hit public endpoints (ticker, funding-rate,
        # candles, spot instrument metadata). Private endpoints will
        # return error envelopes, which we tolerate in market-data-only
        # mode.
        api_key = os.environ.get("OKX_API_KEY", "")
        api_secret = os.environ.get("OKX_API_SECRET", "")
        passphrase = os.environ.get("OKX_API_PASSPHRASE", "")
        self.have_private_creds = bool(api_key and api_secret and passphrase)

        adapter_cfg: Dict[str, Any] = {
            "api_key": api_key,
            "api_secret": api_secret,
            "passphrase": passphrase,
            "demo_mode": cfg.okx_demo,
        }
        if cfg.okx_base_url:
            adapter_cfg["base_url"] = cfg.okx_base_url
        self.adapter = OkxAdapter(adapter_cfg)

        logging.info(
            "CarryRunner instance=%s exchange=%s spot=%s perp=%s "
            "private_creds=%s dry_run=%s",
            cfg.instance_name, cfg.exchange, cfg.spot_symbol, cfg.perp_symbol,
            self.have_private_creds, cfg.dry_run,
        )

    # ---------- state I/O ----------

    def load_state(self) -> CarryRunnerState:
        if self.state_path.exists():
            with open(self.state_path) as f:
                data = json.load(f)
            return CarryRunnerState.from_json(data)
        return CarryRunnerState(
            started_ts=datetime.now(timezone.utc).isoformat(),
            simulated_equity=self.cfg.initial_notional_usd,
            dry_run=self.cfg.dry_run,
        )

    def save_state(self, state: CarryRunnerState) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(state.to_json(), f, indent=2, default=str)
        tmp.replace(self.state_path)

    def append_log(self, entry: Dict[str, Any]) -> None:
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def write_health(self, payload: Dict[str, Any]) -> None:
        try:
            with open(self.health_path, "w") as f:
                json.dump(payload, f, indent=2, default=str)
        except Exception as e:
            logging.warning("health write failed: %s", e)

    # ---------- market-data probes ----------

    def fetch_spot_price(self) -> Optional[float]:
        resp = self.adapter.get_spot_ticker(self.cfg.spot_symbol)
        if not isinstance(resp, dict) or not resp.get("data"):
            return None
        try:
            return float(resp["data"][0].get("last") or 0.0) or None
        except (TypeError, ValueError, IndexError):
            return None

    def fetch_perp_price(self) -> Optional[float]:
        resp = self.adapter.get_ticker(self.cfg.perp_symbol)
        if not isinstance(resp, dict) or not resp.get("data"):
            return None
        try:
            return float(resp["data"][0].get("last") or 0.0) or None
        except (TypeError, ValueError, IndexError):
            return None

    def fetch_funding_rate(self) -> Optional[float]:
        # Use the adapter's perp symbol path → forwards as -SWAP to OKX.
        resp = self.adapter.api.get_funding_rate(
            inst_id=f"{self.cfg.perp_symbol}-SWAP"
            if not self.cfg.perp_symbol.endswith("-SWAP") else self.cfg.perp_symbol
        )
        if not isinstance(resp, dict) or not resp.get("data"):
            return None
        try:
            return float(resp["data"][0].get("fundingRate") or 0.0)
        except (TypeError, ValueError, IndexError):
            return None

    def fetch_funding_history(self, limit: int = 100) -> List[float]:
        """Fetch up to `limit` recent per-settlement funding rates.

        For seeding the trailing window. OKX returns newest-first; we
        return oldest-first so callers can append to a chronological list.
        """
        resp = self.adapter.api.get_funding_rate_history(
            inst_id=f"{self.cfg.perp_symbol}-SWAP"
            if not self.cfg.perp_symbol.endswith("-SWAP") else self.cfg.perp_symbol,
            limit=min(int(limit), 100),
        )
        if not isinstance(resp, dict) or not resp.get("data"):
            return []
        out: List[float] = []
        for row in reversed(resp["data"]):
            try:
                out.append(float(row.get("fundingRate", "0")))
            except (TypeError, ValueError):
                continue
        return out

    def fetch_account_snapshot(self) -> Optional[Dict[str, Any]]:
        """Private-endpoint probe. None if credentials missing."""
        if not self.have_private_creds:
            return None
        try:
            check = self.adapter.assert_unified_margin()
            snap = self.adapter.get_margin_snapshot(
                perp_inst_id=self.cfg.perp_symbol,
            )
            snap["acct_lv"] = check.get("acct_lv")
            snap["unified_margin_ok"] = check.get("ok")
            snap["unified_margin_msg"] = check.get("message")
            return snap
        except Exception as e:
            logging.exception("account snapshot failed: %s", e)
            return {"errors": [{"step": "snapshot", "error": str(e)}]}

    # ---------- cycle ----------

    def one_cycle(self) -> Dict[str, Any]:
        """Run a single dry-run cycle and return the trade-log entry."""
        state = self.load_state()
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        spot_px = self.fetch_spot_price()
        perp_px = self.fetch_perp_price()
        funding = self.fetch_funding_rate()

        # If first cycle and we have nothing seeded, pull recent funding
        # history to populate the trailing window. We only pull once.
        if not state.funding_samples:
            seed = self.fetch_funding_history(limit=100)
            if seed:
                state.funding_samples.extend(seed)
                state.funding_samples_ts.extend([now_iso] * len(seed))
                # cap to trailing_window_samples
                excess = len(state.funding_samples) - self.cfg.trailing_window_samples
                if excess > 0:
                    state.funding_samples = state.funding_samples[excess:]
                    state.funding_samples_ts = state.funding_samples_ts[excess:]

        # Append the latest tick (deduped on (timestamp, rate)).
        if funding is not None:
            if not state.funding_samples or state.funding_samples[-1] != funding \
                    or not state.funding_samples_ts \
                    or state.funding_samples_ts[-1] != now_iso:
                state.funding_samples.append(float(funding))
                state.funding_samples_ts.append(now_iso)
                excess = len(state.funding_samples) - self.cfg.trailing_window_samples
                if excess > 0:
                    state.funding_samples = state.funding_samples[excess:]
                    state.funding_samples_ts = state.funding_samples_ts[excess:]

        # Green-button decision
        gate = green_button_on(
            state.funding_samples,
            self.cfg.funding_on_threshold_annualised,
            min_samples=1,
        )

        # Target sizing
        target_notional = (
            self.cfg.initial_notional_usd * self.cfg.target_dn_notional_fraction
            if gate["on"] else 0.0
        )
        target = None
        if spot_px and target_notional > 0:
            target = target_position_for(
                notional_usd=target_notional,
                btc_price=spot_px,
                leverage=self.cfg.leverage_cap,
            )

        # Compare to simulated position
        cur_pos = CarryPosition.from_json(state.simulated_position or {})
        drift = None
        if spot_px and perp_px and not cur_pos.is_flat:
            drift = delta_neutral_drift(cur_pos, spot_px, perp_px)

        # Project next 8h funding if currently positioned
        projected = None
        if funding is not None and not cur_pos.is_flat:
            notional = abs(cur_pos.perp_qty) * (spot_px or cur_pos.entry_spot_price)
            projected = projected_next_funding_usd(notional, funding)

        # Decide the "would do" action
        action = {"kind": "noop", "reason": "no_change"}
        if spot_px is None or perp_px is None:
            action = {"kind": "skip", "reason": "missing_market_data"}
        elif not gate["on"] and not cur_pos.is_flat:
            action = {
                "kind": "would_unwind",
                "reason": "green_button_off",
                "spot_sell_qty": cur_pos.spot_qty,
                "perp_buy_qty": -cur_pos.perp_qty,
                "spot_price": spot_px,
                "perp_price": perp_px,
            }
        elif gate["on"] and cur_pos.is_flat and target is not None and target.spot_qty > 0:
            action = {
                "kind": "would_open",
                "reason": "green_button_on_flat_book",
                "spot_buy_qty": target.spot_qty,
                "perp_sell_qty": -target.perp_qty,
                "spot_price": spot_px,
                "perp_price": perp_px,
                "leg_notional_usd": target.notional_usd,
                "perp_margin_usd": target.perp_margin_usd,
            }
        elif gate["on"] and not cur_pos.is_flat and target is not None:
            # rebalance if drift > some threshold
            target_qty = target.spot_qty
            qty_diff = target_qty - cur_pos.spot_qty
            if abs(qty_diff) / max(target_qty, 1e-9) > 0.05:
                action = {
                    "kind": "would_resize",
                    "reason": "drift_from_target",
                    "qty_diff": qty_diff,
                    "current_spot_qty": cur_pos.spot_qty,
                    "target_spot_qty": target_qty,
                }

        # Basis-blowout check
        risk_alerts: List[Dict[str, Any]] = []
        if spot_px and perp_px:
            basis_abs = abs(spot_px - perp_px)
            basis_frac = basis_abs / max(spot_px, 1.0)
            state.last_basis_usd = float(spot_px - perp_px)
            if basis_frac > self.cfg.basis_kill_pct:
                risk_alerts.append({
                    "kind": "basis_blowout",
                    "basis_usd": spot_px - perp_px,
                    "basis_frac": basis_frac,
                    "threshold": self.cfg.basis_kill_pct,
                })

        # Account snapshot (private creds only)
        account = self.fetch_account_snapshot()
        if account:
            state.last_account_check = account
            if account.get("margin_ratio") is not None:
                mr = account["margin_ratio"]
                if mr < self.cfg.margin_ratio_alarm:
                    risk_alerts.append({
                        "kind": "margin_low",
                        "margin_ratio": mr,
                        "threshold": self.cfg.margin_ratio_alarm,
                    })

        # Reconcile self-consistency
        recon = reconcile_carry_state(
            state, self.cfg, account_snapshot=account, now_iso=now_iso,
        )
        state.last_reconcile_ok = recon.ok
        state.last_reconcile_errors = list(recon.errors)

        # Update cached prices/funding for health endpoint
        if spot_px is not None:
            state.last_spot_price = float(spot_px)
        if perp_px is not None:
            state.last_perp_price = float(perp_px)
        if funding is not None:
            state.last_funding_rate = float(funding)

        # Log entry (mirrors plan_e_runner.py's structured trade-log style).
        entry: Dict[str, Any] = {
            "ts": now_iso,
            "instance": self.cfg.instance_name,
            "mode": "dry_run",
            "spot_price": spot_px,
            "perp_price": perp_px,
            "basis_usd": (spot_px - perp_px) if (spot_px and perp_px) else None,
            "funding_rate_8h": funding,
            "funding_rate_annualised": (
                annualize_funding(funding) if funding is not None else None
            ),
            "gate": gate,
            "target_notional_usd": target_notional,
            "target": target.to_json() if target else None,
            "current_position": cur_pos.to_json(),
            "drift": asdict(drift) if drift else None,
            "projected_next_funding_usd": projected,
            "action": action,
            "risk_alerts": risk_alerts,
            "account": account,
            "reconcile": recon.to_json(),
        }
        # In dry-run we DO NOT mutate simulated_position based on action.
        # P2 will: on a paper-fill, update simulated_position to match the
        # paper book. P1 keeps the book flat to maximise the runner's
        # value as a market-data + decision-trace producer.

        state.cycles_total += 1
        state.last_cycle_ts = now_iso

        self.save_state(state)
        self.append_log(entry)
        self.write_health(self.health(state, recon))

        log_msg = (
            f"[{self.cfg.instance_name}] cycle #{state.cycles_total} "
            f"spot={spot_px} perp={perp_px} fund_8h={funding} "
            f"gate={'ON' if gate['on'] else 'OFF'} "
            f"trail_ann={gate['trailing_annualised']:.4%} "
            f"action={action['kind']} alerts={len(risk_alerts)} "
            f"reconcile_ok={recon.ok}"
        )
        logging.info(log_msg)
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

    def health(self, state: CarryRunnerState,
               recon: Optional[CarryReconcileResult] = None) -> Dict[str, Any]:
        """Small dict the dashboard can pick up. No I/O.

        Format chosen to match Plan E's health surface (alive flag,
        last_cycle_ts, key metrics, dry_run state).
        """
        return {
            "alive": True,
            "instance": self.cfg.instance_name,
            "dry_run": self.cfg.dry_run,
            "last_cycle_ts": state.last_cycle_ts,
            "cycles_total": state.cycles_total,
            "last_funding_rate_8h": state.last_funding_rate,
            "last_funding_annualised": annualize_funding(state.last_funding_rate),
            "last_basis_usd": state.last_basis_usd,
            "last_spot_price": state.last_spot_price,
            "last_perp_price": state.last_perp_price,
            "simulated_equity": state.simulated_equity,
            "simulated_position": state.simulated_position,
            "reconcile_ok": (recon.ok if recon else state.last_reconcile_ok),
            "reconcile_errors_count": (
                len(recon.errors) if recon else len(state.last_reconcile_errors)
            ),
            "have_private_creds": self.have_private_creds,
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
    runner = CarryRunner(cfg)

    if args.loop:
        runner.loop(max_cycles=args.max_cycles)
        return 0

    entry = runner.one_cycle()
    print(json.dumps(entry, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
