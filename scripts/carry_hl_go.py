#!/usr/bin/env python3
"""Operator onboarding + go/park CLI for the HL carry lane — unit carry@hl-btc
(root, on-LXC).

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
    go [--live-max N]      size from actual balances, pre-validate the live
                           config, archive the DRY state (its SIMULATED book
                           must never reach the live runner), write config +
                           env atomically, restart the unit and verify a fresh
                           P3_LIVE cycle. Any failure → ROLLBACK (config, env
                           and state restored, unit back in DRY_RUN) — unless
                           a live position may already exist, then the halt
                           sentinel is set instead and the operator is told.
    park [--force]         flip back to DRY_RUN. Refuses while health shows an
                           open live book (set the halt sentinel first so the
                           runner unwinds), unless --force.

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
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from carry_position import (  # noqa: E402
    GO_PERP_FUND_FRACTION, GO_SPOT_BUFFER, go_sizing,
)

CONFIG_DEFAULT = "/opt/trading-bot/configs/carry-hl-btc.json"
ENV_DEFAULT = "/etc/trading-bot/carry-hl-btc.env"
# The ONE unit for this lane (carry-hl@.service was retired 2026-10-08: it
# pointed at the same config/state/env — two runners on one book).
UNIT = "carry@hl-btc"
STATE_DIR = "/opt/trading-bot/state/carry/btc-hl"
HEALTH = STATE_DIR + "/health.json"
VERIFY_TIMEOUT_SEC = 180
VERIFY_POLL_SEC = 10

# Sizing buffers — single source of truth in carry_position.go_sizing.
SPOT_BUFFER = GO_SPOT_BUFFER
PERP_FUND_FRACTION = GO_PERP_FUND_FRACTION


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


def _atomic_write(path: str, body: str, mode: Optional[int] = None) -> None:
    """tmp + fsync + rename: a crash leaves the old or the new file, never
    a torn one; the mode is set BEFORE the rename (no 0644 window for keys)."""
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp-go")
    with open(tmp, "w") as f:
        f.write(body)
        f.flush()
        os.fsync(f.fileno())
    if p.exists():                       # keep owner (botuser) + mode
        st = p.stat()
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except (PermissionError, OSError):
            pass
        if mode is None:
            os.chmod(tmp, st.st_mode & 0o7777)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, p)


def _env_body(env: Dict[str, str]) -> str:
    return "".join(f"{k}={v}\n" for k, v in env.items())


def _write_env(path: str, env: Dict[str, str]) -> None:
    _atomic_write(path, _env_body(env), 0o600)


def _load_config(path: str) -> Dict[str, Any]:
    return json.loads(Path(path).read_text())


def _save_config(path: str, cfg: Dict[str, Any]) -> None:
    _atomic_write(path, json.dumps(cfg, indent=2) + "\n")


def _systemctl(*args: str) -> bool:
    """True on exit 0. Never raises (the caller decides on rollback)."""
    try:
        return subprocess.run(["systemctl", *args]).returncode == 0
    except Exception as e:
        print(f"systemctl {' '.join(args)} faalde: {e}")
        return False


def _read_health(path: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def _book_open(h: Optional[Dict[str, Any]]) -> bool:
    pos = (h or {}).get("simulated_position") or {}
    return (abs(float(pos.get("spot_qty") or 0.0)) > 1e-9
            or abs(float(pos.get("perp_qty") or 0.0)) > 1e-9)


def _wait_for_live(health_path: str, since: datetime, timeout: float,
                   poll: float) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Poll health.json for a cycle written AFTER `since`.
    → ("ok"|"halted"|"wrong_mode"|"timeout", last fresh health)."""
    deadline = time.time() + timeout
    last: Optional[Dict[str, Any]] = None
    while time.time() < deadline:
        time.sleep(poll)
        h = _read_health(health_path)
        if not h:
            continue
        try:
            ts = datetime.fromisoformat(h.get("last_cycle_ts") or "")
        except ValueError:
            continue
        if ts <= since:
            continue                      # stale health from the DRY run
        last = h
        if h.get("mode") != "P3_LIVE":
            return "wrong_mode", h
        if h.get("halted"):
            return "halted", h
        return "ok", h
    return "timeout", last


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
    return go_sizing(spot_free, perp_av, cap, frac)


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


