# Crypto Trader

A multi-strategy crypto trading research-and-execution system. It pairs a disciplined
backtesting pipeline (every candidate must beat a random-entry null before it is taken
seriously) with live paper and small real-money runners managed by systemd on a Proxmox
LXC, surfaced through a single dashboard.

**Python:** 3.10+ · **Live execution venue:** Hyperliquid (perps) · **Status:** research + paper, with one small live strategy

---

## What it actually runs today

| Strategy | What it is | State |
|----------|-----------|-------|
| **Cross-sectional momentum** | Long the top-3 / short the bottom-3 of 10 perps by trailing 120-day return, dollar-neutral, rebalanced every 5 days. `scripts/hl_xs_runner.py`. | **Live on Hyperliquid mainnet** (small real capital) + a testnet soak instance. Dollar-neutral, ~2× gross, with a catastrophe backstop. |
| **Plan E (mean-reversion)** | A fleet of mean-reversion variants run in parallel as a paper bake-off. `scripts/plan_e_runner.py`. | Paper only (10 instances), forward-running. |
| **Research / sweeps** | Feasibility harness, cross-sectional & VRP studies, carry. `backtest/sweep/`. | Offline; promotes a candidate to a paper runner only after it clears the gate. |

Earlier single-asset directional strategies (a multi-indicator confluence bot and an
earlier mean-reversion attempt) were **retired** after an honest edge diagnosis showed no
durable entry edge; their code is kept for reference only. See `DECISIONS.md` for the full
trail of what was tried, killed, and why.

---

## Core discipline

The project's process rule (recorded in `DECISIONS.md`) is what keeps it honest:

- **Null gate first.** A candidate must clear a random-entry / random-basket null at OOS
  before any parameter tuning — and a shuffled-signal "sham" control must *fail* the same
  gate, or the gate isn't discriminating.
- **One strategy at a time**, with hard kill-criteria written down up front.
- **Paper before real money**, real money small and gated, with the legal/tax call on any
  venue left to the operator.
- **Adversarial verification** of every result and every money-path change.

The current honest verdict: the momentum lead clears the null but its edge is
regime-concentrated and modest out-of-sample, so it runs on paper plus token real capital —
it is **not** presented as a proven money-maker.

---

## Venues

Venue integrations are functional adapters, independent of the strategies that use them:

| Venue | Used for |
|-------|----------|
| **Hyperliquid** | Live perp execution for the momentum strategy (`scripts/hl_adapter.py`) |
| **OKX** | Historical daily/funding data + perp adapter (`scripts/okx_api.py`, `okx_adapter.py`) |
| **BloFin** | Legacy perp adapter from the original bot (`scripts/blofin_api.py`) |
| **Deribit** | Implied-volatility (DVOL) history for the VRP research lane |

---

## Repository layout

```
scripts/
  hl_xs_runner.py        # Cross-sectional momentum runner (Hyperliquid) — the live strategy
  hl_adapter.py          # Hyperliquid execution adapter (EIP-712 signing via the official SDK)
  hl_watchdog.py         # External catastrophe watchdog for the live runner
  plan_e_runner.py       # Plan E mean-reversion paper runner
  xs_runner.py           # Cross-sectional momentum runner (OKX data, DRY)
  okx_api.py / okx_adapter.py / blofin_api.py / exchange_adapter.py   # venue adapters
  dashboard_api.py / dashboard.html                                   # dashboard
  trading_bot.py         # retired single-asset confluence bot (reference only)

backtest/
  sweep/                 # candidate strategies + the null-gate harness
  *_backfill.py          # public market-data backfill per venue
  backtester.py          # backtesting engine + risk-adjusted metrics
  data/                  # historical CSVs (gitignored, regenerable)

deployment/              # Proxmox LXC + systemd unit templates
configs/                 # per-instance runner configs (venue creds live only on the host)
docs/                    # ops runbooks + per-study writeups
tests/                   # unit + integration tests
```

---

## Running it

Strategies run as systemd template units on the host, one service per instance:

```bash
# momentum (Hyperliquid) — testnet soak and mainnet
systemctl status hl-xsectional@main        # TESTNET
systemctl status hl-xsectional@mainnet     # MAINNET (gated: allow_live + HL_CONFIRM_LIVE)

# mean-reversion paper fleet
systemctl status 'plan-e@*'

# dashboard (http://<host>:8080)
systemctl status trading-dashboard
```

Live trading is double-gated: a config flag (`allow_live: true`) **and** an out-of-band
environment confirm (`HL_CONFIRM_LIVE=YES`) — real money is never reachable by accident.
Credentials and `.env` files live only on the host and are never committed. The mainnet
go-live procedure (including the mandatory state reset) is in `docs/HL-TESTNET-OPS.md`.

---

## Safety

- **Circuit breaker** — flattens and halts on a drawdown limit; auto-resumes only the soft
  breaker, never the terminal one.
- **Catastrophe backstop** — a fast safety cycle re-checks equity/reconcile far more often
  than the rebalance clock; a terminal, non-resuming breaker flattens on a deep drawdown or
  a single-cycle equity collapse; an external watchdog (`hl_watchdog.py`) flags a dead or
  wedged runner.
- **Fail-safe reconciliation** — a confirmed non-neutral / over-legged live book is
  flattened and halted; a *transient* venue-read failure is held and retried, never flattened.
- **Atomic state** — tmp+rename writes; backward-compatible state schema.

---

## Tests

```bash
python3 -m pytest tests/ -q
```

---

## Further reading

| Document | Content |
|----------|---------|
| `DECISIONS.md` | Dated decision log — every strategy tried, killed, or shipped, and why |
| `docs/HL-TESTNET-OPS.md` | Hyperliquid testnet/mainnet ops + the go-live procedure |
| `docs/XS-TRIGGER-STUDY.md`, `XS-BETA-STUDY.md`, `XS-BREADTH-STUDY.md` | Momentum-lane studies |
| `docs/VENUE-ACCESS-RESEARCH.md` | Which venues are reachable, and the legal/regulatory framing |
| `config.example.json` | Configuration schema |
