#!/usr/bin/env python3
"""Execution-mode safety gates — one home for every "is this real money?" check.

Previously this logic was hand-duplicated three times (architecture audit P2 #14):
`xs_runner.resolve_mode`, `carry_runner.resolve_mode`, `hl_adapter.resolve_hl_mode`.
Centralising it means the rule that stands between paper and real funds is defined
once, reviewed once, and tested once.

Two distinct gates live here because the two execution families have different
state machines:

  * DEMO/PAPER gate (OKX paper runner + carry) — `resolve_demo_mode`
        DRY_RUN  → simulate fills, no orders
        P2_DEMO  → real orders on the demo venue (x-simulated-trading)
        P3_LIVE  → real money (hard-gated behind allow_live; callers may layer
                   their own extra guards, e.g. carry's per-leg sizing cap)

  * HYPERLIQUID gate (live venue) — `resolve_hl_mode`
        TESTNET       → real signed orders, mock funds
        MAINNET_DRY   → mainnet data only, order calls refused
        MAINNET_LIVE  → REAL money (allow_live must be the bool True by identity
                        AND env HL_CONFIRM_LIVE=YES)

Pure functions: no I/O beyond reading the one HL confirmation env var. Fail closed.
"""

from __future__ import annotations

import os


# ---------------------------------------------------------------------------
# Demo/paper gate (OKX paper runner + carry runner)
# ---------------------------------------------------------------------------

MODE_DRY = "DRY_RUN"
MODE_P2 = "P2_DEMO"
MODE_P3 = "P3_LIVE"


def resolve_demo_mode(*, dry_run: bool, okx_demo: bool, allow_live: bool) -> str:
    """Three-state DRY/P2/P3 safety gate. Pure — no environment, no I/O. Raises
    `RuntimeError` for any unsupported/unsafe combination so callers fail closed.

    Callers that need an extra P3 guard (e.g. carry's per-leg sizing cap) apply it
    after this returns MODE_P3."""
    if dry_run:
        return MODE_DRY
    if okx_demo:
        return MODE_P2
    if not allow_live:
        raise RuntimeError(
            "Refusing to run live: dry_run=false + okx_demo=false requires "
            "allow_live=true (P3 gate). Use dry_run=true (DRY_RUN) or "
            "okx_demo=true (P2_DEMO), or explicitly allow_live=true (P3_LIVE).")
    return MODE_P3


# ---------------------------------------------------------------------------
# Hyperliquid gate (live venue)
# ---------------------------------------------------------------------------

MODE_TESTNET = "TESTNET"
MODE_MAINNET_DRY = "MAINNET_DRY"
MODE_MAINNET_LIVE = "MAINNET_LIVE"


def resolve_hl_mode(network: str, allow_live: bool) -> str:
    """Strict mode gate. MAINNET_LIVE (real money) requires allow_live to be the
    bool `True` (identity, not truthiness — so a stray 'false'/'0'/1 can never
    enable it) AND an out-of-band confirmation env var HL_CONFIRM_LIVE=YES."""
    if network == "testnet":
        return MODE_TESTNET
    if network == "mainnet":
        if allow_live is True and os.environ.get("HL_CONFIRM_LIVE") == "YES":
            return MODE_MAINNET_LIVE
        return MODE_MAINNET_DRY
    raise ValueError(f"network must be 'testnet' or 'mainnet', got {network!r}")
