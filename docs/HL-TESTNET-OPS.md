# Hyperliquid momentum runner — going TESTNET-live (runbook)

The runner (`scripts/hl_xs_runner.py`) is hardened (6 review criticals fixed) and
runs on the LXC in **MAINNET_DRY** (forward-paper on real prices, no wallet,
no orders). To validate the **real order path on testnet** (mock money, zero
financial risk), do the following. Testnet = `app.hyperliquid-testnet.xyz`.

## 1. Create + fund a testnet wallet (your step)
- Open `app.hyperliquid-testnet.xyz`, connect an EVM wallet (MetaMask/Rabby).
- Claim **mock USDC** from the testnet faucet (the UI's "claim test funds" /
  deposit-mock flow). A few hundred USDC is plenty.
- **Recommended (safer pattern): use an API/agent wallet** — in the HL UI go to
  *API → generate agent wallet*. The agent key can place orders but **cannot
  withdraw**, so the runner never holds your main key. The agent trades the main
  account.
  - Single-wallet alternative (fine for testnet mock money): just use the funded
    wallet's own private key; no separate account address needed.

## 2. Put the key on the LXC (env file, chmod 600)
```bash
ssh root@trading-bot
cat > /etc/trading-bot/hl-xsectional-main.env <<'EOF'
HL_PRIVATE_KEY=0x<agent-or-wallet-private-key>
# only if using an AGENT wallet for a different main account:
# HL_ACCOUNT_ADDRESS=0x<main-account-address>
EOF
chmod 600 /etc/trading-bot/hl-xsectional-main.env
```
The key is read once from env into an in-memory signer — never logged, never
written to state/health/trades (verified by the security review).

## 3. Flip the config to testnet
Edit `/opt/trading-bot/configs/hl-xsectional-main.json`: set `"network": "testnet"`
(leave `allow_live` false — it is irrelevant on testnet). For a quicker first
signal you may also temporarily lower `lookback_days` (testnet has shorter
history; ~30 works) and raise the rebalance cadence.

## 4. Restart + verify
```bash
systemctl restart hl-xsectional@main
journalctl -u hl-xsectional@main -n 5 --no-pager
```
Expect: `mode=TESTNET live_trading=True`. On the next rebalance the runner places
**real testnet orders**; the dashboard "Hyperliquid" tab shows the live book with
a red **LIVE ORDERS** badge and `execution: live` rebalances. An **unfunded**
account logs a clean "account unfunded — deposit USDC" skip (no halt).

## Safety behaviour now built in (from the review hardening)
- **Atomic-or-flatten rebalance:** if any leg rejects/partials, the runner
  retries the missing legs, and if it still can't form the balanced book it
  **flattens everything** (never leaves a one-legged/directional book) and
  `op_halt`s for an operator.
- **Notional-aware reconcile:** a size-skewed (non-neutral) live book is detected
  and triggers flatten + halt.
- **Circuit breaker flattens** the book on a drawdown halt and auto-resumes once
  drawdown recovers; transient `account_value` read errors skip the cycle (never
  a false halt).
- **MAINNET_LIVE is double-gated:** requires `allow_live: true` (a real JSON
  bool — strings are rejected) AND env `HL_CONFIRM_LIVE=YES`. Real money is never
  reachable by accident.

## Mainnet (real money) — NOT yet
Only after testnet validates AND your legal/tax review of using an EU-unregulated
DEX as an NL resident (grey area — see docs/VENUE-ACCESS-RESEARCH.md). Then:
`network: mainnet`, `allow_live: true`, env `HL_CONFIRM_LIVE=YES`, and start
**small**. Review the money-path findings in the 2026-06-04 execution review
first (slippage tuning, resize-on-drift, and a real-money risk review).

## Op note: dashboard restart
`systemctl restart trading-dashboard` can leave an orphan on :8080 on this LXC
(systemd-tracking quirk). If the dashboard shows `activating`, do a clean cycle:
`systemctl stop trading-dashboard; pkill -9 -f scripts/dashboard_api.py;
systemctl start trading-dashboard` (then MainPID == the :8080 holder).
