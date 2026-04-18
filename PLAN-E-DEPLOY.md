# Plan E — Paper-trade deploy runbook

**Status:** paper-only. Live mode is a separate decision post-P1 window.
**Target host:** Proxmox LXC that previously hosted the old BTC-USDT bot.
**Runner entrypoint:** `scripts/plan_e_runner.py`
**Config file:** `config.plan-e.json`
**Decision doc:** `backtest/results/PLAN-E-final.md`

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
├── plan_e_portfolio.json     # current cash, equity, positions, counters
├── plan_e_trades.log         # JSONL, one line per rebalance event
└── runner_cache/             # per-symbol 1h CSVs used by the runner
    ├── BTC-USDT_1H.csv
    └── ...
```

`state/` is gitignored. Do NOT commit operational state.

**Important:** runner cache is isolated from `backtest/data/` so the runner's
short-window refresh never clobbers the 12-month CSVs used by backtest scripts.

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
# One-shot: fires exactly one rebalance IF current UTC hour == rebalance_hour_utc.
# Safe to run from cron.
python -m scripts.plan_e_runner --mode paper --once --config config.plan-e.json

# Loop: sleeps between checks, fires at the configured UTC hour.
python -m scripts.plan_e_runner --mode paper --loop --config config.plan-e.json

# Dry run: compute signal + intended weights, do not persist.
python -m scripts.plan_e_runner --mode paper --dry-run --config config.plan-e.json
```

## Deploy on Proxmox LXC

Assumes LXC at `/opt/trading-bot` (same layout as the old bot) with Python 3.11+
and `numpy`, `pandas`, `requests` available.

### Option A — cron (simpler, recommended)

```
# /etc/cron.d/plan-e-runner
# Runs once at UTC 00:05 daily. 5-min offset lets the 00:00 bar close cleanly.
5 0 * * * botuser cd /opt/trading-bot && \
  BLOFIN_API_KEY=public BLOFIN_API_SECRET=public BLOFIN_PASSPHRASE=public \
  /usr/bin/python3 -m scripts.plan_e_runner --mode paper --once \
  --config config.plan-e.json >> /var/log/plan-e-runner.log 2>&1
```

### Option B — systemd (loop mode)

```ini
# /etc/systemd/system/plan-e-runner.service
[Unit]
Description=Plan E paper-trade runner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=botuser
Group=botuser
WorkingDirectory=/opt/trading-bot
Environment=BLOFIN_API_KEY=public
Environment=BLOFIN_API_SECRET=public
Environment=BLOFIN_PASSPHRASE=public
ExecStart=/usr/bin/python3 -m scripts.plan_e_runner --mode paper --loop --config /opt/trading-bot/config.plan-e.json
Restart=always
RestartSec=30

StandardOutput=journal
StandardError=journal
SyslogIdentifier=plan-e-runner

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/trading-bot

[Install]
WantedBy=multi-user.target
```

Enable:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now plan-e-runner
sudo journalctl -u plan-e-runner -f
```

## Inspecting state

```bash
# Current portfolio snapshot
cat state/plan_e_portfolio.json | python3 -m json.tool

# Last 5 rebalance events
tail -n 5 state/plan_e_trades.log | python3 -c \
  'import sys, json; [print(json.dumps(json.loads(l), indent=2)) for l in sys.stdin]'

# Equity timeline
awk -F'"equity":' '/equity/{print $2}' state/plan_e_trades.log | cut -d, -f1
```

## Reset procedure

```bash
# Stop the runner first (systemctl stop plan-e-runner or kill cron)
rm -rf state/
# Next invocation will recreate state/ with initial_balance from config.
```

## Pre-flight before going paper

1. Confirm `config.plan-e.json` has the validated values (lb=72, k_exit=6,
   signal_sign=-1, initial_balance=5000).
2. Run `--dry-run` once and inspect the printed intended weights — should be
   3 longs + 3 shorts at 10% notional each.
3. Run `--once` during a non-rebalance hour; should report "not rebalance
   hour, skipping" and update equity MTM only.
4. Wait for the first UTC 00:00 firing and verify: 6 legs opened, fees ~
   0.11% × 0.60 × $5000 ≈ $3.30, state JSON written, trade log line appended.

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
- Min-notional safety halt triggers if equity drops such that `leg_notional_pct
  × equity` falls below Blofin's per-asset minimum. Halt is graceful (keeps
  existing positions); manual intervention required to resume.
