# Axis E — Entry Timing / Signal Information Content

Scope: 185 long-only 'advanced' trades, BTC-USDT 5m. Forward returns from raw OHLCV close (lookahead-free; entry mapped to its own bar). n≈185 → a Spearman of |0.10| has p≈0.17, |0.14|→p≈0.06, |0.20|→p≈0.006.

## 1. Information coefficient (Spearman ρ; * = p<0.05, ** = p<0.01)

| predictor | pnl_pct | fwd_1 | fwd_5 | fwd_15 | fwd_60 | fwd_240 |
|---|---|---|---|---|---|---|
| confidence | -0.139 | -0.04 | -0.05 | -0.05 | -0.114 | -0.06 |
| ind_quality_rr_ratio | -0.02 | 0.00 | -0.02 | 0.00 | -0.05 | -0.104 |
| ind_quality_atr_pct | -0.104 | 0.05 | -0.03 | -0.06 | 0.10 | 0.04 |
| ind_quality_volume_score | 0.04 | -0.05 | 0.01 | 0.07 | -0.02 | 0.00 |
| ind_quality_bb_pos / ind_bb_pos | -0.02 | -0.102 | -0.07 | -0.04 | -0.04 | -0.01 |
| ind_regime_confidence | 0.07 | 0.04 | 0.06 | 0.07 | -0.01 | 0.00 |
| ind_htf_alignment_score | (const ≈0) | — | — | — | — | — |
| ind_efficiency_ratio | 0.05 | **0.159*** | -0.07 | -0.05 | -0.08 | -0.05 |
| ind_rsi_value | -0.155* | -0.102 | -0.10 | **-0.257*** | -0.04 | -0.148* |
| ind_macd_hist | -0.123 | -0.106 | -0.075 | **-0.275*** | -0.077 | -0.109 |
| ind_trend_bias | 0.00 | -0.03 | -0.03 | -0.02 | -0.02 | -0.02 |

(Full table: `backtest/results/diag_e_ic_table.csv`; heatmap `diag_e_ic_heatmap.png`; scatter `diag_e_scatter.png`.)

Read-out:
- **confidence: IC ≈ 0** vs every forward horizon (|ρ|≤0.11, all p>0.12). Its only ≥0.1 hit is a *negative* −0.14 vs realized pnl_pct (p=0.06) — i.e. higher "confidence" trades did marginally *worse*. It also barely varies: 164/185 trades sit at 0.71, 21 at >0.74. Effectively a constant.
- **ind_quality_*: IC ≈ 0** across the board (max |ρ|≈0.10, no significant horizon). ind_quality_rr_ratio is near-discrete (62 trades pinned at 1.333) and uninformative. ind_quality_bb_pos == ind_bb_pos.
- **ind_regime_confidence: IC ≈ 0** (|ρ|≤0.07).
- ⇒ **This directly explains why Fase 5's continuous risk-score did nothing.** The score is a blend of confidence / quality / regime_confidence / MTF-alignment — every one of those inputs has zero information coefficient against actual forward returns. A weighted sum of noise is noise.
- The only inputs with a *statistically real* relationship are **ind_rsi_value (ρ=−0.26 vs fwd_15, p<0.001)** and **ind_macd_hist (ρ=−0.28 vs fwd_15, p<0.001)** — and the sign is *backwards*: the strategy issues buys preferentially when RSI / MACD-hist are relatively *high*, but high RSI/MACD-hist at entry predicts *lower* 15-bar BTC returns. Median-split confirms: high-RSI buys → fwd_15 −0.13%, win 7.6%; low-RSI buys → fwd_15 +0.08%, win 17.2% (same pattern for MACD-hist). So the strategy has a small but genuine *anti-edge* in its momentum gating. (ind_efficiency_ratio +0.16 vs fwd_1 is the only positive hit and it decays to 0 by 5 bars — momentum lasting 5 minutes, not tradable.)

## 2. Monotonicity (terciles)

confidence (only 2 effective buckets): mean pnl_pct −0.228 vs −0.230 — **flat**. win-rate 13.4% vs 4.8% (worse for "high confidence").
ind_quality_rr_ratio terciles: pnl_pct −0.217 / −0.244 / −0.223; win 9.7% / 8.1% / 19.7% — **non-monotone, noise**.
ind_regime_confidence terciles: pnl_pct −0.238 / −0.232 / −0.214; win 14.5% / 8.2% / 14.5% — **flat**.
No predictor produces a monotone, exploitable gradient in either mean PnL or win-rate.

## 3. Late vs early entries (prior run-up vs post-entry)

Mean prior return is *negative* at every lookback (prior_3 −0.15%, prior_12 −0.24%, prior_48 −0.15%) → entries are nominally **buying dips** (contrarian on average), consistent with the mean-reversion intent. BUT the dip does not revert: mean fwd_5/15/60 are all slightly *negative* (−0.017% / −0.023% / −0.014%). And **prior_12 vs fwd_15: ρ=−0.30 (p<0.001)** — the deeper the recent drop, the *more* it keeps falling. So within the dip-buys, the bigger dips are momentum-down continuations, not reversions. The strategy is buying falling knives in a market where 5m moves are mildly trend-persistent.

## 4. Is there ANY exploitable timing info? — No.

Mean forward returns are statistically indistinguishable from zero at every horizon (fwd_1…240: |t|≤0.8, all p≥0.44, ~50–57% positive). vs a 2000-draw random-entry bootstrap: observed fwd_5 mean −0.017% sits at the 23rd percentile of random entries — i.e. the entry timing is, if anything, slightly *worse* than throwing darts, never better. The only non-zero ICs (RSI/MACD-hist) point the wrong way.

## Implications for go/no-go

- **(1) Fix the exit — necessary but not sufficient.** Even a perfect exit on these entries is buying into ~zero-to-negative expected forward returns; you'd at best stop bleeding, not generate alpha. Axis E gives no support for "just fix exits."
- **(2) Redesign the entry signal — supported, with a specific lead.** The confidence/quality/regime apparatus is provably uninformative (kill it; stop tuning it). The one real relationship is that the strategy's momentum gates (RSI, MACD-hist) are *inverted* relative to the 5m edge. A redesign that (a) discards the confidence/quality/regime scores, (b) **flips the RSI/MACD-hist polarity** (buy on low RSI / negative MACD-hist, or short on high), and (c) respects the −0.30 prior-12-bar momentum persistence (don't catch deepening dips) is the only evidence-based path. Note effect sizes are tiny (|IC|≈0.25–0.3, fwd_15 means ≈0.1%) — likely below 5m round-trip costs/slippage, so even this is marginal.
- **(3) Abandon the strategy family — strongly on the table.** The MultiIndicatorConfluence "confluence" inputs carry no forward information and the only signal present is a small reversed momentum tilt. There is no demonstrated edge for a long-only confluence approach on BTC 5m here. Recommend: do NOT continue iterating this signal stack; either pivot to the (2) inverted-momentum micro-experiment as a *bounded* test, or retire the family.
