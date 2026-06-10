#!/usr/bin/env python3
"""Carry-position math — pure functions, no I/O, no exchange calls.

The cash-and-carry trade is delta-neutral by construction:
  * spot_qty (BTC)  : long position
  * perp_qty (BTC)  : short position, ideally ≈ -spot_qty

Sign convention (binding across runner + tests):
  * spot_qty > 0 (always long)
  * perp_qty < 0 for the carry (short); the runner stores it as a
    signed number so we can detect a flipped book trivially.
  * entry_basis = spot_price - perp_price at entry (USD).

Per `docs/STRATEGY-CARRY.md` the perp short RECEIVES `notional * rate`
when funding rate > 0 (the persistent regime). `funding_accrued` is the
running sum in USD (positive = received).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class CarryPosition:
    """In-memory model of a single delta-neutral carry.

    All numeric fields are plain floats in their natural units (BTC for
    quantities, USD for prices/PnL). Serializes cleanly via dataclasses.asdict.
    """
    spot_qty: float = 0.0                # BTC, >= 0
    perp_qty: float = 0.0                # BTC, <= 0 for carry short
    entry_spot_price: float = 0.0        # USD per BTC at spot entry
    entry_perp_price: float = 0.0        # USD per BTC at perp entry
    funding_accrued: float = 0.0         # cumulative USD (positive = received)
    fees_paid: float = 0.0               # cumulative USD
    opened_ts: Optional[str] = None      # ISO-8601 UTC
    last_updated_ts: Optional[str] = None

    @property
    def entry_basis(self) -> float:
        """spot - perp at entry, USD per BTC. Small and noisy in practice."""
        return self.entry_spot_price - self.entry_perp_price

    @property
    def is_flat(self) -> bool:
        return abs(self.spot_qty) < 1e-9 and abs(self.perp_qty) < 1e-9

    @property
    def notional_usd_at_entry(self) -> float:
        """Notional of the spot leg at entry (the carry's deployed size)."""
        return self.spot_qty * self.entry_spot_price

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> "CarryPosition":
        return cls(**{k: data.get(k, getattr(cls(), k))
                      for k in asdict(cls()).keys()})


# ---------------------------------------------------------------------------
# Target-sizing helper
# ---------------------------------------------------------------------------

@dataclass
class TargetSize:
    spot_qty: float          # BTC to hold long on spot
    perp_qty: float          # BTC to hold short on perp (negative)
    notional_usd: float      # USD notional of one leg
    perp_margin_usd: float   # USD margin posted on the perp short

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


def target_position_for(
    notional_usd: float, btc_price: float,
    leverage: float = 2.0,
    min_btc: float = 0.0,
) -> TargetSize:
    """Recommended target sizes for a `notional_usd` carry at `btc_price`.

    Args:
        notional_usd: USD size of each leg (long spot, short perp). E.g.
            for a $5k book with `notional_fraction=0.6` you pass $3000 here.
        btc_price: current BTC price (use spot ≈ perp; basis << 1%).
        leverage: target effective leverage on the perp short. Higher =
            less margin posted but more liquidation risk on a rally. The
            carry spec recommends ≤2-3× with unified margin.
        min_btc: floor on absolute qty (broker min order size). If the
            computed qty falls below this, returns zeros — caller decides
            whether to deploy or sit out.

    Returns:
        TargetSize with spot_qty > 0, perp_qty < 0 (signed short).

    Pure math: this function does not check whether the equity is actually
    available, whether the basis is favourable, or whether the green-button
    rule is on. Those are the runner's job.
    """
    if notional_usd <= 0 or btc_price <= 0:
        return TargetSize(0.0, 0.0, 0.0, 0.0)
    if leverage <= 0:
        raise ValueError(f"leverage must be > 0, got {leverage}")
    qty = notional_usd / btc_price
    if qty < min_btc:
        return TargetSize(0.0, 0.0, 0.0, 0.0)
    perp_margin = notional_usd / leverage
    return TargetSize(
        spot_qty=qty,
        perp_qty=-qty,
        notional_usd=notional_usd,
        perp_margin_usd=perp_margin,
    )


# ---------------------------------------------------------------------------
# Drift / monitoring helpers
# ---------------------------------------------------------------------------

@dataclass
class DriftReport:
    """How far the live position is from the intended delta-neutral state.

    `net_qty_btc` = spot_qty + perp_qty. For a perfect carry this is ~0
    (spot long + perp short of equal size). Non-zero values mean the
    runner has net directional exposure — fills were imperfect, or one
    leg drifted (e.g. only one of two legs filled).

    `net_qty_usd` is that drift expressed as USD at the *current* spot
    price — i.e. the dollar P&L impact per 1% move in BTC.

    `basis_now_usd` and `basis_drift_usd_per_btc` track how far the
    spot↔perp basis has moved since entry — this is the "basis blow-out"
    kill-switch input from `docs/STRATEGY-CARRY.md` §6.
    """
    net_qty_btc: float
    net_qty_usd: float
    basis_now_usd: float
    basis_drift_usd_per_btc: float
    spot_qty: float
    perp_qty: float
    current_spot: float
    current_perp: float


def delta_neutral_drift(
    position: CarryPosition,
    current_spot: float,
    current_perp: float,
) -> DriftReport:
    """Compute the current delta-neutral drift report."""
    net = position.spot_qty + position.perp_qty
    basis_now = float(current_spot) - float(current_perp)
    basis_drift = basis_now - position.entry_basis
    return DriftReport(
        net_qty_btc=net,
        net_qty_usd=net * float(current_spot),
        basis_now_usd=basis_now,
        basis_drift_usd_per_btc=basis_drift,
        spot_qty=position.spot_qty,
        perp_qty=position.perp_qty,
        current_spot=float(current_spot),
        current_perp=float(current_perp),
    )


def funding_accrual_step(
    position: CarryPosition, funding_rate: float, btc_price: float,
) -> float:
    """USD funding for one 8h settlement, given the *current* perp leg.

    Convention: a short (perp_qty < 0) RECEIVES when funding_rate > 0.
    The OKX/Blofin spec is `payment = notional * rate * sign(position)`
    with longs PAYING positive rates. Here:
        cash_flow = -perp_qty_signed * btc_price * rate
                  = +(spot_size) * btc_price * rate    [for the carry]

    So for the carry trade with perp_qty = -X:
        cash_flow = X * btc_price * rate  →  positive when rate > 0.

    This is the same sign convention as `backtest/carry_backtest.py`
    where `f_pnl = notional * rates[i]`.
    """
    # Use |perp_qty| because the position is short; sign of cash flow
    # depends on side (short) and rate. We hard-code the "short receives
    # positive rate" convention.
    if position.perp_qty == 0 or btc_price <= 0:
        return 0.0
    notional = abs(position.perp_qty) * float(btc_price)
    # short → +rate when rate > 0; -rate when rate < 0
    side_sign = -1.0 if position.perp_qty < 0 else 1.0  # for clarity
    # For the canonical carry (perp_qty < 0): side_sign = -1, cash_flow = -(-1)*… = + ; wait, work it through:
    # The plan-e convention in apply_funding_charges was:
    #   side_sign = -1.0 if pos.side == "long" else 1.0
    #   charge = side_sign * pos.notional * rate
    # i.e. short → +notional*rate  (short receives when rate > 0).
    # Here perp_qty < 0 means short → side_sign = +1.
    side_sign = +1.0 if position.perp_qty < 0 else -1.0
    return side_sign * notional * float(funding_rate)


def projected_next_funding_usd(
    notional_usd: float, funding_rate: float,
) -> float:
    """Forward-looking projection of the next 8h funding cash flow (USD).

    Helper for the runner's "would-do" log so the dashboard can show
    "next settlement we expect $X based on the current funding rate".
    Positive = expected inflow to the short.
    """
    if notional_usd <= 0:
        return 0.0
    return float(notional_usd) * float(funding_rate)


# ---------------------------------------------------------------------------
# Green-button rule
# ---------------------------------------------------------------------------

SETTLEMENTS_PER_YEAR = 3 * 365   # 8h funding settlements (OKX default)

# Hyperliquid settles funding hourly → 24 * 365 settlements per year.
SETTLEMENTS_PER_YEAR_HOURLY = 24 * 365


def annualize_funding(
    per_settlement_rate: float,
    settlements_per_year: float = SETTLEMENTS_PER_YEAR,
) -> float:
    """Convert a single per-settlement funding rate to an annualised fraction.

    Default cadence is the OKX 8h schedule (3 * 365 settlements/yr),
    matching `docs/STRATEGY-CARRY.md` and `backtest/carry_backtest.py`:
    a `+0.96 bps/8h` mean ≈ `+10.5%/yr` (0.000096 * 3 * 365 ≈ 0.1051).

    For Hyperliquid's hourly funding pass
    `settlements_per_year=SETTLEMENTS_PER_YEAR_HOURLY` (24 * 365 = 8760);
    do NOT aggregate hourly rates to 8h-equivalents.
    """
    return float(per_settlement_rate) * float(settlements_per_year)


def green_button_on(
    trailing_funding_rates: list, threshold_annualised: float,
    min_samples: int = 1,
    settlements_per_year: float = SETTLEMENTS_PER_YEAR,
) -> Dict[str, Any]:
    """Decide whether the carry should be ON for the next cycle.

    Args:
        trailing_funding_rates: list of *per-settlement* funding rates
            (most recent 90d worth, ~270 settlements). If shorter than
            `min_samples`, returns off with reason="insufficient_history".
        threshold_annualised: green-button threshold, e.g. 0.05 (=+5%/yr).
            Per the DECISIONS.md 2026-05-13 entry: the trade is "always on
            when funding is on" — i.e. ON if trailing 90d annualised
            funding > threshold, else FLAT (target_notional=0, hold cash).
        settlements_per_year: funding cadence used to annualise the mean
            of the trailing window. Defaults to the OKX 8h schedule
            (3 * 365 = 1095). Hyperliquid hourly = 24 * 365 = 8760.

    Returns:
        {
          "on": bool,
          "trailing_annualised": float,   # annualised mean of the window
          "threshold": float,
          "samples": int,
          "reason": str,
        }
    """
    samples = len(trailing_funding_rates)
    if samples < min_samples:
        return {
            "on": False, "trailing_annualised": 0.0,
            "threshold": float(threshold_annualised),
            "samples": samples,
            "reason": f"insufficient_history (have {samples}, need {min_samples})",
        }
    mean_rate = sum(float(r) for r in trailing_funding_rates) / samples
    annualised = annualize_funding(mean_rate, settlements_per_year)
    on = annualised > float(threshold_annualised)
    return {
        "on": on,
        "trailing_annualised": annualised,
        "threshold": float(threshold_annualised),
        "samples": samples,
        "reason": "above_threshold" if on else "below_threshold",
    }
