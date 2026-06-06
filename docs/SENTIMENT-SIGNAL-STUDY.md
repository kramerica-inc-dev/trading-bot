# Free exogenous sentiment signals — Phase-0 edge gate (2026-06-06)

**Question (user / plan `eerst-even-terug-naar-glittery-matsumoto`):** build a
forward-looking *sentiment meter* from FREE, structured, EXOGENOUS data that gives
the bot an edge over purely-reactive traders. Two uses, tested side-by-side on the
same data, before any infra is built:

- **Track A — risk-off (PRIMARY, good prior):** does a causal "risk-elevated" flag
  materially WORSEN the live dollar-neutral MOMENTUM book's forward DOWNSIDE
  (CVaR-5 / max-DD / %-negative / forward-Sharpe), out of sample, past a
  frequency+clustering-matched random-flag null and a shuffle sham? For a neutral
  book this is factor/dispersion risk, not "market down" per se.
- **Track B — directional alpha (RESEARCH, skeptical prior):** does the signal
  predict forward RETURN DIRECTION past the null — **long-side and short-side
  measured SEPARATELY** (the risk⇄reward asymmetry; shorting has different
  cost/risk)? The prior is skeptical because price-derived regime-tilt was beta,
  not alpha (`docs/XS-SENTIMENT-TILT-STUDY.md`, `docs/XS-BETA-STUDY.md`). The open
  question is whether EXOGENOUS data carries directional info price-data didn't.

**Answer: NOTHING CLEARS THE GATE — a clean, money-saving null result.** No signal's
risk-flag worsens the book's forward downside OOS past the null+sham (Track A: all
KILL). No signal predicts forward BTC direction past the random-entry null on either
side (Track B: every long leg AND every short leg KILL, ≤92.8th and ≤87.0th
percentile respectively, gate 95). Per the plan's decision tree this is the
**"NEITHER" branch: documented null, no infra, no integration.** Build nothing.

`scripts/study_sentiment_signals.py`, OKX 3.45y daily panel (10 assets, 1259d;
book-returns 1258d), train-70 / OOS-30 (book OOS span 2025-05-22 → 2026-06-03),
fwd-window 5d (= rebal cadence). Reuses the book-returns + IC + null harness
(`backtest/sweep/xsectional.py`), the single-asset Candidate gate + signal-IC +
sham (`scripts/sweep_feasibility.py`), and the random-entry null
(`backtest/random_entry_null.py`). Writes
`backtest/results/sweep/sentiment_signals.json`. Touches NO live runner / config /
state / git / LXC.

## First-wave signals (already-available free data; no new network backfills)

All causal (value at t uses only data ≤ t), aligned to the daily book index by
strictly-past as-of (reindex + ffill):

| signal | source | construction |
|--|--|--|
| **dvol_level** | `deribit_dvol_BTC.csv` | causal 252d trailing percentile of the BTC DVOL implied-vol index |
| **dvol_change** | `deribit_dvol_BTC.csv` | 5d DVOL change, z-scored over a trailing year |
| **btc_vol_ratio** | OKX BTC daily | 5d / 30d std of daily log-returns (`compute_vol_halt`, daily-adapted) — *price-derived, not exogenous* |
| **breadth** | OKX panel | fraction of the universe above its 50d SMA (`compute_breadth_skip`) — *price-derived* |
| **dispersion** | OKX panel | causal percentile of the cross-sectional std of trailing-20d returns (`regime_classifier_e` logic) — *price-derived* |
| **funding_extreme** | `funding_btc_usdt.csv` | causal percentile of \|daily-summed 8h BTC funding\| (crowded positioning) |
| **regime_e_daily** | OKX panel | DAILY analogue of `regime_classifier_e`'s loss-tail logistic, fit on TRAIN only, applied OOS — *price-derived* |

