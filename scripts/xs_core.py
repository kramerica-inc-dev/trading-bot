#!/usr/bin/env python3
"""Cross-sectional momentum core — the pure, venue-agnostic signal + state lib.

This is the small library the live Hyperliquid runner (`hl_xs_runner.py`) and the
OKX paper runner (`xs_runner.py`) both depend on. It holds ONLY pure pieces:

  * `Position`  — a single signed perp leg (side / notional / entry).
  * `XSState`   — the runner's persisted state (cash, equity, positions, breaker
                  counters). Shared by both runners; HL-only fields are noted.
  * `compute_target_weights` — the dollar-neutral long-top-m / short-bottom-m
                  basket math (lookahead-free).

Extracting these out of `xs_runner.py` means the LIVE runner depends on a tiny,
exchange-independent library rather than on the OKX *paper* runner (architecture
audit P2 #12). No I/O, no exchange client, no environment — pure data + numpy.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, Optional

import numpy as np


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class Position:
    side: int                # +1 long, -1 short
    notional: float          # USD notional at entry
    entry_price: float
    entered_ts: str


@dataclass
class XSState:
    cash: float = 0.0
    equity: float = 0.0
    peak_equity: float = 0.0
    positions: Dict[str, Position] = field(default_factory=dict)
    rebalances_total: int = 0
    skips_total: int = 0
    cycles_total: int = 0
    funding_paid_total: float = 0.0     # cumulative net funding outflow (USD)
    fees_paid_total: float = 0.0
    cb_state: str = "normal"            # normal | halted | op_halt | catastrophe_halt
    last_settled_equity: Optional[float] = None   # last accepted settled equity (intracycle-drop ref)
    last_funding_ts: Optional[str] = None         # last sim-funding accrual (HL runner)
    data_outage_streak: int = 0                   # consecutive insufficient/stale-data cycles
    equity_jump_streak: int = 0                   # consecutive suspicious equity-jump reads (HL runner)
    started_ts: Optional[str] = None
    last_rebalance_ts: Optional[str] = None
    last_cycle_ts: Optional[str] = None
    last_reconcile_ok: bool = True
    dry_run: bool = True

    def to_json(self) -> dict:
        d = asdict(self)
        d["positions"] = {s: asdict(p) for s, p in self.positions.items()}
        return d

    @classmethod
    def from_json(cls, d: dict) -> "XSState":
        pos = {s: Position(**p) for s, p in (d.get("positions") or {}).items()}
        d = {**d, "positions": pos}
        known = {f for f in cls().__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# Strategy (the hardened lead — mirrors backtest/sweep/xsectional.py)
# ---------------------------------------------------------------------------

def compute_target_weights(closes: Dict[str, np.ndarray], lookback: int, m: int
                           ) -> Dict[str, float]:
    """Dollar-neutral equal-weight basket: +1/m on the top-m, -1/m on the
    bottom-m by trailing `lookback`-day return. Lookahead-free (uses only the
    available history). Returns {} if too few assets."""
    trail = {}
    for sym, cl in closes.items():
        if len(cl) > lookback and cl[-1 - lookback] > 0:
            trail[sym] = cl[-1] / cl[-1 - lookback] - 1.0
    if len(trail) < 2 * m:
        return {}
    ranked = sorted(trail, key=lambda s: trail[s])     # worst -> best
    weights = {s: 0.0 for s in trail}
    for s in ranked[-m:]:
        weights[s] = 1.0 / m                           # longs (top)
    for s in ranked[:m]:
        weights[s] = -1.0 / m                          # shorts (bottom)
    return weights
