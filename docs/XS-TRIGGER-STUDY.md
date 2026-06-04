# Rebalance trigger study — time-based vs event-based (2026-06-04)

**Question (user):** is the cross-sectional momentum rebalance only time-based, or
can we optimise by triggering it dynamically (event-based)?

**Answer: keep the fixed time-based clock. No dynamic trigger beats it.** And the
bigger truth the study surfaced is that the trigger is *second-order* — the edge
is concentrated in one bull regime.

`scripts/sweep_xs_triggers.py` (OKX 3.5y daily panel, lb=120/m=3). Adversarially
verified: a v1 had four real bugs (verdict-label gate, OOS boundary-reset,
turnover-unmatched null framing, a drift metric blind to leverage); v2 fixes all
four — **continuous OOS** (book carried across the train/test boundary like a live
runner), a **leverage-aware drift trigger** (fires on neutrality OR gross-leverage
drift — its fair best shot), a **zero-cost control**, and a corrected gate.

## Results (HL 4.5bps cost, ~6%/yr funding)

| trigger | net % | Sharpe | null %ile | rebals | turnover/yr | cont. holdout net | hold null |
|---|--:|--:|--:|--:|--:|--:|--:|
| fixed-3d | 89 | 0.63 | 99.5 | 380 | 58.5 | −6.0 | 82.5 |
| **fixed-5d (validated)** | **173** | **0.86** | 98.5 | 228 | 42.9 | −3.4 | 80.0 |
| fixed-7d | 111 | 0.71 | 99.0 | 163 | 37.1 | −11.9 | 63.0 |
| fixed-10d | 118 | 0.72 | 98.0 | 114 | 29.3 | +2.4 | 73.5 |
| drift(lev)-10% | 98 | 0.67 | 95.5 | 109 | 31.3 | +8.9 | 84.0 |
| drift(lev)-20% | −19 | 0.08 | 69.0 | 41 | 18.8 | −1.2 | 70.5 |
| signal-change-1d | 92 | 0.64 | 99.0 | 400 | 95.6 | −18.1 | 71.5 |
| signal-change-5d | 72 | 0.57 | 98.5 | 178 | 49.6 | −11.7 | 63.0 |

Zero-cost control (fixed-5d): gross **+323%**, clears null at **98.7th** → the
momentum-ranking edge is **real**, not a cost-asymmetry artifact.

## Findings
1. **The alpha is in WHICH assets you hold, not WHEN you rebalance.** The ranking
   clears the null even at zero cost; the cross-sectional IC of 120d momentum is
   +0.06/+0.07 (p<0.05) flat across 3–7d and decaying by 10d. So a faster clock
   captures no fresher signal worth its cost.
2. **Event triggers lose from both ends (cost vs signal-decay).**
   - *signal-change* over-trades near rank ties (95.6 turns/yr vs fixed's 42.9) —
     it trails fixed-5d even GROSS, so its extra turnover is pure drag.
   - *drift-band*, even leverage-aware, fires on neutrality/leverage not signal, so
     it tracks the momentum ranking less tightly → it's respectable (98%) but below
     fixed-5d, and the 20% band breaks (−19%).
3. **Cadence is a wide indifference band (≈3–7d), not a 5d optimum.** Block-bootstrap
   CIs for 5d−7d and 5d−10d straddle zero — they're statistically indistinguishable.
   Keep the validated 5d (it's in the band); 7d is equally defensible and slightly
   lower-turnover if cost-reduction ever matters. Do not switch on this evidence.
4. **BIGGER caveat (first-order, above the trigger question):** the entire edge is
   concentrated in the **2023 bull**. In the continuous 2024–25 holdout the strategy
   is ~flat-to-negative for **every** trigger (fixed-5d **−3.4%**), and in a 4-window
   walk-forward each trigger clears the null in only **1/4** windows. The momentum
   lane's out-of-bull robustness is weak — that, not the rebalance trigger, is the
   real risk to size before mainnet.

## Decision
Keep `rebal=5` fixed time-based. Do NOT add a dynamic/event-based trigger — it adds
cost or breaks neutrality without adding edge. The maintenance-resize already in
`_execute_live` (corrects intra-cycle drift at each fixed rebalance) is the right and
sufficient drift control. The regime-concentration caveat (finding 4) is the item
that matters for go-live sizing.
