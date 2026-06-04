"""Broad-sweep candidate probes (M3).

Each candidate module exposes a `build_candidates()` returning a list of
`sweep_feasibility.Candidate` (single-asset, long-or-flat) or, for
market-neutral lanes, its own `run() -> FeasibilityVerdict`.
"""
