# Plan E — Paper-trade deploy runbook (multi-instance)

**Status:** paper-only. Live mode is a separate decision post-P1 window.
**Target host:** Proxmox LXC that previously hosted the old BTC-USDT bot.
**Runner entrypoint:** `scripts/plan_e_runner.py`
**Instance configs:** `configs/plan-e-*.json`
**Systemd template:** `deploy/plan-e@.service` → `/etc/systemd/system/plan-e@.service`
**Deploy script:** `deploy/deploy_multi.sh`
**Decision doc:** `backtest/results/PLAN-E-final.md`

## Multi-instance architecture

Several paper instances run in parallel on the same market data to compare
strategy variants directly. Each instance has its own state directory; they
share a single market-data cache.

| Instance         | Variant                                       | Cadence |
|------------------|-----------------------------------------------|---------|
| `plan-e-base`    | Baseline (control, all flags off)             | 24h     |
| `plan-e-c`       | + Agent C: BTC vol-halt (k=1.5, 24h/30d)      | 24h     |
| `plan-e-g`       | + Agent G: breadth tail-skip (SMA-200)        | 24h     |
| `plan-e-cg`      | + C + G stacked                               | 24h     |
| `plan-e-i`       | + Agent I: outlier exclude (K=4, 60d σ)       | 24h     |
| `plan-e-12h`     | Baseline @ 12h cadence                        | 12h     |
| `plan-e-48h`     | Baseline @ 48h cadence                        | 48h     |

Exit criteria per agent's walk-forward result: `plan-e-c` and `plan-e-g`
are expected winners (+0.72 / +0.60 Sharpe), the others track.

## What it does

Runs the validated cross-sectional reversal strategy once per UTC day:

1. Pulls ~100 hours of 1h candles for the 10-asset universe into
   `state/runner_cache/`.
2. Computes `log(close[-1] / close[-73])` per asset, ranks cross-sectionally.
3. Applies rank hysteresis (`k_exit=6`): retains existing legs while in
   top/bottom-6 band, fills gaps from top-3.
4. Simulates fills at last-close with taker fee + slippage, persists state.

No real orders are placed. Paper-only.

## State layout

```
state/
├── shared_cache/             # per-symbol 1h CSVs used by ALL instances
│   ├── BTC-USDT_1H.csv
│   └── ...
├── plan-e-base/
│   ├── portfolio.json        # cash, equity, positions, counters
│   └── trades.log            # JSONL rebalance + skip events
├── plan-e-c/
│   ├── portfolio.json
│   └── trades.log
├── plan-e-g/   ...
├── plan-e-cg/  ...
├── plan-e-i/   ...
├── plan-e-12h/ ...
└── plan-e-48h/ ...
```

`state/` is gitignored. Do NOT commit operational state.

**Shared cache:** all instances read the same `state/shared_cache/` so they
see identical prices — the only source of difference between instances is
the strategy variant, not the market data. Isolated from `backtest/data/`.

## Environment variables

The runner uses the public market-data endpoints only; secrets are not
strictly required for paper mode, but the BlofinAPI client expects three vars.
Fallback to literal `"public"` is built in.

```
BLOFIN_API_KEY=public
BLOFIN_API_SECRET=public
BLOFIN_PASSPHRASE=public
```

For a real paper deploy, set any non-empty values; no private endpoints are
called in paper mode.

## CLI modes

```bash
# Dry run: compute signal + intended weights, do not persist. No cadence check.
python -m scripts.plan_e_runner --mode paper --dry-run \
  --config configs/plan-e-base.json

# Loop: sleeps between 60s checks, fires on the instance's cadence
# (rebalance_interval_hours aligned to rebalance_hour_utc).
python -m scripts.plan_e_runner --mode paper --loop \
  --config configs/plan-e-c.json

# One-shot: fires a rebalance only if the current UTC tick aligns with the
# instance cadence (used for cron-based deploys; systemd --loop preferred).
python -m scripts.plan_e_runner --mode paper --once \
  --config configs/plan-e-base.json
```

## Deploy on Proxmox LXC

Assumes LXC at `/opt/trading-bot` with Python 3.11+ and `numpy`, `pandas`,
`requests` available. From the laptop checkout:

```bash
# Deploy code + configs + systemd template, stop legacy unit, start base + c.
./deploy/deploy_multi.sh

# Deploy + enable all 7 instances (base, c, g, cg, i, 12h, 48h).
./deploy/deploy_multi.sh --full

# Deploy code only (no service changes).
./deploy/deploy_multi.sh --no-enable
```

Behind the scenes the script:

