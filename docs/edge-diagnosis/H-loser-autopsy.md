# Axis H — Loser (and winner) autopsy

Scope: 185 'buy' trades from `diag_trades.csv`, candles `BTC-USDT_5m.csv`.
Per-trade MFE = (max high entry→exit − entry)/entry; MAE = (min low entry→exit − entry)/entry; post-exit return measured from exit_price (positive = price kept moving the trade's intended/long direction). Enriched data: `backtest/results/diag_h_enriched.csv`. Plots: `backtest/results/diag_h_buckets.png`, `diag_h_mfe.png`.

Note: actual CSV split is **162 losers / 23 winners** (not 117/68). "take_profit" is an exit *trigger*, not a money outcome — only 23 of 68 TP exits actually closed green; the other 45 TP exits lost money (friction ate the micro-target). Total PnL −$37.84 (losers −$39.08, winners +$1.24).

## 1. Loser-cause histogram (162 losers)

| Bucket | n | $ lost | mean MFE | mean MAE | mean bars | exit reasons |
|---|---|---|---|---|---|---|
| (a) WRONG DIRECTION (MFE<~0.2%, exits near MAE via stop) | 16 | −$5.85 | 0.08% | −0.35% | 3.3 | 16 stop_loss |
| (b) RIGHT DIRECTION, EXITED TOO EARLY (MFE>0.3%) | 11 | −$3.45 | 0.44% | −0.34% | 3.0 | 8 stop, 2 TP, 1 stale |
| (c) WHIPSAW / NOISE (|MFE|&|MAE| both small, killed by cost) | **135** | **−$29.78** | 0.10% | −0.17% | 3.9 | 62 stop, 43 TP, 27 stale, 3 maxhold |
| (d) GAP / SLIPPAGE | 0 | $0 | — | — | — | — (max adverse fill vs candle low ≈ −0.05%, negligible) |
| (e) REGIME FLIP | folded into above | | | | | regime is ~constant 'range'; no material flip detectable |

Dominant bucket by a wide margin: **(c) whipsaw/noise — 83% of losers, 76% of all loss dollars.** (a)+(c) together = 93% of losers. The "exited too early" bucket (b) is tiny: 11 trades, $3.45.

Supporting facts: 86% of losers never had MFE above the 0.22% round-trip friction; 53% never got above 0.10% favorable; only 2 losers ever showed >0.5% MFE, none >1%. Stop-loss losers hit a mean MAE of −0.27% on a median 2-bar hold — the stop is right behind entry and noise alone trips it. The 28 stale_trade exits (mean 6 bars, MFE 0.10%, MAE −0.15%) are pure flat churn, −$5.42.

## 2. Winners: genuine vs lucky (23 winners, all exited via take_profit)

| Kind (MFE threshold 0.35%) | n | $ won | mean MFE | mean pnl% |
|---|---|---|---|---|
| genuine (MFE > 0.35%) | 7 | +$0.76 | 0.51% | 0.12% |
| lucky_whipsaw (MFE < 0.35%) | 16 | +$0.47 | 0.25% | 0.03% |

70% of "winners" are lucky whipsaws that closed green by a hair. Even the 7 "genuine" ones captured only ~24% of their own MFE; across all winners, realized pnl% is **18% of available MFE** (median capture 14%). There is no real winning mechanism — the TP target is set so small that wins are indistinguishable from noise that happened to tick up first.

## 3. Post-exit continuation

| Group | post-12bar (mean / median) | post-48bar (mean / median) | % post-12 > 0 |
|---|---|---|---|
| Losers | +0.094% / +0.098% | +0.122% / +0.076% | 65% |
| Winners | −0.150% / −0.093% | −0.354% / −0.047% | 35% |
| All | +0.063% / +0.083% | +0.063% / +0.074% | 62% |

After losers exit, price drifts *up* (the long direction) ~0.1% over the next hour — but this is just the gentle market-wide uptrend, not a sign the trade was about to work (the magnitude is below friction and the losers had already gone the wrong way). After winners exit, price *reverses* slightly — the TP got out near a local top, so exits aren't leaving much on the table. The b-bucket (n=11) does show +0.50% mean post-exit continuation, confirming a handful of stopped-out trades would have worked — but it's 11 trades / $3.45, not the problem.

## 4. Synthesis — direction vs exit vs noise

$ split of the −$37.84: whipsaw/noise −$29.8 (≈79%), wrong-direction −$5.9 (≈15%), exited-too-early −$3.5 (≈9%), gap/slippage ≈$0. Decisive structural fact: **mean MFE across all 185 trades is 0.14% vs mean |MAE| 0.20%** — the entry signal puts price, on average, *closer to the adverse extreme than the favorable one*. The signal has no positive directional information; combined with sub-0.1% typical excursions, every trade is a coin-flip on a wiggle that 0.22% friction turns into a guaranteed loss.

This is **not an exit problem** (fixing TP/stop/stale only recovers ~$3–6 of $38, and would surface more (a)-type losses since wider stops sit longer in unfavorable noise). It is **not primarily a wrong-way-conviction problem** either — the strategy isn't confidently wrong, it's *confidently nothing*. It is a **NOISE / NO-EDGE problem with a cost overlay**: the strategy fires ~185 times into 5m BTC range chop where its indicators carry zero forward signal, holds 2–4 bars, and bleeds the round-trip cost 162 times.

## Implications for go/no-go

- **(1) Fix the exit — NO.** Best case recovers <15% of losses; the engine is throwing away almost nothing because there is almost nothing to keep (winners already capture only 18% of a 0.3% MFE).
- **(2) Redesign the entry signal — only if a *fundamentally different* signal is on the table.** The current MultiIndicatorConfluence entry shows negative directional edge (MFE < |MAE|) on this instrument/timeframe. Tweaking thresholds/weights of the same indicator stack won't fix a sign-flipped edge. A redesign would need (a) a real edge hypothesis validated out-of-sample on returns >> 0.22% friction, and (b) far fewer, higher-conviction entries — not 185 churns over a year.
- **(3) Abandon the strategy family — strongly supported.** 'advanced' = confluence-of-lagging-indicators on 5m BTC in 'range' regime is structurally a friction-harvesting machine for the exchange. Axis H finds no salvageable winning mechanism. Recommend NO-GO on continuing to iterate this family; if anything proceeds, it should be a clean-sheet edge built and validated before any further parameter work.
