# Beta-decomposition of the momentum basket (2026-06-05)

**Trigger:** a critique that the dollar-neutral momentum basket is not *beta*-neutral
(high-beta coins move more, so equal dollars don't cancel) → hidden directional risk.
We measured it (don't guess — regress). `scripts/xs_beta_analysis.py`, OKX 3.5y panel,
causal 90d rolling beta vs BTC. Adversarially verified (3 latent bugs found + fixed:
a ddof mismatch inflating betas ~1.1%, a clip-dependent beta-neutral construction,
a mixed-base P&L decomposition; none flipped a sign).

## Findings
1. **Hidden beta is real and PRO-CYCLICAL — the critique was right.** Net beta of the
   dollar-neutral basket: mean ~0 but **std 0.34, range [−1.36, +0.67]**, net-long
   54% of days, and **corr(net beta, BTC 90d trend) = +0.51** → net-*long* beta when
   BTC has been rising, net-*short* when falling. Momentum longs the high-beta leaders
   in a bull and shorts the high-beta losers in a bear. (The +0.51 is a *fragile* point
   estimate — ~14 effective independent 90d windows; directionally sound, not precise.)
2. **It was a minority of the historical edge: ~15% of gross P&L was beta, ~85%
   idiosyncratic** — but time-concentrated. Regime split: **13% in the bull-heavy
   train, 38% in the flat 2024–25 holdout** (high there only because the out-of-bull
   *idiosyncratic* edge is ~0, so a small beta P&L is a big share of a small total).
3. **Beta-neutralising HURTS — the residual beta HELPED over this BTC-rising sample.**
   Reweighting legs to net beta 0: full-sample **172.6% → 98.2%**, holdout **−3.4% →
   −11.2%** (holdout-null 80th → 72.5th, walk-forward window-4 flips +2.9% → −8.0%).
   It still clears the *full-sample* null (98th) but that test is bull-dominated; every
   informative OOS metric degrades.

## Decision
**Beta-neutralisation = diagnostic / risk gauge, NOT a live change.** It strips a
positive-contributing exposure (lowering return everywhere) without fixing the real
problem — the OOS edge is thin and concentrated in the 2023 bull (docs/XS-TRIGGER-STUDY.md).
Do **not** ship a beta-neutral live executor on this evidence. Worth doing: **log the
realized net beta of the live HL book as a risk gauge** (we already track the venue
book; net beta = Σ w·β is a cheap add) so the pro-cyclical tilt is visible, not hidden.
The genuine open risk before real-money sizing remains regime-robustness, not the
residual beta.
