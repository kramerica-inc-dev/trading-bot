# blofin-trader — live-money trading bot

**LET OP: dit project raakt ECHT geld.** De bot draait op de Proxmox-LXC (`/opt/trading-bot`, systemd); hl-xsectional@mainnet handelt live op Hyperliquid.

## Werkafspraken
1. **Full-sync na elke wijziging**: deploy naar prod (LXC) → sanitized mirror bijwerken → commit & push naar GitHub (kramerica-inc-dev/trading-bot, **private**).
2. **Sanitized mirror**: `/Users/michiel/Projects/trading/blofin-trader-sanitized` — elke code-wijziging ook daar doorvoeren, EXCLUSIEF creds/state/data.
3. Credentials/API-keys: alleen lokaal/LXC, nooit committen (gitleaks-gecheckt 2026-06-11: history schoon).
4. Onderzoeksdiscipline: pre-registratie + null-gates; dode lanes staan in DECISIONS.md — niet hertesten zonder nieuw bewijs.

## Omgeving
- Prod: LXC `openclaw` (Tailscale), systemd-units als botuser, state onder `/opt/trading-bot/state/`
- Monitoring: dashboard (X-Sectional tab), dagelijks Telegram-healthreport 08:00 UTC
- Research-repo: `../trading-bot-research` (apart, private)
- Notities/handoffs: OneDrive-vault `Claude/Trading/notities/`
