# Findings — Why the baseline strategy has 12% win rate

**Date:** 2026-04-18
**Trigger:** Plan A step 3 surfaced a 9.7% WR over 3 months. User requested
investigation before continuing Plan A (option 2).
**Dataset:** 106,559 5-minute BTC-USDT candles covering 2025-04-12 to
2026-04-17 (just over 12 months), 185 baseline trades.

## Headline

The low WR is **not period-specific**. It is the product of **three
independent structural issues**, each of which matters more than the
funding-rate question Plan A was trying to answer. Adding a funding gate
on top of this base is mechanically helpful (skips ~11 losers for every
winner it skips) but cannot fix any of them.

**Recommendation: pause Plan A steps 4-6. Fix the three structural issues
first. Re-evaluate funding gate once the base strategy is sane.**

## The three structural issues

### 1. The bot never shorts

Over 185 trades across 12 months, including clearly trending-down periods:
**0 short trades.** 100% long.

Root cause: `advanced_strategy.py:110` defaults `range_allow_shorts =
False` unless the config explicitly passes `allow_shorts: true` inside
the `mean_reversion` block. The baseline config used by
`run_baseline.py` and `funding_gate_backtest.py` does not pass it. Short
signals in range regime are gated behind `self.range_allow_shorts`, so
zero shorts are emitted.

Trend-regime shorts exist in code too, but **they never fire either** —
see issue #2.

**Impact.** Half the market is unreachable. On a year that included clear
down moves, the bot could only flip long or stay flat — it could never
short the drop. This alone caps realistic annual return at roughly half
of what a dual-direction version would produce.

### 2. The regime classifier emits "range" 100% of the time

Across 185 trade bars over 12 months: every single regime label is
`"range"`. The classifier's bull_trend / bear_trend / breakout / chop /
other outputs never appear.

This was partially predicted by the AUC 0.503 finding (the regime
features have no predictive power) but the *uniformity* is new — it
means the soft-confluence scoring (`bull_score >= trend_min_score=5.0`)
never reaches threshold on real data. With the trend_min_score set to
5.0 and seven binary conditions, we are effectively demanding all 5-7
trend conditions fire simultaneously — which never happens on the
actual data distribution.

**Impact.** Range-regime trading logic drives 100% of signals. The whole
trend-following machinery that the last 10 rounds of optimization were
tuning is inactive. Lowering `trend_min_score` would be the immediate
fix — but that is parameter tuning of a frozen feature set, which
`DECISIONS.md` bans. The *right* fix is either (a) re-examine the
scoring scale, or (b) accept that the bot is now a range-only strategy
and stop pretending it has a trend mode.

### 3. TP levels are below the friction floor

Round-trip friction: 2 × 0.06% fee + 2 × 0.05% slippage = **0.22%** per
round trip.

Take-profit is set at entry + 3 × ATR. On 5m BTC, ATR is typically
$40-$150 against a $100k price, so 3×ATR is often 0.12%-0.45%. When
3×ATR is below 0.22%, hitting TP produces a **gross-positive, net-negative**
exit — the trade closes at the TP level, but fees and slippage on the
round-trip consume the gain.

Diagnostic evidence:
- 68 "take_profit" exits → only 33.8% winners. **45 of 68 TP exits were
  net losses.**
- Average win $0.054, average loss $0.241. Win:loss ratio 0.22.
- Breakeven WR at that ratio: 81.8%. Actual: 12.4%.

**Impact.** Even a mechanically perfect signal at this TP/SL geometry
would need >80% accuracy to break even. That is unachievable for any
directional strategy on OHLCV features. Either:
- Widen TP multiplier (e.g. 5 × ATR) so the winners clear friction, or
- Widen SL multiplier to let winners run longer before stale-exit fires,
  or
- Reduce friction — which is Plan B (maker execution) — which we
  deferred specifically because the nominal 4bps saving seemed small.
  At 12% WR with TPs inside the friction floor, **4bps saved on entry
  is a ~20% relative reduction in the friction floor**, which moves the
  TP-net-positive threshold meaningfully. B may deserve revisiting.

