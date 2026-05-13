# Carry runner — operations runbook

> Status: written 2026-05-13 for the P2 deploy on OKX demo (simulated
> trading). Companion docs: `docs/STRATEGY-CARRY.md` (the strategy),
> `docs/CARRY-BUILD-PLAN.md` (the phased build). Decision authority:
> `DECISIONS.md` 2026-05-13 entry.

This is the short-and-dated checklist a person needs to:
1. provision OKX EU demo credentials,
2. install them on the LXC,
3. start the `carry@btc.service`,
4. read the logs and verify the first cycles look correct,
5. operate the runner day-to-day (halt, resume, stop).

---

## 1. Create OKX EU demo (simulated-trading) API key

The carry runner P2 talks to OKX's *simulated-trading* endpoint (same
base URL as production, plus the `x-simulated-trading: 1` header). A
**demo API key** is required; production keys won't be accepted by the
simulated-trading endpoint.

1. Go to `https://my.okx.com/nl/account/my-api` and log in.
2. Switch into **Demo Trading** mode (top of the page or via the account
   menu — OKX UIs change but the key/state cards are clearly labelled
   "Demo trading").
3. Click **Create v5 API Key**. Set:
   - **Name**: `carry-btc-demo` (any unique label).
   - **Passphrase**: pick a strong one — note it now; OKX will not show
     it again.
   - **Permissions**: **Read** + **Trade** on **Spot** and **Perpetual
     Swap**. **No Withdraw**, no Fast-API, no Earn — this is a carry
     runner, never moves funds off the account.
   - **IP whitelist**: bind it to the LXC's Tailscale IP
     (`100.125.10.88`) and/or the LXC's egress public IP if known. If
     uncertain, set "any IP" for the demo key — it's simulated funds —
     and tighten before P3.
4. After creation, OKX shows the **API Key** (looks like a UUID) and the
   **Secret Key** (long base64-ish blob). Copy both — the secret is
   only shown once.
5. Note the three values you'll need:
   - `OKX_API_KEY` — the public key.
   - `OKX_API_SECRET` — the secret shown immediately after creation.
   - `OKX_API_PASSPHRASE` — the passphrase you chose in step 3.

If you accidentally provision a production key (no "Demo trading" badge
on the key card), the runner will fail with auth errors against the
demo endpoint. Delete it and re-create under Demo Trading.

---

## 2. Install credentials on the LXC

The deploy step has already created `/etc/trading-bot/carry-btc.env` as
a template. Fill it with the three values from step 1:

```bash
ssh root@trading-bot
chmod 600 /etc/trading-bot/carry-btc.env
nano /etc/trading-bot/carry-btc.env
```

The file body, with the three lines uncommented and filled:

```
OKX_API_KEY=<paste-api-key>
OKX_API_SECRET=<paste-secret>
OKX_API_PASSPHRASE=<paste-passphrase>
```

Confirm the file:

```bash
ls -l /etc/trading-bot/carry-btc.env
# -rw------- 1 root root … /etc/trading-bot/carry-btc.env
```

(The runner reads the env file via systemd `EnvironmentFile=`. systemd
loads it on service start; no daemon-reload is needed if you only
edited the env file.)

---

## 3. First run + smoke test

```bash
systemctl start carry@btc
systemctl status carry@btc
```

Expected `status` after ~10 seconds:

```
● carry@btc.service - Carry runner (btc) — funding/basis cash-and-carry on OKX
     Loaded: loaded (/etc/systemd/system/carry@.service; disabled; …)
     Active: active (running) since …
   Main PID: <pid> (python3)
      Tasks: 2 (limit: …)
     Memory: ~30M
```

Then check the first cycle in the journal:

```bash
journalctl -u carry@btc -n 80 --no-pager
```

What to look for **in the first 60 seconds**:

1. **Mode line** (logged on every cycle). Should read:
   ```
   CarryRunner instance=btc mode=P2_DEMO exchange=okx spot=BTC-USDT
   perp=BTC-USDT private_creds=True dry_run=False okx_demo=True
   allow_live=False
   ```
   - `mode=P2_DEMO` confirms the three-state gate landed in the demo
     trading state. If you see `mode=DRY_RUN`, the runner ignored the
     config — check the path in `ExecStart` and that
     `/opt/trading-bot/configs/carry-btc.json` exists.
   - `private_creds=True` confirms systemd loaded the env-file.

