#!/usr/bin/env python3
"""Event-driven async runner for the Hyperliquid cross-sectional momentum lane
(Phase 1). Reuses the proven HLXSRunner money-path VERBATIM — run_once /
run_safety_once / _execute_live / reconcile / flatten_all / circuit breakers —
and adds three things, nothing more:

  1. Cycles run in a thread-pool executor, so a slow/blocking venue call can
     never freeze the event loop (and, with the adapter's REST timeouts, can
     never hang indefinitely either).
  2. A live WS mid cache (hl_ws_feed) transparently feeds the reused logic via
     an `adapter.all_mids` override — tens-of-ms-fresh ticks instead of a 180s
     REST poll — with an automatic REST fallback whenever the feed is stale/down.
  3. A DECOUPLED heartbeat task is the SINGLE writer of health.json: its `ts`
     reflects PROCESS liveness, not cycle completion. This is the structural fix
     for the 2026-06-20 watchdog-flatten race (blocking call hangs → health
     stale → watchdog flattens on recovery). The heartbeat keeps beating while a
     cycle is in flight, and exposes ws_connected / cycle_age / executor_inflight
     / maintenance so the (hardened) watchdog can act on real process death only.

Execution stays the sync path here; Phase 2 makes it event-driven. Same mode
gate, same testnet→mainnet discipline, same idempotent CLOIDs.

Run (MAINNET_DRY soak — mainnet data, NO orders):
    python -m scripts.hl_runner_async --config configs/hl-xsectional-main.json \
        --loop --interval-sec 3600 --max-runtime-sec 0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from hl_xs_runner import HLXSRunner, HLXSConfig, load_config, _utcnow  # noqa: E402
from hl_ws_feed import HLWsFeed  # noqa: E402
from event_bus import InProcAsyncBus, FillEvent  # noqa: E402
import hl_nonce  # noqa: E402  (monotonic nonce — makes concurrent order signing safe)


class AsyncHLXSRunner(HLXSRunner):
    def __init__(self, cfg: HLXSConfig):
        super().__init__(cfg)
        self._hb_sec = max(2, int(getattr(cfg, "heartbeat_sec", 15)))
        self._ws_max_age = float(getattr(cfg, "ws_max_age_sec", 5.0))
        self._latest_health = None
        self._last_cycle_ts = None
        self._maintenance = False
        self._executor_inflight = 0
        self._bus = InProcAsyncBus()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hl-cycle")
        self._leg_pool = ThreadPoolExecutor(max_workers=max(1, int(getattr(cfg, "exec_concurrency", 6))),
                                            thread_name_prefix="hl-leg")
        self._ws = None
        self._maint_path = self.dir / "MAINTENANCE"
        # Keep the ORIGINAL REST all_mids, then route the reused money-path through
        # the WS cache (REST fallback). Resync always uses the true REST snapshot.
        self._rest_all_mids = self.adapter.all_mids
        self.adapter.all_mids = self._ws_or_rest_mids

        # --- Phase 2: event-driven safety/reconcile + concurrent execution -----
        hl_nonce.install()                        # monotonic nonces → concurrent legs are safe
        self._wake = asyncio.Event()              # risk/fill events wake the safety cadence early
        self._risk_eval_sec = float(getattr(cfg, "risk_eval_sec", 1.0))
        self._risk_trigger_frac = float(getattr(cfg, "risk_trigger_frac", 0.6))
        self._risk_cooldown_sec = float(getattr(cfg, "risk_cooldown_sec", 5.0))
        self._exec_concurrency = max(1, int(getattr(cfg, "exec_concurrency", 6)))
        self._mtm_baseline = None
        self._last_risk_drop = 0.0
        self._last_risk_trigger = 0.0
        self._risk_triggers = 0
        self._fills_consumed = 0
        self._last_fill_seen = 0.0

    # -- WS-or-REST mid source (transparent to the reused logic) ------------
    def _ws_or_rest_mids(self):
        ws = self._ws
        if ws is not None and ws.last_msg_age() < self._ws_max_age:
            snap = ws.snapshot()
            if snap:
                return snap
        return self._rest_all_mids()              # feed stale/down → safe fallback

    # -- capture health instead of writing (heartbeat is the single writer) -
    def write_health(self, s, extra) -> None:
        self._latest_health = self._health_payload(s, extra)

    def _bootstrap_health(self) -> dict:
        return {"ts": _utcnow().isoformat(), "instance": self.cfg.instance_name,
                "mode": self.mode, "venue": "hyperliquid",
                "live_trading": self.live_trading, "cb_state": "normal",
                "equity": 0.0, "n_positions": 0, "booting": True}

    def _write_heartbeat(self) -> None:
        self._maintenance = self._maint_path.exists()
        payload = self._latest_health
        h = dict(payload) if payload else self._bootstrap_health()
        h["ts"] = _utcnow().isoformat()           # PROCESS liveness, not cycle completion
        ws = self._ws
        h["liveness"] = {
            "loop_alive": True,
            "heartbeat_sec": self._hb_sec,
            "ws_connected": bool(ws and ws.connected),
            "ws_last_msg_age_sec": round(ws.last_msg_age(), 1) if ws else None,
            "ws_reconnects": ws.reconnects if ws else 0,
            "ws_using_fallback": bool(ws and ws.last_msg_age() >= self._ws_max_age),
            "cycle_age_sec": (round(time.time() - self._last_cycle_ts, 1)
                              if self._last_cycle_ts else None),
            "executor_inflight": self._executor_inflight,
            "maintenance": self._maintenance,
            "risk_est_drop_pct": round(self._last_risk_drop * 100, 3),
            "risk_triggers": self._risk_triggers,
            "fills_consumed": self._fills_consumed,
            "ws_last_fill_age_sec": (round(ws.last_fill_age(), 1)
                                     if ws and ws.last_fill_ts > 0 else None),
        }
        tmp = self.health_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(h, indent=2))
        tmp.replace(self.health_path)

    # -- tasks --------------------------------------------------------------
    async def _heartbeat_task(self) -> None:
        while True:
            try:
                self._write_heartbeat()
            except Exception as e:
                print(f"[hl_runner_async] heartbeat error: {e}", file=sys.stderr)
            await asyncio.sleep(self._hb_sec)

    async def _exec(self, fn):
        """Run a blocking cycle in the executor; track in-flight count so the
        heartbeat can surface a stuck/slow cycle without ever blocking itself."""
        loop = asyncio.get_running_loop()
        self._executor_inflight += 1
        try:
            return await loop.run_in_executor(self._pool, fn)
        finally:
            self._executor_inflight -= 1

    # --- Phase 2: event-driven risk monitor + fill-driven reconcile --------
    def _cb_halted(self) -> bool:
        cb = (self._latest_health or {}).get("cb_state")
        return cb in ("halted", "op_halt", "catastrophe_halt")

    def _capture_mtm_baseline(self) -> None:
        """Snapshot the live book + the mids it was marked at + last settled
        equity, so the risk monitor can mark-to-market forward against live WS
        ticks between cycles. Runs in the executor (book read is a REST call).
        No-op in sim/DRY (no live book)."""
        if not self.live_trading:
            return
        try:
            book = self.adapter.book_notional()        # {coin: signed usd} at WS mids
        except Exception:
            return
        eq = (self._latest_health or {}).get("equity")
        mids = self._ws.snapshot() if self._ws else {}
        if eq and book:
            self._mtm_baseline = {"equity": float(eq), "book": book,
                                  "mids": dict(mids), "ts": time.time()}

    def _eval_risk(self) -> None:
        """Mark the baseline book to the LIVE WS mids (non-blocking, cache only)
        and, if the estimated intra-cycle drawdown crosses a fraction of the
        catastrophe-intracycle threshold, wake the safety cadence NOW. This only
        ACCELERATES a real safety cycle (which re-reads settled equity and runs
        the unchanged breaker) — it never flattens directly, so a marking error
        costs at most one extra safety read, never a wrong action."""
        base = self._mtm_baseline
        if not self.live_trading or not base or not base.get("book") or self._cb_halted():
            return
        eq0 = base.get("equity")
        if not eq0 or eq0 <= 0:
            return
        now_mids = self._ws.snapshot() if self._ws else {}
        if not now_mids:
            return
        pnl = 0.0
        for coin, notional in base["book"].items():
            m0 = base["mids"].get(coin); m1 = now_mids.get(coin)
            if m0 and m1 and m0 > 0:
                pnl += notional * (m1 / m0 - 1.0)
        drop = (eq0 - (eq0 + pnl)) / eq0
        self._last_risk_drop = drop
        if drop >= self._risk_trigger_frac * self.cfg.catastrophe_intracycle_pct:
            if (time.time() - self._last_risk_trigger) >= self._risk_cooldown_sec:
                self._last_risk_trigger = time.time()
                self._risk_triggers += 1
                try:
                    self.log({"action": "risk_event_wake", "est_drop_pct": round(drop * 100, 2),
                              "threshold_pct": round(self._risk_trigger_frac
                                                     * self.cfg.catastrophe_intracycle_pct * 100, 2)})
                except Exception:
                    pass
                self._wake.set()

    async def _risk_monitor_task(self) -> None:
        while True:
            await asyncio.sleep(self._risk_eval_sec)
            try:
                self._eval_risk()
            except Exception as e:
                print(f"[hl_runner_async] risk eval error: {e}", file=sys.stderr)

    async def _fill_consumer_task(self) -> None:
        """Consume FillEvents off the bus → a fill changed the book, so wake the
        safety cadence for a faster reconcile (REST book_notional stays the source
        of truth). Also drives fill observability in health."""
        sub = self._bus.subscribe()
        try:
            async for ev in sub:
                if isinstance(ev, FillEvent):
                    self._fills_consumed += 1
                    self._last_fill_seen = time.time()
                    self._wake.set()
        except asyncio.CancelledError:
            pass
        finally:
            sub.close()

    # --- Phase 2: nonce-safe concurrent execution -------------------------
    def _run_legs(self, fn, items):
        """Fire `fn` over items concurrently (bounded by exec_concurrency),
        preserving input order in the results. A single item runs inline (no
        pool overhead). Nonce-safe via hl_nonce — concurrent signing would
        otherwise collide on the millisecond nonce."""
        items = list(items)
        if not items:
            return []
        if len(items) == 1 or self._exec_concurrency <= 1:
            return [fn(i) for i in items]
        return list(self._leg_pool.map(fn, items))

    def _execute_live(self, targets, now):
        """Concurrent override of HLXSRunner._execute_live (Phase 2). Identical
        contract — close→open→verify→retry→flatten, deterministic CLOIDs,
        partial-fill-aware retry, never one-legged — but the CLOSE batch and the
        OPEN/RESIZE batch each fire their legs concurrently, with a BARRIER
        between them so margin is freed before new legs open. Cuts the wall-clock
        the basket is exposed mid-rebalance from ~N×RTT to ~1×RTT per batch."""
        orders = []
        cid = now.isoformat()
        inst = self.cfg.instance_name

        def cl(coin, action):
            return self.adapter.make_cloid(f"{inst}|{cid}|{coin}|{action}")

        try:
            cur = self.adapter.positions()
        except Exception as e:
            return {"action": "rebalance", "mode": self.mode, "execution": "live",
                    "complete": False, "longs": [], "shorts": [],
                    "orders": [{"act": "abort", "err": f"positions read: {e}"}]}

        # 1. CLOSE legs leaving the basket / flipping side — concurrently.
        close_coins = [coin for coin, p in cur.items()
                       if targets.get(coin, 0.0) == 0.0
                       or int(np.sign(targets.get(coin, 0.0))) != (1 if p["szi"] > 0 else -1)]

        def _do_close(coin):
            r = self.adapter.close(coin, cloid=cl(coin, "close"))
            return {"act": "close", "coin": coin, "ok": r.ok, "err": r.error}

        orders += self._run_legs(_do_close, close_coins)

        # BARRIER: closes are filled (margin freed) → establish / resize targets.
        try:
            held = self.adapter.positions()
        except Exception:
            held = cur
        mids = self.adapter.all_mids()

        def _do_target(coin):
            tgt = targets[coin]
            h = held.get(coin)
            if h and int(np.sign(h["szi"])) == int(np.sign(tgt)):
                mk = mids.get(coin) or h.get("entry_px") or 0.0
                cur_notional = h["szi"] * mk
                ro = self._resize_order(cur_notional, tgt, self.cfg.resize_threshold,
                                        self.adapter.MIN_ORDER_USD)
                if ro is None:
                    return None                  # drift within tolerance — leave it
                is_buy, usd = ro
                r = self.adapter.market_order_usd(coin, is_buy, usd, slippage=self.cfg.slippage,
                                                  cloid=cl(coin, "resize"))
                return {"act": "resize", "coin": coin, "side": int(np.sign(tgt)),
                        "delta": round(tgt - cur_notional, 2), "ok": r.ok, "err": r.error}
            r = self.adapter.market_order_usd(coin, tgt > 0, abs(tgt), slippage=self.cfg.slippage,
                                              cloid=cl(coin, "open"))
            return {"act": "open", "coin": coin, "side": int(np.sign(tgt)),
                    "ok": r.ok, "filled": r.filled_sz, "err": r.error}

        orders += [o for o in self._run_legs(_do_target, list(targets.keys())) if o is not None]

        # 3. verify realized book → bounded partial-fill-aware retry → FLATTEN if
        #    still wrong (never leave a one-legged / non-neutral book). UNCHANGED
        #    logic; retries within an attempt also fire concurrently.
        try:
            ok, missing, detail = self._verify_book(targets)
            attempt = 0
            while not ok and attempt < 2:
                attempt += 1
                book = self.adapter.book_notional()
                retry_items = []
                for coin in list(missing):
                    tgt = targets[coin]
                    curn = book.get(coin, 0.0)
                    remaining = (tgt - curn) if int(np.sign(curn)) == int(np.sign(tgt)) else tgt
                    if abs(remaining) >= self.adapter.MIN_ORDER_USD:
                        retry_items.append((coin, remaining))

                def _do_retry(item, _attempt=attempt):
                    coin, remaining = item
                    r = self.adapter.market_order_usd(coin, remaining > 0, abs(remaining),
                                                      slippage=self.cfg.slippage,
                                                      cloid=cl(coin, f"retry{_attempt}"))
                    return {"act": "retry", "coin": coin, "remaining": round(remaining, 2),
                            "ok": r.ok, "err": r.error}

                orders += self._run_legs(_do_retry, retry_items)
                ok, missing, detail = self._verify_book(targets)
        except Exception as e:
            ok, detail = False, f"verify error: {e}"
        if not ok:
            orders += self.flatten_all()
            orders.append({"act": "rebalance_failed_flattened", "detail": detail})
        longs = sorted(c for c, t in targets.items() if t > 0)
        shorts = sorted(c for c, t in targets.items() if t < 0)
        return {"action": "rebalance", "mode": self.mode, "execution": "live",
                "complete": bool(ok), "longs": longs, "shorts": shorts,
                "orders": orders, "book": detail}

    async def _cycle_task(self, interval_sec: int, max_runtime_sec: int) -> None:
        loop = asyncio.get_running_loop()
        safety_sec = max(1, int(self.cfg.safety_interval_sec))
        full_sec = max(safety_sec, int(interval_sec))
        print(f"[hl_runner_async] {self.cfg.instance_name} mode={self.mode} "
              f"live_trading={self.live_trading} full={full_sec}s safety={safety_sec}s "
              f"heartbeat={self._hb_sec}s ws_max_age={self._ws_max_age}s")
        deadline = (loop.time() + max_runtime_sec) if max_runtime_sec > 0 else None
        next_full = 0.0
        while True:
            if deadline is not None and loop.time() >= deadline:
                print("[hl_runner_async] max-runtime reached — stopping")
                return
            self._wake.clear()                        # events DURING the cycle re-arm it
            try:
                due_full = loop.time() >= next_full
                r = await self._exec(self.run_once if due_full else self.run_safety_once)
                if due_full:
                    next_full = loop.time() + full_sec
                self._last_cycle_ts = time.time()
                await self._exec(self._capture_mtm_baseline)
                ws_age = self._ws.last_msg_age() if self._ws else float("inf")
                print(f"[{_utcnow().isoformat()}] {r.get('action')} eq={r.get('equity')} "
                      f"reconcile_ok={r.get('reconcile_ok')} ws_age={ws_age:.1f}s "
                      f"risk_drop={self._last_risk_drop * 100:.2f}%")
            except Exception as e:
                print(f"[hl_runner_async] cycle error: {e}", file=sys.stderr)
            # Event-driven cadence: wake on a risk/fill event OR the safety timer.
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=safety_sec)
                print(f"[hl_runner_async] woke early (risk/fill) "
                      f"risk_triggers={self._risk_triggers} fills={self._fills_consumed}")
            except asyncio.TimeoutError:
                pass

    async def run(self, interval_sec: int = 3600, max_runtime_sec: int = 0) -> None:
        loop = asyncio.get_running_loop()
        addr = self.adapter.address if (self.live_trading and self.adapter.wallet) else None
        self._ws = HLWsFeed(self.adapter.base_url, list(self.cfg.universe),
                            bus=self._bus, loop=loop,
                            resync_mids=self._rest_all_mids, user_address=addr).start()
        # Let the WS connect + resync so the first cycle already has live data.
        for _ in range(20):
            if self._ws.last_msg_age() < self._ws_max_age:
                break
            await asyncio.sleep(0.25)
        print(f"[hl_runner_async] ws stats: {json.dumps(self._ws.stats())}")

        tasks = [asyncio.create_task(self._heartbeat_task()),
                 asyncio.create_task(self._risk_monitor_task()),
                 asyncio.create_task(self._fill_consumer_task())]
        try:
            await self._cycle_task(interval_sec, max_runtime_sec)
        finally:
            for t in tasks:
                t.cancel()
            self._ws.stop()
            self._pool.shutdown(wait=False)
            self._leg_pool.shutdown(wait=False)
            self._write_heartbeat()               # final stamp


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="configs/hl-xsectional-main.json")
    ap.add_argument("--loop", action="store_true", help="run the async loop (default)")
    ap.add_argument("--interval-sec", type=int, default=3600)
    ap.add_argument("--max-runtime-sec", type=int, default=0,
                    help="auto-stop after N seconds (0 = run forever); for bounded soaks")
    args = ap.parse_args()

    cfg = load_config(args.config) if Path(args.config).exists() else HLXSConfig()
    runner = AsyncHLXSRunner(cfg)

    async def _amain():
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, ValueError):
                pass
        run_task = asyncio.create_task(runner.run(args.interval_sec, args.max_runtime_sec))
        stop_task = asyncio.create_task(stop.wait())
        done, pending = await asyncio.wait({run_task, stop_task},
                                           return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done:                     # graceful shutdown on signal
            print("[hl_runner_async] shutdown signal — stopping")
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass
        for t in pending:
            t.cancel()

    asyncio.run(_amain())
    return 0


if __name__ == "__main__":
    sys.exit(main())
