# Strategy v1 — Low-frequency trend + volatility-target overlay

> Status: SPEC / not built. Supersedes the 'advanced' (MultiIndicatorConfluence) and Plan-D
> single-asset strategies as the active development direction (see DECISIONS.md, 2026-05-12).
> Driven by `docs/edge-diagnosis.md`: in this project's own data, the edge is in low-frequency
> risk management, not timed entries — a trailing-stop overlay on plain BH returned Calmar +1.76
> / 10% max-DD vs BH's −0.18 / 52% over the diagnosis window.

## 1. Problem statement & objective

Deliver **measurably better risk-adjusted performance than BTC buy-and-hold** (the project's founding goal) with a strategy simple enough that we can trust it: low turnover (friction stops mattering), few moving parts (few ways to overfit), and a transparent thesis (capture crypto's positive long-run drift while sitting out the deep drawdowns). Account context: $5k initial → $5k–$50k; at that size minimum contract sizes are not binding and maker execution is worthwhile.

**Primary success metric:** Calmar ratio and max drawdown vs two benchmarks — (B1) BTC buy-and-hold, (B2) trailing-stop-on-BH (the −10% trailing rule from the diagnosis). The strategy must beat **B2** out-of-sample on Calmar and max-DD. If it can't, there is no reason to run anything more complex than B2.
**Secondary:** total return ≥ B2; turnover ≤ ~50 round-trips/year; no single-day loss > ~1.5× a vol-target unit.

## 2. Universe

- v1: **BTC-USDT** only. Add **ETH-USDT** in v1.1 once BTC works, equal-risk-weighted. Nothing wider (no 50-asset cross-section — that's Plan E's territory and a different, more complex bet).
- Bars: **daily (1d)** primary; **4h** allowed only if a clean reason emerges. Explicitly NOT 5m/15m — the diagnosis showed intraday turnover is structurally a friction harvester here.

## 3. Signal — regime / trend filter (long-or-flat)

Pick ONE of these as the v1 trend definition (decide via the bake-off in §7, not by gut):
- **(a) Trailing-stop regime switch** (the diagnosis winner): in-market until price falls ≥ X% from the running high since entry; re-enter when price makes a new N-day high. X ≈ 10–15%, N ≈ 20–50 days — to be chosen on a coarse grid with walk-forward, NOT fine-tuned.
- **(b) Long-term moving-average filter**: long when close > the M-day SMA/EMA (M ≈ 100–200), flat otherwise. Optional confirmation: M-day slope > 0.
- **(c) Donchian channel breakout**: long on a new N-day high, flat on a new K-day low (N ≈ 50, K ≈ 20) — classic turtle-style.

All three are long-or-flat for v1. **Shorting is deferred** to v1.2: crypto's long-run drift is up, shorting doubles the ways to be wrong, and the diagnosis sign-flip test showed our intuitions about "just go short" are unreliable. Add shorts only if a short-side filter independently clears the §7 bar.

Signal discipline: every decision uses **closed bars only** (the lookahead-audit convention from `docs/lookahead-audit.md`). A daily decision at the close of day t uses data ≤ day t and executes at day t close or t+1 open (model whichever is realistic).

## 4. Position sizing — volatility targeting

This is where the Calmar lift actually comes from (constant fractional sizing is what sank 'advanced' — 95% of equity per trade on a no-edge signal).

- Estimate realized vol σ_t = annualized stdev of daily log-returns over a trailing window (e.g. 20–60 days), or an EWMA.
- Target portfolio vol σ_target (e.g. 15–25% annualized — choose so leverage stays sane).
- Position size (fraction of equity) = clip( σ_target / σ_t , 0 , L_max ) when the trend filter says "long", else 0. L_max ≈ 1.0 for v1 (no leverage); revisit later. So in calm uptrends you're ~fully invested, in volatile chop you're scaled down, in a downtrend you're flat.
- Rebalance: **daily**, but with a no-trade band — only adjust the position if the target size differs from current by more than a threshold (e.g. > 10–20% relative), to keep turnover and fees down.
- Hard floor/ceiling: never > L_max equity exposure; an absolute portfolio stop (e.g. −20% from equity high) flattens everything and stands aside for a cooldown — belt-and-suspenders on top of the trend filter.