2. **Fee schedule line**, immediately after:
   ```
   [btc] FEES spot_maker=0.0008 spot_taker=0.001 perp_maker=0.0002
   perp_taker=0.0005 (sources={'spot_maker': 'live', ...})
   ```
   All four values from `sources` should be `live`. If any are
   `fallback`, the runner uses the spec-default constant for that leg —
   it's safe but worth investigating; usually means the demo account
   doesn't expose `/api/v5/account/trade-fee` for that instType.

3. **Leverage-cap verification line**:
   ```
   [btc] LEVERAGE leverage cap OK: configured=2.0×, effective_max=2.0×
   (account=2.0, contract=125.0)
   ```
   - `configured=2.0×` from the config.
   - `effective_max=<account_max>` — the actually-enforced cap on this
     account+contract.
   - **If you see `MISMATCH`**: EU caps your effective leverage below
     `2.0`. Lower `leverage_cap` in `/opt/trading-bot/configs/carry-btc.json`
     to match, then `systemctl restart carry@btc`. The cap matters for
     the perp margin sizing in `target_position_for()`.

4. **First cycle line** (one per minute):
   ```
   [btc] cycle #1 mode=P2_DEMO spot=63425.0 perp=63430.5 fund_8h=0.00012
   gate=OFF trail_ann=-0.0034% action=noop alerts=0 halted=False
   reconcile_ok=True
   ```
   The big tell is `gate=OFF action=noop`: with 2026 YTD funding still
   compressed (trailing-90d ≈ -0.9%/yr per `STRATEGY-CARRY.md` §3),
   the green-button threshold (+5%/yr) is not met, so the runner sits
   flat and just logs. **This is the expected steady state until BTC
   funding normalises.**

5. **Per-cycle JSONL trade log**:
   ```bash
   tail -f /opt/trading-bot/state/carry/btc/trades.log
   ```
   Each line is one cycle. Useful fields per entry:
   - `mode` — should be `P2_DEMO` on every line.
   - `gate.on` / `gate.trailing_annualised` — the green-button.
   - `action.kind` — `noop` / `do_open` / `do_unwind` / `would_resize`
     / `skip`. In P2 the `do_*` kinds also place real demo orders;
     check `order_result.ok` and `order_result.legs[*]`.
   - `risk_alerts` — `basis_blowout`, `margin_low`. Should be `[]`
     under normal conditions.
   - `halted` / `halt_reason` — true if a kill-switch has fired.
   - `reconcile.ok` and `reconcile.errors` — exchange-vs-runner state
     consistency (C5/C7 in live mode).

### Failure shapes — what to do

| Symptom in journal | Likely cause | Fix |
|---|---|---|
| `[btc] mode=P2_DEMO … private_creds=False` | env-file missing or empty | Check `/etc/trading-bot/carry-btc.env`; restart service. |
| `OK-ACCESS-PASSPHRASE error / Authentication failed` repeatedly | wrong passphrase or key from production (non-demo) tier | Re-create the key under "Demo Trading" mode in `my.okx.com/nl`. |
| `LEVERAGE … MISMATCH: configured=2.0× but effective_max=1.0×` | EU retail cap below the config | Edit `configs/carry-btc.json`, set `leverage_cap: 1.0`, `systemctl restart carry@btc`. |
| `RECONCILE … C5: perp drift exchange=X vs simulated=Y` | the runner's view of position drifted from the exchange | Investigate; usually a leg-2 abort that didn't fully roll back. The runner won't *crash* on this but it will flag it. |
| `legging_abort_perp_failed` in `order_result.reason` | leg-2 didn't fill within `legging_window_sec` | Expected on illiquid moments. Counted in `legging_aborts_total`. Spot leg is flattened immediately, no net exposure. |

---

## 4. Operational controls

All commands run as `root` on the LXC.

### Fast operator halt (manual flatten + park)

```bash
touch /opt/trading-bot/state/carry/btc/halt
```

- On the *next cycle* the runner sees the sentinel and:
  - **If positioned**: unwinds the carry pair immediately
    (`action.kind=do_unwind`, `action.reason=manual_halt_with_open_position`).
  - **If flat**: stays noop (`action.reason=manual_halt_flat`).
