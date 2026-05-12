#!/usr/bin/env python3
"""Fase 5 — continuous weighted risk score.

Replaces the strategy's *binary* entry gates (``min_confidence`` /
``min_quality_score`` / ``min_regime_confidence``) with a continuous score in
``[0, 1]`` that maps to a position-size multiplier.  A low-confidence signal
becomes a *smaller* trade rather than no trade — but a score that maps to ``0``
is still effectively "no trade", which preserves the spirit of the old hard
floors at the low end.

Inputs (per IMPROVEMENT_PLAN.md Fase 5, **minus funding** — funding rate was a
NO-GO in Fase 3, see ``docs/funding-analysis.md``, so it is *not* an input):

- ``confidence``      — the signal confidence, already in ``[0, 1]``.
- ``quality_score``   — the strategy's trade-quality score, already in
                        ``[0, 1]`` (it is ``_clamp01``-ed in
                        ``_evaluate_trade_quality``).  Re-normalized against
                        its configured ``[min_score, 1.0]`` range so that a
                        score at the old gate threshold maps to ``0`` and a
                        perfect quality score maps to ``1``.
- ``regime_confidence`` — the regime-classifier confidence, already in
                          ``[0, 1]``.  Re-normalized against its configured
                          ``[min_regime_confidence, 1.0]`` range, same logic.
- ``mtf_alignment``   — derived from ``htf_alignment_score`` (= 1h-state +
                        4h-state codes, each in ``{-1, -0.5, 0, 0.5, 1}`` ->
                        raw range ``[-2, 2]``) signed by the trade side:
                        ``0.5 + 0.25 * side_sign * htf_score`` clipped to
                        ``[0, 1]``.  So full agreement (both HTFs with the
                        trade) -> ``1.0``; neutral HTFs -> ``0.5``; both HTFs
                        against -> ``0.0``.

The four normalized components are combined as a **weighted average**.  Default
weights are **equal** (option A in the plan — simpler, lower overfit risk).
Weights are config-tunable; they are normalized to sum to 1.

Score -> size mapping (continuous, not a threshold).  Default shape from the
plan:

    position_size_multiplier = clip(slope * score + intercept, 0.0, 1.0)
    # default slope = 2.0, intercept = -0.5
    #   score <= 0.25 -> 0.0x  (no trade)
    #   score  = 0.50 -> 0.5x
    #   score >= 0.75 -> 1.0x

This module is *pure* — no I/O, no strategy state.  It is wired into
``scripts/advanced_strategy.py`` behind ``risk_scoring.enabled`` (default
``false``); when disabled the strategy keeps its byte-for-byte old behaviour.
See ``docs/risk-scoring.md``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# Equal weights — option A startpunt (see plan §"Gewichten bepalen").
DEFAULT_COMPONENT_WEIGHTS: Dict[str, float] = {
    "confidence": 0.25,
    "quality_score": 0.25,
    "regime_confidence": 0.25,
    "mtf_alignment": 0.25,
}

# Score -> position-size-multiplier curve: clip(slope * score + intercept, 0, 1).
DEFAULT_SIZE_SLOPE = 2.0
DEFAULT_SIZE_INTERCEPT = -0.5

# Defaults for the re-normalization ranges of the bounded-but-not-[0,1]
# components.  These mirror the strategy's old gate thresholds so a signal
# sitting exactly on the old threshold contributes ~0 from that component.
DEFAULT_QUALITY_MIN = 0.55
DEFAULT_REGIME_CONFIDENCE_MIN = 0.40

COMPONENT_KEYS = ("confidence", "quality_score", "regime_confidence", "mtf_alignment")


def _clamp01(value: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def _renorm(value: float, lo: float, hi: float) -> float:
    """Map ``value`` from ``[lo, hi]`` onto ``[0, 1]`` (clamped)."""
    if hi - lo <= 1e-9:
        return _clamp01(value)
    return _clamp01((float(value) - lo) / (hi - lo))


def _side_sign(action: Optional[str]) -> float:
    a = (str(action or "")).lower()
    if a == "buy":
        return 1.0
    if a == "sell":
        return -1.0
    return 0.0


def _mtf_alignment_from_context(side_sign: float, market_context: Dict[str, Any]) -> float:
    """Derive an MTF-alignment in ``[0, 1]`` from ``htf_alignment_score``.

    ``htf_alignment_score`` is the sum of the 1h and 4h state codes (each in
    ``{-1, -0.5, 0, 0.5, 1}``), so its raw range is ``[-2, 2]``.  We sign it by
    the trade side and map ``[-2, 2] -> [0, 1]`` with the neutral point at
    ``0.5``.
    """
    htf = 0.0
    for key in ("htf_alignment_score", "htf_alignment"):
        if key in market_context:
            try:
                htf = float(market_context[key])
            except (TypeError, ValueError):
                htf = 0.0
            break
    return _clamp01(0.5 + 0.25 * side_sign * htf)


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


def compute_risk_score(signal: Any, market_context: Dict[str, Any],
                       config: Optional[Dict[str, Any]] = None) -> float:
    """Return a continuous risk score in ``[0.0, 1.0]`` for ``signal``.

    ``signal``         — an object with ``action``, ``confidence`` and
                         (optionally) ``quality_score`` / ``regime_confidence``
                         attributes (an ``AdvancedSignal``), *or* a mapping
                         with those keys.
    ``market_context`` — a mapping; only ``htf_alignment_score`` (alias
                         ``htf_alignment``) is read.
    ``config``         — the ``risk_scoring`` config section (or ``None`` for
                         defaults).  Recognized keys: ``weights`` (per-component
                         weights, default equal), ``quality_min`` /
                         ``regime_confidence_min`` (re-normalization floors).

    The result is the weighted average of the four normalized components; it
    does *not* itself decide entry — it feeds :func:`score_to_size_multiplier`.
    """
    components, _ = compute_risk_score_components(signal, market_context, config)
    weights = _resolve_weights(config)
    score = sum(weights[k] * components[k] for k in COMPONENT_KEYS)
    return _clamp01(score)


def compute_risk_score_components(signal: Any, market_context: Dict[str, Any],
                                  config: Optional[Dict[str, Any]] = None
                                  ) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Return ``(normalized_components, raw_inputs)`` for logging / analysis.

    ``normalized_components`` are the four ``[0, 1]`` values fed to the weighted
    average; ``raw_inputs`` are the pre-normalization values (useful for
    post-hoc analysis).
    """
    cfg = config or {}
    quality_min = float(cfg.get("quality_min", DEFAULT_QUALITY_MIN))
    regime_conf_min = float(cfg.get("regime_confidence_min", DEFAULT_REGIME_CONFIDENCE_MIN))

    def _get(name: str, default: float = 0.0) -> float:
        if isinstance(signal, dict):
            return float(signal.get(name, default) or default)
        return float(getattr(signal, name, default) or default)

    raw_conf = _get("confidence", 0.0)
    raw_quality = _get("quality_score", 0.0)
    raw_regime_conf = _get("regime_confidence", 0.0)
    action = signal.get("action") if isinstance(signal, dict) else getattr(signal, "action", None)
    side_sign = _side_sign(action)
    raw_mtf = 0.0
    for key in ("htf_alignment_score", "htf_alignment"):
        if key in market_context:
            try:
                raw_mtf = float(market_context[key])
            except (TypeError, ValueError):
                raw_mtf = 0.0
            break

    components = {
        # confidence is already in [0, 1].
        "confidence": _clamp01(raw_conf),
        # quality score is in [0, 1] but we re-normalize against [quality_min, 1]
        # so the old gate threshold maps to 0.
        "quality_score": _renorm(raw_quality, quality_min, 1.0),
        # regime confidence is in [0, 1]; re-normalize against [regime_conf_min, 1].
        "regime_confidence": _renorm(raw_regime_conf, regime_conf_min, 1.0),
        # MTF-alignment derived from the signed htf_alignment_score.
        "mtf_alignment": _clamp01(0.5 + 0.25 * side_sign * raw_mtf),
    }
    raw_inputs = {
        "confidence": raw_conf,
        "quality_score": raw_quality,
        "regime_confidence": raw_regime_conf,
        "htf_alignment_score": raw_mtf,
        "side_sign": side_sign,
    }
    return components, raw_inputs


