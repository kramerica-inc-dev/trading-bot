# Strategy & Architecture Decisions

A dated log of binding decisions about strategy direction, feature scope, and
what is deliberately **not** being worked on. Entries are append-only; if a
decision is reversed, add a new entry rather than editing the old one.

---

## 2026-04-18 — Current feature set is frozen; edge must come from new information

**Context.** On 2026-04-17 an ML diagnostic (logistic regression on the seven
regime-condition features vs. 12-bar forward returns) produced AUC **0.5030**
— statistically indistinguishable from random. Ten rounds of threshold
re-tuning, weight re-balancing, regime-conditioning, and grid expansion across
two competition rounds had been rearranging a predictor set that has no
measurable edge on forward returns.

**Decision.** The existing seven-condition feature set is frozen. No further
optimization work will be done on it. Future strategy lift must come from
**new information sources**, not from re-aggregating existing features.

**Banned for the next 3 months unless a concrete bug is found:**
- Re-running `backtest/calibrate_per_timeframe.py` with new parameter grids
- Re-balancing the weights / score contributions of the seven trend conditions
- Adjusting `trend_min_score`, `min_confidence`, ATR multipliers, or anchor
  thresholds on the current strategy
- "One more calibration pass" on `efficiency_ratio`, `trend_strength`, or
  `anchor_slope`

**Still allowed:**
- Bug fixes if the scoring math is provably wrong
- Execution improvements (order sizing, slippage, TP/SL reliability, circuit
  breakers, reconciliation)
- Monitoring, observability, logging improvements
- Adding genuinely new signals (funding rate, open interest, cross-sectional,
  on-chain). These introduce new information; they are not tuning the
  existing strategy.

**Review condition.** Revisit this decision only after at least one new
information source (funding rate, OI divergence, or cross-sectional ranking)
has been live for 4+ weeks with measurable results. At that point we will
have a new baseline to compare against.

**Why this matters.** The temptation under drawdown will be to "tune our way
out." The AUC result says that path is closed. Recording the decision here
so future work does not drift back into it by default.

---

## 2026-04-18 — Planned path: A → C → D → E (funding → OI → mean-reversion → cross-sectional)

**Context.** After freezing the current feature set, we identified four
candidate work streams that add new information or new strategy surface:
A (funding rate signal), C (OI divergence filter), D (mean-reversion strategy
for chop regimes), E (cross-sectional multi-asset). B (maker-only execution)
was considered and deferred — realistic fee savings are ~4bps round-trip and
likely eaten by adverse selection.

**Decision.** Execute in strict order A → C → D → E. Each phase must be live
and measured for at least 2 weeks before the next begins. Do not parallelize
— risk budget and debugging surface are the constraints, not engineering time.

**Rationale.**
- A introduces the single most documented retail-accessible edge (funding).
  Cheapest to ship, highest expected information gain.
- C reuses A's data infrastructure and acts as a filter on both the legacy
  trend strategy and A.
- D is a new strategy, not an addition. Doubles monitoring surface. Requires
  strategy router. Must wait until A+C are stable.
- E is the largest project and the most durable edge, but requires
  multi-symbol data collection, portfolio accounting, and a restructured
  backtest framework. Defer until A/C/D validate the approach.

**Review condition.** Each phase gates on the previous phase being live and
not introducing regressions. If A fails to show any lift after 4 weeks live,
pause before C and re-examine assumptions.

---
## 2026-05-12 — 'advanced' strategy deprecated; next strategy is a simple low-frequency trend + vol-target overlay

**Context.** The v2.7 improvement plan (`IMPROVEMENT_PLAN.md`) ran 6 phases (benchmark/metrics, lookahead audit, funding analysis, regime-multiplier calibration, continuous risk score, bear-check). Fase 1 established the baseline; Fases 3/4/5/6 all came back NO-GO. An 8-axis post-mortem (`docs/edge-diagnosis.md` + `docs/edge-diagnosis/{A..I}-*.md`) then showed conclusively that `MultiIndicatorConfluence` ('advanced') has **no entry edge even gross**: over ~370d on BTC-USDT 5m it returns −32.9% vs a passive BTC buy-and-hold's −9.5% (alpha −23.4%); gross PnL before fees/slippage ≈ −$1.2 on $115 (break-even) so ~97% of the net loss is transaction-cost drag; median per-trade gross move 0.13% vs the ~0.22% round-trip cost floor (82% of trades "dead on arrival"); MFE caps at ~1% ever; the confluence scores (confidence/quality/regime_confidence/MTF-alignment) all have ~zero IC (which is why Fase 5 did nothing); RSI/MACD-hist IC ≈ −0.27 vs 15-bar forward return — wrong sign; it loses in all months and all regimes (worst in sideways, its own thesis regime); a random-entry-same-turnover-same-friction backtest produces ≈ the same result; and **a single trailing −10% stop on plain buy-and-hold returns +17.7% / Calmar +1.76 / 10% max-DD over the same period** — the dumbest systematic rule beats it by ~50 pp. Plan D (single-asset mean-reversion) had already failed the same way (fee share >60%).

