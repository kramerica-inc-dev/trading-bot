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
## 2026-05-13 — Carry on BloFin also blocked (no spot trading API); carry direction parked

**Context.** After OKX EU was found to be acctLv=1-capped today (no perp for EU retail → carry blocked), the plan was to pivot to BloFin. Feasibility check first (`docs/CARRY-BLOFIN-FEASIBILITY.md`) before any adapter work.

**Findings (Phase 0 only — no build started).**
- BloFin's **spot market data API works** (253 SPOT instruments incl. BTC-USDT, healthy depth). But the **spot trading API is undocumented / unimplemented**: no docs at `docs.blofin.com`, no spot place-order method in the official Python SDK, every trading method goes through `/api/v1/trade/*` with `marginMode`+`positionSide` (i.e. swap-only). CCXT issue #24675 (re-confirmed by a maintainer **2026-03-04**): "BloFin only supports swap trading through the API." Probes against guessed spot trade endpoints return 401 with no signing-shape documentation — reverse-engineering an undocumented endpoint is out of scope.
- The user's OKX KYC is already at the EU max ("Identiteit geverifieerd", fee tier "Vaste gebruiker"). There is no higher KYC tier to try on OKX EU. The 2026-05-13 plan-step "(a) user tries KYC Lv3" is therefore a dead path — the cap is regulatory, not KYC-tier.
- BloFin's UTA Multi-Currency Margin mode (since April 2025) would have made BloFin yield-comparable to OKX unified-margin (~9%/yr, not the ~6.3% the spec assumed) — but this is moot given no spot trading API.
- BloFin's regulatory exposure for NL users: not on their geo-block list, BVI/Cayman base, "seeking MiCA CASP authorization before 2026-07-01"; AFM has investigated unlicensed CASPs (Binance NL exit 2023). Tail risk, not a current blocker.