## How the issues interact

- Issue #2 forces all trades into range regime
- Issue #1 blocks shorts in range regime
- Result: the bot is permanently long-biased in a mode the classifier
  calls "range" for 100% of the year
- Issue #3 ensures that even when signals are directionally right, they
  often don't clear friction when TP hits
- Net: 12.4% WR, 0.22 win:loss ratio, 81.8% breakeven WR — unwinnable.

## Funding gate in context

The funding gate filters **losing** trades more than winning trades
(11:1 ratio, confirmed over 12 months at best threshold). That is real
edge from the funding signal. But in absolute terms:

- Baseline net P&L over 12 months: **-$37.84**
- With best gate: **-$29.54** (improvement of $8.30)
- Both still losing. Both still with the same underlying structural
  problems.

**The gate is a working intervention on a broken base.** Deploying it
to prod would improve results at the margin while fixing none of the
root causes.

## Three paths forward

**Path 1 — Fix structural issues, then resume Plan A.**
  - Enable shorts in range regime (add `allow_shorts: true` to
    `mean_reversion` config block)
  - Investigate regime classifier: why never bull/bear/breakout? Either
    rescale features, lower trend_min_score with explicit tracking, or
    accept range-only and delete trend machinery.
  - Widen TP multiplier to ≥ 5 × ATR OR activate Plan B (maker entries
    to reduce friction floor).
  - Re-run baseline, verify WR > 30% and expectancy positive gross.
  - Then run step 3 funding backtest again. Gate may be even more
    valuable (or no longer needed) at a sane baseline.

**Path 2 — Replace the strategy entirely.**
  - 12.4% WR with a 0.22 win:loss ratio after 12 months is not a
    strategy that can be fixed by filters. Skip to Plan D (mean
    reversion for chop) or Plan E (cross-sectional) with a clean-slate
    strategy. Keep the current one running live with defensive sizing
    while building the replacement.
  - **But** — the current bot *is* running live with user's real money
    at 1.5% risk / 1.8×ATR SL / 4.0×ATR TP (this session's earlier
    config deploy). So "keep running" means continuing to bleed. The
    wider 4.0×ATR TP is already an improvement over the 3.0×ATR
    backtest default, which may help live vs backtest — needs
    measurement.

**Path 3 — Accept truth and stop trading until fixed.**
  - The strategy has lost money consistently over 12 months of backtest.
    The live version at 1.5% risk is bleeding slowly rather than
    quickly, but it is bleeding. Stop the service until a structural
    fix is in place.

## My recommendation

**Path 1 for the short term, Path 3 for the account.** Specifically:

1. **Now:** stop `trading-bot.service` on prod. The live bot is running
   a strategy that has not demonstrated edge over 12 months of historic
   data. Running it live at 1.5% risk is slow bleed, not investment.
2. **This week:** three quick fixes:
   - Set `mean_reversion.allow_shorts: true` in config
   - Set `strategy.take_profit_atr_mult: 5.0` (from current 4.0)
   - Log regime classifier output for 48h to characterize the scale
     problem before tuning trend_min_score
3. **Next:** re-run baseline, verify WR > 30% on 12 months. If yes,
   re-run Plan A step 3. If no, escalate to Plan D/E replacement.
4. **Do not deploy Plan A steps 4-6 until the base WR is sane.** The
   funding gate works, but adding it to a broken base is premature.

Tasks parked for now: Plan A steps 4 (live poller), 5 (shadow mode), 6
(live report harness). Code written for steps 1-3 remains valid and will
be reused once the base is fixed.

## What to do with DECISIONS.md

The feature-freeze decision recorded on 2026-04-18 is still correct:
further threshold tuning of the seven-condition feature set will not
generate edge. The three structural issues above are **not** feature-set
tuning:
- Issue #1 is a config bug (short signals disabled unintentionally)
- Issue #2 is a classifier-output problem (scale issue, not weights)
- Issue #3 is execution geometry (TP multiplier + friction), not signal

So fixing them does not violate the freeze. A follow-up entry in
DECISIONS.md will record the decision to pause Plan A and fix structural
issues first.
