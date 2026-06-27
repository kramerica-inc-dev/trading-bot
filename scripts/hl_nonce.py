#!/usr/bin/env python3
"""Monotonic, thread-safe nonce clock for Hyperliquid order signing.

HL rejects a reused order nonce. The SDK derives every nonce from a raw
millisecond clock (`get_timestamp_ms`), so two order actions signed in the SAME
millisecond — exactly what concurrent leg execution does — collide on the nonce
and one is rejected. This installs a process-wide monotonic clock: each call
returns `max(now_ms, last+1)`, so concurrently-signed actions always get strictly
increasing, unique nonces. It patches the symbol the SDK actually calls
(`hyperliquid.exchange.get_timestamp_ms`); idempotent.

Without this, the concurrent execution path in hl_runner_async would be UNSAFE on
Hyperliquid. With it, concurrency is nonce-safe.

Self-test (16 threads × 1000 calls → all unique AND strictly increasing):
    python -m scripts.hl_nonce --selftest
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

_lock = threading.Lock()
_last = 0
_installed = False


def monotonic_timestamp_ms() -> int:
    """Strictly-increasing millisecond nonce, safe under concurrent signing."""
    global _last
    with _lock:
        t = int(time.time() * 1000)
        if t <= _last:
            t = _last + 1
        _last = t
        return t


def install() -> bool:
    """Replace the SDK's nonce clock with the monotonic one. Idempotent;
    returns True once the SDK is patched (or was already)."""
    global _installed
    if _installed:
        return True
    try:
        import hyperliquid.exchange as _ex
        _ex.get_timestamp_ms = monotonic_timestamp_ms
        _installed = True
    except Exception:
        return False
    return _installed


def _selftest() -> int:
    n_threads, per = 16, 1000
    out: list = []
    out_lock = threading.Lock()

    def worker():
        local = [monotonic_timestamp_ms() for _ in range(per)]
        with out_lock:
            out.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = n_threads * per
    unique = len(set(out))
    out_sorted = sorted(out)
    strictly_increasing = all(b > a for a, b in zip(out_sorted, out_sorted[1:]))
    ok = unique == total and strictly_increasing
    print(f"  calls={total} unique={unique} strictly_increasing={strictly_increasing}")
    print("  hl_nonce self-test", "PASSED ✓" if ok else "FAILED ✗")
    # prove install() patches the SDK symbol
    if install():
        import hyperliquid.exchange as _ex
        patched = _ex.get_timestamp_ms is monotonic_timestamp_ms
        print(f"  SDK patched: {patched}", "✓" if patched else "✗")
        ok = ok and patched
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