- The runner keeps cycling (so you keep the JSONL/health logs) but
  never opens a new position while the sentinel exists.

### Resume after a manual halt

```bash
rm /opt/trading-bot/state/carry/btc/halt
```

The next cycle resumes normal decision-making (subject to the
green-button gate).

### Resume after an *automatic* halt (basis-kill, startup probe failure)

The automatic halt sets `state.halted=true` in
`/opt/trading-bot/state/carry/btc/state.json`. To clear it (after
investigating the cause):

```bash
systemctl stop carry@btc
python3 -c "
import json, sys
p='/opt/trading-bot/state/carry/btc/state.json'
d=json.load(open(p))
d['halted']=False
d['halt_reason']=None
json.dump(d, open(p,'w'), indent=2)
print('cleared')
"
systemctl start carry@btc
```

### Stop the runner entirely

```bash
systemctl stop carry@btc
systemctl disable carry@btc   # only if you want it to not come back on reboot
```

Stopping the service leaves any open demo position untouched. If you
want to flatten first, drop the halt sentinel, wait one cycle, then
stop.

### Inspect current state without restarting

```bash
cat /opt/trading-bot/state/carry/btc/health.json
# alive, mode, halted, last_cycle_ts, simulated_position, reconcile_ok
tail -n 1 /opt/trading-bot/state/carry/btc/trades.log | python3 -m json.tool
# the full per-cycle entry — fees, leverage, gate, action, reconcile
```

### Inspect basis / funding / exposure across cycles

```bash
# last 20 funding-rate, basis, action lines:
tail -n 20 /opt/trading-bot/state/carry/btc/trades.log | \
  python3 -c "
import sys, json
for line in sys.stdin:
  d=json.loads(line)
  print(f\"{d['ts'][11:19]}  fund_ann={d.get('funding_rate_annualised'):.4f}  basis_pct={(d.get('basis_frac') or 0)*100:.4f}%  action={d['action']['kind']}\")
"
```

---

## 5. Promoting to P3 (live money) — what it will require

P2 is **not** P3. Once the demo run has accumulated ≥4 weeks of clean
operation (per `docs/CARRY-BUILD-PLAN.md` §P2→P3), the promotion path is:

1. Provision a *production* OKX EU API key with the same permissions
   (Read + Trade on Spot + Perpetual, **no Withdraw**), bound to the
   LXC's egress IP.
2. Replace the contents of `/etc/trading-bot/carry-btc.env` with the
   production credentials.
3. Edit `/opt/trading-bot/configs/carry-btc.json`:
   - `okx_demo: false`
   - `allow_live: true`
   - `initial_notional_usd: 1000` and `target_dn_notional_fraction: 0.5`
     (so per-leg = 500 USD, well under `live_max_usd: 1000`).
4. `systemctl restart carry@btc`. The first cycle's mode line should
   read `mode=P3_LIVE`. If sizing exceeds `live_max_usd` the constructor
   refuses to start with a clear error — fix the sizing and try again.
5. Watch the first 24h closely. Same risk controls (basis-kill,
   manual halt, legging window) are active.

P4 (scale to target book $5k–$50k) requires a separate go/no-go gate per
the build plan; that's not on this runbook.

---

## 6. Where things live

| Thing | Path |
|---|---|
| Service unit | `/etc/systemd/system/carry@.service` |
| Per-instance config | `/opt/trading-bot/configs/carry-btc.json` |
| Per-instance credentials | `/etc/trading-bot/carry-btc.env` (chmod 600) |
| Per-instance state | `/opt/trading-bot/state/carry/btc/state.json` |
| Per-instance JSONL log | `/opt/trading-bot/state/carry/btc/trades.log` |
| Per-instance health (dashboard) | `/opt/trading-bot/state/carry/btc/health.json` |
| Manual-halt sentinel | `/opt/trading-bot/state/carry/btc/halt` |
| Runner code | `/opt/trading-bot/scripts/carry_runner.py` |
| Strategy spec | `/opt/trading-bot/docs/STRATEGY-CARRY.md` |
| Phased build plan | `/opt/trading-bot/docs/CARRY-BUILD-PLAN.md` |
