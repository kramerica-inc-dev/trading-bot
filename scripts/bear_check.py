#!/usr/bin/env python3
"""Fase 6 (option A) — deterministic "devil's advocate" / bear-check module.

Before each entry, this module evaluates the *opposing* case — the bearish case
for a long, the bullish case for a short — and produces a single
"counter-argument strength" score in ``[0, 1]``.  A high counter-argument maps
to a smaller effective position size; it is *not* a hard block.  It plugs into
the continuous-score architecture from Fase 5: the resulting ``size_multiplier``
is applied on top of the Fase 5 risk-score sizing (or, when
``risk_scoring.enabled`` is false, directly onto ``risk_multiplier``).

Components (each a normalized sub-score in ``[0, 1]`` where *higher = stronger
counter-argument against the proposed trade*), per IMPROVEMENT_PLAN.md Fase 6:

- ``mtf_opposition`` — the lower / entry timeframe agrees with the trade
  direction but the higher timeframes (1h + 4h, via ``htf_alignment_score`` in
  ``[-2, 2]``) oppose it.  Full strength only when the entry TF agrees *and* the
  HTFs are maximally against; halved if the entry TF does not itself agree.
- ``recent_lower_highs`` (for longs) / ``recent_higher_lows`` (for shorts) —
  within a recent window of *closed* bars, structure works against the trade:
  for a long, the recent half's swing-high is below the earlier half's
  swing-high (lower highs); for a short, the recent half's swing-low is above
  the earlier half's swing-low (higher lows).  Magnitude normalized by ATR.
  Computed from ``market_context['recent_closes']`` (a list of closed-bar
  closes, no look-ahead) when provided, else read from a precomputed
  ``market_context['recent_structure_against']`` in ``[0, 1]``, else ``0``.
- ``bb_extreme_against`` — price sits at a Bollinger-band extreme on the wrong
  side for the trade (a long near the upper band, a short near the lower band).
  Uses ``market_context['bb_pos']`` (= ``(price - lower) / (upper - lower)``,
  ``[0, 1]``) when present; otherwise falls back to an RSI proxy from
  ``indicators['rsi_value']`` (a long with RSI deep into overbought, a short
  with RSI deep into oversold).
- ``loser_correlation`` — similarity to recently-closed losing trades in the
  same regime / same active strategy.  **Stubbed to 0.0**: threading the
  strategy's realized trade outcomes back into ``analyze()`` is invasive for
  the backtester's bar loop and out of scope for a 1-day phase; the field is
  kept (and weighted) so the wiring/logging is in place for a future iteration.
  See ``docs/bear-check.md``.

- *funding-extreme* is in the plan's component list but funding rate was a
  Fase 3 NO-GO (see ``docs/funding-analysis.md``) — it is **omitted** here and
  never used anywhere.

The sub-scores are combined as a **weighted average** (equal weights by
default, config-tunable, normalized to sum to 1) into the overall ``score``.
Score -> size mapping (the "soft, but can effectively veto" curve):

    size_multiplier = clip(1.0 - score * max_penalty, min_floor, 1.0)
    # defaults: max_penalty = 1.0, min_floor = 0.0
    #   score 0.0 -> 1.0x (no counter-argument)
    #   score 0.5 -> 0.5x
    #   score 1.0 -> 0.0x (maximal counter-argument zeroes the trade)

This module is *pure* — no I/O, no strategy state.  It is wired into
``scripts/advanced_strategy.py`` behind ``bear_check.enabled`` (default
``false``); when disabled the strategy keeps its byte-for-byte old behaviour.
See ``docs/bear-check.md``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Equal weights — option-A startpoint (cf. Fase 5 risk-scoring weights).
DEFAULT_COMPONENT_WEIGHTS: Dict[str, float] = {
    "mtf_opposition": 0.25,
    "recent_lower_highs": 0.25,
    "bb_extreme_against": 0.25,
    "loser_correlation": 0.25,
}

COMPONENT_KEYS = (
    "mtf_opposition",
    "recent_lower_highs",
    "bb_extreme_against",
    "loser_correlation",
)

# Strength -> position-size-multiplier curve: clip(1 - strength*max_penalty, min_floor, 1).
DEFAULT_MAX_PENALTY = 1.0
DEFAULT_MIN_FLOOR = 0.0

# How many recent closed-bar closes to inspect for the lower-highs / higher-lows
# structure check, when ``recent_closes`` is supplied.
DEFAULT_STRUCTURE_WINDOW = 20
# A swing-high/low displacement of >= this fraction of ATR counts as a full
# (1.0) structure counter-argument.
DEFAULT_STRUCTURE_ATR_FULL = 1.0
# Bollinger-position thresholds for the "wrong-side extreme" check.
DEFAULT_BB_LONG_EXTREME = 0.8   # long with bb_pos in [0.8, 1.0] -> ramp 0..1
DEFAULT_BB_SHORT_EXTREME = 0.2  # short with bb_pos in [0.0, 0.2] -> ramp 0..1
# RSI fallback thresholds when bb_pos is unavailable.
DEFAULT_RSI_LONG_EXTREME = 70.0   # long with RSI in [70, 100] -> ramp 0..1
DEFAULT_RSI_SHORT_EXTREME = 30.0  # short with RSI in [0, 30] -> ramp 0..1


def _clamp01(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def _side_sign(action: Optional[str]) -> float:
    a = (str(action or "")).lower()
    if a == "buy":
        return 1.0
    if a == "sell":
        return -1.0
    return 0.0


def _get_attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _resolve_weights(config: Optional[Dict[str, Any]]) -> Dict[str, float]:
    weights = dict(DEFAULT_COMPONENT_WEIGHTS)
    if config:
        cfg_w = config.get("weights") or config.get("component_weights") or {}
        if isinstance(cfg_w, dict):
            for k in COMPONENT_KEYS:
                if k in cfg_w:
                    try:
                        weights[k] = max(0.0, float(cfg_w[k]))
                    except (TypeError, ValueError):
                        pass
    total = sum(weights.values())
    if total <= 0.0:
        return dict(DEFAULT_COMPONENT_WEIGHTS)
    return {k: v / total for k, v in weights.items()}


# ---------------------------------------------------------------------------
# Component helpers
# ---------------------------------------------------------------------------

def _mtf_opposition(side_sign: float, market_context: Dict[str, Any],
                    indicators: Dict[str, Any]) -> float:
    """Counter-argument from HTFs opposing a trade whose entry TF agrees.

    ``htf_alignment_score`` (= 1h-state + 4h-state codes, each in
    ``{-1, -0.5, 0, 0.5, 1}``) has raw range ``[-2, 2]``.  ``-side*htf`` is the
    "against the trade" amount in ``[0, 2]``; normalize by 2.  If the entry-TF
    state does *not* agree with the trade side, halve it (the plan's premise is
    "LTF bullish but HTF bearish").
    """
    htf = 0.0
    for src in (market_context, indicators):
        if isinstance(src, dict):
            for key in ("htf_alignment_score", "htf_alignment"):
                if key in src:
                    try:
                        htf = float(src[key])
                    except (TypeError, ValueError):
                        htf = 0.0
                    break
            else:
                continue
            break
    against = max(0.0, -side_sign * htf) / 2.0
    entry_code = 0.0
    for src in (market_context, indicators):
        if isinstance(src, dict) and "entry_tf_state_code" in src:
            try:
                entry_code = float(src["entry_tf_state_code"])
            except (TypeError, ValueError):
                entry_code = 0.0
            break
    if side_sign * entry_code <= 0.0:
        against *= 0.5
    return _clamp01(against)


def _structure_against(side_sign: float, market_context: Dict[str, Any],
                       indicators: Dict[str, Any], config: Dict[str, Any]) -> float:
    """Lower-highs (long) / higher-lows (short) over a recent closed-bar window."""
    # Precomputed value path.
    for src in (market_context, indicators):
        if isinstance(src, dict) and "recent_structure_against" in src:
            return _clamp01(src["recent_structure_against"])
    closes = market_context.get("recent_closes") if isinstance(market_context, dict) else None
    if not closes or len(closes) < 4 or side_sign == 0.0:
        return 0.0
    try:
        seq: List[float] = [float(x) for x in closes]
    except (TypeError, ValueError):
        return 0.0
    window = int(config.get("structure_window", DEFAULT_STRUCTURE_WINDOW))
    seq = seq[-window:] if window > 0 else seq
    if len(seq) < 4:
        return 0.0
    mid = len(seq) // 2
    earlier, recent = seq[:mid], seq[mid:]
    atr = 0.0
    for src in (market_context, indicators):
        if isinstance(src, dict):
            for key in ("atr", "atr_value"):
                if key in src:
                    try:
                        atr = float(src[key])
                    except (TypeError, ValueError):
                        atr = 0.0
                    break
    if atr <= 0.0:
        # Fall back to a fraction of the price level so the check still works.
        ref = abs(seq[-1]) if seq[-1] else 1.0
        atr = ref * 0.01
    atr_full = float(config.get("structure_atr_full", DEFAULT_STRUCTURE_ATR_FULL))
    denom = atr * atr_full if atr_full > 0 else atr
    if side_sign > 0.0:
        # long: counter-argument if recent swing-high < earlier swing-high.
        disp = max(0.0, max(earlier) - max(recent))
    else:
        # short: counter-argument if recent swing-low > earlier swing-low.
        disp = max(0.0, min(recent) - min(earlier))
    if denom <= 0.0:
        return 0.0
    return _clamp01(disp / denom)


def _bb_extreme_against(side_sign: float, market_context: Dict[str, Any],
                        indicators: Dict[str, Any], config: Dict[str, Any]) -> float:
    """Price at a Bollinger extreme on the wrong side for the trade.

    Uses ``bb_pos`` (``[0, 1]``) when available; falls back to ``rsi_value``.
    """
    if side_sign == 0.0:
        return 0.0
    bb_pos: Optional[float] = None
    for src in (market_context, indicators):
        if isinstance(src, dict) and "bb_pos" in src:
            try:
                bb_pos = float(src["bb_pos"])
            except (TypeError, ValueError):
                bb_pos = None
            break
    if bb_pos is not None:
        if side_sign > 0.0:
            thr = float(config.get("bb_long_extreme", DEFAULT_BB_LONG_EXTREME))
            if thr >= 1.0:
                return 0.0
            return _clamp01((bb_pos - thr) / (1.0 - thr))
        thr = float(config.get("bb_short_extreme", DEFAULT_BB_SHORT_EXTREME))
        if thr <= 0.0:
            return 0.0
        return _clamp01((thr - bb_pos) / thr)
    # RSI fallback.
    rsi: Optional[float] = None
    for src in (indicators, market_context):
        if isinstance(src, dict):
            for key in ("rsi_value", "rsi"):
                if key in src:
                    try:
                        rsi = float(src[key])
                    except (TypeError, ValueError):
                        rsi = None
                    break
            if rsi is not None:
                break
    if rsi is None:
        return 0.0
    if side_sign > 0.0:
        thr = float(config.get("rsi_long_extreme", DEFAULT_RSI_LONG_EXTREME))
        if thr >= 100.0:
            return 0.0
        return _clamp01((rsi - thr) / (100.0 - thr))
    thr = float(config.get("rsi_short_extreme", DEFAULT_RSI_SHORT_EXTREME))
    if thr <= 0.0:
        return 0.0
    return _clamp01((thr - rsi) / thr)


def _loser_correlation(side_sign: float, signal: Any, market_context: Dict[str, Any],
                       indicators: Dict[str, Any], config: Dict[str, Any]) -> float:
    """STUB — see module docstring / docs/bear-check.md.

    If ``market_context['recent_loser_outcomes']`` is supplied as a list of
    ``{"regime": str, "active_strategy": str}`` dicts, the similarity is the
    fraction of recent losers sharing this trade's regime *and* active strategy
    (a cheap "same setup" proxy).  Absent that, returns ``0.0``.
    """
    losers = market_context.get("recent_loser_outcomes") if isinstance(market_context, dict) else None
    if not losers:
        return 0.0
    regime = _get_attr(signal, "regime")
    active = _get_attr(signal, "active_strategy")
    try:
        n = len(losers)
        if n == 0:
            return 0.0
        same = sum(
            1 for o in losers
            if isinstance(o, dict)
            and o.get("regime") == regime
            and o.get("active_strategy") == active
        )
        return _clamp01(same / float(n))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_bear_check_components(signal: Any, indicators: Optional[Dict[str, Any]],
                                  market_context: Optional[Dict[str, Any]] = None,
                                  config: Optional[Dict[str, Any]] = None
                                  ) -> Tuple[Dict[str, float], Dict[str, Any]]:
    """Return ``(normalized_components, raw_inputs)``.

    ``normalized_components`` are the four ``[0, 1]`` sub-scores fed to the
    weighted average; ``raw_inputs`` echoes the side sign and the few raw
    context values used (for post-hoc analysis).
    """
    ind = indicators if isinstance(indicators, dict) else {}
    ctx = market_context if isinstance(market_context, dict) else {}
    cfg = config if isinstance(config, dict) else {}
    action = _get_attr(signal, "action")
    side_sign = _side_sign(action)

    components = {
        "mtf_opposition": _mtf_opposition(side_sign, ctx, ind),
        "recent_lower_highs": _structure_against(side_sign, ctx, ind, cfg),
        "bb_extreme_against": _bb_extreme_against(side_sign, ctx, ind, cfg),
        "loser_correlation": _loser_correlation(side_sign, signal, ctx, ind, cfg),
    }
    raw_inputs = {
        "side_sign": side_sign,
        "action": action,
        "htf_alignment_score": (ctx.get("htf_alignment_score")
                                if "htf_alignment_score" in ctx
                                else ind.get("htf_alignment_score")),
        "entry_tf_state_code": (ctx.get("entry_tf_state_code")
                                if "entry_tf_state_code" in ctx
                                else ind.get("entry_tf_state_code")),
        "bb_pos": ctx.get("bb_pos", ind.get("bb_pos")),
        "rsi_value": ind.get("rsi_value", ctx.get("rsi_value")),
        "loser_correlation_stub": "recent_loser_outcomes" not in ctx,
    }
    return components, raw_inputs


def compute_bear_check_strength(signal: Any, indicators: Optional[Dict[str, Any]],
                                market_context: Optional[Dict[str, Any]] = None,
                                config: Optional[Dict[str, Any]] = None) -> float:
    """Return the overall counter-argument strength in ``[0.0, 1.0]``."""
    components, _ = compute_bear_check_components(signal, indicators, market_context, config)
    weights = _resolve_weights(config)
    return _clamp01(sum(weights[k] * components[k] for k in COMPONENT_KEYS))


def strength_to_size_multiplier(strength: float, config: Optional[Dict[str, Any]] = None) -> float:
    """Map a counter-argument strength in ``[0, 1]`` to a size multiplier in ``[0, 1]``.

    ``size_multiplier = clip(1.0 - strength * max_penalty, min_floor, 1.0)``

    Defaults: ``max_penalty = 1.0``, ``min_floor = 0.0`` -> a maximal
    counter-argument zeroes the trade (soft, but can effectively veto).  Both
    config-tunable via the ``bear_check`` section keys ``max_penalty`` /
    ``min_floor``.
    """
    cfg = config if isinstance(config, dict) else {}
    max_penalty = float(cfg.get("max_penalty", DEFAULT_MAX_PENALTY))
    min_floor = float(cfg.get("min_floor", DEFAULT_MIN_FLOOR))
    s = _clamp01(strength)
    raw = 1.0 - s * max_penalty
    lo = 0.0 if min_floor < 0.0 else (1.0 if min_floor > 1.0 else min_floor)
    if raw < lo:
        raw = lo
    if raw > 1.0:
        raw = 1.0
    return raw


def compute_bear_check(signal: Any, indicators: Optional[Dict[str, Any]] = None,
                       market_context: Optional[Dict[str, Any]] = None,
                       config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Evaluate the opposing case for ``signal``.

    ``signal``         — an ``AdvancedSignal``-like object (or mapping) with at
                         least ``action`` (and optionally ``regime`` /
                         ``active_strategy`` for the loser-correlation stub).
    ``indicators``     — the strategy's per-bar indicators dict (``rsi_value``,
                         ``htf_alignment_score``, ``entry_tf_state_code``,
                         optionally ``bb_pos`` / ``recent_structure_against``).
    ``market_context`` — optional extra context (``recent_closes``, ``atr``,
                         ``recent_loser_outcomes``); usually ``None`` — the
                         strategy passes everything via ``indicators``.
    ``config``         — the ``bear_check`` config section (or ``None``).
                         Recognized keys: ``weights`` (per-component weights,
                         default equal), ``max_penalty`` / ``min_floor`` (the
                         strength->size curve), plus the per-component
                         normalization thresholds documented above.

    Returns ``{"score": float in [0,1], "components": {...}, "size_multiplier":
    float in [0,1], "weights": {...}, "raw_inputs": {...}}``.
    """
    components, raw_inputs = compute_bear_check_components(signal, indicators, market_context, config)
    weights = _resolve_weights(config)
    score = _clamp01(sum(weights[k] * components[k] for k in COMPONENT_KEYS))
    return {
        "score": score,
        "components": components,
        "size_multiplier": strength_to_size_multiplier(score, config),
        "weights": weights,
        "raw_inputs": raw_inputs,
    }


def build_bear_check_record(signal: Any, indicators: Optional[Dict[str, Any]] = None,
                            market_context: Optional[Dict[str, Any]] = None,
                            config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Serializable trade-record fragment: ``{"score", "components",
    "size_multiplier", "weights", "raw_inputs"}``.  Used by the strategy /
    backtester to log per-trade bear-check results so post-hoc analysis can ask
    whether high-bear-check trades fared worse.  (Identical to
    :func:`compute_bear_check` today; kept as a named entry point mirroring
    :func:`risk_scoring.build_risk_score_record`.)"""
    return compute_bear_check(signal, indicators, market_context, config)