**Decision.**
1. `MultiIndicatorConfluence` ('advanced') and the single-asset mean-reversion (`mean_reversion_strategy.py`, Plan D) are **deprecated as live candidates.** The code stays in the repo for reference; the inert-by-default infra from Fases 4–6 (`regime_multipliers`, `risk_scoring`, `bear_check` config sections) can remain. No further development, optimization, or exit-tuning on these — the ablations show exit fixes move it < ~2 pp and zero-cost only reaches break-even.
2. The next strategy development effort is a **simple, low-frequency, trend + volatility-target overlay** on a small crypto basket (BTC, optionally + ETH), rebalanced daily, holds of weeks — spec in `docs/STRATEGY-V1-TREND-VOLTARGET.md`. It is benchmarked explicitly against (a) BTC buy-and-hold and (b) trailing-stop-on-BH, on Calmar / max-DD (not raw return). If it cannot beat (b) out-of-sample there is no reason to run anything more complex than that one-line rule.
3. Plan E (cross-sectional multi-asset, the live `plan-e@*` paper instances) keeps running as a paper experiment but gets **no further build-out** (OKX EU live-executor / maker-execution / reconciliation bundle stays on hold per the existing paper-PASS gate) until it demonstrably beats BH on Calmar OOS over its paper window.

**Process rule (binding going forward).** Every future strategy candidate must, BEFORE any backtest optimization: (i) demonstrate forward-return separation from a random-entry null, out-of-sample; (ii) pass the diagnosis template in `docs/edge-diagnosis/` (gross-vs-net per-trade expectancy, MFE/MAE capture, IC of every signal component, exposure/Brinson decomposition). If gross per-trade expectancy ≈ 0, stop — it's a friction harvester. One strategy at a time, hard kill-criteria set in advance, no parallel variant farms.

**Why this matters.** Two strategies have now failed in exactly the same way; the temptation will be to "tune our way out" a third time. The data says the edge in this project is in low-frequency risk management, not in timed entries — record it here so future work doesn't drift back.

**Review condition.** Revisit point 1 only if a concrete scoring/exit bug is found that materially changes the gross-expectancy number. Revisit point 3 after Plan E's paper window closes with measured results.

---
## 2026-05-12 — v1 trend-overlay parked (no rule clears the null); next bet = funding/basis carry

**Context.** Per the 2026-05-12 deprecation entry, the post-'advanced' direction was a simple low-frequency trend + volatility-target overlay (`docs/STRATEGY-V1-TREND-VOLTARGET.md`). The infra (M0–M2: daily-bar backtester, BH+trailing-stop benchmark, random-entry-null harness) was built, and M3 ran the disciplined bake-off on a 3.3-year native-1D BTC history (1216 bars, 2023-01 → 2026-05, multi-regime): three candidate trend rules — re-entering trailing-stop switch, long-MA filter, Donchian breakout, each with vol-target sizing. **Result (`docs/STRATEGY-V1-RESULTS.md`): none clears the §7.1 random-entry-null gate out-of-sample.** Trailing-stop vol-target sits at the 84th percentile on Calmar (inside the 5–95 band), MA100/MA200/Donchian at the 49th/48th/44th. The diagnosis's headline "trailing-stop-on-BH = Calmar +1.76 / 10% max-DD" turned out to be a short-window artifact: on the full 3.3y series B2 (`TrailingStopBH` 10%/20d re-entering) is +0.81 Calmar / 28.5% max-DD vs plain BH's +0.77 / 51.5% — only marginally better, because in that one earlier window the stop fired before BTC's Oct-2025 ATH and the bot never had to re-enter.

