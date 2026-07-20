#!/usr/bin/env python3
"""Operator onboarding + go/park CLI for the carry-hl@btc lane (root, on-LXC).

Design decisions this encodes (2026-07-20 verification):
  * DEDICATED WALLET, not a sub-account — HL gates createSubAccount behind
    $100k cumulative traded volume (master had $6.3k). The runner's
    `hl_dedicated_account_confirmed=true` mode covers this; the wallet must
    run NO other lane.
  * The LXC holds only the AGENT key. Agent keys cannot sign user-signed
    actions (usdClassTransfer is attributed to the agent's own empty account),
    so the perp/spot split of a deposit is done ONCE by the operator in the
    HL UI — this script only VERIFIES balances, it never moves funds.

Flow:  deposit → `onboard` (once) → `check` → `go`  |  `park` to step back.

Commands:
    onboard --master 0x…   write env file (agent key read from stdin, one
                           line) + point the config at the dedicated wallet;
                           verifies the agent is approved for that master.
    check                  balances + the sizing that `go` would apply.
    go [--live-max N]      size from actual balances, flip config to live
                           (dry_run=false, allow_live=true), set
                           HL_CONFIRM_LIVE=YES, restart the unit, verify the
                           first live cycle from health.json.
    park                   flip back to DRY_RUN (position unwind is the
                           runner's job via halt/green-button, not ours).

Usage:  python3 -m scripts.carry_hl_go [--config …] [--env-file …] <cmd>
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

CONFIG_DEFAULT = "/opt/trading-bot/configs/carry-hl-btc.json"
ENV_DEFAULT = "/etc/trading-bot/carry-hl-btc.env"
UNIT = "carry@hl-btc"
HEALTH = "/opt/trading-bot/state/carry/btc-hl/health.json"

# Sizing buffers (see HL-CARRY-STUDY + go-script design note):
#   spot leg needs notional × (fees+slippage buffer);
#   perp leg gets ≥ 0.8 × notional so margin_ratio starts ≥ 1.6 at L=2
#   (alarm threshold is 1.5) and the S1 worst-rally bound keeps headroom.
SPOT_BUFFER = 1.02
PERP_FUND_FRACTION = 0.8


def _info(network: str, payload: Dict[str, Any]) -> Any:
    host = ("https://api.hyperliquid-testnet.xyz" if network == "testnet"
            else "https://api.hyperliquid.xyz")
    req = urllib.request.Request(
        host + "/info", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=15))


def _read_env(path: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if Path(path).exists():
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def _write_env(path: str, env: Dict[str, str]) -> None:
    body = "".join(f"{k}={v}\n" for k, v in env.items())
    p = Path(path)
    p.write_text(body)
    os.chmod(p, 0o600)


def _load_config(path: str) -> Dict[str, Any]:
    return json.loads(Path(path).read_text())


def _save_config(path: str, cfg: Dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(cfg, indent=2) + "\n")


def _balances(network: str, master: str) -> Tuple[float, float]:
    """(free spot USDC, perp withdrawable) of the dedicated wallet."""
    spot = _info(network, {"type": "spotClearinghouseState", "user": master})
    spot_free = 0.0
    for b in spot.get("balances", []):
        if b.get("coin") == "USDC":
            spot_free = float(b["total"]) - float(b.get("hold", 0.0))
    perp = _info(network, {"type": "clearinghouseState", "user": master})
    perp_av = float((perp.get("marginSummary") or {}).get("accountValue", 0.0))
    return spot_free, perp_av


def _sizing(cfg: Dict[str, Any], spot_free: float, perp_av: float,
            live_max: Optional[float]) -> Dict[str, float]:
    cap = float(live_max if live_max is not None
                else cfg.get("live_max_usd", 1000.0))
    frac = float(cfg.get("target_dn_notional_fraction", 0.6))
    notional = min(spot_free / SPOT_BUFFER, perp_av / PERP_FUND_FRACTION, cap)
    notional = max(0.0, round(notional, 2))
    return {
        "per_leg_notional_usd": notional,
        "initial_notional_usd": round(notional / frac, 2),
        "live_max_usd": cap,
    }


def cmd_onboard(a) -> int:
    cfg = _load_config(a.config)
    network = cfg.get("hl_network", "mainnet")
    print("Paste the AGENT private key for the dedicated wallet "
          "(one line, input not stored anywhere but the env file):",
          file=sys.stderr)
    agent_key = sys.stdin.readline().strip()
    if not agent_key.startswith("0x") or len(agent_key) != 66:
        sys.exit("that does not look like a 0x… 32-byte private key")

    from eth_account import Account
    agent_addr = Account.from_key(agent_key).address
    try:
        agents = _info(network, {"type": "extraAgents", "user": a.master})
        approved = [x.get("address", "").lower() for x in (agents or [])]
        if agent_addr.lower() in approved:
            print(f"OK  agent {agent_addr} is approved for {a.master}")
        else:
            print(f"WAARSCHUWING: agent {agent_addr} niet gevonden in "
                  f"extraAgents van {a.master}: {approved} — ga alleen door "
                  "als je de agent zojuist hebt aangemaakt (indexer-lag).")
    except Exception as e:
        print(f"WAARSCHUWING: extraAgents-check faalde ({e}) — handmatig "
              "controleren dat de agent bij deze master hoort.")

    _write_env(a.env_file, {
        "HL_CARRY_PRIVATE_KEY": agent_key,
        "HL_CARRY_ACCOUNT_ADDRESS": a.master,
    })
    print(f"OK  env geschreven: {a.env_file} (0600)")

    cfg["hl_account_address"] = a.master
    cfg["hl_sub_account_address"] = None
    cfg["hl_dedicated_account_confirmed"] = True
    _save_config(a.config, cfg)
    print(f"OK  config bijgewerkt: {a.config} (dedicated wallet {a.master})")
    print("Volgende stap: fondsen storten + splitsen (UI), dan `check` en `go`.")
    return 0


def _preflight(a) -> Tuple[Dict[str, Any], str, float, float, Dict[str, float]]:
    cfg = _load_config(a.config)
    env = _read_env(a.env_file)
    master = cfg.get("hl_account_address") or env.get("HL_CARRY_ACCOUNT_ADDRESS")
    if not master:
        sys.exit("geen master-adres — draai eerst `onboard`")
    if cfg.get("hl_dedicated_account_confirmed") is not True:
        sys.exit("config heeft hl_dedicated_account_confirmed != true — "
                 "draai eerst `onboard`")
    if not env.get("HL_CARRY_PRIVATE_KEY"):
        sys.exit(f"geen agent-key in {a.env_file} — draai eerst `onboard`")
    network = cfg.get("hl_network", "mainnet")
    spot_free, perp_av = _balances(network, master)
    sizing = _sizing(cfg, spot_free, perp_av, getattr(a, "live_max", None))
    print(f"wallet={master} network={network}")
    print(f"spot USDC vrij: ${spot_free:,.2f}   perp accountValue: ${perp_av:,.2f}")
    print(f"sizing → per-leg ${sizing['per_leg_notional_usd']:,.2f}, "
          f"initial_notional ${sizing['initial_notional_usd']:,.2f} "
          f"(cap ${sizing['live_max_usd']:,.0f})")
    return cfg, master, spot_free, perp_av, sizing


def cmd_check(a) -> int:
    _preflight(a)
    print("check klaar — `go` voert dit door en start live.")
    return 0


def cmd_go(a) -> int:
    cfg, master, spot_free, perp_av, sizing = _preflight(a)
    n = sizing["per_leg_notional_usd"]
    if n < 50.0:
        sys.exit(f"per-leg notional ${n:.2f} < $50 — storting/splitsing niet "
                 "compleet? (spot moet ~55%, perp ~45% van de storting zijn)")
    cfg["initial_notional_usd"] = sizing["initial_notional_usd"]
    cfg["live_max_usd"] = sizing["live_max_usd"]
    cfg["dry_run"] = False
    cfg["allow_live"] = True
    _save_config(a.config, cfg)
    env = _read_env(a.env_file)
    env["HL_CONFIRM_LIVE"] = "YES"
    _write_env(a.env_file, env)
    print("OK  config live-geflipt + HL_CONFIRM_LIVE=YES")

    subprocess.run(["systemctl", "restart", UNIT], check=True)
    print(f"OK  {UNIT} herstart — wachten op eerste cycle…")
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(10)
        try:
            h = json.loads(Path(HEALTH).read_text())
        except Exception:
            continue
        if h.get("mode") == "P3_LIVE":
            print(json.dumps(h, indent=2)[:800])
            print("LIVE — controleer de eerste open in trades.log; "
                  "green-button bepaalt de rest.")
            return 0
    print("GEEN P3_LIVE-health binnen 3 min — check "
          f"`journalctl -u {UNIT} -n 50` en health.json handmatig.")
    return 1


def cmd_park(a) -> int:
    cfg = _load_config(a.config)
    cfg["dry_run"] = True
    cfg["allow_live"] = False
    _save_config(a.config, cfg)
    env = _read_env(a.env_file)
    env.pop("HL_CONFIRM_LIVE", None)
    _write_env(a.env_file, env)
    subprocess.run(["systemctl", "restart", UNIT], check=True)
    print("OK  teruggeparkeerd naar DRY_RUN (open positie? eerst de "
          "halt-sentinel gebruiken zodat de runner netjes unwindt).")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=CONFIG_DEFAULT)
    p.add_argument("--env-file", default=ENV_DEFAULT)
    sub = p.add_subparsers(dest="cmd", required=True)
    ob = sub.add_parser("onboard")
    ob.add_argument("--master", required=True, help="dedicated wallet 0x…")
    sub.add_parser("check")
    g = sub.add_parser("go")
    g.add_argument("--live-max", type=float, default=None,
                   help="override live_max_usd (default: config)")
    sub.add_parser("park")
    a = p.parse_args(argv)
    return {"onboard": cmd_onboard, "check": cmd_check,
            "go": cmd_go, "park": cmd_park}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
