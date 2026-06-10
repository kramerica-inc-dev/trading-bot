"""Tests for the Lane C cross-venue quote collector (network-free).

Covers the per-venue response parsers (fixtures captured from the live recon,
2026-06-10), the cumulative-notional depth math, cycle assembly with a failing
venue, repeated-failure backoff, the atomic health write, and the date-rollover
gzip naming. No sockets: every fetch method is stubbed at the instance level.
"""

import gzip
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import lanec_collector as LC  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures: shapes captured live during recon (values truncated, shapes exact).
# ---------------------------------------------------------------------------

BITVAVO_BOOK = {
    "market": "BTC-EUR", "nonce": 1234567,
    "bids": [["53915", "0.24483955"], ["53910", "1.5"]],
    "asks": [["53920", "0.4"], ["53925", "2.0"]],
    "timestamp": 1781108070298270569,   # NANOSECONDS — never treat as ms
}

KRAKEN_BOOK = {
    "error": [],
    "result": {
        # request was pair=XBTEUR — Kraken answers under the CLASSIC key
        "XXBTZEUR": {
            "asks": [["53920.0", "0.400", 1781108070], ["53925.0", "2.000", 1781108071]],
            "bids": [["53915.0", "0.245", 1781108070], ["53910.0", "1.500", 1781108069]],
        }
    },
}

OKX_BOOK = {
    "code": "0", "msg": "",
    "data": [{
        # 4-tuples: [px, sz, "0" (deprecated), numOrders] — all strings
        "asks": [["62362.8", "0.003", "0", "1"], ["62363.0", "1.2", "0", "3"]],
        "bids": [["62362.7", "0.5", "0", "2"], ["62360.0", "0.9", "0", "1"]],
        "ts": "1781108105609", "seqId": 987654,
    }],
}

HL_BOOK = {
    "coin": "BTC", "time": 1781108105609,
    "levels": [
        [{"px": "62371.0", "sz": "3.82392", "n": 4}, {"px": "62370.0", "sz": "1.0", "n": 1}],
        [{"px": "62372.0", "sz": "0.5", "n": 2}, {"px": "62373.0", "sz": "2.0", "n": 5}],
    ],
}

SPOT_META = {
    "tokens": [
        {"index": 0, "name": "USDC", "szDecimals": 8},
        {"index": 7, "name": "UBTC", "szDecimals": 5},
        {"index": 9, "name": "UETH", "szDecimals": 4},
        {"index": 1, "name": "PURR", "szDecimals": 0},
    ],
    "universe": [
        {"tokens": [1, 0], "name": "PURR/USDC", "index": 0, "isCanonical": True},
        {"tokens": [7, 0], "name": "@142", "index": 142, "isCanonical": False},
        {"tokens": [9, 0], "name": "@151", "index": 151, "isCanonical": False},
    ],
}

GOOD_REC = {"bid": 100.0, "ask": 100.1, "bid_qty": 1.0, "ask_qty": 1.0,
            "depth": {"bid": {"1k": 100.0, "5k": None, "10k": None},
                      "ask": {"1k": 100.1, "5k": None, "10k": None}},
            "lat_ms": 12}


class TestParsers(unittest.TestCase):
    def test_bitvavo(self):
        bids, asks = LC.parse_bitvavo(BITVAVO_BOOK)
        self.assertEqual(bids[0], (53915.0, 0.24483955))
        self.assertEqual(asks[0], (53920.0, 0.4))
        self.assertEqual(len(bids), 2)

    def test_bitvavo_bad_shape_raises(self):
        with self.assertRaises(ValueError):
            LC.parse_bitvavo({"errorCode": 205, "error": "market not found"})
        with self.assertRaises(ValueError):
            LC.parse_bitvavo(None)

    def test_kraken_result_key_quirk(self):
        # request XBTEUR, result key XXBTZEUR — parser must not need the name
        bids, asks = LC.parse_kraken(KRAKEN_BOOK)
        self.assertEqual(bids[0], (53915.0, 0.245))
        self.assertEqual(asks[0], (53920.0, 0.4))

    def test_kraken_error_list_raises(self):
        with self.assertRaises(ValueError):
            LC.parse_kraken({"error": ["EQuery:Unknown asset pair"]})
        with self.assertRaises(ValueError):
            LC.parse_kraken({"error": [], "result": {}})

    def test_okx_four_tuple_levels(self):
        bids, asks = LC.parse_okx(OKX_BOOK)
        self.assertEqual(bids[0], (62362.7, 0.5))
        self.assertEqual(asks[0], (62362.8, 0.003))

    def test_okx_nonzero_code_raises(self):
        with self.assertRaises(ValueError):
            LC.parse_okx({"code": "50011", "msg": "rate limit", "data": []})

    def test_hl_levels(self):
        bids, asks = LC.parse_hl(HL_BOOK)
        self.assertEqual(bids[0], (62371.0, 3.82392))
        self.assertEqual(asks[0], (62372.0, 0.5))

    def test_hl_null_raises(self):
        # l2Book for a composed spot name returns literal null
        with self.assertRaises(ValueError):
            LC.parse_hl(None)