Only **DVOL** and **funding** are genuinely exogenous (options market / perp
positioning); the rest are price-derived re-expressions already studied in the XS-*
arc. They are included as the plan's named building blocks and as controls — and,
as expected, they behave like beta. OKX per-asset funding has only ~3mo history
(283 rows), so the cross-sectional **funding dispersion** signal is not built this
wave (see deferred note). DVOL covers the full panel; BTC funding covers most of it.

## Lookahead poison-test (the #1 trap) — PASSED

The trailing-percentile / vol-ratio / breadth family is poison-tested: poison the
FUTURE tail of a source series to +1e9 and confirm the earlier percentile-rank
values are byte-identical (`poison_test_pct_rank`). PASSED. `regime_e_daily` is fit
on TRAIN-fold rows only with a TRAIN-quantile label threshold (no OOS peek). Track-A
flag thresholds are TRAIN-fold quantiles. Track-B alignment is reindex+ffill of
strictly-past values, and the IC uses `close.shift(-h)` forward returns. The book
return `port[t]` is over day t→t+1, decided at close of t — so a signal at t is
correctly causal for it. No data fabrication; DVOL/funding/panel are the real CSVs.

## Track A — does a risk-flag worsen the book's forward downside, OOS?

Flag = signal in its risk-elevated TRAIN tail (high DVOL/vol/dispersion/funding/
regime-p, or LOW breadth). Compare the book's forward-5d returns conditional-on-flag
vs unconditional, on the OOS holdout only, vs a frequency+clustering-matched
random-flag null (2000 reps, geometric in/out runs like `make_random_schedule`) and
a shuffle sham (5 reps). ADVANCE rule: conditional mean OR Sharpe in the worst 5% of
the matched null AND conditional CVaR-5 worse than the null median AND worse than
unconditional in level — i.e. the flag must depress the *central* forward return,
not just nick the tail.

Unconditional OOS book forward-5d: mean +0.00057, CVaR-5 −9.60%.

| signal | fires | run | cond mean (unc +0.057%) | mean-%ile | cond CVaR5 (unc −9.60%) | CVaR-%ile | sham fails | verdict |
|--|--:|--:|--:|--:|--:|--:|:--:|:--:|
| dvol_level | 23% | 12.4d | −0.110% | 39.6 | −11.04% | **9.5** | yes | **KILL** |
| dvol_change | 21% | 2.6d | +0.145% | 54.9 | −9.54% | 37.3 | yes | **KILL** |
| btc_vol_ratio | 19% | 3.4d | +0.283% | 61.5 | −6.18% | 91.7 | yes | **KILL** |
| breadth_low | 42% | 13.2d | +0.010% | 47.6 | −11.06% | **1.9** | yes | **KILL** |
| dispersion | 7% | 5.2d | +0.349% | 58.0 | −4.17% | 82.9 | yes | **KILL** |
| regime_e_daily | 22% | 3.2d | +0.523% | 77.5 | −7.28% | 83.2 | yes | **KILL** |
| funding_extreme | 15% | 2.4d | −0.378% | 26.9 | −8.03% | 62.6 | yes | **KILL** |

### What Track A says
- **No flag clears.** Not one flag puts the conditional forward MEAN in the worst 5%
  of its matched random-flag null. The two flags whose conditional mean is even
  negative (dvol_level −0.11%, funding_extreme −0.38%) sit at the 39.6th / 26.9th
  null percentile — i.e. a random flag of the same frequency/clustering produces a
  conditional mean that bad or worse 60–73% of the time. There is no demonstrated
  conditioning power on the book's central forward return.
