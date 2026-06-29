#!/usr/bin/env bash
# Hyperliquid momentum lane deploy (runner + watchdog + dashboard + sampler)
# to the Proxmox LXC — the HL counterpart of deploy_multi.sh (Plan E).
#
# Run from the project root on the laptop (NOT on Proxmox).
#   ./deploy/deploy_hl.sh                 # code + units, restart runner + dashboard
#   ./deploy/deploy_hl.sh --no-restart    # code + units only, no service touch
#
# Assumes SSH: root@trading-bot (Tailscale host)
# Installs to:  /opt/trading-bot/   (owned by botuser)
#
# What it does:
#   1. Timestamped backups of the HL scripts on the LXC.
#   2. rsyncs the HL lane scripts + configs/hl-xsectional-*.json.
#      NOTE: the LIVE LXC config (gross_exposure, max_gross_usd, allow_live)
#      is intentionally NOT overwritten — repo configs ship as *.repo.json for
#      manual diffing, so a deploy can never silently flip live sizing.
#   3. Installs hl-xsectional-async@.service (LIVE runner since the 2026-06-29
#      event-driven migration), hl-xsectional@.service (retired sync unit, kept
#      for rollback), hl-watchdog@.service + .timer.
#   4. Restarts hl-xsectional-async@mainnet + trading-dashboard, enforces the
#      enable-state (async enabled, sync disabled — so a reboot brings up the
#      async runner, never the stale sync unit), enables the watchdog timer
#      (unless --no-restart).

set -euo pipefail

# Always operate from the project root, regardless of invocation cwd.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

HOST="${HOST:-root@trading-bot}"
REMOTE_DIR="${REMOTE_DIR:-/opt/trading-bot}"
STAMP="$(date +%Y%m%d-%H%M%S)"
RESTART=1
[[ "${1:-}" == "--no-restart" ]] && RESTART=0

# dashboard_api.py/dashboard.html are SHARED with deploy_multi.sh (Plan E);
# both scripts ship the same repo copy, so last-deploy-wins is always the
# current repo state — keep the repo the single source of truth.
FILES=(scripts/hl_xs_runner.py scripts/hl_runner_async.py scripts/hl_ws_feed.py
       scripts/hl_nonce.py scripts/regime_tag.py scripts/hl_adapter.py
       scripts/hl_watchdog.py scripts/xs_core.py scripts/mode_gate.py scripts/notify.py
       scripts/equity_sampler.py scripts/dashboard_api.py scripts/dashboard.html)

echo "→ Target: $HOST:$REMOTE_DIR  (restart: $RESTART)"

echo "→ Backing up remote HL files on LXC…"
ssh "$HOST" bash <<EOF
set -euo pipefail
cd "$REMOTE_DIR"
for f in ${FILES[*]}; do
  if [ -f "\$f" ]; then cp "\$f" "\$f.backup-$STAMP"; fi
done
EOF

echo "→ Syncing scripts…"
rsync -avz "${FILES[@]}" "$HOST:$REMOTE_DIR/scripts/"

echo "→ Shipping repo HL configs as *.repo.json (live config never overwritten)…"
for c in configs/hl-xsectional-*.json; do
  rsync -avz "$c" "$HOST:$REMOTE_DIR/${c%.json}.repo.json"
done

echo "→ Installing systemd units…"
rsync -avz deployment/systemd/hl-xsectional-async@.service \
           deployment/systemd/hl-xsectional@.service \
           deployment/systemd/hl-watchdog@.service \
           deployment/systemd/hl-watchdog@.timer \
           "$HOST:/etc/systemd/system/"

ssh "$HOST" bash <<EOF
set -euo pipefail
chown -R botuser:botuser "$REMOTE_DIR/scripts" "$REMOTE_DIR/configs"
systemctl daemon-reload
EOF

if [[ "$RESTART" == "1" ]]; then
  echo "→ Restarting services…"
  # NOTE: a restart resets the in-process leverage verification, so the first
  # cycle after restart sizes any gross_exposure>1.0 as 1.0 until the venue
  # read-back confirms the pin (conservative by design, ~1 cycle).
  ssh "$HOST" bash <<'EOF'
set -euo pipefail
# Live lane runs the event-driven async runner since 2026-06-29; the old sync
# unit is retired. Enforce the enable-state so a reboot brings up async and the
# two never run concurrently on the same live wallet/state (nonce/double-order
# hazard). `disable` only touches boot-state — it does not stop a running unit.
systemctl disable hl-xsectional@mainnet 2>/dev/null || true
systemctl enable hl-xsectional-async@mainnet
systemctl restart hl-xsectional-async@mainnet
systemctl restart trading-dashboard || true
systemctl enable --now hl-watchdog@mainnet.timer
sleep 3
systemctl --no-pager --lines=0 status hl-xsectional-async@mainnet | head -5
if ! grep -qs '^TELEGRAM_BOT_TOKEN=' /etc/trading-bot/hl-watchdog-mainnet.env; then
  echo "⚠️  watchdog --notify is on but TELEGRAM_* not set in /etc/trading-bot/hl-watchdog-mainnet.env (alerts journal-only)"
fi
EOF
  echo "→ Health snapshot:"
  ssh "$HOST" "sleep 5; python3 -c \"import json;print(json.dumps({k:v for k,v in json.load(open('$REMOTE_DIR/state/hl_xsectional/mainnet/health.json')).items() if k in ('ts','mode','live_trading','equity','cb_state','gross_exposure','leverage_verified','delever_active','margin_ratio','margin_read_ok','reconcile_ok')}, indent=2))\"" \
    || echo "⚠️  health snapshot unavailable (runner may still be starting — check: ssh $HOST cat $REMOTE_DIR/state/hl_xsectional/mainnet/health.json)"
fi

echo "✓ Deploy complete."