def _validate_live_config(cfg: Dict[str, Any]) -> None:
    """Run the new config through the runner's own loader + mode gate (with
    HL_CONFIRM_LIVE as the unit will see it) BEFORE anything is written."""
    import carry_runner as cr
    known = set(cr.CarryRunnerConfig.__dataclass_fields__)
    rc = cr.CarryRunnerConfig(**{k: v for k, v in cfg.items()
                                 if k in known and not k.startswith("_")})
    mode = cr.resolve_mode(rc)          # raises on the P3 sizing guard
    if mode != cr.MODE_P3:
        raise RuntimeError(f"live config resolves to {mode}, not P3_LIVE")


def _archive_state(state_dir: str, stamp: str) -> Dict[str, str]:
    """Move state.json/health.json aside (→ *.dry-<stamp>) so the live runner
    starts from a clean book. Returns {original: archived} for rollback."""
    moved: Dict[str, str] = {}
    for name in ("state.json", "health.json"):
        src = Path(state_dir) / name
        if src.exists():
            dst = src.with_name(f"{name}.dry-{stamp}")
            os.replace(src, dst)
            moved[str(src)] = str(dst)
    return moved


def _rollback(a, cfg_text: str, env_text: Optional[str],
              moved: Dict[str, str]) -> None:
    print("ROLLBACK: config, env en state terugzetten, unit terug naar DRY_RUN")
    _atomic_write(a.config, cfg_text)
    if env_text is None:
        try:
            Path(a.env_file).unlink()
        except FileNotFoundError:
            pass
    else:
        _atomic_write(a.env_file, env_text, 0o600)
    for orig, archived in moved.items():
        live_written = Path(orig)
        if live_written.exists():          # whatever the failed live run wrote
            os.replace(live_written, live_written.with_name(
                live_written.name + ".failed-live"))
        os.replace(archived, orig)
    if not _systemctl("restart", UNIT):
        print(f"LET OP: herstart {UNIT} na rollback faalde — handmatig "
              f"`systemctl restart {UNIT}` en health controleren.")


def cmd_go(a) -> int:
    cfg, master, spot_free, perp_av, sizing = _preflight(a)
    n = sizing["per_leg_notional_usd"]
    if n < 50.0:
        sys.exit(f"per-leg notional ${n:.2f} < $50 — storting/splitsing niet "
                 "compleet? (spot moet ~55%, perp ~45% van de storting zijn)")
    new_cfg = dict(cfg)
    new_cfg["initial_notional_usd"] = sizing["initial_notional_usd"]
    new_cfg["live_max_usd"] = sizing["live_max_usd"]
    new_cfg["dry_run"] = False
    new_cfg["allow_live"] = True
    env = _read_env(a.env_file)
    new_env = dict(env)
    new_env["HL_CONFIRM_LIVE"] = "YES"

    # 1. Validate BEFORE writing anything.
    saved_confirm = os.environ.get("HL_CONFIRM_LIVE")
    try:
        os.environ["HL_CONFIRM_LIVE"] = "YES"
        _validate_live_config(new_cfg)
    except Exception as e:
        print(f"AFGEBROKEN vóór schrijven — live-config ongeldig: {e}")
        return 1
    finally:
        if saved_confirm is None:
            os.environ.pop("HL_CONFIRM_LIVE", None)
        else:
            os.environ["HL_CONFIRM_LIVE"] = saved_confirm

    # 2. Snapshot for rollback (in memory + on disk next to the originals).
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cfg_text = Path(a.config).read_text()
    env_text = Path(a.env_file).read_text() if Path(a.env_file).exists() else None
    _atomic_write(a.config + f".bak-go-{stamp}", cfg_text)
    if env_text is not None:
        _atomic_write(a.env_file + f".bak-go-{stamp}", env_text, 0o600)

    # 3. Stop the DRY runner, archive its (simulated) state, write live files.
    if not _systemctl("stop", UNIT):
        print(f"AFGEBROKEN: `systemctl stop {UNIT}` faalde — niets gewijzigd.")
        return 1
    moved = _archive_state(a.state_dir, stamp)
    try:
        _save_config(a.config, new_cfg)
        _write_env(a.env_file, new_env)
    except Exception as e:
        print(f"schrijven faalde: {e}")
        _rollback(a, cfg_text, env_text, moved)
        return 1
    print("OK  config live-geflipt + HL_CONFIRM_LIVE=YES; DRY-state "
          f"gearchiveerd ({', '.join(Path(v).name for v in moved.values()) or 'geen'})")

    # 4. Start + verify a FRESH P3_LIVE cycle; anything else → rollback.
    started = datetime.now(timezone.utc)
    if not _systemctl("start", UNIT):
        print(f"`systemctl start {UNIT}` faalde")
        _rollback(a, cfg_text, env_text, moved)
        return 1
    print(f"OK  {UNIT} gestart — wachten op een verse P3_LIVE-cycle…")
    verdict, h = _wait_for_live(a.health, started, a.verify_timeout,
                                VERIFY_POLL_SEC)
    if verdict == "ok":
        print(json.dumps(h, indent=2)[:800])
        print("LIVE — controleer de eerste open in trades.log; "
              "green-button bepaalt de rest.")
        return 0
    if _book_open(h):
        # A live position may exist: flipping to DRY would orphan it. Halt so
        # the live runner unwinds, and hand over to the operator.
        Path(a.state_dir, "halt").touch()
        print(f"VERIFICATIE FAALDE ({verdict}) MET OPEN BOEK — halt-sentinel "
              f"gezet in {a.state_dir}; de live runner unwindt. GEEN rollback "
              f"naar DRY. Controleer `journalctl -u {UNIT} -n 80` en de "
              "venue, daarna `park`.")
        return 2
    print(f"VERIFICATIE FAALDE ({verdict}) — "
          f"{json.dumps(h)[:400] if h else 'geen verse health'}")
    _rollback(a, cfg_text, env_text, moved)
    return 1