- **Two flags DO worsen the CVaR-5 tail — but tail-only, and unsurprising.**
  `breadth_low` (CVaR −11.06% vs −9.60%, 1.9th null %ile) and `dvol_level`
  (−11.04%, 9.5th) deepen the left tail of the forward distribution. This is exactly
  the volatility-clustering / risk-off literature's prediction — low breadth and high
  implied vol coincide with the book's nastier days. **But the central return is
  unaffected (mean-%ile 47.6 / 39.6), so it is not a clean risk-off edge:** a
  de-risk reflex on these flags would cut return on ~23–42% of OOS days to trim a
  tail that the flag does not show is worth the give-up. Under the ADVANCE rule
  (mean/Sharpe worsening required, not tail alone) both KILL. The honest read: a
  tail-coincidence consistent with beta, not a validated forward-downside predictor.
- **All shams fail** (the shuffled flag does not reproduce the conditioning) → the
  test is discriminating; **no VOID, no data issue.** The flags simply have no
  central-tendency edge to discriminate.

## Track B — directional, long-side vs short-side SEPARATELY (BTC OOS)

Each exogenous signal expressed as a directional score for BTC; gated through
`sweep_feasibility.evaluate` (cost-floor + random-entry null>95 + signal-IC + sham).
**Long leg** = long-or-flat BTC when the oriented-signal is in its top tercile.
**Short leg** = the same machinery on the NEGATED BTC price series (so the
random-entry null + sham apply to the short leg too) when the oriented-signal is in
its bottom tercile. Plus a zero-cost control and descriptive per-side
forward-return/hit/IC.

| signal | LONG null | LONG IC | LONG hit | SHORT null | SHORT hit | SHORT IC (p) | zero-cost null | verdict |
|--|--:|--:|--:|--:|--:|--:|--:|:--:|
| dvol_level | 59.2 | −0.044 | 0.481 | 57.5 | 0.538 | −0.171 (0.052) | 59.7 | **KILL** |
| dvol_change | 48.5 | −0.100 | 0.512 | 46.8 | 0.589 | 0.106 (0.232) | 49.3 | **KILL** |
| btc_vol_ratio | 92.8 | −0.163 | 0.543 | 87.0 | 0.628 | 0.222 (0.012) | 93.3 | **KILL** |
| breadth | 26.5 | −0.040 | 0.421 | 76.5 | 0.551 | 0.037 (0.644) | 29.3 | **KILL** |
| dispersion | 79.2 | −0.097 | 0.481 | 81.8 | 0.554 | 0.200 (0.023) | 79.0 | **KILL** |
| funding_extreme | 69.0 | −0.001 | 0.450 | 37.8 | 0.543 | 0.161 (0.068) | 69.0 | **KILL** |

### What Track B says
- **No long leg and no short leg clears the random-entry null (gate 95).** Best long
  leg is `btc_vol_ratio` at 92.8th (price-derived, not exogenous, and the signal IC
  has the WRONG sign for the rule: −0.163); best short leg is the same signal at
  87.0th. Both below 95. The zero-cost control confirms it is not a friction problem
  — even with zero fees/slippage the long legs stay sub-gate (93.3rd best).
- **The risk⇄reward asymmetry IS visible, and IS insufficient.** Short buckets have
  higher hit-rates (0.54–0.63) than long buckets (0.42–0.54) and the
  vol/dispersion signals have a real-looking short-side descriptive IC
  (`btc_vol_ratio` 0.222 p=0.012; `dispersion` 0.200 p=0.023): vol-elevated /
  high-dispersion days do precede weaker forward BTC, more reliably than they
  precede rallies. **But this asymmetry does not survive the short-leg null** —
  87.0th / 81.8th percentile, below 95 — i.e. random short-entry with matched
  turnover captures the same down-drift, because the OOS holdout simply contains
  net-down stretches. This is precisely the "down-move detection = market beta, not
  alpha" trap; the descriptive short-side IC is the bucket-conditioned echo of it,
  not a tradeable edge after the entry null.
- **The genuinely exogenous signals (DVOL, funding) are the WEAKEST**, not the
  strongest: dvol_change short null 46.8, funding_extreme short null 37.8. The
  hoped-for "exogenous data carries directional info price didn't" does **not**
  appear in this wave — the only suggestive numbers come from price-derived vol, and
  those are beta that fails its own null.

