# Hyperliquid momentum runner — going TESTNET-live (runbook)

The runner (`scripts/hl_xs_runner.py`) is hardened (6 review criticals fixed) and
runs on the LXC in **MAINNET_DRY** (forward-paper on real prices, no wallet,
no orders). To validate the **real order path on testnet** (mock money, zero
financial risk), do the following. Testnet = `app.hyperliquid-testnet.xyz`.

## 1. Unlock the faucet + create a trading key (your step)

**The testnet faucet is GATED** (official anti-bot rule, verified 2026-06-04): it
only pays out to an address that has **already deposited on Hyperliquid mainnet**.
So this is a two-network flow — one tiny **real** deposit on **mainnet** unlocks
**mock** money on **testnet**. The runner only ever trades the testnet mock money;
the mainnet deposit just sits there.

### 1a. Unlock — one small real mainnet deposit (~$10)
- In your self-custody EVM wallet (OKX), on **Arbitrum One**, hold: ~10 USDC of
  **native** Arbitrum USDC (`0xaf88d065e77c8cC2239327C5EDb3A432268e5831`, **not**
  bridged USDC.e) + a little Arbitrum **ETH** for gas.
- Open the **official** mainnet app `https://app.hyperliquid.xyz` (verify the URL
  — phishing clones exist), connect OKX, **Deposit** → Arbitrum One → USDC → ~10.
  First deposit needs a one-time on-chain `approve` tx (a few cents of Arb ETH).
  **⚠️ Never send < 5 USDC — anything below the 5 USDC minimum is lost forever.**
  Credits in < 1 min. (Mainnet bridge is `0x2df1c51e09aecf9cacb7bc98cb1742757f163df7`;
  the *testnet* bridge is a different address and does NOT unlock the faucet.)

### 1b. Claim the testnet mock USDC
- Open `https://app.hyperliquid-testnet.xyz`, connect the **same** OKX address
  (do **not** use email/Privy login — it yields a different address and the gate
  won't see your deposit), go to `/drip`, click **Claim 1000 Mock USDC**. One-time
  claim, 1,000 mock USDC in seconds. All trading from here is mock money.

### 1c. Create an agent (API) wallet for the bot — never use your main key
- Testnet UI: **More → API**, name the agent, set an expiry (~30d), Generate, then
  **Authorize** (sign `approveAgent` in OKX — gas-free EIP-712; the master must be
  funded, which your mock USDC covers). **Save the agent private key shown once.**
- The agent key can place/cancel orders but **cannot withdraw or transfer out** —
  a leaked bot key can't drain you (it *can* still trade/liquidate, so guard it).
  The bot signs with the agent key but **trades/queries the master account**, so
  you MUST set `HL_ACCOUNT_ADDRESS` to your **master public address** (step 2).
  Querying the agent's own address returns empty balances and silently breaks the
  runner.
  - Single-wallet alternative (fine for testnet mock money): use the funded
    wallet's own private key as `HL_PRIVATE_KEY` and omit `HL_ACCOUNT_ADDRESS`.

## 2. Put the key on the LXC (env file, chmod 600)
```bash
ssh root@trading-bot
cat > /etc/trading-bot/hl-xsectional-main.env <<'EOF'
HL_PRIVATE_KEY=0x<AGENT private key>
HL_ACCOUNT_ADDRESS=0x<MASTER public address>   # required with an agent wallet: the account it trades
# (single-wallet alt: use the wallet's own key above and omit HL_ACCOUNT_ADDRESS)
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
