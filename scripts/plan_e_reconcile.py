"""Plan E reconciliation — paper-mode self-consistency + live-mode hooks.

See RECONCILE-DESIGN.md for the full spec. This module is intentionally
side-effect-free: it returns ReconcileResult objects, does not mutate
state, does not call the exchange. Callers wire it in.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple


# =========================  Result types  =========================

@dataclass
class ReconcileResult:
    """Outcome of a reconciliation pass.

    `ok` is True iff there are no errors. Warnings do not flip ok.
    """
    ok: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    rules_evaluated: int = 0
    ts: str = ""

    def add_error(self, code: str, msg: str) -> None:
        self.errors.append(f"{code}: {msg}")
        self.ok = False

    def add_warning(self, code: str, msg: str) -> None:
        self.warnings.append(f"{code}: {msg}")

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "rules_evaluated": self.rules_evaluated,
            "ts": self.ts,
        }

    def merge(self, other: "ReconcileResult") -> "ReconcileResult":
        out = ReconcileResult(
            ok=self.ok and other.ok,
            errors=list(self.errors) + list(other.errors),
            warnings=list(self.warnings) + list(other.warnings),
            rules_evaluated=self.rules_evaluated + other.rules_evaluated,
            ts=self.ts or other.ts,
        )
        return out


@dataclass
class ExchangePosition:
    """Snapshot of an exchange-side position.

    Caller is responsible for mapping Blofin's API response into this shape.
    Side is normalized to {"long", "short"}; qty is signed-positive
    (absolute), and notional is qty * mark_price.
    """
    symbol: str
    side: str
    qty: float
    notional: float
    entry_price: Optional[float] = None
    mark_price: Optional[float] = None


# =========================  Self-consistency  =========================

def reconcile_self(
    state: Any,
    cfg: Any,
    *,
    now_iso: Optional[str] = None,
    equity_tolerance_usd: float = 1.0,
) -> ReconcileResult:
    """Pure local consistency check on a PortfolioState.

    Rules R1-R8 from RECONCILE-DESIGN.md. No network, no mutations.

    `state` and `cfg` are kept untyped so this module doesn't need to
    import plan_e_runner. The expected shape:
      state: PortfolioState (cash, equity, positions, peak_equity,
                              cb_state, cb_tripped_*, funding_paid_total,
                              last_funding_ts)
      cfg:   PlanEConfig (long_n, short_n)
    """
    res = ReconcileResult(ts=now_iso or datetime.now(timezone.utc).isoformat())

    # R1: cash + Σ unrealized_pnl(pos, entry) ≈ equity (entry as proxy)
    res.rules_evaluated += 1
    cash = float(getattr(state, "cash", 0.0))
    equity = float(getattr(state, "equity", 0.0))
    positions = getattr(state, "positions", {}) or {}
    # When positions are at their entry price, unrealized = 0, so equity = cash.
    # We can't fetch a live price here — this is a *structural* check, not a
    # mark check. So we compare equity vs cash + 0 with a generous tolerance.
    # If positions are open, equity may legitimately drift from cash by the
    # accumulated unrealized P&L. Skip R1 when positions exist.
    if not positions:
        if abs(cash - equity) > equity_tolerance_usd:
            res.add_error(
                "R1",
                f"flat book but cash ${cash:.4f} ≠ equity ${equity:.4f} "
                f"(diff ${abs(cash-equity):.4f} > tol ${equity_tolerance_usd:.2f})",
            )

    # R2: peak_equity >= equity
    res.rules_evaluated += 1
    peak = getattr(state, "peak_equity", None)
    if peak is not None and peak < equity - equity_tolerance_usd:
        res.add_error(
            "R2",
            f"peak ${peak:.4f} < equity ${equity:.4f} "
            f"(peak should be monotonic ≥ equity)",
        )

    # R3: cb_state in valid set
    res.rules_evaluated += 1
    cb_state = getattr(state, "cb_state", "normal")
    if cb_state not in ("normal", "halved", "halted"):
        res.add_error("R3", f"unknown cb_state {cb_state!r}")

    # R4: tripped → tripped_* populated
    res.rules_evaluated += 1
    if cb_state in ("halved", "halted"):
        if not getattr(state, "cb_tripped_ts", None):
            res.add_error("R4", f"cb_state={cb_state} but cb_tripped_ts is null")
        if getattr(state, "cb_tripped_equity", None) is None:
            res.add_error("R4", f"cb_state={cb_state} but cb_tripped_equity is null")
        if getattr(state, "cb_tripped_peak", None) is None:
            res.add_error("R4", f"cb_state={cb_state} but cb_tripped_peak is null")

    # R5: position count ≤ long_n + short_n
    res.rules_evaluated += 1
    max_legs = int(getattr(cfg, "long_n", 0)) + int(getattr(cfg, "short_n", 0))
    if len(positions) > max_legs:
        res.add_error(
            "R5",
            f"{len(positions)} positions > max {max_legs} (long_n+short_n)",
        )

    # R6: each position has valid side, positive notional + entry_price
    res.rules_evaluated += 1
    for sym, pos in positions.items():
        side = getattr(pos, "side", None)
        if side not in ("long", "short"):
            res.add_error("R6", f"{sym}: invalid side {side!r}")
        notional = float(getattr(pos, "notional", 0.0))
        if not (notional > 0 and math.isfinite(notional)):
            res.add_error("R6", f"{sym}: bad notional {notional}")
        entry = float(getattr(pos, "entry_price", 0.0))
        if not (entry > 0 and math.isfinite(entry)):
            res.add_error("R6", f"{sym}: bad entry_price {entry}")

    # R7: last_funding_ts <= now
    res.rules_evaluated += 1
    lf = getattr(state, "last_funding_ts", None)
    if lf:
        try:
            lf_dt = datetime.fromisoformat(lf)
            if lf_dt.tzinfo is None:
                lf_dt = lf_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc) if not now_iso else (
                datetime.fromisoformat(now_iso)
                if "T" in (now_iso or "")
                else datetime.now(timezone.utc)
            )
            if lf_dt > now:
                res.add_error(
                    "R7",
                    f"last_funding_ts {lf} is in the future vs {now.isoformat()}",
                )
        except ValueError as e:
            res.add_error("R7", f"unparseable last_funding_ts {lf!r}: {e}")

    # R8: funding_paid_total finite
    res.rules_evaluated += 1
    fp = getattr(state, "funding_paid_total", 0.0)
    if not math.isfinite(float(fp)):
        res.add_error("R8", f"funding_paid_total not finite: {fp}")

    return res


# =========================  Exchange comparison  =========================

def reconcile_against_exchange(
    state: Any,
    exchange_positions: Mapping[str, ExchangePosition],
    *,
    notional_tolerance_pct: float = 0.05,
    allow_foreign_positions: bool = False,
    now_iso: Optional[str] = None,
) -> ReconcileResult:
    """Compare local state.positions against exchange ground truth.

    Rules X1-X4 from RECONCILE-DESIGN.md. No mutations; the caller decides
    what to do with the result (warn, abort, or --force-reconcile).
    """
    res = ReconcileResult(ts=now_iso or datetime.now(timezone.utc).isoformat())
    local_positions = getattr(state, "positions", {}) or {}

    local_syms = set(local_positions.keys())
    remote_syms = set(exchange_positions.keys())

    # X1 + X2: every local position must match remote with same side and notional
    for sym, pos in local_positions.items():
        res.rules_evaluated += 1
        remote = exchange_positions.get(sym)
        if remote is None:
            res.add_error("X4", f"{sym}: local has position, exchange does not")
            continue
        local_side = getattr(pos, "side", None)
        if remote.side != local_side:
            res.add_error(
                "X1",
                f"{sym}: local side={local_side} vs remote side={remote.side}",
            )
        local_notional = float(getattr(pos, "notional", 0.0))
        remote_notional = float(remote.notional)
        if local_notional > 0:
            rel_diff = abs(remote_notional - local_notional) / local_notional
            if rel_diff > notional_tolerance_pct:
                res.add_error(
                    "X2",
                    f"{sym}: notional diff {rel_diff*100:.2f}% "
                    f"(local ${local_notional:.4f} vs remote ${remote_notional:.4f}) "
                    f"> tol {notional_tolerance_pct*100:.1f}%",
                )

    # X3: foreign positions on exchange not present locally
    foreign = remote_syms - local_syms
    if foreign:
        res.rules_evaluated += 1
        msg = f"foreign positions on exchange: {sorted(foreign)}"
        if allow_foreign_positions:
            res.add_warning("X3", msg + " (allowed)")
        else:
            res.add_error("X3", msg)

    return res


# =========================  Client-order-id scheme  =========================

# pe-<instance>-<ts_compact>-<sym>-<side_letter>
# ts_compact: YYYYMMDDTHHMM (no separator inside; matches format that fits
# Blofin's clientOrderId char limit of ~32). sym is the full symbol.
# side_letter: l|s
_COID_RE = re.compile(
    r"^pe-(?P<instance>[a-zA-Z0-9_-]+?)-(?P<ts>\d{8}T\d{4})-"
    r"(?P<symbol>[A-Z0-9-]+)-(?P<side>[ls])$"
)
_TS_FMT = "%Y%m%dT%H%M"


def client_order_id(instance: str, ts: datetime, symbol: str, side: str) -> str:
    """Mint a deterministic clientOrderId for a Plan E leg.

    side ∈ {"long", "short"} → letter "l"/"s".
    Raises ValueError on invalid inputs (unknown side, non-UTC ts).
    """
    if side not in ("long", "short"):
        raise ValueError(f"side must be long|short, got {side!r}")
    if ts.tzinfo is None or ts.tzinfo.utcoffset(ts).total_seconds() != 0:
        raise ValueError("ts must be tz-aware UTC")
    if "-" not in symbol:
        raise ValueError(f"symbol must contain '-', got {symbol!r}")
    side_letter = "l" if side == "long" else "s"
    return f"pe-{instance}-{ts.strftime(_TS_FMT)}-{symbol}-{side_letter}"


def parse_client_order_id(coid: str) -> Optional[Tuple[str, datetime, str, str]]:
    """Inverse of client_order_id. Returns None on non-matching format
    (i.e., orders that weren't minted by Plan E)."""
    m = _COID_RE.match(coid)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("ts"), _TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    side = "long" if m.group("side") == "l" else "short"
    return m.group("instance"), ts, m.group("symbol"), side


def find_orphan_order_ids(
    open_orders: List[Dict[str, Any]],
    instance: str,
    intended_legs: Optional[List[Tuple[str, str]]] = None,
) -> List[str]:
    """From a list of exchange-open-orders dicts, return the orderIds for
    orders this instance owns that don't match any intended leg.

    If `intended_legs` is None, all instance-owned orders are considered
    orphans (used during shutdown / hard reset).

    Each open order dict is expected to have keys "clientOrderId" and
    "orderId" (Blofin field names; caller may need to translate from
    snake_case if the API uses that)."""
    intended = set(intended_legs or [])
    orphans: List[str] = []
    for order in open_orders:
        coid = order.get("clientOrderId") or order.get("client_order_id") or ""
        parsed = parse_client_order_id(coid)
        if not parsed:
            continue
        coid_inst, _ts, symbol, side = parsed
        if coid_inst != instance:
            continue
        if intended_legs is None or (symbol, side) not in intended:
            order_id = order.get("orderId") or order.get("order_id") or ""
            if order_id:
                orphans.append(order_id)
    return orphans