class TestSpotResolution(unittest.TestCase):
    def test_resolves_at_index_form(self):
        m = LC.resolve_hl_spot(SPOT_META)
        self.assertEqual(m, {"UBTC/USDC": "@142", "UETH/USDC": "@151"})

    def test_garbage_meta_is_empty_not_crash(self):
        self.assertEqual(LC.resolve_hl_spot(None), {})
        self.assertEqual(LC.resolve_hl_spot({"tokens": [], "universe": [{"bad": 1}]}), {})


class TestDepthMath(unittest.TestCase):
    def test_cum_notional_thresholds(self):
        # cum notional: 500 -> 6440 -> 16240
        levels = [(100.0, 5.0), (99.0, 60.0), (98.0, 100.0)]
        d = LC.depth_px(levels)
        self.assertEqual(d["1k"], 99.0)    # 1k first reached on level 2
        self.assertEqual(d["5k"], 99.0)    # 5k also inside level 2
        self.assertEqual(d["10k"], 98.0)

    def test_exact_boundary_counts(self):
        d = LC.depth_px([(100.0, 10.0)])   # exactly 1000 notional
        self.assertEqual(d["1k"], 100.0)
        self.assertIsNone(d["5k"])

    def test_shallow_book_gives_none(self):
        d = LC.depth_px([(100.0, 0.001)])
        self.assertEqual(d, {"1k": None, "5k": None, "10k": None})

    def test_summarize_best_and_depth(self):
        rec = LC.summarize([(100.0, 2.0), (99.0, 50.0)], [(101.0, 3.0), (102.0, 60.0)])
        self.assertEqual(rec["bid"], 100.0)
        self.assertEqual(rec["ask"], 101.0)
        self.assertEqual(rec["bid_qty"], 2.0)
        self.assertEqual(rec["ask_qty"], 3.0)
        self.assertEqual(rec["depth"]["bid"]["1k"], 99.0)
        self.assertEqual(rec["depth"]["ask"]["1k"], 102.0)

    def test_summarize_empty_side_raises(self):
        with self.assertRaises(ValueError):
            LC.summarize([], [(101.0, 1.0)])


def _stub_collector(tmpdir, fail=()):
    """Collector with every venue fetch stubbed; venues in `fail` raise."""
    c = LC.Collector(state_dir=Path(tmpdir))
    for v in LC.VENUES:
        keys = {
            "bitvavo": [f"bitvavo:{m}" for m in LC.BITVAVO_BOOKS],
            "kraken": [f"kraken:{k}" for k, _ in LC.KRAKEN_BOOKS] + [f"kraken:{LC.KRAKEN_FX[0]}"],
            "okx": [f"okx:{i}" for i in LC.OKX_BOOKS],
            "hl": [f"hl_spot:{p}" for p in LC.HL_SPOT_BOOKS] + [f"hl_perp:{p}" for p in LC.HL_PERP_BOOKS],
        }[v]
        if v in fail:
            def boom(_v=v):
                raise RuntimeError(f"{_v} unreachable")
            setattr(c, f"fetch_{v}", boom)
        else:
            setattr(c, f"fetch_{v}", lambda ks=keys: {k: dict(GOOD_REC) for k in ks})
    return c