1. Backs up `plan_e_runner.py`, `dashboard_api.py`, `dashboard.html` on LXC.
2. rsyncs code into `/opt/trading-bot/scripts/` and configs into
   `/opt/trading-bot/configs/`.
3. Installs `plan-e@.service` at `/etc/systemd/system/` and runs
   `daemon-reload`.
4. Disables the legacy `plan-e-runner.service` (the prior single-instance
   deploy; 1 day old, no rebalance had fired yet, so this is a clean cut).
5. `systemctl enable --now plan-e@<suffix>.service` for each instance.

**Per-instance unit name:** `plan-e@base.service`, `plan-e@c.service`,
`plan-e@cg.service`, `plan-e@12h.service`, etc. The template reads the
matching config at `configs/plan-e-<suffix>.json`.

**Tail logs:**
```bash
ssh root@trading-bot 'journalctl -u plan-e@base -u plan-e@c -f'
```

## Inspecting state

```bash
# Per-instance portfolio snapshot
cat state/plan-e-base/portfolio.json | python3 -m json.tool

# Last 5 events for a specific instance
tail -n 5 state/plan-e-c/trades.log | python3 -c \
  'import sys, json; [print(json.dumps(json.loads(l), indent=2)) for l in sys.stdin]'

# Equity timeline for an instance
jq -r 'select(.action=="rebalance") | [.ts, .equity_after] | @csv' \
  state/plan-e-base/trades.log
```

**Dashboard API (multi-instance):**
```
GET /api/plan-e/instances          # list instances + per-instance status
GET /api/plan-e/status?instance=X  # full payload for one instance
GET /api/plan-e/trades?instance=X  # rebalance/skip events
GET /api/plan-e/equity_curves      # {instance: [{ts,equity}, …]} for chart
```

The dashboard header shows an instance dropdown (persisted in
`localStorage`), and the **Compare** tab overlays normalized equity curves.

## Reset procedure

```bash
# Stop instances first
ssh root@trading-bot 'systemctl stop plan-e@base plan-e@c plan-e@g plan-e@cg plan-e@i plan-e@12h plan-e@48h'
# Remove per-instance state (keep shared_cache to avoid refetching 30d/60d)
ssh root@trading-bot 'rm -rf /opt/trading-bot/state/plan-e-*'
```

## Pre-flight before going paper

1. Confirm each `configs/plan-e-*.json` carries the validated baseline
   values (lb=72, k_exit=6, signal_sign=-1, initial_balance=5000) plus the
   variant's flag overrides.
2. `python3 -m scripts.plan_e_runner --mode paper --dry-run
   --config configs/plan-e-base.json` — should print 3 longs + 3 shorts at
   10% notional, no gates active.
3. Same for `plan-e-c.json` — gates section should show `vol_halt.enabled:
   true` and a `ratio` vs `k` value.
4. After deploy, wait for the first UTC 00:00 firing on plan-e-base and
   plan-e-c. Both should show 6 legs opened and fees ~$3.30, with identical
   `target_longs`/`target_shorts` (same market → same signal → same picks).
5. 48h later, both should still be identical unless `plan-e-c`'s vol_halt
   gate triggered a skip. If `plan-e-base` and `plan-e-c` have diverged
   without a `skip` event in `plan-e-c`'s log, something is wrong — stop
   and investigate.

## Paper-window exit criteria (P1 policy)

From `backtest/results/PLAN-E-final.md`:

- Minimum 2 weeks paper.
- Extend to 4 weeks if any of:
  - Rolling 2-week Sharpe < 0.5.
  - Single leg shows > 20% slippage vs taker assumption.
  - Regime shift: 7-day return of the long/short basket sharply negative.
- Go/no-go at end of window. Live flip is a separate commit + explicit sign-off.

## Known limitations

- Paper executor uses the rebalance bar's last close as the fill price.
  Real taker fills in production will differ, especially on smaller-cap legs
  (DOT, LINK, ADA, DOGE).
- Runner does not currently alert on API errors or stale data — check logs
  daily during the first week.
- Each instance calls `dc.get_data(..., force_refresh=True)` per symbol per
  cycle, so 7 parallel instances = 7× the Blofin public-endpoint traffic at
  their respective cadences. Blofin's public rate limits are generous
  relative to this, but worth monitoring on the `/api/health` endpoint.
- Instances run in separate processes — the shared cache is file-based with
  no locking. Concurrent writes to the same CSV could in theory corrupt it
  if two instances fire at exactly the same UTC minute. In practice only
  `plan-e-12h` and `plan-e-base` ever align (UTC 00:00), and by then the
  data is ~idempotent. If corruption is ever observed, add a file-lock in
  `DataCollector.get_data`.
