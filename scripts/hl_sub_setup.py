#!/usr/bin/env python3
"""Hyperliquid sub-account setup/ops CLI for the carry-hl lane.

One-time (and occasional) operator actions that the carry runner itself must
NOT perform: creating the isolating sub-account, moving USDC between master
and sub, and verifying — on TESTNET first — which of those actions the AGENT
key may sign at all (HL-CARRY-STUDY open question: user-signed
`usdClassTransfer` inside a sub vs the L1 `subAccount*Transfer` pair).

All actions are signed with the agent key from env (HL_CARRY_PRIVATE_KEY or
HL_PRIVATE_KEY) against the master account (HL_CARRY_ACCOUNT_ADDRESS or
HL_ACCOUNT_ADDRESS). Addresses are printed, key material never.

Usage (run on the LXC where the env files live):
    python3 -m scripts.hl_sub_setup --env-file /etc/trading-bot/carry-hl-btc.env \
        [--network mainnet|testnet] <command> [...]

Commands:
    list                              query_sub_accounts of the master
    create --name carry               create a sub-account (idempotent-ish:
                                      fails venue-side if the name exists)
    status --sub 0x…                  perp + spot balances of the sub
    fund --sub 0x… [--spot-usd X] [--perp-usd Y]
                                      master → sub USDC (spot leg / perp leg;
                                      L1-signed subAccount*Transfer pair)
    defund --sub 0x… [--spot-usd X] [--perp-usd Y]
                                      sub → master (cleanup / wind-down)
    class-transfer --sub 0x… --usd X --to-perp|--to-spot
                                      usdClassTransfer INSIDE the sub (the
                                      agent-signability open question)
    verify --sub-name carry-verify    TESTNET-only end-to-end capability probe:
                                      create → fund spot+perp → class-transfer
                                      → defund; prints PASS/FAIL per action
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, Optional

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants


def _load_env_file(path: Optional[str]) -> None:
    """Set KEY=VAL lines from a systemd-style env file into os.environ
    (existing environment wins, so ad-hoc overrides stay possible)."""
    if not path:
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _base_url(network: str) -> str:
    return (constants.TESTNET_API_URL if network == "testnet"
            else constants.MAINNET_API_URL)


class SubOps:
    def __init__(self, network: str) -> None:
        key = (os.environ.get("HL_CARRY_PRIVATE_KEY")
               or os.environ.get("HL_PRIVATE_KEY") or "")
        master = (os.environ.get("HL_CARRY_ACCOUNT_ADDRESS")
                  or os.environ.get("HL_ACCOUNT_ADDRESS") or "")
        if not key or not master:
            sys.exit("need HL(_CARRY)_PRIVATE_KEY and HL(_CARRY)_ACCOUNT_ADDRESS "
                     "in env (use --env-file)")
        self.network = network
        self.master = master
        self.wallet = Account.from_key(key)
        self.url = _base_url(network)
        self.info = Info(self.url, skip_ws=True)
        # Master-scoped L1 actions (createSubAccount, subAccount*Transfer).
        self.ex = Exchange(self.wallet, self.url, account_address=master)
        print(f"network={network} master={master} agent={self.wallet.address}")

    # ------------------------------------------------------------- helpers

    def sub_exchange(self, sub: str) -> Exchange:
        """Exchange whose actions run INSIDE the sub (agent-trades-sub
        pattern; the SDK routes usdClassTransfer via 'subaccount:' tagging
        when vault_address is set)."""
        return Exchange(self.wallet, self.url,
                        vault_address=sub, account_address=self.master)

    def usdc_token_str(self) -> str:
        """'USDC:0x<tokenId>' — subAccountSpotTransfer wants the full token
        identifier; resolve it from spot meta instead of hard-coding."""
        for t in (self.info.spot_meta() or {}).get("tokens", []):
            if t.get("name") == "USDC":
                return f"USDC:{t['tokenId']}"
        sys.exit("USDC token not found in spot meta")

    @staticmethod
    def _report(label: str, resp: Any) -> bool:
        ok = isinstance(resp, dict) and resp.get("status") == "ok"
        print(f"{'PASS' if ok else 'FAIL'}  {label}: {json.dumps(resp)[:300]}")
        return ok

    # ------------------------------------------------------------ commands

    def list(self) -> None:
        subs = self.info.query_sub_accounts(self.master) or []
        if not subs:
            print("(no sub-accounts)")
        for s in subs:
            av = ((s.get("clearinghouseState") or {}).get("marginSummary")
                  or {}).get("accountValue")
            spot = {b["coin"]: b["total"]
                    for b in (s.get("spotState") or {}).get("balances", [])
                    if float(b.get("total", 0)) > 0}
            print(f"{s.get('name')}  {s.get('subAccountUser')}  "
                  f"perpAV=${av}  spot={spot}")

    def create(self, name: str) -> bool:
        resp = self.ex.create_sub_account(name)
        ok = self._report(f"create_sub_account({name})", resp)
        if ok:
            time.sleep(1.0)
            self.list()
        return ok

    def status(self, sub: str) -> None:
        ch = self.info.user_state(sub)
        av = (ch.get("marginSummary") or {}).get("accountValue")
        sp = self.info.spot_user_state(sub)
        bals = {b["coin"]: b["total"] for b in sp.get("balances", [])
                if float(b.get("total", 0)) > 0}
        print(f"sub={sub} perpAV=${av} spot={bals}")

    def fund(self, sub: str, spot_usd: float, perp_usd: float,
             *, deposit: bool = True) -> bool:
        """Master↔sub USDC. Perp leg = subAccountTransfer (MICRO-dollar int);
        spot leg = subAccountSpotTransfer (float, full token id)."""
        ok = True
        if spot_usd > 0:
            r = self.ex.sub_account_spot_transfer(
                sub, deposit, self.usdc_token_str(), float(spot_usd))
            ok &= self._report(
                f"sub_account_spot_transfer({'in' if deposit else 'out'} "
                f"${spot_usd})", r)
        if perp_usd > 0:
            r = self.ex.sub_account_transfer(
                sub, deposit, int(round(float(perp_usd) * 1_000_000)))
            ok &= self._report(
                f"sub_account_transfer({'in' if deposit else 'out'} "
                f"${perp_usd})", r)
        return ok

    def class_transfer(self, sub: str, usd: float, to_perp: bool) -> bool:
        r = self.sub_exchange(sub).usd_class_transfer(float(usd), to_perp)
        return self._report(
            f"usd_class_transfer(sub, ${usd}, to_perp={to_perp})", r)

    def verify(self, sub_name: str) -> None:
        """TESTNET capability probe with tiny amounts; answers the study's
        open question empirically and leaves the account as it found it."""
        if self.network != "testnet":
            sys.exit("verify is testnet-only (use --network testnet)")
        results: Dict[str, bool] = {}

        subs = {s.get("name"): s.get("subAccountUser")
                for s in (self.info.query_sub_accounts(self.master) or [])}
        sub = subs.get(sub_name)
        if sub is None:
            results["create_sub_account"] = self.create(sub_name)
            subs = {s.get("name"): s.get("subAccountUser")
                    for s in (self.info.query_sub_accounts(self.master) or [])}
            sub = subs.get(sub_name)
            if not sub:
                print("VERDICT: create_sub_account failed — cannot continue")
                return
        else:
            print(f"(sub '{sub_name}' already exists: {sub})")
            results["create_sub_account"] = True

        results["spot_transfer_in"] = self.fund(sub, 5.0, 0.0)
        results["perp_transfer_in"] = self.fund(sub, 0.0, 5.0)
        time.sleep(1.0)
        results["class_transfer_in_sub"] = self.class_transfer(sub, 1.0, True)
        time.sleep(1.0)
        self.status(sub)
        # Wind back what we can so the probe is ~side-effect-free.
        if results.get("class_transfer_in_sub"):
            self.class_transfer(sub, 1.0, False)
        self.fund(sub, 5.0, 5.0, deposit=False)

        print("\n=== VERDICT (agent-key capabilities on testnet) ===")
        for k, v in results.items():
            print(f"  {k}: {'PASS' if v else 'FAIL'}")
        if results.get("class_transfer_in_sub"):
            print("→ funding path A available: spot→sub, class-transfer inside sub")
        if results.get("perp_transfer_in"):
            print("→ funding path B available: L1 subAccountTransfer pair (fallback)")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", help="systemd-style env file with the HL creds")
    p.add_argument("--network", default="mainnet",
                   choices=["mainnet", "testnet"])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    c = sub.add_parser("create"); c.add_argument("--name", required=True)
    s = sub.add_parser("status"); s.add_argument("--sub", required=True)
    for name in ("fund", "defund"):
        f = sub.add_parser(name)
        f.add_argument("--sub", required=True)
        f.add_argument("--spot-usd", type=float, default=0.0)
        f.add_argument("--perp-usd", type=float, default=0.0)
    ct = sub.add_parser("class-transfer")
    ct.add_argument("--sub", required=True)
    ct.add_argument("--usd", type=float, required=True)
    g = ct.add_mutually_exclusive_group(required=True)
    g.add_argument("--to-perp", action="store_true")
    g.add_argument("--to-spot", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("--sub-name", default="carry-verify")
    a = p.parse_args(argv)

    _load_env_file(a.env_file)
    ops = SubOps(a.network)
    if a.cmd == "list":
        ops.list()
    elif a.cmd == "create":
        ops.create(a.name)
    elif a.cmd == "status":
        ops.status(a.sub)
    elif a.cmd == "fund":
        ops.fund(a.sub, a.spot_usd, a.perp_usd, deposit=True)
    elif a.cmd == "defund":
        ops.fund(a.sub, a.spot_usd, a.perp_usd, deposit=False)
    elif a.cmd == "class-transfer":
        ops.class_transfer(a.sub, a.usd, a.to_perp)
    elif a.cmd == "verify":
        ops.verify(a.sub_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