def score_to_size_multiplier(score: float, config: Optional[Dict[str, Any]] = None) -> float:
    """Map a risk score in ``[0, 1]`` to a position-size multiplier in ``[0, 1]``.

    ``position_size_multiplier = clip(slope * score + intercept, 0.0, 1.0)``

    Defaults: ``slope = 2.0``, ``intercept = -0.5`` (the plan's example) ->
    score 0.25 -> 0.0x (no trade), 0.5 -> 0.5x, 0.75 -> 1.0x.  Both are
    config-tunable via the ``risk_scoring`` section keys ``size_slope`` /
    ``size_intercept``.
    """
    cfg = config or {}
    slope = float(cfg.get("size_slope", DEFAULT_SIZE_SLOPE))
    intercept = float(cfg.get("size_intercept", DEFAULT_SIZE_INTERCEPT))
    return _clamp01(slope * _clamp01(score) + intercept)


def build_risk_score_record(signal: Any, market_context: Dict[str, Any],
                            config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return a serializable trade-record fragment: the scalar score, the
    normalized components, the raw inputs, the weights and the resulting
    size multiplier.  Used by the backtester to log per-trade risk-scoring."""
    components, raw_inputs = compute_risk_score_components(signal, market_context, config)
    weights = _resolve_weights(config)
    score = _clamp01(sum(weights[k] * components[k] for k in COMPONENT_KEYS))
    return {
        "score": score,
        "size_multiplier": score_to_size_multiplier(score, config),
        "components": components,
        "raw_inputs": raw_inputs,
        "weights": weights,
    }