**Decision.**
1. **The funding/basis-carry strategy is parked** for now. Two venues blocked for two different reasons (OKX EU = regulatory cap on perp; BloFin = no spot trading API). No third venue is materially better placed: BloFin's competitors with API spot+perp (Bybit, Binance global, OKX global) all either geo-block NL, withdrew from the Dutch market post-MiCA, or carry the same offshore tail risk.
2. **Project falls back to the recorded fallback path** (per the 2026-05-12 DECISIONS entry's last paragraph): "this account holds BTC/ETH with a drawdown circuit-breaker + vol-targeting". That's the legitimate endpoint when the active-strategy candidates don't clear the bar — exactly the situation we're in.
3. **Keep what's been built.** The OKX P1/P2 adapter work (spot order surface, unified-margin awareness, EU base URL, UA fix, dry-run/P2_DEMO/P3_LIVE three-state gate, legging window, basis-kill, manual halt, reconcile) stays in the repo. The carry runner, position math, dashboard tab, ops doc, and infra all stay. If BloFin ships a spot trading API later, or OKX EU rules change, or the user moves to a non-EU entity, the carry is one config/adapter swap away from running.
4. **Plan E** unchanged: keeps paper-running on BloFin with its own go/no-go gate at the end of its paper window. The Plan-E live-executor + maker-execution + reconciliation bundle (`[[project_okx_live_executor_commit]]`) is at the same OKX EU regulatory wall the carry hit — so it's also effectively on hold unless Plan E's live-deploy plan migrates to BloFin-only or to a non-EU entity. To be decided when Plan E's paper window closes.

**One low-cost option worth keeping in mind:** contact BloFin support to ask if a spot trading API exists on partner/institutional tier or is on the public roadmap. Cheap (~5 min email), 1–3 day turnaround, and if they say "yes Q3" or "white-listable" we have a path. Not blocking; just noted.

**Review condition.** Revisit point 1 when any of: (a) BloFin's spot trading API ships, (b) OKX EU loosens its EU retail acctLv cap, (c) a different licensed-in-NL exchange offers spot+perp with API access, (d) the user opens an account under a non-EU entity. Otherwise the carry stays parked.

---
## 2026-06-04 — VRP deepened (premium real, tail un-hedgeable cheaply); dashboard/trader pruned to the live route

**Context.** The OKX-sweep V3 surfaced two candidates past the random-entry null: cross-sectional momentum (perp, OKX) and VRP short-vol (options, Deribit). VRP was the stronger but had only been tested through a variance-swap proxy. This session built a faithful option-replication model (`backtest/sweep/vrp_replication.py`, 8 tests): Black-Scholes short ATM straddle, daily delta-hedge at frozen IV, real hedge + option-spread costs, held to expiry, plus a long-OTM-wing tail-hedge. A 5-agent adversarial validation workflow independently reproduced every number and caught two first-draft errors (a "−16% of capital" tail figure that is actually −10.1%, and a dynamic-sizing thesis that was backwards). Full write-up: `docs/VRP-DEEPENING.md`.

**Findings.**
1. **The VRP premium SURVIVES faithful replication.** Naked delta-hedged short straddle at realistic costs (6 bps hedge / 1 vp option spread) = **+6.4 vol-pts/mo, t=2.34, Sharpe 1.24, null 98th, 3/3 sub-periods, 100% of 30 roll-offsets positive**; survives dropping the FTX-era outlier (+4.7, p=0.038). The dollar-gamma weighting did not erode it. This is the project's most-validated edge.
2. **The tail is un-hedgeable cheaply.** The −50 vp worst month is a multi-day high-vol BURST starting from calm, not a single gap. Static OTM wings are gap insurance → a near-pure cost: no symmetric or put-only wing beats naked at a matched worst-month budget; the symmetric hedge cuts return ~5× (40.6%/yr → 7.4%/yr) without improving the dollar-scale worst month.
3. **Dynamic de-risking only helps in the INVERSE of the intuitive thesis** — the edge lives in HIGH-IV entries (corr +0.64) and the tail starts calm, so "cut size when vol rises" is backwards. The robust rule is "sell vol only when DVOL is rich," but its tail benefit is knife-edge / single-outlier-driven (best-of-sweep p=0.16) → suggestive, not proven.
4. **Deribit feasibility: MED, a live-decision gate not a research gate.** DVOL backfillable; option-surface skew is live-snapshot only (forward-collect needed). Venue reachable by an NL retail user offshore (Panama entity + KYC) but unregulated-in-EU; the compliant Coinbase-EU route is futures-only in 2026.

**Decision.**
1. **Next VRP step = walk-forward / OOS-validate the DVOL-richness filter OFFLINE on data in hand** (cheapest high-value test). Do **not** build the Deribit options-adapter + delta-hedge loop until that passes. The momentum lead stays the parallel paper-candidate (needs perp access).
2. **Dashboard + trader pruned to the live route.** Removed the "BH Overlay" fallback lane (no demonstrated edge — runner/strategy/config/systemd/tests + dashboard tab + API all deleted) and the "Backtest" tab (the deprecated 'advanced' baseline view; the benchmark/metrics *backtester* code stays — shared infra). Added an "OKX Sweep" tab + `/api/sweep/status` reflecting the current route.
3. **Carry is explicitly KEPT as a parked-but-potential direction** (per user, 2026-06-04): its feasibility is platform-dependent and may be viable on another venue/entity. Runner, position math, OKX adapter, config, systemd unit, and the "OKX Carry" dashboard tab all stay.
4. **The deprecated 'advanced'/Plan-D/v1 strategy SOURCE stays in the repo** (option A): it's entangled with the disabled legacy bot + ~10 tests, and the bot is already operationally disabled. Only its dashboard presence was removed. A deeper source-level removal is a separate, larger refactor if ever wanted.

**LXC follow-up (manual — prod SSH not auto-authorized this session):** stop+disable `bh_overlay@btc`, remove its unit + state, and redeploy the dashboard. Plan E paper instances and `carry@btc` (already stopped) are untouched.

*(Update same day: the LXC follow-up was done via SSH once authorized — `bh_overlay@btc` stopped/removed, dashboard redeployed + enabled; an orphan dashboard process crash-looping the service was also fixed.)*

---
## 2026-06-04 — DVOL-richness filter walk-forward done; VRP statistical deepening EXHAUSTED

**Context.** The prior entry's recommended next step was to walk-forward / OOS-validate the DVOL-richness filter offline. Done: `backtest/sweep/vrp_richness.py` (+6 tests) implements a causal trailing-percentile rule (trade full size iff `DVOL[t] ≥ trailing-pctl`, strictly before t), a subset null (IV-selection vs random same-count), and matched-tail sizing. A 4-agent adversarial validation reproduced every number and corrected a too-optimistic first draft.

**Findings.**
1. **The causal high-DVOL SELECTION signal is real.** corr(entry DVOL, P&L) = +0.638; high-DVOL half +12.3 vp vs low-DVOL +0.2 vp. The DVOL-selected cycles beat random same-count subsets at the 99.9th pct (mean) / 97th (Sharpe); a random subset beats naked only 12.4% of the time. Genuinely causal and null-beating.
2. **But it is NOT a tail-reduction overlay.** At a matched 10% CVaR budget the realised worst month is *worse* for the filter (−12.2% vs naked −10.1%) — it cuts the −50 vp cycle and re-levers ~1.9× on the surviving tail.
3. **The headline +24 pp %/yr (40.6 → 64.5) is a whole-sample-CVaR sizing look-ahead.** Re-sized causally (expanding-window CVaR) the gap collapses to **+4–9 pp**. Single-digit-pp deployable uplift.
4. **Borderline + fragile:** 0/36 (lookback×pctl) configs clear a Bonferroni multiple-testing threshold; the entire %/yr edge traces to 1–2 of 43 cycles (drop the 2 worst → the win reverses). Tuning the cutpoint FAILS OOS (keeps the −50 cycle → below naked).

**Decision.**
1. **VRP's statistical deepening is exhausted.** 43 non-overlapping cycles, with the edge resting on 1–2 events — no further backtesting / re-slicing adds information. The premium itself remains the project's **best-validated edge** (faithful-replication-proof, roll-robust, +6.4 vp/mo); the DVOL-richness rule is a **modest causal entry-gate, not a risk overlay**.
2. **No live executor on this evidence, and no more in-sample grids.** The only step that adds genuine new information is **forward data**: collect the Deribit DVOL surface live and paper-log the causal gate forward (≥~12 independent forward cycles) before any runner. That is gated on the offshore-Deribit venue/regulatory question (MED — accessible to NL retail via the Panama entity + KYC, but unregulated-in-EU; the compliant Coinbase-EU route is futures-only in 2026).
3. **Until forward data exists, VRP is parked at the research boundary** (premium proven, execution venue + dataset are the blockers). The cross-sectional momentum lead remains the parallel paper-candidate (needs perp access). Full write-up: `docs/VRP-DEEPENING.md`.

---
## 2026-06-04 — Venue wall is NOT absolute: momentum/carry executable today; VRP needs Deribit

**Context.** The momentum lead's P2_DEMO was confirmed blocked (OKX EU demo = acctLv=1, perp code 51155 — the same MiCA wall as carry). Three validated edges (carry, VRP, momentum) were all execution-blocked. User chose to investigate the venue/entity problem. A deep-research effort (6 angles, 27 sources, **25 claims adversarially 3-vote-verified, 0 killed**) produced `docs/VENUE-ACCESS-RESEARCH.md`.

**Finding: there IS a path.** Momentum + carry are executable TODAY for an NL retail user:
- **Hyperliquid (DEX)** — no KYC, no NL/EU geoblock (NL not on its restricted list; independently corroborated), 100+ perps incl. all 10 momentum assets long+short, full public REST/SDK API with funding history, hourly funding, low fees. **Grey area** (EU-unregulated; custody/smart-contract risk); build = a new DEX SDK adapter. The momentum runner is an adapter-swap away (strategy is exchange-agnostic).
- **Kraken Pro EU** (CySEC/MiFID, all 10 perps, ~10x, **no options**) and **OKX X-Perps** (Malta MFSA OEML-15905 — a DIFFERENT entity than the MiCA-blocked OKX retail one; 5yr-expiry futures w/ funding mechanism, 10x, suitability-gated) — the **compliant** route, closer to the existing OKX adapter, but gated + leverage-capped + universe/NL-enablement needs verification.
- **VRP** still needs **Deribit** (offshore Panama DRB Panama Inc., NL not restricted, retail after KYC) — heaviest + least certain (no 2026 source confirms an NL-retail options trade end-to-end). On-chain Aevo options too thin.
- **Non-EU entity (Path C): unsubstantiated + unnecessary.** ESMA (Feb-2026) is tightening EU retail perp access (CFD intervention) — compliant venues carry leverage-cap/re-gating risk; offshore/DEX carry enforcement risk.

**Decision.** The execution path is **reopened** but the venue choice has a legal/tax dimension that is the user's (grey-area offshore/DEX vs gated-but-compliant EU CEX). No venue committed yet. **This is a factual feasibility assessment, not legal/tax advice** — the user should confirm NL tax/legality before deploying real capital. Recommended sequencing: (1) momentum first (a working DRY_RUN runner already exists), via either Hyperliquid (fast, grey) or Kraken-EU/OKX-X-Perps (compliant, gated); (2) carry follows on the same venue; (3) VRP/Deribit last. The momentum DRY_RUN forward-paper keeps running meanwhile. Open questions (10-asset coverage on the compliant venues, Deribit NL-retail end-to-end, Hyperliquid net-edge at size) to verify before capital.

---

## 2026-06-04 — Hyperliquid momentum runner VALIDATED on testnet (real orders, mock money)

**Context.** User approved option A (a ~$10 real mainnet deposit to unlock the HL testnet faucet — the grey-area call is theirs). Faucet mechanics were adversarially re-verified (gated on a prior mainnet deposit; native Arbitrum USDC; 5 USDC floor; 1000 mock USDC). User deposited 57.52 USDC on mainnet, claimed 1000 mock USDC, created an **agent (API) wallet** (master `0x70Cb…4c89`, agent `0x34B9…5B12`). Runner flipped TESTNET on `hl-xsectional@main`.

**Findings (all caught on mock money, none on real capital).**
1. **Unified-account funds split.** HL's default *unified account* keeps USDC collateral in the **spot** clearinghouse; the per-perp `marginSummary.accountValue` reads a "not meaningful" 0. Fix: `account_value = perp accountValue + free spot USDC (total − hold)` — correct in unified AND standard modes (no Spot→Perps transfer; leaving unified needs >$10k).
2. **Transient post-trade equity read.** Right after fills the collateral split momentarily under-reports → an 80% false-drawdown. Guard added: ignore a post-rebalance equity read <50% of the (settled) pre-rebalance equity; the drawdown breaker only acts on settled top-of-cycle reads.
3. **Testnet BTC oracle is stale** (mid ~+2.7% off oracle) → marketable BTC orders rejected `Price too far from oracle`. Testnet universe = the 6 healthy perps `[ETH,SOL,BNB,ADA,AVAX,DOGE]`; **mainnet keeps all 10** (BTC mainnet oracle healthy).
4. **Slippage 0.05→0.02** (both testnet and mainnet config): 5% stacked on stale mids and busted the oracle band; 2% still crosses every book (spreads ≤0.4%) and is a saner real-money cap.

**Decision.** Execution path is **validated**: a clean 6-leg dollar-neutral basket held (net ~$0, neutrality skew 0.0%, reconcile_ok), correct equity (990), CB normal; and the atomic-or-flatten safety fired correctly when a leg rejected (flatten + op_halt, no one-legged book). The bot runs live on **testnet** mock money under the agent key (key in `/etc/trading-bot/hl-xsectional-main.env`, 600, never logged — verified no leak). **Mainnet (real money) remains gated** on: testnet soak + the user's legal/tax green light + `allow_live:true` + `HL_CONFIRM_LIVE=YES`. Remaining money-path items before real capital: live-position count in health (cosmetic), resize-on-drift, and a real-money risk review.

---

## 2026-06-05 — Hyperliquid momentum LIVE on mainnet (real money)

**Context.** Testnet soak closed (turnover / drift-resize / op_halt all validated) and three offline studies (trigger, beta, breadth) all converge on the same config — keep **U10, m=3, lb=120, rebal=5** — and the same open risk: out-of-bull regime robustness, which more breadth or legs does not fix (U20 makes it worse). The user gave the go: *"enable live trade — only $57, risk negligible."* The legal/tax call (NL resident, EU-unregulated DEX) is the user's; **this is not legal/tax advice.**

**What shipped.** `hl-xsectional@mainnet` flipped MAINNET_DRY → **MAINNET_LIVE** (real orders) on agent wallet `0x70Cb…4c89`. Required a **state reset first**: the DRY instance carried a *simulated* `peak_equity` ~$5,231 + phantom positions; flipping without resetting would compute a ~99% drawdown vs the real $57 and instantly trip the circuit-breaker (flatten + halt, never trade). Resetting `state.json`/`health.json` re-anchors peak to the real equity on `cycles_total==1`. The documented go-live was missing this step — `docs/HL-TESTNET-OPS.md` corrected. The flip is **LXC-only** (`allow_live=true` in the LXC config + `HL_CONFIRM_LIVE=YES` env); the **repo config stays `allow_live:false`** — the real gate is the out-of-band env var, which never lives in git.

**First live rebalance (11:15:26 UTC) — clean.** 6-leg dollar-neutral basket: L **BNB/BTC/LINK**, S **ADA/DOT/SOL**; all legs filled ok; L/S = $57/$58 (1.7% skew); $114 gross = **2× leverage** on $57.47 margin (~$19/leg, above HL's $10 floor); `reconcile_ok`, `cb_state=normal`, `book_source=venue`, net_beta −0.091, drawdown 0.09%. **Max loss = the wallet margin** (DEX, no clawback); the 25% drawdown breaker flattens ≈ −$14.

**Open before scaling capital (non-blocking at $57).** A bot-side **catastrophe backstop** (currently only the hourly 25% CB + per-cycle reconcile guard the live book) — a fast safety cycle + a terminal (non-auto-resume) breaker + an external watchdog are being built. Plus the soak items: funding modeling in DRY paper, staleness guard, realized-slippage measurement. Testnet `@main`, plan-e 10/10, and xsectional@okx (DRY) untouched.

---

## 2026-06-10 — Leverage-timing overlay budget CLOSED (A0 + A0c: 12 variants, zero null-beaters)

**Context.** The leverage/yield plan (2026-06-09) gated every leverage-timing idea behind the binding null/sham/OOS process before live implementation. Two studies ran on the HL panel with the audited harness (research repo `backtest/sweep/xs_tilt.py` + `xs_conviction.py`, 1000-path nulls, block-shuffled shams, 2025+ OOS holdout).

**A0 — regime/vol overlays: 7/7 KILL.** legs-tilt (neutral + net), weight-tilt, beta-sleeve (0.25/0.5/1.0× BTC), vol-target. Zero G1/G2/V passes; every variant lost OOS in the 59.9%-bear holdout — the regime the tilt was built for. Vol-target failed even matched-gross Sharpe/Calmar/maxDD. Full numbers: research `docs/XS-TILT-STUDY.md`.

**A0c — conviction-scaled sizing (user hypothesis: leverage ∝ prediction certainty): 5/5 KILL.** momentum-spread z (G1 49.8th, sign-flips train→holdout), rank-stability (below its own null median at 3.78× gross — levered-beta mirage), breadth-extremity (G1 3.6th, worst of five), combo (V1+V2 pass but G4-fatal: +8.49pp gross vs 27.83pp added cost = friction harvester), leg-|z| weights (best raw OOS +25.38pp / Sharpe 0.474 but only 55.1st vs a bootstrap of its own weight profiles — shape indistinguishable from luck). Full numbers: research `docs/XS-CONVICTION-STUDY.md`.

**Decision.** All 12 variants permanently dead under the one-shot rule. Counting the May v1-trend overlay and sentiment tilt, **13 directional/timing/sizing overlays have now died on this book with zero null-beaters — the overlay budget is CLOSED.** Leverage on the live XS lane stays **STATIC** (operator risk preference: `gross_exposure 1.5` = 3× gross, Track-0 rails: read-back-verified pin, soft de-lever 6%→×0.5, ceilings $750/$120, breakers unwidened). The ladder's "4× adaptive" step is cancelled; the 5–6× unlock is moot. Any future sizing/composition idea requires a **new pre-registered hypothesis on forward paper data** — no more backtest overlay mining. The open evidence channels are forward records only: `xsectional@okx` paper (the OOS-flatness question) and the live mainnet account. Yield work continues on the orthogonal lane: **B1 HL funding-carry** (B0 passed all gates: +15.94%/yr net over ON time, ON 80%, L≤2; green-button OFF at +1.98%/yr trailing — ships parked, deploys on green flip).
