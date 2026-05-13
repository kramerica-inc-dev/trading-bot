#!/usr/bin/env python3
"""Funding/basis carry runner — P1 dry-run + P2 OKX-demo paper-trade.

Modeled on `plan_e_runner.py`:
  * config-driven (JSON), state in `state/<instance>/`
  * structured JSONL trade log at `state/<instance>/trades.log`
  * periodic cycle: read account+market state, compute target, reconcile

Mode invariant (P1 / P2 / P3 gate). Computed once at startup, logged on
every cycle:

  * dry_run=true                          → DRY_RUN   never places orders (P1)
  * dry_run=false & okx_demo=true         → P2_DEMO   places orders on OKX
                                                       simulated-trading
                                                       (x-simulated-trading: 1)
  * dry_run=false & okx_demo=false & allow_live=true
                                          → P3_LIVE   real-money trades.
                                                       Refuses to run unless
                                                       `notional_per_leg_usd <=
                                                       live_max_usd` AND
                                                       `allow_live=true` in
                                                       config or env.
  * dry_run=false & okx_demo=false & allow_live=false
                                          → REJECTED  startup fails.

Per the 2026-05-13 DECISIONS.md entry, this is the first phase of the
carry build that actually places orders. P3 stays gated on an extra
`allow_live=true` flag so we can't accidentally trip into real money
during a P2 deploy.

Green-button rule (also from DECISIONS.md):
    if trailing_90d_annualised_funding > +5%/yr  →  ON, target_notional > 0
    else                                          →  OFF, target_notional = 0

Risk controls (binding, all wired live in P2):
    * legging window <`legging_window_sec` (default 5s) on open & unwind;
      on leg-2 failure, leg-1 is flattened immediately.
    * basis-kill: |basis|/spot > basis_kill_pct → flatten + halt.
    * manual halt sentinel `state/<instance>/halt` — flatten if present.
    * leverage cap verified live at startup via /account/config.
    * live fee schedule pulled at startup; falls back to constants.

Usage:
    # market-data-only (no credentials):
    python3 -m scripts.carry_runner --config configs/carry.json --once

    # P2 demo paper-trade with credentials (OKX simulated trading):
    OKX_API_KEY=... OKX_API_SECRET=... OKX_API_PASSPHRASE=... \\
        python3 -m scripts.carry_runner --config configs/carry-btc.json --loop
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
from typing import Any, Dict, List, Optional, Tuple

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

    Lives end-to-end in this file so the runner is reviewable in one
    pass. All risk parameters are read from here; no hard-coded values.
    """
    instance_name: str = "carry"
    # exchange + symbol
    exchange: str = "okx"                  # only "okx" supported
    spot_symbol: str = "BTC-USDT"          # OKX spot
    perp_symbol: str = "BTC-USDT"          # passed to OkxAdapter — gets -SWAP
    # sizing
    initial_notional_usd: float = 5000.0   # book size (projection / live cap basis)
    target_dn_notional_fraction: float = 0.6   # fraction of book deployed delta-neutral
    leverage_cap: float = 2.0              # perp short leverage cap (≤2× per spec §6)
    # green-button gate
    funding_on_threshold_annualised: float = 0.05   # +5%/yr per DECISIONS 2026-05-13
    trailing_window_samples: int = 270     # ~90d of 8h settlements
    # risk controls
    basis_kill_pct: float = 0.01           # flatten if |basis|/spot > 1%
    margin_ratio_alarm: float = 1.5        # warn if account margin_ratio < this
    legging_window_sec: int = 5            # max wait between leg 1 and leg 2 fills
    # cycle
    cycle_interval_sec: int = 60           # paper cadence — health every minute
    # mode gates (see module docstring for the truth table)
    dry_run: bool = True                   # P1 safe default
    okx_demo: bool = False                 # P2 toggle; needs dry_run=false
    allow_live: bool = False               # P3 explicit unlock
    live_max_usd: float = 1000.0           # P3 per-leg notional ceiling
    # rebalance trigger
    rebalance_threshold_pct: float = 0.05  # resize when current/target drifts >5%
    # OKX base URL override (EU region or testnet)
    okx_base_url: Optional[str] = None


def load_config(path: Optional[str]) -> CarryRunnerConfig:
    if not path:
        return CarryRunnerConfig()
    with open(path) as f:
        data = json.load(f)
    known = set(CarryRunnerConfig.__dataclass_fields__.keys())
    # Strip comment keys (we use `_*_comment` for human notes in JSON).
    data = {k: v for k, v in data.items() if not k.startswith("_")}
    unknown = set(data.keys()) - known
    if unknown:
        logging.warning("unknown config keys ignored: %s", sorted(unknown))
    return CarryRunnerConfig(**{k: v for k, v in data.items() if k in known})


# =========================  Mode resolution  =========================

# Mode codes used everywhere (logs, state, tests).
MODE_DRY = "DRY_RUN"
MODE_P2 = "P2_DEMO"
MODE_P3 = "P3_LIVE"


