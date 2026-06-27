#!/usr/bin/env python3
"""Robust Hyperliquid WebSocket feed → live mid cache + EventBus (Phase 1).

The read-side replacement for the 180s REST poll. A `websocket-client`
WebSocketApp runs in a daemon thread with:

  * explicit certifi TLS trust (the system CA store is not always configured —
    and the SDK's WebsocketManager SILENTLY swallowed that failure, delivering
    zero messages with no signal: the exact reason we don't use it);
  * app-level ping + auto-reconnect with capped backoff;
  * a snapshot RESYNC on every (re)connect — seeds the mid cache from a REST
    `all_mids()` so a consumer never trades on a post-reconnect gap;
  * VISIBLE state: `connected`, `last_msg_age()`, `reconnects`.

The mid cache is updated synchronously in the WS thread under a lock, so any
thread (incl. the runner's executor cycle) can read a consistent snapshot. Bus
publishing (for Phase-2 async consumers) is marshalled onto the asyncio loop via
`call_soon_threadsafe`, keeping the single-threaded InProcAsyncBus single-
threaded. We chose `websocket-client` (already a prod dependency) over the
asyncio `websockets` lib to avoid adding a dependency to the live bot; the
thread→loop hop is microseconds, invisible against HL's ~200ms order floor.

Self-test (streams mainnet for a few seconds, prints cache + stats):
    python -m scripts.hl_ws_feed --selftest --coins BTC,ETH,SOL
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import certifi  # noqa: E402
import websocket  # noqa: E402  (websocket-client)

try:                                              # bus is optional (Phase 2 wiring)
    from event_bus import MarketEvent, FillEvent
except Exception:                                 # pragma: no cover
    MarketEvent = None
    FillEvent = None


class HLWsFeed:
    """Streams allMids + per-coin bbo for `coins`, maintaining a thread-safe mid
    cache and (optionally) publishing MarketEvents to a bus on the asyncio loop."""

    def __init__(self, base_url: str, coins: List[str], *, bus=None, loop=None,
                 resync_mids: Optional[Callable[[], Dict[str, float]]] = None,
                 user_address: Optional[str] = None,
                 ping_interval: int = 30, ping_timeout: int = 10,
                 max_backoff: float = 30.0):
        self.ws_url = "ws" + base_url[len("http"):] + "/ws"
        self.coins = list(coins)
        self._coinset = set(self.coins)
        self.bus = bus
        self.loop = loop
        self.resync_mids = resync_mids
        # Subscribe the account fill stream too when an address is known (live /
        # testnet wallet) → event-time reconcile instead of polling positions().
        self.user_address = user_address.lower() if user_address else None
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.max_backoff = max_backoff

        self._lock = threading.Lock()
        self._mids: Dict[str, float] = {}
        self._last_msg_ts: float = 0.0
        self.connected = False
        self.reconnects = 0
        self.resyncs = 0
        self.msgs = 0
        self.fills_seen = 0
        self.last_fill_ts: float = 0.0
        self._fills_primed = False                # first userFills msg is a snapshot
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self.ws: Optional[websocket.WebSocketApp] = None

    # -------------------------------------------------------------- lifecycle
    def start(self) -> "HLWsFeed":
        self._thread = threading.Thread(target=self._run, name="hl-ws-feed", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        backoff = 1.0
        first = True
        while not self._stop:
            if not first:
                self.reconnects += 1
                time.sleep(min(backoff, self.max_backoff))
                backoff = min(backoff * 2, self.max_backoff)
            first = False
            try:
                self.ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_close=self._on_close,
                    on_error=self._on_error,
                )
                self.ws.run_forever(sslopt={"ca_certs": certifi.where()},
                                    ping_interval=self.ping_interval,
                                    ping_timeout=self.ping_timeout)
                backoff = 1.0                     # clean exit → reset backoff
            except Exception:
                self.connected = False

    def stop(self) -> None:
        self._stop = True
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass

    # ----------------------------------------------------------- ws callbacks
    def _on_open(self, ws) -> None:
        # Resync FIRST so the cache is seeded before any tick is processed — a
        # reconnect must never expose a stale/empty book to the strategy.
        if self.resync_mids is not None:
            try:
                snap = self.resync_mids() or {}
                with self._lock:
                    for c in self.coins:
                        if c in snap:
                            self._mids[c] = float(snap[c])
                    self._last_msg_ts = time.time()
                self.resyncs += 1
            except Exception:
                pass
        self.connected = True
        ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
        for c in self.coins:
            ws.send(json.dumps({"method": "subscribe",
                                "subscription": {"type": "bbo", "coin": c}}))
        if self.user_address:
            self._fills_primed = False            # next userFills is the resync snapshot
            ws.send(json.dumps({"method": "subscribe", "subscription": {
                "type": "userFills", "user": self.user_address}}))

    def _on_close(self, *_a) -> None:
        self.connected = False

    def _on_error(self, *_a) -> None:
        self.connected = False

    def _on_message(self, _ws, raw) -> None:
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return
        ch = msg.get("channel")
        if ch == "allMids":
            self._handle_allmids(msg)
        elif ch == "bbo":
            self._handle_bbo(msg)
        elif ch == "userFills":
            self._handle_user_fills(msg)

    def _handle_allmids(self, msg: dict) -> None:
        data = msg.get("data") or {}
        mids = data.get("mids") if isinstance(data, dict) else None
        if not isinstance(mids, dict):
            return
        now = time.time()
        updates = []
        with self._lock:
            self.msgs += 1
            self._last_msg_ts = now
            for c in self.coins:
                raw = mids.get(c)
                if raw is None:
                    continue
                try:
                    px = float(raw)
                except (TypeError, ValueError):
                    continue
                self._mids[c] = px
                updates.append((c, px))
        for c, px in updates:
            self._publish(MarketEvent(ts=now, coin=c, mid=px, source="allMids")
                          if MarketEvent else None)

    def _handle_bbo(self, msg: dict) -> None:
        data = msg.get("data") or {}
        coin = data.get("coin")
        if coin not in self._coinset:
            return
        try:
            bbo = data.get("bbo")
            bid = float(bbo[0]["px"]); ask = float(bbo[1]["px"])
        except (TypeError, ValueError, KeyError, IndexError):
            return
        mid = (bid + ask) / 2.0
        now = time.time()
        with self._lock:
            self.msgs += 1
            self._last_msg_ts = now
            self._mids[coin] = mid
        self._publish(MarketEvent(ts=now, coin=coin, mid=mid, bid=bid, ask=ask, source="bbo")
                      if MarketEvent else None)

    def _handle_user_fills(self, msg: dict) -> None:
        data = msg.get("data") or {}
        fills = data.get("fills")
        if not isinstance(fills, list):
            return
        # The first userFills after (re)subscribe is a HISTORICAL snapshot — prime
        # on it but never replay old fills as live reconcile signals.
        if bool(data.get("isSnapshot")) and not self._fills_primed:
            self._fills_primed = True
            return
        self._fills_primed = True
        now = time.time()
        for f in fills:
            try:
                coin = f["coin"]; px = float(f["px"]); sz = float(f["sz"])
            except (KeyError, TypeError, ValueError):
                continue
            side = f.get("side", "")
            signed = sz if side == "B" else -sz       # B=buy(+), A=sell(-)
            with self._lock:
                self.fills_seen += 1
                self.last_fill_ts = now
            if FillEvent is not None:
                self._publish(FillEvent(ts=now, coin=coin, sz=signed, px=px,
                                        cloid=f.get("cloid"), side=side))

    def _publish(self, event) -> None:
        if event is None or self.bus is None:
            return
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.bus.publish, event)
        else:                                     # no loop → publish inline (tests)
            self.bus.publish(event)

    # --------------------------------------------------------------- readers
    def snapshot(self) -> Dict[str, float]:
        """A consistent copy of the current mid cache (thread-safe)."""
        with self._lock:
            return dict(self._mids)

    def mid(self, coin: str) -> Optional[float]:
        with self._lock:
            return self._mids.get(coin)

    def last_msg_age(self) -> float:
        """Seconds since the last WS message (inf if none yet) — the freshness
        gate a consumer checks before trusting the cache over a REST fallback."""
        with self._lock:
            ts = self._last_msg_ts
        return math.inf if ts <= 0 else time.time() - ts

    def last_fill_age(self) -> float:
        with self._lock:
            ts = self.last_fill_ts
        return math.inf if ts <= 0 else time.time() - ts

    def stats(self) -> dict:
        return {"connected": self.connected, "reconnects": self.reconnects,
                "resyncs": self.resyncs, "msgs": self.msgs,
                "fills_seen": self.fills_seen,
                "coins_cached": len(self.snapshot()),
                "last_msg_age_sec": round(self.last_msg_age(), 2)}


# ------------------------------------------------------------------ selftest
def _selftest(coins: List[str], secs: int) -> int:
    from hyperliquid.utils import constants
    from hl_adapter import HLAdapter

    adapter = HLAdapter(network="mainnet")        # read-only; for resync snapshot
    feed = HLWsFeed(constants.MAINNET_API_URL, coins, resync_mids=adapter.all_mids).start()
    print(f"[ws_feed selftest] streaming {coins} for {secs}s …")
    for _ in range(secs):
        time.sleep(1)
    feed.stop()
    print(f"  stats: {json.dumps(feed.stats())}")
    print(f"  cache: {json.dumps(feed.snapshot())}")
    ok = feed.msgs > 0 and feed.last_msg_age() < 5 and len(feed.snapshot()) == len(coins)
    print("  ws_feed self-test", "PASSED ✓" if ok else "FAILED ✗")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--coins", default="BTC,ETH,SOL")
    ap.add_argument("--secs", type=int, default=8)
    args = ap.parse_args()
    if args.selftest:
        return _selftest([c.strip() for c in args.coins.split(",") if c.strip()], args.secs)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
