# BH-Overlay Runner — Operations

> **Honest framing — read first.** This is the **documented fallback** from
> `DECISIONS.md` 2026-05-13 — *"this account holds BTC with a drawdown
> circuit-breaker + vol-targeting"* — recorded in the 2026-05-12 entry as the
> legitimate endpoint when active-strategy candidates don't clear the bar.
>
> **It does NOT have demonstrated edge vs the random-entry null.** Per
> `docs/STRATEGY-V1-RESULTS.md` §3, the underlying re-entering trailing-stop
> trend rule sits at the **84th percentile** of its matched random in/out null
> on Calmar (full-series 3.3y BTC, vol-targeted), **inside the 5–95 band**.
> Three of the four candidate trend rules (the MA and Donchian variants) sit
> right at the median (44–49th). The §7.1 gate in
> `docs/STRATEGY-V1-TREND-VOLTARGET.md` was **not cleared**.
>
> The rationale to run it anyway, per DECISIONS.md:
>   - DECISIONS.md (2026-05-12) records it as the legitimate fallback when
>     nothing else clears the null,
>   - it is marginally risk-adjusted-better than plain BH on the same 3.3y
>     series (Calmar +0.81 vs +0.77, max-DD 28% vs 52%; the vol-target overlay
>     further halves the in-trade DD to ~17%),
>   - it is **EU-executable on spot only** (no perp short leg, no MiCA
>     blocker, no funding cost — the carry was blocked by these exact
>     constraints, see DECISIONS.md 2026-05-13).

## What it is

A daily-decision paper-trade runner for the strategy:

    target_exposure = VolTarget(σ_target=0.20, window=30d, L_max=1.0)
                      · TrailingStopBH(trail=10%, breakout=20d, reenter=True)

i.e. M3 vol-target sizing on top of B2 (the re-entering trailing-stop BH
overlay). The runner reads public BTC daily price (OKX or BloFin public
market endpoint — no auth), simulates rebalance fills with a configurable
round-trip cost (default 0.20% on the traded notional delta), persists state
atomically, and exposes everything via the dashboard's **BH Overlay** tab.

**Strategy logic is `scripts/bh_overlay_strategy.py`**, a thin composition
layer over `backtest/v1_strategies.py:VolTarget` and
`backtest/daily_strategies.py:TrailingStopBH`. The math is unit-tested in
`tests/test_v1_strategies.py` (existing) and `tests/test_bh_overlay_strategy.py`
(new).

**Runner is `scripts/bh_overlay_runner.py`**, modelled on `carry_runner.py`:
config-driven, atomic state, JSONL trade log, per-cycle health.json,
three-state mode gate, manual halt sentinel, systemd-friendly.

## How to start / stop

The runner is **not auto-started**. Watch the first cycle, then enable manually.

```bash
# Verify the unit is loaded but inactive (expected initial state):
systemctl status bh_overlay@btc
# loaded, inactive (dead)  ← what you should see after deploy

# First start — keep an eye on the logs:
sudo systemctl start bh_overlay@btc
journalctl -u bh_overlay@btc -f

# Make it survive reboots once happy:
sudo systemctl enable bh_overlay@btc

# Stop:
sudo systemctl stop bh_overlay@btc
```

## State layout

```
/opt/trading-bot/state/bh_overlay/btc/
├── state.json            # persisted runner state (atomic write)
├── trades.log            # JSONL, one entry per cycle (decision or hold)
├── health.json           # last-cycle snapshot for the dashboard
├── halt                  # manual sentinel — presence pauses decisions
└── cache/btc_daily.csv   # cached daily OHLCV, refreshed once per cycle
```

`cache/btc_daily.csv` is regenerable — safe to delete. The next cycle
re-fetches up to 300 daily bars from the public market endpoint.

## How to read `health.json`

```jsonc
{
  "instance": "btc",
  "mode": "PAPER",
  "paper_only": true,
  "halted": false,
  "halt_reason": null,
  "last_cycle_ts": "2026-05-13T11:00:00+00:00",
  "last_decision_date": "2026-05-13",   // YYYY-MM-DD UTC of the last decision
  "cycles_total": 24,
  "simulated_equity": 5043.21,           // strategy book, USD
  "current_exposure": 0.50,              // 0..L_max
  "signal_on": true,                     // trend filter says long
  "vol_realized": 0.42,                  // σ_t annualised (NaN until warmed up)
  "vol_target": 0.20,                    // σ_target from config
  "vol_target_multiplier": 0.48,         // clip(σ_t / σ_target, 0, L_max)
  "vol_target_active": true,             // multiplier < L_max
  "drawdown_from_peak": 0.012,           // 0..1 — strategy equity DD
  "days_under_water": 0,
  "peak_price": 126345.0,                // running high used by the trail
  "drawdown_pct_price": 0.36,            // current price DD from that high
  "bh_equity": 5012.50,                  // passive BH benchmark, same $5k start
  "trailing_stop_pct": 0.10,
  "reentry_n_days": 20,
  "price_source": "okx_spot_public",
  ...
}
```