def resolve_mode(cfg: CarryRunnerConfig) -> str:
    """Resolve the runner's execution mode from the config flags.

    Pure function — no environment, no I/O. Raises `RuntimeError` for
    any unsupported / unsafe combination so the runner fails closed.
    """
    if cfg.dry_run:
        return MODE_DRY
    if cfg.okx_demo:
        return MODE_P2
    # dry_run=false AND okx_demo=false → live attempt
    if not cfg.allow_live:
        raise RuntimeError(
            "Refusing to run live: dry_run=false + okx_demo=false requires "
            "allow_live=true in config (P3 gate). Set dry_run=true (DRY_RUN), "
            "okx_demo=true (P2_DEMO), or explicitly allow_live=true (P3_LIVE)."
        )
    # P3 sizing guard. Computed against per-leg notional, which equals
    # target_dn_notional_fraction × initial_notional_usd.
    per_leg = cfg.initial_notional_usd * cfg.target_dn_notional_fraction
    if per_leg > cfg.live_max_usd:
        raise RuntimeError(
            f"P3 sizing guard tripped: per-leg notional ${per_leg:.2f} > "
            f"live_max_usd ${cfg.live_max_usd:.2f}. Lower "
            "initial_notional_usd or target_dn_notional_fraction, or raise "
            "live_max_usd intentionally."
        )
    return MODE_P3


# =========================  Persistent runner state  =========================

@dataclass
class CarryRunnerState:
    """Persistent state in `state/<instance>/state.json`."""
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
    # P2 state
    halted: bool = False                   # set on basis-kill trip
    halt_reason: Optional[str] = None
    last_mode: Optional[str] = None
    last_basis_kill_ts: Optional[str] = None
    legging_aborts_total: int = 0
    # Persisted dry-run flag - sanity check on reload (informational).
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


