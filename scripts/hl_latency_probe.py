#!/usr/bin/env python3
"""Hyperliquid latency baseline probe (Phase 0 — measurement, no trading logic).

Quantifies, SEPARATELY, the two latencies the WS migration is meant to improve:

  * READ side  — market-data freshness. Streams the WS feed and runs a parallel
                 REST poller, so we can show concretely how stale a REST-only
                 view is between polls (the "blind window") versus the live tick
                 cadence on WebSocket. Fully public, read-only, safe on mainnet.

  * WRITE side — order-to-ack round-trip (sign → POST /exchange → response). This
                 is HL's own inclusion floor (~200ms+); the WS migration does NOT
                 lower it, so we measure it once as a fixed reference. TESTNET
                 ONLY — hard-gated, never touches mainnet money.

Both reuse the instrumented `HLAdapter` (per-call timings land in its latency
ring) so the numbers here are the exact baseline the live runner will report.

Usage:
    # Read-side baseline (run for hours; mainnet data is fine, it's read-only):
    python -m scripts.hl_latency_probe --mode read --network mainnet \
        --coin BTC --duration-sec 3600 --rest-interval-sec 180

    # Write-side baseline (TESTNET ONLY; uses HL_PRIVATE_KEY/HL_ACCOUNT_ADDRESS
    # if set, else an ephemeral wallet that still round-trips the POST):
    python -m scripts.hl_latency_probe --mode write --network testnet \
        --coin BTC --n-orders 10 --usd 12
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import certifi  # noqa: E402
import eth_account  # noqa: E402
import websocket  # noqa: E402  (websocket-client)
from hyperliquid.utils import constants  # noqa: E402

from hl_adapter import HLAdapter  # noqa: E402

REPO_ROOT = HERE.parent
OUT_DIR = REPO_ROOT / "state" / "hl_xsectional" / "_latency"


def _base_url(network: str) -> str:
    return constants.TESTNET_API_URL if network == "testnet" else constants.MAINNET_API_URL


def _pct(xs: List[float], p: float) -> float:
    """Nearest-rank percentile (no numpy dep here — keep the probe self-light)."""
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def _open_jsonl(mode: str) -> tuple:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = OUT_DIR / f"probe-{mode}-{stamp}.jsonl"
    return path, path.open("w", buffering=1)      # line-buffered: durable for hours-long soaks


# ---------------------------------------------------------------- read side
class _WsState:
    """Shared, lock-guarded WS observation state for one reference coin.
    The SDK delivers messages on its own thread; the REST poller reads this
    snapshot from the main thread, so every field is touched under `lock`."""

    def __init__(self, coin: str):
        self.coin = coin
        self.lock = threading.Lock()
        self.last_mid: Optional[float] = None
        self.last_change_t: Optional[float] = None
        self.mid_gaps_ms: List[float] = []       # inter-CHANGE gaps for the coin
        self.allmids_msgs = 0                    # raw allMids messages seen
        self.bbo_ticks = 0                       # raw bbo (top-of-book) updates
        # excursion window since the last REST poll (the "blind window"):
        self.win_min: Optional[float] = None
        self.win_max: Optional[float] = None

    def on_allmids(self, msg: dict) -> Optional[dict]:
        try:
            data = msg.get("data") or {}
            mids = data.get("mids") if isinstance(data, dict) else None
            if not isinstance(mids, dict):
                return None
            raw = mids.get(self.coin)
            if raw is None:
                return None
            mid = float(raw)
        except (TypeError, ValueError):
            return None
        now = time.time()
        with self.lock:
            self.allmids_msgs += 1
            self.win_min = mid if self.win_min is None else min(self.win_min, mid)
            self.win_max = mid if self.win_max is None else max(self.win_max, mid)
            changed = self.last_mid is None or mid != self.last_mid
            if changed and self.last_change_t is not None:
                self.mid_gaps_ms.append((now - self.last_change_t) * 1000.0)
            if changed:
                self.last_change_t = now
            self.last_mid = mid
        return {"t": now, "ev": "ws_allmids", "coin": self.coin, "mid": mid, "changed": changed}

    def on_bbo(self, msg: dict) -> Optional[dict]:
        try:
            bbo = (msg.get("data") or {}).get("bbo")
            bid = float(bbo[0]["px"]); ask = float(bbo[1]["px"])
            mid = (bid + ask) / 2.0
        except (TypeError, ValueError, KeyError, IndexError):
            with self.lock:
                self.bbo_ticks += 1
            return None
        # bbo is the actionable top-of-book tick — feed its mid into the
        # excursion window and last_mid so the blind-window metric tracks the
        # finest price movement, not the throttled allMids aggregate.
        with self.lock:
            self.bbo_ticks += 1
            self.win_min = mid if self.win_min is None else min(self.win_min, mid)
            self.win_max = mid if self.win_max is None else max(self.win_max, mid)
            self.last_mid = mid
        return {"t": time.time(), "ev": "ws_bbo", "coin": self.coin,
                "bid": bid, "ask": ask, "mid": mid}

    def take_window(self) -> tuple:
        """Atomically read & reset the since-last-poll excursion window."""
        with self.lock:
            lo, hi, mid = self.win_min, self.win_max, self.last_mid
            self.win_min = self.last_mid
            self.win_max = self.last_mid
        return lo, hi, mid


class _WsReader(threading.Thread):
    """Minimal robust WS reader (websocket-client + certifi CA + auto-reconnect)
    used by the read probe. Deliberately mirrors the Phase-1 feed contract:
    explicit TLS trust, app-level ping, reconnect-with-backoff, and a VISIBLE
    connection state — unlike the SDK's WebsocketManager, which silently
    swallows connect failures (the exact reason Phase 1 replaces it)."""

    def __init__(self, ws_url: str, subscriptions: List[dict], on_message):
        super().__init__(daemon=True)
        self.ws_url = ws_url
        self.subscriptions = subscriptions
        self.on_message = on_message
        self.ws = None
        self._stop = False
        self.connected = False
        self.reconnects = 0

    def _on_open(self, ws):
        self.connected = True
        for sub in self.subscriptions:
            ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

    def _on_close(self, *_a):
        self.connected = False

    def run(self):
        first = True
        while not self._stop:
            if not first:
                self.reconnects += 1
                time.sleep(1.0)              # backoff before reconnect
            first = False
            try:
                self.ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=lambda _ws, m: self.on_message(m),
                    on_close=self._on_close,
                )
                self.ws.run_forever(sslopt={"ca_certs": certifi.where()},
                                    ping_interval=30, ping_timeout=10)
            except Exception:
                self.connected = False

    def stop(self):
        self._stop = True
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass


def run_read(args) -> int:
    coin = args.coin
    ws = _WsState(coin)
    path, fh = _open_jsonl("read")
    write_lock = threading.Lock()

    def emit(rec: Optional[dict]) -> None:
        if rec is None:
            return
        with write_lock:
            fh.write(json.dumps(rec) + "\n")

    def on_ws_message(raw):
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        ch = msg.get("channel")
        if ch == "allMids":
            emit(ws.on_allmids(msg))
        elif ch == "bbo":
            emit(ws.on_bbo(msg))

    base = _base_url(args.network)
    ws_url = "ws" + base[len("http"):] + "/ws"
    reader = _WsReader(ws_url, [{"type": "allMids"}, {"type": "bbo", "coin": coin}], on_ws_message)
    print(f"[read] WS connecting → {ws_url}  coin={coin}  out={path}")
    reader.start()
    for _ in range(20):                          # give the WS up to ~5s to connect
        if reader.connected:
            break
        time.sleep(0.25)

    # REST poller uses the instrumented adapter (records read RTT in its ring).
    adapter = HLAdapter(network=args.network)
    t_end = time.time() + args.duration_sec
    poll_n = 0
    blind_bps: List[float] = []                  # max excursion seen between polls
    print(f"[read] polling REST every {args.rest_interval_sec}s for {args.duration_sec}s "
          f"(prod cadence is {args.prod_poll_sec}s) …")
    ws_connected = False
    try:
        next_poll = time.time()
        while time.time() < t_end:
            time.sleep(0.25)
            if time.time() < next_poll:
                continue
            next_poll += args.rest_interval_sec
            rest_mid = adapter.all_mids().get(coin)
            lo, hi, ws_mid = ws.take_window()
            poll_n += 1
            excursion = ((hi - lo) / ws_mid * 1e4) if (lo and hi and ws_mid) else 0.0
            blind_bps.append(excursion)
            emit({"t": time.time(), "ev": "rest_poll", "coin": coin,
                  "rest_mid": rest_mid, "ws_mid": ws_mid,
                  "blind_window_bps": round(excursion, 3)})
            print(f"  poll {poll_n:>3}: rest={rest_mid} ws={ws_mid} "
                  f"blind_window={excursion:.1f}bps", flush=True)
    except KeyboardInterrupt:
        print("\n[read] interrupted — summarising what we have …")
    finally:
        ws_connected = reader.connected      # capture BEFORE stop() flips it
        reader.stop()
        fh.close()

    dur = max(1e-9, args.duration_sec)
    rest = adapter.latency_summary().get("read", {})
    gaps = list(ws.mid_gaps_ms)
    summary = {
        "mode": "read", "network": args.network, "coin": coin,
        "duration_sec": args.duration_sec, "out": str(path),
        "ws": {
            "connected_at_end": ws_connected,
            "reconnects": reader.reconnects,
            "allmids_msgs": ws.allmids_msgs,
            "bbo_ticks": ws.bbo_ticks,
            "mid_changes": len(gaps),
            "mid_updates_per_sec": round(len(gaps) / dur, 3),
            "bbo_ticks_per_sec": round(ws.bbo_ticks / dur, 3),
            "mid_gap_ms_p50": round(_pct(gaps, 50), 1) if gaps else None,
            "mid_gap_ms_p95": round(_pct(gaps, 95), 1) if gaps else None,
            "mid_gap_ms_max": round(max(gaps), 1) if gaps else None,
        },
        "rest": {
            "polls": poll_n,
            "interval_sec": args.rest_interval_sec,
            "rtt_ms": rest,
            "blind_window_bps_p50": round(_pct(blind_bps, 50), 2) if blind_bps else None,
            "blind_window_bps_p95": round(_pct(blind_bps, 95), 2) if blind_bps else None,
            "blind_window_bps_max": round(max(blind_bps), 2) if blind_bps else None,
        },
    }
    # Headline: how many more price updates WS delivers than the production
    # poll cadence — based on the actionable bbo (top-of-book) tick rate, with
    # the throttled allMids rate as fallback. 7.5 ticks/s vs 1 poll/180s ≈ 1350x.
    ws_rate = summary["ws"]["bbo_ticks_per_sec"] or summary["ws"]["mid_updates_per_sec"]
    if ws_rate:
        summary["freshness_gain_x"] = round(ws_rate * args.prod_poll_sec, 1)
    _print_summary(summary)
    (OUT_DIR / "last-read-summary.json").write_text(json.dumps(summary, indent=2))
    return 0


# --------------------------------------------------------------- write side
def run_write(args) -> int:
    if args.network != "testnet":
        print("REFUSED: write probe is TESTNET ONLY (it places real signed orders). "
              "Re-run with --network testnet.", file=sys.stderr)
        return 2

    pk = os.environ.get("HL_PRIVATE_KEY")
    addr = os.environ.get("HL_ACCOUNT_ADDRESS")
    ephemeral = pk is None
    if ephemeral:
        acct = eth_account.Account.create()      # unfunded; POST still round-trips
        pk, addr = acct.key.hex(), acct.address
        print("[write] no HL_PRIVATE_KEY — using an EPHEMERAL wallet. Orders will be "
              "rejected on margin, but the POST /exchange round-trip is still measured.")

    adapter = HLAdapter(network="testnet", private_key=pk, account_address=addr)
    path, fh = _open_jsonl("write")
    coin, usd = args.coin, args.usd
    print(f"[write] mode={adapter.mode}  coin={coin}  n={args.n_orders}  usd=${usd}  out={path}")

    filled = 0
    try:
        for i in range(args.n_orders):
            seed = f"probe|{i}|{coin}|open"
            r = adapter.market_order_usd(coin, True, usd, cloid=adapter.make_cloid(seed))
            fh.write(json.dumps({"t": time.time(), "ev": "order", "i": i,
                                 "ok": r.ok, "filled_sz": r.filled_sz, "err": r.error}) + "\n")
            if r.ok and r.filled_sz > 0:
                filled += 1
                adapter.close(coin, cloid=adapter.make_cloid(f"probe|{i}|{coin}|close"))
            time.sleep(args.gap_sec)
    except KeyboardInterrupt:
        print("\n[write] interrupted …")
    finally:
        # Best-effort: never leave a probe position open.
        try:
            if not ephemeral:
                adapter.close(coin)
        except Exception:
            pass
        fh.close()

    summary = {
        "mode": "write", "network": "testnet", "coin": coin,
        "orders": args.n_orders, "filled": filled, "ephemeral_wallet": ephemeral,
        "write_rtt_ms": adapter.latency_summary().get("write", {}),
        "out": str(path),
    }
    _print_summary(summary)
    (OUT_DIR / "last-write-summary.json").write_text(json.dumps(summary, indent=2))
    return 0


def _print_summary(summary: dict) -> None:
    print("\n" + "=" * 60)
    print(json.dumps(summary, indent=2))
    print("=" * 60)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["read", "write"], required=True)
    ap.add_argument("--network", choices=["testnet", "mainnet"], default="mainnet")
    ap.add_argument("--coin", default="BTC")
    # read
    ap.add_argument("--duration-sec", type=int, default=600)
    ap.add_argument("--rest-interval-sec", type=float, default=180.0,
                    help="REST poll cadence to sample (default mirrors prod safety loop)")
    ap.add_argument("--prod-poll-sec", type=float, default=180.0,
                    help="production poll cadence used for the freshness-gain headline")
    # write
    ap.add_argument("--n-orders", type=int, default=10)
    ap.add_argument("--usd", type=float, default=12.0)
    ap.add_argument("--gap-sec", type=float, default=2.0)
    args = ap.parse_args()

    if args.mode == "read":
        return run_read(args)
    return run_write(args)


if __name__ == "__main__":
    sys.exit(main())