## Decision (mechanical, per the plan's tree)

**NEITHER track clears → documented null result. No collector, no overlay, no
integration.** No signal ADVANCEs on Track A (risk-off) and none on Track B (long or
short). The plan's tree: *"Geen van beide → gedocumenteerd null-resultaat, geen
infra/integratie."* Sham/poison are clean (no VOID), so this is a real null, not a
broken gate. Consistent with the whole XS-* arc and the user's skeptical directional
prior: price-derived regime info is beta; the two free exogenous feeds available
this wave (DVOL, funding) add no validated forward edge for the neutral book.

The one defensible carry-over (same as XS-BETA / XS-SENTIMENT-TILT): the tail
coincidence of `breadth_low` / `dvol_level` with the book's worst forward days could
be **logged as an observe-first risk GAUGE** — never as an automatic de-risk lever,
since it does not beat the random-flag null on central return and would cost return
on a fifth-to-half of days. That is a Phase-1 *gauge* decision, not a Phase-0 pass,
and it is explicitly NOT triggered by this study (no ADVANCE).

## Deferred SECOND wave (build ONLY if a future wave looks promising)

- **OKX open-interest surge + liquidation-cascade signals** — per-asset OI and
  liquidation history is NOT in the repo, and OKX public OI/funding history is only
  ~3 months (283 rows), far too short for a cross-sectional dispersion signal over
  the 3.45y panel. These were the plan's "small new OKX backfill" candidates; per
  the edge-first discipline they are deferred because this first wave did not clear.
- **Cross-sectional funding DISPERSION** — same reason (only BTC funding has
  multi-year history here; per-asset OKX funding is ~3mo). Only single-asset BTC
  funding extreme was testable this wave.
- **The hourly `regime_classifier_e` model** — needs an aligned hourly panel for all
  10 assets; only the original universe has 1H files. Phase 0 used a faithful DAILY
  analogue (same feature family, train-only fit). The hourly model is unlikely to
  change the verdict (its features are the same price-derived family that failed
  here) but is the cleanest second-wave re-test if ever revisited.

## Caveats (adversarially honest)

- **One 30% holdout, one regime.** OOS is a single 2025-05 → 2026-06 tail (378 book
  days, 390 BTC days); like XS-TRIGGER/XS-SENTIMENT-TILT treat the OOS numbers as
  directional, not precise. A null result on one holdout is the *conservative*
  reading (we did not over-fit a pass), but a future "this signal works" claim would
  need a second window and a multiple-testing correction across the 7 signals × 2
  tracks × 2 sides surface tested here (~28 looks; nothing cleared, so no correction
  needed now, but it is a real multiple-testing surface).
- **Most "signals" are price-derived** (vol-ratio, breadth, dispersion, regime_e) —
  included as the plan's named blocks and as beta controls, and they behave like
  beta. The genuinely exogenous feeds (DVOL, funding) are the two that matter for the
  open question, and they were the weakest. The short multi-year exogenous history
  (DVOL from 2021, BTC funding from 2023; per-asset funding ~3mo) caps how much
  exogenous breadth this wave could test — a real reason the *next* info is forward
  data collection, not another backfill.
- **Track-A null is frequency+clustering-matched but not regime-matched** — a flag
  that clusters in the holdout's worst weeks could look bad without being causal;
  the shuffle sham guards against exactly that and it passes (shams fail to
  reproduce), so the KILLs are not sham artifacts. The tail-only worsening of
  breadth_low/dvol_level is reported honestly as a coincidence, not an edge.
- **Costs:** Track-B uses the conservative OKX spot config (maker 0.08% / taker
  0.10%, half-maker, 0.05% slippage, funding-free spot). A signal that needed
  tighter execution to clear would be more fragile, not less; the zero-cost control
  shows friction is not what kills these (they fail the null even free).