Key fields for triage:
- **`mode`**: always `PAPER` for now. If you see anything else with this build,
  it's a bug (the runner refuses to start with `paper_only=false`).
- **`signal_on`** / **`current_exposure`**: the strategy's view. ON + nonzero
  exposure = vol-target-sized long. OFF + 0 = stopped out, waiting for a
  new 20-day high to re-enter.
- **`drawdown_from_peak`**: the **equity** DD, the operator-relevant one.
  Set against the simulated paper book — at this size, with vol-target on,
  any single-day move > σ_target/√252 ≈ 1.26% (at σ_target=0.20) with full
  exposure is unusual and should be investigated.
- **`vol_target_multiplier`**: at BTC's ~40–60% annualised vol with
  σ_target=0.20, expect this to sit around 0.3–0.5 most of the time. If it
  pegs at 1.0 for days, the realized-vol estimator is warming up or saw a
  quiet patch.

## How to halt fast

```bash
# Drop the manual sentinel — next cycle (within cycle_interval_sec) reads it
# and stops making decisions. Existing simulated position is NOT auto-flattened
# in paper mode (there's nothing to flatten — no live order); the runner just
# stops advancing the strategy state.
touch /opt/trading-bot/state/bh_overlay/btc/halt

# Clear it to resume:
rm /opt/trading-bot/state/bh_overlay/btc/halt
```

For a hard stop (service goes away), `sudo systemctl stop bh_overlay@btc`.
The state file is left in place, so a restart picks up exactly where it left
off (including the trailing-stop state machine — see `strategy_state` in
`state.json`).

## What success / failure looks like in the first day / week

### Day 1 — first cycle
Given today's BTC ≈ $81k, in the post-ATH drawdown leg (ATH ≈ $126k Oct-2025):
- The trailing-stop state machine starts `in_market=True` (the default — the
  rule is "BH with a stop on top"). Running high gets initialised to today's
  high.
- σ_realized may show NaN for the first ~30 cycles (vol_window default 30d) —
  the strategy returns `vol_target_multiplier = L_max = 1.0` during warm-up.
- First rebalance: should be from current_exposure=0 → target≈1.0 (entry).
  Fee = 1.0 × $5000 × 0.20% = ~$10 on the entry.
- `bh_equity` and `simulated_equity` should both be ~$5000 right after that
  fill (minus $10 on the strategy side).

### Day 1–7 — warm-up window
- Once 30+ daily bars are cached, vol_target_multiplier should drop from 1.0
  to ~0.3–0.5 (BTC is in a high-vol regime — Oct 2025 → today is a -36% leg).
- Strategy equity should under-perform BH on the way up (vol-target caps
  exposure) and over-perform on a down-leg (vol-target halves the DD).
- One decision per UTC day. If `state.json:last_decision_date` matches today,
  subsequent intra-day cycles just refresh price/equity for the dashboard.

### Week 1 expectations
- 7 decisions logged in `trades.log` (one per day).
- 0–1 rebalances (the no-trade band of 15% suppresses small adjustments).
- `signal_on` switches OFF only if BTC's low touches the running high × 0.90.
  At today's setup that requires roughly a 10% drop from the running-high
  the trail is tracking.

### What would trigger a re-evaluation
- **σ_realized stays NaN past day 35**: data fetch is broken — check
  `cache/btc_daily.csv` and the runner log.
- **Strategy equity diverges materially from BH benchmark in the wrong
  direction** (e.g. > -5% from BH after week 1 with no stop firing): rebalance
  fees are eating more than expected, or the cost assumption is too low —
  revisit `round_trip_cost_pct`.
- **Stop fires once and never re-enters**: that's the *one-shot* behaviour —
  the `reenter=True` flag is set, so the runner should re-enter on a new
  20-day-high. If it doesn't, the strategy state machine is stuck — inspect
  `state.json:strategy_state` and the recent `trades.log` entries.
- **Days under water > ~14 with no DD recovery**: not a bug per se, but worth
  comparing to BH on the same window. If both are similarly underwater, the
  strategy is doing its job (and so is the market). If BH is fine and the
  strategy is bleeding, the vol-target estimator is misbehaving.

## When to retire / replace

This is a *fallback*. It earns its keep only as long as nothing better is
available. Replace it when any of the following become true:
- BloFin ships a spot trading API (carry comes back online),
- OKX EU loosens its retail acctLv cap (carry comes back online),
- a different EU-licensed exchange offers spot+perp with API access,
- the user opens an account under a non-EU entity,
- a new information source clears the §7.1 random-entry null gate on its own,
- a longer daily history (8–15y) becomes available, allowing a re-run of
  the §7.1 gate with real statistical power.

Until then, the BH-overlay runs and the dashboard shows whether it's
beating or losing to passive BH on the same starting capital.