def cmd_park(a) -> int:
    h = _read_health(a.health)
    if _book_open(h) and (h or {}).get("mode") != "DRY_RUN" and not a.force:
        print("WEIGERING: health toont een open LIVE boek. Zet eerst de "
              f"halt-sentinel (`touch {a.state_dir}/halt`) en wacht tot de "
              "runner geunwind heeft (simulated_position = 0), of gebruik "
              "--force als je weet wat je doet.")
        return 1
    cfg = _load_config(a.config)
    cfg["dry_run"] = True
    cfg["allow_live"] = False
    _save_config(a.config, cfg)
    env = _read_env(a.env_file)
    env.pop("HL_CONFIRM_LIVE", None)
    _write_env(a.env_file, env)
    if (h or {}).get("mode") not in (None, "DRY_RUN"):
        # Live state (state.dry_run=false) would trip reconcile C4 forever in
        # DRY; archive it so the DRY simulation starts clean.
        _systemctl("stop", UNIT)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        moved = _archive_state(a.state_dir, stamp)
        for v in moved.values():
            print(f"OK  live-state gearchiveerd: {v}")
    if not _systemctl("restart", UNIT):
        print(f"LET OP: `systemctl restart {UNIT}` faalde")
        return 1
    print("OK  teruggeparkeerd naar DRY_RUN.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=CONFIG_DEFAULT)
    p.add_argument("--env-file", default=ENV_DEFAULT)
    p.add_argument("--state-dir", default=STATE_DIR)
    p.add_argument("--health", default=HEALTH)
    p.add_argument("--verify-timeout", type=float, default=VERIFY_TIMEOUT_SEC)
    sub = p.add_subparsers(dest="cmd", required=True)
    ob = sub.add_parser("onboard")
    ob.add_argument("--master", required=True, help="dedicated wallet 0x…")
    sub.add_parser("check")
    g = sub.add_parser("go")
    g.add_argument("--live-max", type=float, default=None,
                   help="override live_max_usd (default: config)")
    pk = sub.add_parser("park")
    pk.add_argument("--force", action="store_true",
                    help="park even while health shows an open live book")
    a = p.parse_args(argv)
    return {"onboard": cmd_onboard, "check": cmd_check,
            "go": cmd_go, "park": cmd_park}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
