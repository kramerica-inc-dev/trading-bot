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
Edit `/opt/trading-bot/configs/hl-xsectional-main.json`:
- `"network": "testnet"` (leave `allow_live` false — irrelevant on testnet).
- **Drop BTC from `universe` on testnet.** HL *testnet*'s BTC oracle is badly
  stale (mid ~+2.7% off oracle), so marketable BTC orders are rejected
  `Price too far from oracle` and the basket can't form. Use the 6 healthy
  testnet perps `["ETH","SOL","BNB","ADA","AVAX","DOGE"]` → a clean top-3/bottom-3
  basket. (Mainnet keeps all 10 — BTC's mainnet oracle is healthy. XRP/DOT/LINK
  don't exist on testnet and are auto-dropped anyway.)
- `"slippage": 0.02` — 5% stacks on stale testnet mids and busts the oracle band;
  2% still crosses every book (spreads ≤0.4%) and is a saner cap for mainnet too.
- `lookback_days` can stay 120 (testnet has 400+ daily bars for these 6).

**Unified accounts are handled automatically — no transfer/mode-switch needed.**
HL's default *unified account* mode keeps USDC collateral in the **spot**
clearinghouse (the per-perp `marginSummary.accountValue` reads a "not meaningful"
0; the faucet's mock USDC lands in spot). The adapter computes equity as
`perp accountValue + free spot USDC (total − hold)`, correct in both unified and
standard modes. So you do **not** transfer Spot→Perps (that button is greyed in
unified mode) or switch modes (leaving unified needs >$10k anyway).

## 4. Restart + verify
```bash
systemctl restart hl-xsectional@main
journalctl -u hl-xsectional@main -n 5 --no-pager
```
Expect: `mode=TESTNET live_trading=True`. On the next rebalance the runner places
**real testnet orders**; the dashboard "Hyperliquid" tab shows the live book with
a red **LIVE ORDERS** badge and `execution: live` rebalances. An **unfunded**
account logs a clean "account unfunded — deposit USDC" skip (no halt).

## Validated on testnet (2026-06-04)
First real-order run on `hl-xsectional@main` (TESTNET, agent wallet
`0x34B9…5B12`, master `0x70Cb…4c89`, 1000 mock USDC unified account): a clean
**6-leg dollar-neutral basket held** — long BNB/DOGE/ETH, short ADA/AVAX/SOL,
~$330/leg, **net ~$0, neutrality skew 0.0%**, `reconcile_ok=True`, equity read
**990** (unified fix), drawdown 0.01%, CB normal. The atomic-or-flatten safety
also fired correctly mid-debug (a rejected BTC leg → all legs flattened +
`op_halt`, never a one-legged book). Four issues found+fixed on mock money before
any mainnet exposure: (1) unified-account equity read
(`account_value = perp AV + free spot USDC`), (2) a transient post-trade equity
guard (ignore a settling read <50% of pre-rebalance equity), (3) testnet BTC
oracle exclusion, (4) marketable `slippage` 0.05→0.02.

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

## Pre-mainnet soak agenda (from the 2026-06-04 adversarial audit)
A 4-lens adversarial audit (code-correctness / live-state / secret-hygiene /
completeness) passed the testnet deployment with **no blockers**. Four code items
it flagged are **already fixed** (commit after 895a24a): the post-trade transient
guard now gates on a completed+clean rebalance (`_accept_post_trade_equity`, not a
bare magnitude threshold) so a real >50% loss on a failed/one-legged book is
recorded; `account_value` no longer skips a perp-funded account on a spot-endpoint
outage; `health.json n_positions` reflects the live venue book in live mode (was
structurally 0); the equity docstring is corrected. **Close these during the soak,
before any mainnet capital:**
- [x] **Turnover rebalance — DONE 2026-06-04.** Forced a lb=30 ranking (flips 4 of
  6 legs) → a 4-close + 4-open rotation completed cleanly: `complete=True`, neutral
  (994/992), **0 retries/flattens, no op_halt**. The close-some/open-others path holds.
- [x] **Drift / resize — DONE 2026-06-04.** `_execute_live` now resizes a
  held-correct-side leg toward target (`_resize_order`, gated by
  `resize_threshold=0.10` so it doesn't churn on noise; the trim/reduce path can
  never flip a side). Validated on testnet: an ETH long manually oversized to $529
  was trimmed by a `resize` (delta −198.71) back to $330, book re-neutralized
  (skew 0.27%), only the drifted leg touched.
- [x] **op_halt trip + recovery — DONE 2026-06-04.** Tripped via a manually
  non-neutral book → reconcile-halt flattened all legs to FLAT + set `op_halt`; the
  recovery procedure below restores cleanly. Verified end-to-end.
- [ ] **Funding** — DRY/sim omits the ~6%/yr funding drag (paper P&L too rosy);
  confirm on-chain equity accrues funding over the soak; decide whether to model it
  in DRY for comparability.
- [ ] **Data staleness guard** — port `xs_runner`'s `data_staleness_hours`; the HL
  path never checks recency of the newest closed bar.
- [ ] **min_assets headroom** — at 6 assets / min_assets 6, losing one asset's data
  freezes the open book indefinitely (skip, no flatten). Decide freeze-vs-flatten.
- [ ] **Slippage / fill quality** — log realized avgPx vs mid per leg; check the 2%
  cap never causes a non-fill on thin books; compare to the 0.00045 sim assumption.
- [ ] **Crash-mid-rebalance / overlap** — kill between stage-1 closes and stage-2
  opens, confirm clean restart recovery; no lock guards overlapping `run_once`.

### op_halt recovery procedure (verified 2026-06-04)
`op_halt` (a failed rebalance or a reconcile divergence) is **manual-clear only**:
the book is auto-flattened and the instance stays parked until an operator clears
it. First read `trades.log` for the `rebalance_halt` / `reconcile_halt` entry and
understand WHY it halted. Then:
```bash
systemctl stop hl-xsectional@main
# 1. CONFIRM THE VENUE IS FLAT before clearing (never clear a non-flat halt):
python3 - <<'PY'
import sys; sys.path.insert(0,"/opt/trading-bot/scripts")
from hl_adapter import HLAdapter
a = HLAdapter(network="testnet", account_address="0x<MASTER>")
assert not a.book_notional(), "NOT flat — investigate, do NOT clear"
print("flat — safe to clear")
PY
# 2. clear the halt and let it re-enter on the next cycle:
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("/opt/trading-bot/state/hl_xsectional/main/state.json")
s = json.loads(p.read_text()); s["cb_state"] = "normal"; s["last_rebalance_ts"] = None
p.write_text(json.dumps(s, indent=2))
PY
systemctl start hl-xsectional@main   # rebuilds the basket
# 3. verify: health.json cb_state=normal, reconcile_ok=true, a fresh neutral book.
```

## Mainnet — STAGED in MAINNET_DRY (creds in place, flag OFF)
A separate instance **`hl-xsectional@mainnet`** is staged (2026-06-04):
`configs/hl-xsectional-mainnet.json` (`instance_name=mainnet` → its own state dir,
never collides with the testnet `@main` soak), `network=mainnet`,
**`allow_live=false` → MAINNET_DRY (zero real orders)**, universe=10 (BTC included —
mainnet oracle is healthy). The mainnet **agent** key is in
`/etc/trading-bot/hl-xsectional-mainnet.env` (600, set by the user directly; never
in chat). Agent validated read-only: derives to the approved agent "Agent Trader"
(`extraAgents`), `account_value` reads the real $57.52. It forward-papers on real
mainnet prices alongside the testnet soak. Both run independently.

### Go-live — the one-flag flip (do NOT do until the gates clear)
**Gates:** testnet soak green + your **legal/tax sign-off** (EU-unregulated DEX as
an NL resident — grey area, see docs/VENUE-ACCESS-RESEARCH.md) + consider a funding
top-up (at $57 a 6-leg basket is ~$19/leg, just above HL's $10 min — thin). Then:
```bash
# 1. arm: flip the flag + add the out-of-band confirm env var
python3 -c "import json,pathlib; p=pathlib.Path('/opt/trading-bot/configs/hl-xsectional-mainnet.json'); c=json.loads(p.read_text()); c['allow_live']=True; p.write_text(json.dumps(c,indent=2)+'\n')"
printf 'HL_CONFIRM_LIVE=YES\n' >> /etc/trading-bot/hl-xsectional-mainnet.env
# 2. restart -> mode=MAINNET_LIVE, live_trading=True (REAL orders)
systemctl restart hl-xsectional@mainnet
journalctl -u hl-xsectional@mainnet -n 6 --no-pager   # expect mode=MAINNET_LIVE
```
Then watch the first live rebalance on the dashboard, start **small**, and review
the open soak items (funding modeling, staleness, slippage) first.

## Op note: dashboard restart
`systemctl restart trading-dashboard` can leave an orphan on :8080 on this LXC
(systemd-tracking quirk). If the dashboard shows `activating`, do a clean cycle:
`systemctl stop trading-dashboard; pkill -9 -f scripts/dashboard_api.py;
systemctl start trading-dashboard` (then MainPID == the :8080 holder).