**Decision.**
1. The **v1 trend-overlay direction is parked** — no v1 trend rule has a demonstrable edge vs a random-in/out null on the available data, so M4/M5 (live wiring, paper) are not pursued. The M0–M3 infra (`backtest/daily_backtester.py`, `daily_strategies.py`, `random_entry_null.py`, `v1_strategies.py`, `run_benchmarks.py`, `run_v1_bakeoff.py`) is kept — it's the reusable daily-bar harness + the null-gate every future candidate must pass. `docs/STRATEGY-V1-TREND-VOLTARGET.md` stays as the (now-shelved) spec.
2. **This closes the price-derived directional-signal lane entirely.** 'advanced' (confluence), Plan D (mean-reversion), and v1 (trend overlays) have all now failed the same way — gross-break-even-or-worse, signal indistinguishable from random. No further work on price-indicator directional strategies.
3. **Next investigation = funding / basis carry (cash-and-carry):** spot-long + perp-short to harvest the persistently-positive perpetual funding premium — delta-neutral by construction, the right size for a $5k–$50k account, and the one place the project's own data showed a *structural* (not statistical) edge. (Fase 3 found no funding *timing* edge; this exploits the funding *level*, which is a different and real thing.) To be scoped: practical executability on BloFin and/or OKX (spot + perp on the same venue, margin/liquidation mechanics on the short leg, fee schedules on both legs), realistic net carry after round-trip fees on two legs amortized over the holding period, drawdown/risk profile (basis risk, liquidation risk, exchange risk), and a historical estimate from the existing funding series. Spec to follow in `docs/STRATEGY-CARRY.md` (or similar).
4. **Plan E** (cross-sectional multi-asset, the live `plan-e@*` paper instances) keeps running and still gets a formal go/no-go at the end of its paper window — must beat BH on Calmar OOS. No further build-out (OKX EU live-executor / maker / reconciliation bundle stays on hold) until then.

**Fallback if neither carry nor Plan E clears the bar.** The honest endpoint is "this account holds BTC/ETH with a drawdown circuit-breaker + vol-targeting and stops trying to beat it" — a legitimate outcome, recorded here so it isn't treated as failure if it's where the evidence lands.

**Why this matters.** Three directional-signal strategies have failed identically. The next move must be a structurally different bet (carry/arb), not a fourth attempt at timing price. Recorded so effort doesn't drift back.

---
## 2026-05-13 — Build the carry now, on OKX EU; decouple from Plan E's paper-PASS gate