# =========================  Reconciliation  =========================

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
    mode: str = MODE_DRY,
) -> CarryReconcileResult:
    """Self-consistency + (if account_snapshot given) exchange-vs-runner check.

    Rules:
      C1: simulated_position fields are finite numerics.
      C2: spot_qty >= 0, perp_qty <= 0 (carry sign convention).
      C3: |spot_qty + perp_qty| within tolerance.
      C4: dry_run flag consistent between state and cfg.
      C5: exchange vs simulated drift (P2+: tight tolerance; P1: tripwire only).
      C6: account_snapshot.acct_lv ≥ 3 (warn).
      C7: spot balance & perp position direction match the simulated book.
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

    # C3 — internal book drift tolerance
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

    # C5 / C6 / C7 — only when an account snapshot is available.
    if account_snapshot:
        # C6: unified margin check
        res.rules_evaluated += 1
        acct_lv = account_snapshot.get("acct_lv")
        if acct_lv is not None and acct_lv < 3:
            res.add_warning(
                "C6",
                f"account acctLv={acct_lv} (need ≥3 for unified margin)",
            )

        # C5 / C7: exchange-vs-runner.
        # In DRY mode, any live position is a tripwire.
        # In P2/P3 mode, the live perp size must match the simulated book
        # within tolerance; the live spot balance must be ≥ simulated spot
        # (loose-bound because spot may hold non-carry coins too).
        live_perp = account_snapshot.get("short_perp_qty")
        live_spot = account_snapshot.get("spot_btc_qty")

        if mode == MODE_DRY:
            res.rules_evaluated += 1
            if live_perp is not None and abs(float(live_perp)) > 1e-9:
                res.add_warning(
                    "C5",
                    f"exchange has live perp position ({live_perp} BTC); "
                    "DRY_RUN expects no live trades",
                )
        else:
            # Live mode — tight tolerance against simulated.
            res.rules_evaluated += 1
            tol_btc = max(1e-6, 0.001 * abs(perp_q))  # 0.1% of size, floor 1e-6
            if live_perp is not None:
                diff = float(live_perp) - perp_q
                if abs(diff) > tol_btc:
                    res.add_error(
                        "C5",
                        f"perp drift exchange={live_perp} vs simulated="
                        f"{perp_q} (diff={diff:+.9f}, tol={tol_btc:.9f})",
                    )
            res.rules_evaluated += 1
            if live_spot is not None and spot_q > 0:
                if float(live_spot) + tol_btc < spot_q:
                    res.add_error(
                        "C7",
                        f"spot balance exchange={live_spot} < simulated "
                        f"{spot_q} (carry leg under-collateralized)",
                    )
    return res


# =========================  Fee schedule pull  =========================

# Fallback constants — only used if the live fee pull fails. These match
# the assumed-fees in `docs/STRATEGY-CARRY.md` §2.2.
FALLBACK_FEES = {
    "spot_maker": 0.0002, "spot_taker": 0.0005,
    "perp_maker": 0.0002, "perp_taker": 0.0005,
}


def pull_live_fees(adapter: Any, *, spot_inst: str, perp_inst: str) -> Dict[str, Any]:
    """Best-effort pull of per-instrument fee rates from OKX.

    OKX exposes `/api/v5/account/trade-fee` which returns maker/taker
    rates per instrument. We probe both legs and return a flat dict;
    on any failure we substitute the FALLBACK_FEES value and tag the
    source so the runner logs both the live and fallback values.
    """
    out: Dict[str, Any] = {
        "spot_maker": None, "spot_taker": None,
        "perp_maker": None, "perp_taker": None,
        "sources": {},
        "errors": [],
    }
    api = getattr(adapter, "api", None)
    if api is None or not hasattr(api, "_request"):
        # Stub adapter (tests) — return fallback.
        for k, v in FALLBACK_FEES.items():
            out[k] = v
            out["sources"][k] = "fallback"
        return out

    def _probe(inst_type: str, inst_id: str) -> Tuple[Optional[float], Optional[float], Optional[str]]:
        try:
            resp = api._request(
                "GET", "/api/v5/account/trade-fee",
                params={"instType": inst_type, "instId": inst_id},
                auth=True,
            )
            if not isinstance(resp, dict) or not resp.get("data"):
                return None, None, f"empty: {resp.get('msg', 'no data')}"
            row = resp["data"][0]
            # OKX returns negative numbers as taker/maker rates (rebate side
            # negative). We take abs() since we just want the magnitude.
            mk = row.get("maker")
            tk = row.get("taker")
            return (
                abs(float(mk)) if mk not in (None, "") else None,
                abs(float(tk)) if tk not in (None, "") else None,
                None,
            )
        except Exception as e:  # pragma: no cover
            return None, None, str(e)

    spot_mk, spot_tk, spot_err = _probe("SPOT", spot_inst)
    if spot_err:
        out["errors"].append({"leg": "spot", "error": spot_err})
    out["spot_maker"] = spot_mk if spot_mk is not None else FALLBACK_FEES["spot_maker"]
    out["spot_taker"] = spot_tk if spot_tk is not None else FALLBACK_FEES["spot_taker"]
    out["sources"]["spot_maker"] = "live" if spot_mk is not None else "fallback"
    out["sources"]["spot_taker"] = "live" if spot_tk is not None else "fallback"

    perp_inst_full = perp_inst if perp_inst.endswith("-SWAP") else f"{perp_inst}-SWAP"
    perp_mk, perp_tk, perp_err = _probe("SWAP", perp_inst_full)
    if perp_err:
        out["errors"].append({"leg": "perp", "error": perp_err})
    out["perp_maker"] = perp_mk if perp_mk is not None else FALLBACK_FEES["perp_maker"]
    out["perp_taker"] = perp_tk if perp_tk is not None else FALLBACK_FEES["perp_taker"]
    out["sources"]["perp_maker"] = "live" if perp_mk is not None else "fallback"
    out["sources"]["perp_taker"] = "live" if perp_tk is not None else "fallback"

    return out


# =========================  Leverage verification  =========================

def verify_leverage_cap(
    adapter: Any, *, configured_cap: float, perp_inst: str,
) -> Dict[str, Any]:
    """Pull effective max leverage on the account+instrument from OKX.

    Two relevant fields:
      * /api/v5/account/leverage-info?instId=<perp> → effective lever for
        the account on that instrument.
      * /api/v5/public/instruments?instType=SWAP&instId=<perp> → maxLever
        for the *contract* (account-agnostic).
    We log both. The cap that binds is min(account_max, contract_max).

    Returns:
        {
          "configured_cap": float,
          "account_max": Optional[float],
          "contract_max": Optional[float],
          "effective_max": Optional[float],
          "ok": bool,         # True iff effective_max >= configured_cap
          "message": str,
          "errors": [...],
        }
    """
    out: Dict[str, Any] = {
        "configured_cap": float(configured_cap),
        "account_max": None,
        "contract_max": None,
        "effective_max": None,
        "ok": False,
        "message": "",
        "errors": [],
    }
    api = getattr(adapter, "api", None)
    if api is None or not hasattr(api, "_request"):
        out["message"] = "no live adapter — skipping leverage check"
        return out

    perp_inst_full = perp_inst if perp_inst.endswith("-SWAP") else f"{perp_inst}-SWAP"

    # 1. Account leverage info (requires auth).
    try:
        resp = api._request(
            "GET", "/api/v5/account/leverage-info",
            params={"instId": perp_inst_full, "mgnMode": "isolated"},
            auth=True,
        )
        if isinstance(resp, dict) and resp.get("data"):
            lv = resp["data"][0].get("lever")
            if lv:
                out["account_max"] = float(lv)
        else:
            out["errors"].append({"step": "account_leverage",
                                  "error": resp.get("msg") if isinstance(resp, dict) else "no data"})
    except Exception as e:  # pragma: no cover
        out["errors"].append({"step": "account_leverage", "error": str(e)})

    # 2. Contract max leverage (public).
    try:
        resp = api._request(
            "GET", "/api/v5/public/instruments",
            params={"instType": "SWAP", "instId": perp_inst_full},
            auth=False,
        )
        if isinstance(resp, dict) and resp.get("data"):
            lv = resp["data"][0].get("lever")
            if lv:
                out["contract_max"] = float(lv)
        else:
            out["errors"].append({"step": "contract_leverage",
                                  "error": resp.get("msg") if isinstance(resp, dict) else "no data"})
    except Exception as e:  # pragma: no cover
        out["errors"].append({"step": "contract_leverage", "error": str(e)})

    # Effective.
    candidates = [v for v in (out["account_max"], out["contract_max"]) if v is not None]
    if candidates:
        out["effective_max"] = min(candidates)
        if out["effective_max"] >= configured_cap:
            out["ok"] = True
            out["message"] = (
                f"leverage cap OK: configured={configured_cap}×, "
                f"effective_max={out['effective_max']}× "
                f"(account={out['account_max']}, contract={out['contract_max']})"
            )
        else:
            out["message"] = (
                f"leverage cap MISMATCH: configured={configured_cap}× but "
                f"effective_max={out['effective_max']}× "
                f"(account={out['account_max']}, contract={out['contract_max']}). "
                "Lower leverage_cap in the config to match the exchange's cap."
            )
    else:
        out["message"] = (
            f"could not read leverage info — keeping configured cap "
            f"{configured_cap}× as assumed (errors: {len(out['errors'])})"
        )
        # No data ≠ block startup — log and continue. Strict mode could be
        # added if a future deploy requires it.
        out["ok"] = True
    return out


# =========================  Order placement (P2/P3)  =========================

class LeggingAbort(Exception):
    """Raised when leg 2 fails to fill in time; caller is responsible for
    flattening leg 1 (the runner does that, then re-raises for the caller
    to log a `legging_abort` event)."""


def _is_filled_state(state: Optional[str]) -> bool:
    """OKX order states that mean "no more fills coming, position settled"."""
    return state in ("filled", "canceled", "rejected")


def _filled_qty(order_detail: Dict[str, Any]) -> float:
    """Best-effort filled-quantity parser from OKX order-detail data."""
    if not isinstance(order_detail, dict):
        return 0.0
    data = order_detail.get("data") or []
    if not data:
        return 0.0
    row = data[0] if isinstance(data, list) else data
    try:
        return float(row.get("accFillSz") or row.get("fillSz") or "0")
    except (TypeError, ValueError):
        return 0.0


def _order_state(order_detail: Dict[str, Any]) -> Optional[str]:
    if not isinstance(order_detail, dict):
        return None
    data = order_detail.get("data") or []
    if not data:
        return None
    row = data[0] if isinstance(data, list) else data
    return row.get("state")


# =========================  Runner  =========================

class CarryRunner:
    """Carry runner with three modes (DRY_RUN / P2_DEMO / P3_LIVE).

    The execution mode is resolved once at __init__ from the config and
    logged on every cycle. It is the single source of truth for whether
    `place_spot_order` / `place_order` are allowed to be invoked.
    """

    def __init__(
        self,
        cfg: CarryRunnerConfig,
        state_dir: Optional[Path] = None,
    ) -> None:
        # Resolve mode — raises RuntimeError on unsafe combinations.
        self.mode = resolve_mode(cfg)

        self.cfg = cfg
        # State namespace: nest under `state/carry/<instance>/` so carry
        # instances don't collide with Plan E's `state/plan-e-*/` layout.
        # `state_dir` (test override) still wins as-is when provided.
        if state_dir is not None:
            instance_dir = state_dir / cfg.instance_name
        else:
            instance_dir = PROJECT_ROOT / "state" / "carry" / cfg.instance_name
        instance_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = instance_dir / "state.json"
        self.log_path = instance_dir / "trades.log"
        self.health_path = instance_dir / "health.json"
        self.halt_sentinel = instance_dir / "halt"

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

        # Live fee + leverage probes (lazy: filled on first cycle in modes
        # that need credentials).
        self.fees: Optional[Dict[str, Any]] = None
        self.leverage_check: Optional[Dict[str, Any]] = None
        self._startup_probes_done = False

        logging.info(
            "CarryRunner instance=%s mode=%s exchange=%s spot=%s perp=%s "
            "private_creds=%s dry_run=%s okx_demo=%s allow_live=%s",
            cfg.instance_name, self.mode, cfg.exchange,
            cfg.spot_symbol, cfg.perp_symbol,
            self.have_private_creds, cfg.dry_run, cfg.okx_demo, cfg.allow_live,
        )

        if self.mode != MODE_DRY and not self.have_private_creds:
            logging.error(
                "[%s] mode=%s requires OKX API credentials in env "
                "(OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE). "
                "Without them the runner cannot place orders or fetch the "
                "account snapshot; cycles will skip the order leg.",
                cfg.instance_name, self.mode,
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

    # ---------- startup probes ----------

    def run_startup_probes(self) -> None:
        """One-shot live probes for fee schedule + leverage cap.

        Idempotent: if already run, no-op.  Called from `one_cycle` so it
        happens the first time *with* credentials — useful because the
        env-file may not be in place when the service first starts.
        """
        if self._startup_probes_done:
            return
        if self.mode == MODE_DRY and not self.have_private_creds:
            # Pure DRY without creds → use fallback constants, mark done.
            self.fees = {**FALLBACK_FEES, "sources": {k: "fallback" for k in FALLBACK_FEES}}
            self.leverage_check = {
                "configured_cap": self.cfg.leverage_cap,
                "ok": True,
                "message": "DRY_RUN without credentials — using configured cap as-is",
                "account_max": None, "contract_max": None,
                "effective_max": None, "errors": [],
            }
            self._startup_probes_done = True
            return

        if not self.have_private_creds:
            logging.warning(
                "[%s] startup probes skipped: no credentials. The runner "
                "will retry on the next cycle.",
                self.cfg.instance_name,
            )
            return

        self.fees = pull_live_fees(
            self.adapter,
            spot_inst=self.cfg.spot_symbol,
            perp_inst=self.cfg.perp_symbol,
        )
        self.leverage_check = verify_leverage_cap(
            self.adapter,
            configured_cap=self.cfg.leverage_cap,
            perp_inst=self.cfg.perp_symbol,
        )

        logging.info(
            "[%s] FEES spot_maker=%s spot_taker=%s perp_maker=%s perp_taker=%s "
            "(sources=%s)",
            self.cfg.instance_name,
            self.fees.get("spot_maker"), self.fees.get("spot_taker"),
            self.fees.get("perp_maker"), self.fees.get("perp_taker"),
            self.fees.get("sources"),
        )
        logging.info(
            "[%s] LEVERAGE %s",
            self.cfg.instance_name, self.leverage_check.get("message"),
        )
        if not self.leverage_check.get("ok"):
            raise RuntimeError(self.leverage_check.get("message")
                               or "leverage cap verification failed")
        self._startup_probes_done = True

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

    # ---------- order placement primitives (P2/P3) ----------

    def _place_spot_market(self, side: str, qty_btc: float) -> Dict[str, Any]:
        """Place a spot market order.

        Note: OKX spot market BUY size is by default in *quote* currency
        (USDT). We always pass `target_currency="base_ccy"` so `size` is
        BTC regardless of side. This keeps both legs comparable.
        """
        return self.adapter.place_spot_order(
            inst_id=self.cfg.spot_symbol,
            side=side,
            order_type="market",
            size=f"{qty_btc:.8f}",
            td_mode="cash",
            target_currency="base_ccy",
        )

    def _place_perp_market(self, side: str, qty_btc: float,
                          reduce_only: bool = False) -> Dict[str, Any]:
        """Place a perp swap market order.

        Sign convention: `side='sell'` opens a short for the carry; `'buy'`
        closes it (or opens a long, which the carry never does).
        """
        kwargs: Dict[str, Any] = {
            "inst_id": self.cfg.perp_symbol,
            "side": side,
            "order_type": "market",
            "size": f"{qty_btc:.8f}",
            "margin_mode": "isolated",
        }
        if reduce_only:
            kwargs["reduce_only"] = True
        return self.adapter.place_order(**kwargs)

    def _wait_for_fill(self, inst_id: str, order_id: str,
                       *, is_spot: bool, timeout_sec: int) -> Tuple[bool, Dict[str, Any]]:
        """Poll order-detail until filled or timeout. Returns (ok, last_detail)."""
        deadline = time.monotonic() + max(1, timeout_sec)
        last_detail: Dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                if is_spot:
                    detail = self.adapter.get_spot_order_detail(inst_id, order_id=order_id)
                else:
                    detail = self.adapter.get_order_detail(inst_id, order_id=order_id)
            except Exception as e:
                logging.warning("order-detail probe failed: %s", e)
                detail = {}
            last_detail = detail
            st = _order_state(detail)
            if st == "filled":
                return True, detail
            if st in ("canceled", "rejected"):
                return False, detail
            time.sleep(0.5)
        return False, last_detail

    def _extract_order_id(self, resp: Dict[str, Any]) -> Optional[str]:
        if not isinstance(resp, dict):
            return None
        if resp.get("code") not in ("0", 0, None):
            return None
        data = resp.get("data") or []
        if not data:
            return None
        try:
            return str(data[0].get("ordId"))
        except (AttributeError, IndexError):
            return None

    def open_carry(self, qty_btc: float, *, now_iso: str) -> Dict[str, Any]:
        """Open a carry pair (long spot + short perp) with legging protection.

        Strategy:
          1. Spot buy first (spot books are deeper, fills more reliably).
          2. Wait up to legging_window_sec for the spot fill.
          3. Perp sell. Wait up to legging_window_sec for the perp fill.
          4. If perp fails → market-sell the spot leg immediately (flatten),
             log `legging_abort`.

        Returns a dict with: ok, legs (list of leg result dicts), reason.
        """
        result: Dict[str, Any] = {"ok": False, "legs": [], "reason": None,
                                  "qty_btc": qty_btc, "ts": now_iso}
        # ---- leg 1: spot buy
        resp1 = self._place_spot_market("buy", qty_btc)
        oid1 = self._extract_order_id(resp1)
        if not oid1:
            result["reason"] = "spot_open_reject"
            result["legs"].append({"leg": "spot_buy", "ok": False, "resp": resp1})
            return result
        ok1, det1 = self._wait_for_fill(
            self.cfg.spot_symbol, oid1, is_spot=True,
            timeout_sec=self.cfg.legging_window_sec,
        )
        result["legs"].append({
            "leg": "spot_buy", "ok": ok1, "ord_id": oid1,
            "filled_qty": _filled_qty(det1), "state": _order_state(det1),
        })
        if not ok1:
            result["reason"] = "spot_open_did_not_fill"
            return result

        # ---- leg 2: perp sell (short)
        resp2 = self._place_perp_market("sell", qty_btc)
        oid2 = self._extract_order_id(resp2)
        ok2 = False
        det2: Dict[str, Any] = {}
        if oid2:
            ok2, det2 = self._wait_for_fill(
                self.cfg.perp_symbol, oid2, is_spot=False,
                timeout_sec=self.cfg.legging_window_sec,
            )
        result["legs"].append({
            "leg": "perp_sell", "ok": ok2, "ord_id": oid2,
            "filled_qty": _filled_qty(det2), "state": _order_state(det2),
            "raw_resp_if_no_oid": None if oid2 else resp2,
        })
        if not ok2:
            # Leg-2 failed → flatten leg 1 immediately.
            flatten = self._place_spot_market("sell", qty_btc)
            result["legs"].append({
                "leg": "spot_flatten", "ok": True,
                "ord_id": self._extract_order_id(flatten),
                "raw_resp": flatten,
            })
            result["reason"] = "legging_abort_perp_failed"
            return result

        result["ok"] = True
        result["reason"] = "filled"
        return result

    def unwind_carry(self, qty_btc: float, *, now_iso: str) -> Dict[str, Any]:
        """Unwind a carry pair (sell spot + buy perp to close)."""
        result: Dict[str, Any] = {"ok": False, "legs": [], "reason": None,
                                  "qty_btc": qty_btc, "ts": now_iso}

        # ---- leg 1: spot sell
        resp1 = self._place_spot_market("sell", qty_btc)
        oid1 = self._extract_order_id(resp1)
        if not oid1:
            result["reason"] = "spot_close_reject"
            result["legs"].append({"leg": "spot_sell", "ok": False, "resp": resp1})
            return result
        ok1, det1 = self._wait_for_fill(
            self.cfg.spot_symbol, oid1, is_spot=True,
            timeout_sec=self.cfg.legging_window_sec,
        )
        result["legs"].append({
            "leg": "spot_sell", "ok": ok1, "ord_id": oid1,
            "filled_qty": _filled_qty(det1), "state": _order_state(det1),
        })
        if not ok1:
            result["reason"] = "spot_close_did_not_fill"
            return result

        # ---- leg 2: perp buy to close
        resp2 = self._place_perp_market("buy", qty_btc, reduce_only=True)
        oid2 = self._extract_order_id(resp2)
        ok2 = False
        det2: Dict[str, Any] = {}
        if oid2:
            ok2, det2 = self._wait_for_fill(
                self.cfg.perp_symbol, oid2, is_spot=False,
                timeout_sec=self.cfg.legging_window_sec,
            )
        result["legs"].append({
            "leg": "perp_buy", "ok": ok2, "ord_id": oid2,
            "filled_qty": _filled_qty(det2), "state": _order_state(det2),
            "raw_resp_if_no_oid": None if oid2 else resp2,
        })
        if not ok2:
            # Leg-2 failed on UNWIND. Re-buy spot to restore the pair.
            relist = self._place_spot_market("buy", qty_btc)
            result["legs"].append({
                "leg": "spot_relist", "ok": True,
                "ord_id": self._extract_order_id(relist),
                "raw_resp": relist,
            })
            result["reason"] = "legging_abort_perp_close_failed"
            return result

        result["ok"] = True
        result["reason"] = "filled"
        return result

    # ---------- order-aware position mutations ----------

    def _apply_open_result(
        self, state: CarryRunnerState, *, qty_btc: float,
        spot_px: float, perp_px: float, now_iso: str,
    ) -> None:
        """Update simulated_position to reflect a successful open."""
        state.simulated_position = asdict(CarryPosition(
            spot_qty=qty_btc, perp_qty=-qty_btc,
            entry_spot_price=spot_px, entry_perp_price=perp_px,
            opened_ts=now_iso, last_updated_ts=now_iso,
        ))

    def _apply_unwind_result(
        self, state: CarryRunnerState, *, now_iso: str,
    ) -> None:
        """Clear simulated_position after a successful unwind."""
        state.simulated_position = asdict(CarryPosition(
            last_updated_ts=now_iso,
        ))

    # ---------- halt management ----------

    def _check_manual_halt(self) -> bool:
        """True if the operator dropped a `halt` sentinel file."""
        return self.halt_sentinel.exists()

    def _trip_halt(self, state: CarryRunnerState, reason: str, now_iso: str) -> None:
        state.halted = True
        state.halt_reason = reason
        state.last_basis_kill_ts = now_iso

    # ---------- cycle ----------

    def one_cycle(self) -> Dict[str, Any]:
        """Run a single cycle. Returns the trade-log entry."""
        # Lazy startup probes (catches the case where the env-file is added
        # after first service start).
        try:
            self.run_startup_probes()
        except Exception as e:
            logging.exception("startup probes failed: %s", e)
            # On leverage MISMATCH we want to refuse to trade. Persist a halt.
            state = self.load_state()
            now_iso = datetime.now(timezone.utc).isoformat()
            self._trip_halt(state, f"startup_probe_failure: {e}", now_iso)
            self.save_state(state)
            self.append_log({
                "ts": now_iso, "instance": self.cfg.instance_name,
                "mode": self.mode, "action": {"kind": "startup_halt", "reason": str(e)},
            })
            raise

        state = self.load_state()
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        spot_px = self.fetch_spot_price()
        perp_px = self.fetch_perp_price()
        funding = self.fetch_funding_rate()

        # Seed funding history on first cycle.
        if not state.funding_samples:
            seed = self.fetch_funding_history(limit=100)
            if seed:
                state.funding_samples.extend(seed)
                state.funding_samples_ts.extend([now_iso] * len(seed))
                excess = len(state.funding_samples) - self.cfg.trailing_window_samples
                if excess > 0:
                    state.funding_samples = state.funding_samples[excess:]
                    state.funding_samples_ts = state.funding_samples_ts[excess:]

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

        cur_pos = CarryPosition.from_json(state.simulated_position or {})
        drift = None
        if spot_px and perp_px and not cur_pos.is_flat:
            drift = delta_neutral_drift(cur_pos, spot_px, perp_px)

        projected = None
        if funding is not None and not cur_pos.is_flat:
            notional = abs(cur_pos.perp_qty) * (spot_px or cur_pos.entry_spot_price)
            projected = projected_next_funding_usd(notional, funding)

        # ---- Risk gates BEFORE deciding the action ----
        risk_alerts: List[Dict[str, Any]] = []
        basis_kill_trip = False
        basis_frac = 0.0
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
                if not cur_pos.is_flat:
                    basis_kill_trip = True

        manual_halt = self._check_manual_halt()

        # Account snapshot
        account = self.fetch_account_snapshot()
        if account:
            state.last_account_check = account
            mr = account.get("margin_ratio")
            if mr is not None and mr < self.cfg.margin_ratio_alarm:
                risk_alerts.append({
                    "kind": "margin_low",
                    "margin_ratio": mr,
                    "threshold": self.cfg.margin_ratio_alarm,
                })

        # ---- Action decision ----
        action: Dict[str, Any] = {"kind": "noop", "reason": "no_change"}
        order_result: Optional[Dict[str, Any]] = None

        # In order of priority:
        # 1. Manual halt — flatten if positioned, then noop.
        # 2. Basis kill — flatten + halt.
        # 3. Existing halted flag (sticky) — noop unless flat.
        # 4. Green-button OFF + positioned → unwind.
        # 5. Green-button ON + flat → open.
        # 6. Green-button ON + positioned + drift → resize (P2: skipped, log only).

        if spot_px is None or perp_px is None:
            action = {"kind": "skip", "reason": "missing_market_data"}
        elif manual_halt:
            if not cur_pos.is_flat:
                action = {
                    "kind": "do_unwind" if self.mode != MODE_DRY else "would_unwind",
                    "reason": "manual_halt_with_open_position",
                    "spot_sell_qty": cur_pos.spot_qty,
                    "perp_buy_qty": -cur_pos.perp_qty,
                }
                if self.mode != MODE_DRY:
                    order_result = self.unwind_carry(cur_pos.spot_qty, now_iso=now_iso)
                    if order_result["ok"]:
                        self._apply_unwind_result(state, now_iso=now_iso)
                    else:
                        state.legging_aborts_total += 1
            else:
                action = {"kind": "noop", "reason": "manual_halt_flat"}
        elif basis_kill_trip:
            action = {
                "kind": "do_unwind" if self.mode != MODE_DRY else "would_unwind",
                "reason": "basis_blowout_kill",
                "basis_frac": basis_frac,
                "spot_sell_qty": cur_pos.spot_qty,
                "perp_buy_qty": -cur_pos.perp_qty,
            }
            if self.mode != MODE_DRY:
                order_result = self.unwind_carry(cur_pos.spot_qty, now_iso=now_iso)
                if order_result["ok"]:
                    self._apply_unwind_result(state, now_iso=now_iso)
                else:
                    state.legging_aborts_total += 1
            self._trip_halt(state, "basis_blowout", now_iso)
        elif state.halted:
            action = {
                "kind": "noop",
                "reason": f"halted: {state.halt_reason or 'unknown'}; clear "
                          f"`{self.halt_sentinel.parent}/halted` flag to resume",
            }
        elif not gate["on"] and not cur_pos.is_flat:
            action = {
                "kind": "do_unwind" if self.mode != MODE_DRY else "would_unwind",
                "reason": "green_button_off",
                "spot_sell_qty": cur_pos.spot_qty,
                "perp_buy_qty": -cur_pos.perp_qty,
                "spot_price": spot_px, "perp_price": perp_px,
            }
            if self.mode != MODE_DRY:
                order_result = self.unwind_carry(cur_pos.spot_qty, now_iso=now_iso)
                if order_result["ok"]:
                    self._apply_unwind_result(state, now_iso=now_iso)
                else:
                    state.legging_aborts_total += 1
        elif gate["on"] and cur_pos.is_flat and target is not None and target.spot_qty > 0:
            action = {
                "kind": "do_open" if self.mode != MODE_DRY else "would_open",
                "reason": "green_button_on_flat_book",
                "spot_buy_qty": target.spot_qty,
                "perp_sell_qty": -target.perp_qty,
                "spot_price": spot_px, "perp_price": perp_px,
                "leg_notional_usd": target.notional_usd,
                "perp_margin_usd": target.perp_margin_usd,
            }
            if self.mode != MODE_DRY:
                order_result = self.open_carry(target.spot_qty, now_iso=now_iso)
                if order_result["ok"]:
                    self._apply_open_result(
                        state, qty_btc=target.spot_qty,
                        spot_px=spot_px, perp_px=perp_px, now_iso=now_iso,
                    )
                else:
                    state.legging_aborts_total += 1
        elif gate["on"] and not cur_pos.is_flat and target is not None:
            target_qty = target.spot_qty
            qty_diff = target_qty - cur_pos.spot_qty
            if abs(qty_diff) / max(target_qty, 1e-9) > self.cfg.rebalance_threshold_pct:
                # P2 ships open + unwind only. Resize is logged but deferred
                # to a later phase (rebalance via close-then-reopen if needed
                # — the slow on/off gate in v2 will trigger that naturally
                # via OFF→ON transitions).
                action = {
                    "kind": "would_resize",
                    "reason": "drift_from_target_p2_no_resize",
                    "qty_diff": qty_diff,
                    "current_spot_qty": cur_pos.spot_qty,
                    "target_spot_qty": target_qty,
                }

        # Reconcile (uses fresh state in case we just unwound/opened).
        recon = reconcile_carry_state(
            state, self.cfg, account_snapshot=account, now_iso=now_iso,
            mode=self.mode,
        )
        state.last_reconcile_ok = recon.ok
        state.last_reconcile_errors = list(recon.errors)

        if spot_px is not None:
            state.last_spot_price = float(spot_px)
        if perp_px is not None:
            state.last_perp_price = float(perp_px)
        if funding is not None:
            state.last_funding_rate = float(funding)
        state.last_mode = self.mode

        entry: Dict[str, Any] = {
            "ts": now_iso,
            "instance": self.cfg.instance_name,
            "mode": self.mode,
            "dry_run": self.cfg.dry_run,
            "okx_demo": self.cfg.okx_demo,
            "allow_live": self.cfg.allow_live,
            "spot_price": spot_px,
            "perp_price": perp_px,
            "basis_usd": (spot_px - perp_px) if (spot_px and perp_px) else None,
            "basis_frac": basis_frac if (spot_px and perp_px) else None,
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
            "order_result": order_result,
            "risk_alerts": risk_alerts,
            "manual_halt": manual_halt,
            "halted": state.halted,
            "halt_reason": state.halt_reason,
            "account": account,
            "fees": self.fees,
            "leverage_check": (
                {k: v for k, v in self.leverage_check.items() if k != "errors"}
                if self.leverage_check else None
            ),
            "reconcile": recon.to_json(),
        }

        state.cycles_total += 1
        state.last_cycle_ts = now_iso

        self.save_state(state)
        self.append_log(entry)
        self.write_health(self.health(state, recon))

        log_msg = (
            f"[{self.cfg.instance_name}] cycle #{state.cycles_total} "
            f"mode={self.mode} "
            f"spot={spot_px} perp={perp_px} fund_8h={funding} "
            f"gate={'ON' if gate['on'] else 'OFF'} "
            f"trail_ann={gate['trailing_annualised']:.4%} "
            f"action={action['kind']} alerts={len(risk_alerts)} "
            f"halted={state.halted} reconcile_ok={recon.ok}"
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
        return {
            "alive": True,
            "instance": self.cfg.instance_name,
            "mode": self.mode,
            "dry_run": self.cfg.dry_run,
            "okx_demo": self.cfg.okx_demo,
            "allow_live": self.cfg.allow_live,
            "halted": state.halted,
            "halt_reason": state.halt_reason,
            "last_cycle_ts": state.last_cycle_ts,
            "cycles_total": state.cycles_total,
            "last_funding_rate_8h": state.last_funding_rate,
            "last_funding_annualised": annualize_funding(state.last_funding_rate),
            "last_basis_usd": state.last_basis_usd,
            "last_spot_price": state.last_spot_price,
            "last_perp_price": state.last_perp_price,
            "simulated_equity": state.simulated_equity,
            "simulated_position": state.simulated_position,
            "legging_aborts_total": state.legging_aborts_total,
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
