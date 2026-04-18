# Plan D — Step 3 Sweep: Config Variations

**Generated:** 2026-04-18T06:34:48.947919+00:00
**Test slice:** 2026-01-12 → 2026-04-17 (out-of-sample relative to chop classifier training)
**Backtest config:** fee=0.0006 slip=0.05% risk=0.5%

## Variation results

| Variant | p_chop | z_entry | z_stop | Trades | WR | W/L | Expect ($) | Net P&L ($) | Gross ($) | Fee share | Sharpe | Max DD |
|---------|--------|---------|--------|--------|-----|-----|------------|-------------|-----------|-----------|--------|--------|
| V0_default | 0.60 | 2.0 | 3.5 | 403 | 25.8% | 0.74 | -0.159 | **-64.06** | -28.29 | 126% | -20.70 | -55.8% |
| V1_strict_gate | 0.75 | 2.0 | 3.5 | 45 | 24.4% | 0.96 | -0.228 | **-10.28** | -4.67 | 120% | -6.13 | -9.9% |
| V2_wide_geom | 0.60 | 2.5 | 4.0 | 238 | 27.3% | 0.92 | -0.167 | **-39.86** | -14.31 | 179% | -12.97 | -34.7% |
| V3_strict_wide | 0.75 | 2.5 | 4.0 | 31 | 22.6% | 0.86 | -0.262 | **-8.13** | -4.14 | 96% | -6.04 | -7.9% |
| V4_very_strict | 0.85 | 2.5 | 4.5 | 2 | 0.0% | 0.00 | -0.717 | **-1.43** | -1.18 | 22% | -2.89 | -1.2% |

## Best: V4_very_strict (FAIL)

### Gate breakdown

- FAIL — WR > 50%  (observed: 0.0%)
- FAIL — W/L ratio >= 0.8  (observed: 0.00)
- FAIL — Expectancy > 0  (observed: -0.7169)
- FAIL — Sharpe > 1.0  (observed: -2.89)
- PASS — Max DD > -15%  (observed: -1.2%)

## Conclusion

No variation clears the step 3 gate. Patterns across the sweep:

- Best net P&L: V4_very_strict at $-1.43 (still losing; fee share 22%)
- Worst net P&L: V0_default at $-64.06

### Diagnosis

- **Chop classifier has real signal (AUC 0.6436) but does not improve on-signal WR enough.** At P(chop)>0.6 precision is 68.5% — meaning 32% of 'chop' predictions precede non-chop behavior, which drives stops.
- **Selection bias in the classifier target.** The classifier was trained on ALL bars, not bars *conditional on |z|≥2*. Overextended bars may have systematically different reversion dynamics from the general population — and the classifier is never asked that conditional question during training.
- **Friction floor remains the unspoken constraint.** At 5m bars with 0.22% round-trip friction, the edge from z-score reversion on a single asset is too small relative to fees. Fee share >60% across all variations confirms this — the strategy generates some gross alpha but fees eat most of it.

### Next step

Per PLAN-D-mean-reversion.md step 3 failure policy: **stop Plan D, document in DECISIONS.md, escalate to Plan E or γ.** Do not proceed to walk-forward — the in-sample step 3 backtest has already failed.

Candidate next moves (for user decision on return):

1. **Plan E** — cross-sectional multi-asset ranking. Higher implementation cost but fundamentally different signal source. Cross-sectional alpha is less friction-constrained because long/short pairs partially hedge each other.
2. **Plan D-v2: conditional classifier.** Retrain the chop classifier only on bars where |z|≥2, so it learns the conditional reversion probability. This is a targeted fix to the selection-bias flaw but may simply yield AUC ≤ 0.52 on the narrower problem — if so, we're back to γ.
3. **γ (shutdown).** Accept that retail-scale BTC-USDT 5m strategies are friction-bound below viability. Halt trading indefinitely. Prod bot remains stopped from 2026-04-17.