class TestCycleAssembly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_one_venue_erroring_never_kills_the_cycle(self):
        c = _stub_collector(self.tmp, fail=("okx",))
        line = c.run_cycle()
        self.assertIsInstance(line["ts"], int)
        for inst in LC.OKX_BOOKS:                       # failed venue -> err records
            self.assertIn("err", line["venues"][f"okx:{inst}"])
            self.assertIn("okx unreachable", line["venues"][f"okx:{inst}"]["err"])
        for m in LC.BITVAVO_BOOKS:                      # others unaffected
            self.assertEqual(line["venues"][f"bitvavo:{m}"]["bid"], 100.0)
        self.assertIn("hl_perp:BTC", line["venues"])
        self.assertEqual(c.counts["okx"]["err"], len(LC.OKX_BOOKS))
        self.assertEqual(c.counts["bitvavo"]["ok"], len(LC.BITVAVO_BOOKS))

    def test_all_eleven_books_present_on_fx_cycle(self):
        c = _stub_collector(self.tmp)
        line = c.run_cycle()                            # cycle 1 -> FX due
        self.assertEqual(len(line["venues"]), 11)
        self.assertIn("kraken:USDT-EUR", line["venues"])

    def test_fx_cadence(self):
        c = _stub_collector(self.tmp)
        c.cycle_no = 0
        c.run_cycle()                                   # cycle 1
        self.assertTrue(c._fx_due())
        c.run_cycle()                                   # cycle 2
        self.assertFalse(c._fx_due())
        c.cycle_no = LC.USDT_EVERY                      # next is 1 + USDT_EVERY
        c.run_cycle()
        self.assertTrue(c._fx_due())

    def test_backoff_after_repeated_total_failure(self):
        c = _stub_collector(self.tmp, fail=("kraken",))
        for _ in range(LC.ERR_CYCLES_BEFORE_BACKOFF):
            c.run_cycle()
        self.assertGreater(c.venue_state["kraken"]["skip_until"], c.cycle_no)
        calls = []
        c.fetch_kraken = lambda: calls.append(1) or {}
        line = c.run_cycle()                            # inside backoff window
        self.assertEqual(calls, [])                     # venue NOT fetched
        self.assertIn("backoff", line["venues"]["kraken:BTC-EUR"]["err"])
        self.assertEqual(c.counts["kraken"]["skip"], 1)
        # healthy venues keep flowing during the backoff
        self.assertEqual(line["venues"]["bitvavo:BTC-EUR"]["bid"], 100.0)

    def test_partial_failure_does_not_strike(self):
        c = _stub_collector(self.tmp)
        keys = [f"okx:{i}" for i in LC.OKX_BOOKS]
        c.fetch_okx = lambda: {keys[0]: {"err": "x"}, keys[1]: dict(GOOD_REC)}
        for _ in range(LC.ERR_CYCLES_BEFORE_BACKOFF + 1):
            c.run_cycle()
        self.assertEqual(c.venue_state["okx"]["skip_until"], 0)


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_filename_is_utc_date(self):
        c = LC.Collector(state_dir=Path(self.tmp))
        ts = int(datetime(2026, 6, 10, 23, 59, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(c.path_for_ts(ts).name, "quotes-20260610.jsonl")
        self.assertEqual(c.path_for_ts(ts + 120_000).name, "quotes-20260611.jsonl")

    def test_health_write_atomic_shape(self):
        c = _stub_collector(self.tmp)
        line = c.run_cycle()
        path = c.append_line(line)
        c.write_health(line["ts"], path)
        h = json.loads((Path(self.tmp) / "health.json").read_text())
        self.assertEqual(h["cycles"], 1)
        self.assertEqual(h["ts"], line["ts"])
        self.assertGreater(h["bytes_today"], 0)
        self.assertEqual(h["current_file"], str(path))
        for v in LC.VENUES:
            self.assertIn("ok", h["venues"][v])
            self.assertIn("err", h["venues"][v])
        self.assertFalse((Path(self.tmp) / "health.json.tmp").exists())

    def test_appended_line_roundtrips(self):
        c = _stub_collector(self.tmp)
        line = c.run_cycle()
        path = c.append_line(line)
        back = json.loads(path.read_text().splitlines()[-1])
        self.assertEqual(back["ts"], line["ts"])
        self.assertEqual(back["venues"]["hl_perp:ETH"]["bid"], 100.0)

    def test_rollover_gzips_previous_day(self):
        c = _stub_collector(self.tmp)
        d1 = int(datetime(2026, 6, 10, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000)
        d2 = d1 + 60_000                                # crosses UTC midnight
        c.append_line({"ts": d1, "venues": {}})
        c.append_line({"ts": d2, "venues": {}})
        gz = Path(self.tmp) / "quotes-20260610.jsonl.gz"
        self.assertTrue(gz.exists())
        self.assertFalse((Path(self.tmp) / "quotes-20260610.jsonl").exists())
        with gzip.open(gz, "rt") as f:                  # content survives the gzip
            self.assertEqual(json.loads(f.read().splitlines()[0])["ts"], d1)
        self.assertTrue((Path(self.tmp) / "quotes-20260611.jsonl").exists())

    def test_startup_sweep_gzips_stale_only(self):
        c = LC.Collector(state_dir=Path(self.tmp))
        now = int(datetime(2026, 6, 11, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
        (Path(self.tmp) / "quotes-20260609.jsonl").write_text("{}\n")
        (Path(self.tmp) / "quotes-20260611.jsonl").write_text("{}\n")
        c.gzip_stale(now_ms=now)
        self.assertTrue((Path(self.tmp) / "quotes-20260609.jsonl.gz").exists())
        self.assertFalse((Path(self.tmp) / "quotes-20260609.jsonl").exists())
        self.assertTrue((Path(self.tmp) / "quotes-20260611.jsonl").exists())  # today kept

    def test_gzip_missing_file_is_quiet_false(self):
        self.assertFalse(LC.gzip_file(Path(self.tmp) / "nope.jsonl"))


class TestFeesConstant(unittest.TestCase):
    """FEES is the single analysis-time source of truth — pin the recon values."""

    def test_all_five_venues_present(self):
        self.assertEqual(set(LC.FEES), {"bitvavo", "kraken", "okx", "hl_perp", "hl_spot"})

    def test_okx_is_the_eu_entity_not_global(self):
        # global base tier is 0.0008/0.0010 — the EU/MiCA account pays 0.20/0.35
        self.assertEqual(LC.FEES["okx"], {"maker": 0.0020, "taker": 0.0035})

    def test_values_are_plausible_fractions(self):
        for venue, f in LC.FEES.items():
            self.assertGreater(f["taker"], 0.0, venue)
            self.assertLess(f["taker"], 0.01, venue)
            self.assertLessEqual(f["maker"], f["taker"], venue)


if __name__ == "__main__":
    unittest.main()
