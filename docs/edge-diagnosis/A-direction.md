# Axis A — Direction (sign quality of the entry call)

Strategy `advanced` (MultiIndicatorConfluence), BTC-USDT 5m, 185 completed trades, **0 shorts ever fired** despite `allow_shorts=True` → long-only in practice.

## 1. Forward-return / hit-rate of long entries vs unconditional

| h (bars) | n | hit-rate (BTC up) | mean fwd ret | median fwd ret | uncond mean | uncond hit |
|---|---|---|---|---|---|---|
| 1  | 185 | 54.6% | +0.0018% | +0.012% | ~0% | 49.9% |
| 5  | 185 | 52.4% | **−0.017%** | +0.009% | ~0% | 50.0% |
| 15 | 185 | 51.4% | **−0.023%** | +0.014% | ~0% | 50.4% |
| 60 | 185 | 56.8% | **−0.014%** | +0.056% | ~0% | 50.6% |

Entry→exit: mean `pnl_pct` **−0.228%**, median **−0.262%**, win-rate (pnl>0) **12.4%**. So even though the *median* short-horizon BTC move after entry is mildly positive, the *mean* is negative (a few sharp adverse moves), and the realized trade outcome is far worse than the raw forward move — the **friction (~0.22% round-trip) on a 3.75-bar average hold swamps any directional signal**. The intraday "edge" (median ~+0.01–0.06%) is an order of magnitude smaller than costs.

## 2. NULL distribution (1000× draws of 185 random entry bars)

| h | actual hit | null hit (mean) | hit %ile | hit z | actual mean fwd | mean-fwd %ile | mean-fwd z |
|---|---|---|---|---|---|---|---|
| 1  | 54.6% | 49.9% | 88.6 | +1.28 | +0.0018% | 57 | +0.18 |
| 5  | 52.4% | 50.1% | 71.0 | +0.65 | −0.017% | 24 | **−0.78** |
| 15 | 51.4% | 50.5% | 56.8 | +0.25 | −0.023% | 26 | **−0.65** |
| 60 | 56.8% | 50.6% | 95.1 | +1.66 | −0.014% | 42 | −0.22 |

Verdict: **statistically indistinguishable from random.** The hit-rate is a hair above 50% (z ≈ 1.3–1.7, never p<0.05), but the mean forward return actually sits *below* the null median at h=5/15 — i.e. when the signal is "right" it's right by a tiny amount, and when wrong it's wrong by more. corr(BTC fwd-5 ret, trade pnl_pct) ≈ +0.10 — essentially noise. The directional signal is **not informative; it is, if anything, mildly anti-informative on the magnitude axis** (consistent with buying into short-lived pops that mean-revert before the stop). See `diag_a_null_meanfwd.png`, `diag_a_null_hitrate.png`, `diag_a_fwd_vs_pnl.png`.

## 3. Long-only stance vs bar-regime (trailing-30d BTC rule)

| bar regime | n (frac) | win-rate | avg pnl_pct | total PnL $ | BTC fwd-60 mean |
|---|---|---|---|---|---|
| bull     | 60 (32%) | 16.7% | −0.197% | −$11.29 | +0.24% |
| bear     | 35 (19%) | 20.0% | −0.237% | −$6.69  | −0.19% |
| sideways | 90 (49%) | 6.7%  | −0.246% | −$19.86 | −0.12% |

It loses money in **every** regime. ~half the entries land in `sideways` (chop) where win-rate collapses to 6.7%. Mirror-image short-only estimate (flip the fwd-60 sign, same ~0.22% friction): bear trades would average ≈ +0.19%−0.22% ≈ ~−0.03% gross — i.e. flipping to shorts in bear would *reduce* the loss but **not** turn profitable, because the raw moves are too small relative to friction. Conclusion: "always long, never short" is mildly costly (the period BTC net-fell −9.5%) but it is **not the primary problem** — the primary problem is that the per-trade signal has no exploitable magnitude in any direction at this holding horizon, and the cost structure is fatal.

## 4. Strategy vs alternatives

| stance | return on $115 |
|---|---|
| Strategy (always-long, this signal) | **−32.9% ROI / −$37.84** |
| Always-long (benchmark, passive BTC) | −9.5% |
| Always-flat | 0% |
| Random-entry, same count(185), same ~4-bar hold, with friction | **≈ −41% of notional summed** (mean net per-trade −0.221%) — i.e. the strategy (sum pnl_pct −42.2%, mean −0.228%/trade) is **indistinguishable from randomly entering and paying the spread**. |

The strategy ≈ a random number generator that pays 0.22% friction per round trip, ~185 times.

## Implications for go/no-go

- **(1) Fix the exit structure — partial help, not a cure.** The signal carries near-zero directional information, so a better exit can only stop the bleeding from friction (fewer/longer trades, wider stops so 1–2-bar noise doesn't trip them). It cannot manufacture alpha that isn't in the entry. Best case: drag loss toward ~0, not toward positive.
- **(2) Redesign the entry signal — required if any version of this is to live.** Hit-rate ~52% with z<2 and negative mean-fwd magnitude means the current confluence vote produces no edge. A redesign must (a) target a horizon where BTC moves dwarf 0.22% friction (hours/days, not 1–4 bars), (b) be allowed/able to go short, and (c) demonstrate forward-return separation from the null *before* any backtest. This is real research, not tuning.
- **(3) Abandon this strategy family — strongly supported by axis A.** Long-only confluence on 5m BTC with this cost structure is structurally a coin-flip minus the spread, in every regime. Combined with Fases 3–6 NO-GOs, the directional core has no demonstrable edge. Recommendation from this axis: **abandon (3)**; if leadership wants to salvage, only path is a ground-up entry redesign on a much longer horizon with shorts enabled (2) — and exit fixes (1) alone are not viable.