**Context.** The 2026-05-12 carry scoping (`docs/STRATEGY-CARRY.md`, `backtest/carry_backtest.py`) concluded GO-conditional: structural ~+10.5%/yr gross funding-premium harvest with ~1.6% max-DD, Calmar ~6.7 — the first thing in the project to clear the bar. Blocker: neither the BloFin nor the OKX adapter in this repo can place SPOT orders (both perp/swap-only — Plan E has only ever traded perps); a spot order/balance leg is net-new (modest) integration work. OKX EU is the better carry venue (unified-margin → spot BTC collateralizes the perp short → ~9%/yr effective book yield vs ~6.3% on BloFin's siloed margin, a ~50% improvement) but OKX EU enablement has been on hold per the 2026-05 memory rule that bundled OKX EU + maker-execution + reconciliation together as "after Plan E paper-PASS."

**Decision.**
1. **Build the carry strategy now on OKX EU, in unified-margin mode**, even though current trailing-90d funding is compressed (2026 YTD ≈ −0.9%). Rationale: the integration work is real (a few days) and we want it ready when funding normalises, not chasing it; the carry is delta-neutral so building "early" has near-zero risk; and the carry is the first strategy with a structural rather than statistical edge.
2. **Unblock OKX EU specifically for the carry** — this *decouples* OKX EU enablement from Plan E's paper-PASS gate. The Plan E live-executor + maker-execution + reconciliation bundle stays on its own gate (still gated on Plan E paper beating BH on Calmar OOS). What changes: OKX EU credentials/adapter can now sign and route for the carry runner without waiting for Plan E. The two strategies stay logically separate (different services, different state directories).
3. **Phased build**, paper/dry-run first per the P1 policy. P1 = extend `scripts/okx_adapter.py` / `scripts/okx_api.py` for spot orders + balance + unified-margin awareness, plus a dry-run carry runner that computes target positions and *simulates* leg placement; verify against the historical-backtest numbers. P2 = paper/demo-account trading on OKX. P3 = live on OKX EU with a tiny notional ($500–$1000), monitor real fills/funding/margin for 2+ weeks. P4 = scale to $5k. A go-live "green-button" rule: trailing-90d funding > +5%/yr (else the build sits ready but no money deployed — the strategy is "always on when funding is on", not "always on regardless").
4. **Risk controls (binding before any P3):** the perp-short leg runs at low effective leverage (book-vs-notional ≤ 2×) with a fat margin buffer + auto-top-up; a basis-blowout kill-switch (flatten if spot−perp basis exceeds N×rolling-stdev); a margin-utilisation alarm; one venue only (OKX EU, no two-venue legging); legging window < 5s with leg-1-flatten on leg-2-failure. Detail in `docs/STRATEGY-CARRY.md`.

**What this does NOT change.** Plan E keeps running on BloFin as paper with its own go/no-go. The 'advanced' / Plan-D deprecation stands. The v1 trend-overlay parking stands. The price-derived directional-signal lane stays closed.

**Review condition.** Revisit point 1/2 if OKX EU integration turns out to be substantially harder than estimated (≥1 week of work) or if a regulatory/account issue blocks spot trading on OKX EU. Revisit the green-button funding threshold (+5%/yr trailing-90d) after a quarter of live data — it's a starting heuristic, not a tuned parameter.

---
## 2026-05-13 — OKX EU retail is acctLv=1 capped; carry on OKX blocked, pivot needed

**Context.** P2 was deployed on the LXC as `carry@btc.service` with the user's freshly-created OKX EU demo-tier API key. After fixing two real bugs (browser User-Agent on the adapter Session, `okx_base_url` set to `https://my.okx.com`), auth started working and live probes ran successfully (fees, leverage cap = 3× = MiCA retail cap). But the funding-on green-button kept firing `do_open` and every open kept failing — root cause: the demo account had `totalEq=0` (never funded) and `acctLv=1` (Simple mode = spot only, no perp).

Probed the OKX EU API directly (with the working demo creds) to see what could be unblocked:
- `POST /api/v5/account/set-account-level acctLv=2` → `"Operation not supported"` (code 3).
- `POST /api/v5/account/set-account-level acctLv=3` → `"Operation not supported"` (code 3).
- `POST /api/v5/asset/transfer` → `"Due to local laws and regulations, you cannot trade with your chosen crypto"` (code 50052).
- `GET /api/v5/asset/deposit-address` → `"This feature is unavailable in demo trading"` (50038).
- The UI itself has no "Account Mode" toggle and deposit/buy redirects are broken.

**The picture: OKX EU retail (NL portal, KYC Lv2, user.region=EU) is locked to acctLv=1 (Simple / spot only) at the API level by what OKX calls "local laws and regulations" — i.e. MiCA / OKX EU's licensing constraints.** Lv≥2 is required for perpetual derivatives; spot-only cannot run a cash-and-carry (which needs the perp short leg). The current account cannot host the carry, in demo OR live.

**Decision.**
1. **Carry on OKX EU is blocked** for retail accounts in this jurisdiction at this time. `carry@btc.service` stopped on the LXC. The P1/P2 OKX adapter work (spot order surface, unified-margin awareness, EU base URL, UA fix) is kept — it's correct and reusable if OKX EU's retail rules change or if the user ever switches to a non-EU entity.
2. **One short detour before pivoting**: try KYC Lv3 (Advanced verification — passport/ID + selfie, ~10–15 min) and re-test. ~30% chance it unlocks Lv2/3 because Lv3 KYC is sometimes a prerequisite; ~70% chance it doesn't because the restriction is regulatory, not KYC-tier. If the API still returns "Operation not supported" after Lv3 → carry on OKX EU is definitively dead.
3. **Pivot to BloFin for the carry** if the OKX route doesn't open. BloFin has no MiCA blocker, spot + perp on one account, and the project already integrates BloFin (Plan E). The BloFin adapter is currently perp-only — needs the same spot-extension work that was done for OKX in P1 (place_spot_order / cancel / balance / instrument-meta). Estimated 1–2 dev days. Trade-off vs OKX unified-margin: BloFin has siloed margin → effective book yield ~6.3%/yr instead of ~9%/yr (already noted in `docs/STRATEGY-CARRY.md`). Strategy logic, risk controls, and `carry_runner.py` are exchange-agnostic — the only changes are the adapter and a `configs/carry-btc-blofin.json`.
4. **Plan E unchanged.** OKX EU enablement for the live-executor bundle (per the prior `[[project_okx_live_executor_commit]]` gate) is now also questionable for the same regulatory reason; revisit only after KYC Lv3 result is in.

**Review condition.** Revisit point 1 only if (a) MiCA/OKX-EU rules change, (b) the user moves to a non-EU entity, or (c) a concrete OKX API/docs change shows acctLv≥2 became available for EU retail. Revisit point 3 if BloFin discontinues or restricts spot trading at the user's KYC level.

---