## 5. Execution

- **Maker-first**: post-only limit orders at/near the touch, with a timeout → fall back to taker if unfilled after T seconds. At daily rebalance frequency the urgency is low, so maker fill rates should be high. Reuse the OKX/Blofin adapter work already in `scripts/`.
- Model in backtest: fee = maker rate when filled as maker (assume e.g. 80% maker / 20% taker until live data says otherwise), slippage = a small fixed bps + a size-impact term. Daily turnover at vol-target with a no-trade band is low enough that this is second-order — but model it honestly anyway.
- Funding: at daily holds funding matters (8h settlements × 3/day). Use the real funding series (`fetch_funding_history()` from Fase 3 already exists) — long positions pay positive funding. Include it in the backtest PnL. (Note: Fase 3 found funding has no *timing* edge, but it's still a real *cost* of holding longs.)

## 6. Risk controls

- Trend filter is the primary risk control (flat in downtrends).
- Vol-target is the secondary (smaller size when scary).
- Absolute equity drawdown circuit-breaker (−20% from high → flat + cooldown) is the tertiary.
- Per-day loss bound ≈ σ_target/√252 × current exposure — log and alert if exceeded (shouldn't happen without a gap).
- One position per symbol, no pyramiding in v1.

## 7. Validation plan — prove the edge BEFORE optimizing (binding, per DECISIONS.md)

1. **Random-entry null first.** Before any parameter search: does "long-or-flat by trend rule X" beat a null of random in/out periods with the same average time-in-market and the same friction, out-of-sample? Bootstrap ~1000×, report where the candidate sits. If it's inside the null band, the rule has no edge — stop, don't tune it. (This is the test that would have killed 'advanced' in a day.)
2. **Coarse grid + walk-forward, Calmar objective.** For the surviving rule(s): a small grid (e.g. 5×5, not fine-mazed), 3+ walk-forward splits, 70/30 train/test, optimize **Calmar** (consistent with the risk-adjusted goal). Report deflated Sharpe and the spread of Calmar across the whole grid (if it's flat — like Fases 4/5 — the parameters don't matter, don't pretend they do).
3. **Single holdout, evaluated once.** Reserve the last ~20% of data, touch it once at the end. Winner must land within ~1σ of its train Calmar AND beat both benchmarks (B1 BH, B2 trailing-stop-BH) on the holdout. If it doesn't beat B2 → ship B2 instead.
4. **Diagnosis template.** Run the `docs/edge-diagnosis/` checks on the final candidate (gross-vs-net per-trade expectancy, MFE/MAE capture, exposure/Brinson decomposition, IC of the trend signal vs forward returns). Gross per-trade expectancy must be clearly > 0 and > the cost floor.
5. **Robustness:** check it doesn't blow up in a sustained bull leg (trailing stops whipsaw), and report performance separately in bull / bear / sideways segments (the conditional metrics from Fase 1).
6. Only after all of the above: **paper-trade 2–4 weeks** (the project's P1 policy), with a hard go/no-go bar (must beat B2 on Calmar over the paper window). Then live.

## 8. Build milestones

- **M0** — backtest harness wiring: daily-bar backtest of a long-or-flat + vol-target strategy (extend `backtest/backtester.py` or a thin daily wrapper), with the §5 cost model and funding. Reuse the Fase-1 benchmark/conditional metrics.
- **M1** — implement benchmark B2 (trailing-stop-on-BH) as a first-class baseline in the backtester.
- **M2** — implement the random-entry null harness (§7.1).
- **M3** — bake off trend rules (a)/(b)/(c) through §7.1–§7.3; pick one (or conclude "none beats B2" → ship B2).
- **M4** — diagnosis template (§7.4) + robustness (§7.5) on the winner; write it up.
- **M5** — live wiring: maker execution, funding accounting, the circuit-breaker; deploy as a paper instance alongside Plan E.
- **M6** — 2–4w paper; go/no-go; live.

## 9. Explicit non-goals for v1

- No intraday timing, no indicator confluence, no ML, no funding/OI/on-chain *signals* (funding is used only as a cost). No multi-asset cross-section (that's Plan E). No shorting. No more than ~2 symbols. No parameter farm of variants — one configuration, chosen by §7, period.
