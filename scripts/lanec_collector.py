#!/usr/bin/env python3
"""Lane C cross-venue order-book snapshot collector (READ-ONLY public market data).

Every cycle (default 2.0s) fetch, in parallel (one thread per venue), the top of
book + depth for:

    bitvavo : BTC-EUR, ETH-EUR        GET /v2/{market}/book?depth=10
    kraken  : BTC-EUR, ETH-EUR        GET /0/public/Depth?pair=XBTEUR|ETHEUR
              USDT-EUR (FX leg)       every USDT_EVERY cycles only — analysis-time
                                      EUR<->USDT conversion source (Bitvavo has NO
                                      USDT market under MiCA)
    okx     : BTC-USDT, ETH-USDT      GET /api/v5/market/books?sz=10 (browser UA —
                                      Cloudflare blocks python-requests UA on
                                      datacenter IPs, CF 1010 -> 403)
    hl_spot : UBTC/USDC, UETH/USDC    POST /info {"type":"l2Book","coin":"@<idx>"}
                                      spot index resolved at startup via spotMeta
                                      (composed-name form returns null) — NEVER
                                      hardcode @142/@151
    hl_perp : BTC, ETH                POST /info {"type":"l2Book","coin":"BTC"}

Per book we record best bid/ask px+qty, fetch latency ms, and the price at which
cumulative notional reaches 1k/5k/10k QUOTE-CURRENCY units per side (EUR books in
EUR, USDT books in USDT, USDC/USD books likewise). No FX conversion at collect
time — store raw, convert at analysis time using the logged kraken:USDT-EUR book.

METHODOLOGY HONESTY: these are REST snapshots, not websocket streams. A 2s
snapshot cadence can only observe cross-venue spreads that PERSIST >= 2s; any
sub-2s opportunity is invisible. Counts derived from this data are therefore a
CONSERVATIVE LOWER BOUND on opportunity frequency — fine for a go/no-go gate
(if the persistent-spread economics already clear fees, the real opportunity
set is a superset), useless for latency-game sizing.

Rate budget per 2.0s cycle vs public limits:
    bitvavo  2 req/2s   vs 1000 weighted/min/IP        (~6%)
    kraken   2 req/2s + FX every USDT_EVERY cycles     (~1.1 req/s vs ~1/s guide;
             sub-requests staggered KRAKEN_STAGGER_SEC apart; Kraken tolerates
             short bursts — repeated-error backoff below is the safety net.
             If lockouts appear in the journal, raise --cycle-sec to 3.0)
    okx      2 req/2s   vs 40 req/2s/IP                (5%)
    hl       4 req/2s   = weight 8/2s vs 1200/min/IP   (~20%, incl. rare spotMeta)

Failure containment: a venue failure NEVER kills the cycle (per-book records get
{"err": "..."}) and NEVER kills the process (the loop catches everything, sleeps,
continues). A venue whose books ALL fail ERR_CYCLES_BEFORE_BACKOFF cycles in a
row is skipped for BACKOFF_CYCLES cycles (records say "backoff").

Output:  state/lanec/quotes-YYYYMMDD.jsonl (UTC date), one line per cycle:
         {"ts": <ms>, "venues": {"venue:pair": {bid, ask, bid_qty, ask_qty,
          depth:{bid:{1k,5k,10k}, ask:{...}}, lat_ms, off_ms} | {"err", off_ms}}}
         off_ms = fetch-start offset from the cycle's ts — the per-book sample
         skew (Kraken's staggered books sample ~0.65-1.3s late; during a price
         drift a late book shows a directional phantom spread, so analysis must
         de-skew or discard high-off_ms comparisons).
         At UTC date rollover the previous day's file is gzipped (best-effort;
         a startup sweep also gzips any stale prior-day files).
Disk:    ~2.2KB/line x ~43k cycles/day = ~95 MB/day live file, ~10-15 MB/day
         gzipped. Retention: .gz files older than RETAIN_GZ_DAYS (45) are
         deleted in the startup sweep — a 30d measurement plus analysis margin
         fits; nothing accumulates unbounded.
Health:  state/lanec/health.json each cycle (atomic tmp+replace, same pattern
         as the other runners).

No API keys, no order code, no imports from order-placing modules. Public
endpoints only. Stdlib + requests; no websockets.

systemd: deployment/systemd/lanec-collector.service
Run once locally: python3 -m scripts.lanec_collector --once
"""

import argparse
import gzip
import json
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

STATE_DIR = Path(__file__).resolve().parent.parent / "state" / "lanec"

CYCLE_SEC = 2.0
HTTP_TIMEOUT = 5.0
KRAKEN_STAGGER_SEC = 0.65      # gap between Kraken sub-requests inside a cycle
USDT_EVERY = 5                 # sample kraken USDT-EUR every Nth cycle (FX moves slowly)
ERR_CYCLES_BEFORE_BACKOFF = 3  # consecutive all-books-failed cycles before skipping
BACKOFF_CYCLES = 15            # cycles to skip a misbehaving venue (~30s at 2s)
RESOLVE_RETRY_CYCLES = 30      # min cycles between HL spotMeta resolution retries
RETAIN_GZ_DAYS = 45            # delete gzipped quote files older than this (startup sweep)

# Cumulative-notional depth thresholds, in QUOTE currency units (EUR / USDT /
# USDC / USD depending on the book). "€1k/€5k/€10k equivalents" — equivalence is
# applied at ANALYSIS time via the logged USDT-EUR book, never here.
DEPTH_NOTIONALS: Tuple[Tuple[str, float], ...] = (
    ("1k", 1_000.0), ("5k", 5_000.0), ("10k", 10_000.0))

# Cloudflare-fronted venues (OKX always, Bitvavo on datacenter egress) block the
# default python-requests UA (CF error 1010 -> 403). Browser-shaped UA everywhere
# — same string as scripts/okx_api.py.
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

BITVAVO_BASE = "https://api.bitvavo.com/v2"
KRAKEN_DEPTH_URL = "https://api.kraken.com/0/public/Depth"
OKX_BOOKS_URL = "https://www.okx.com/api/v5/market/books"
HL_INFO_URL = "https://api.hyperliquid.xyz/info"

BITVAVO_BOOKS = ("BTC-EUR", "ETH-EUR")
# (record key, Kraken request pair). NB Kraken's RESULT key differs from the
# request pair (XBTEUR -> XXBTZEUR, ETHEUR -> XETHZEUR, USDTEUR -> USDTEUR);
# parse_kraken takes next(iter(result)) instead of trusting the request name.
KRAKEN_BOOKS = (("BTC-EUR", "XBTEUR"), ("ETH-EUR", "ETHEUR"))
KRAKEN_FX = ("USDT-EUR", "USDTEUR")
OKX_BOOKS = ("BTC-USDT", "ETH-USDT")
HL_SPOT_BOOKS = ("UBTC/USDC", "UETH/USDC")
HL_PERP_BOOKS = ("BTC", "ETH")

VENUES = ("bitvavo", "kraken", "okx", "hl")

# ---------------------------------------------------------------------------
# Fee table — ANALYSIS-TIME input, single source of truth for the Lane C gate
# math. Fractions of notional (0.0025 = 0.25%). Retail BASE tier, verified
# 2026-06-10 against the venues' public schedules:
#   bitvavo : EUR pairs, category A/C, EUR 0 30d volume  (bitvavo.com/en/fees)
#   kraken  : Kraken Pro spot, $0-10k 30d tier           (kraken.com/features/fee-schedule)
#   okx     : **EU/MiCA entity** spot, account-measured. The GLOBAL OKX base
#             tier (0.08%/0.10%) does NOT apply to this account — see
#             docs/CARRY-OPS.md fee-source discipline; with keys, read live
#             from /api/v5/account/trade-fee (this collector stays keyless).
#   hl_*    : Hyperliquid tier 0 (hyperliquid docs: fees)
# ---------------------------------------------------------------------------
FEES: Dict[str, Dict[str, float]] = {
    "bitvavo": {"maker": 0.0015,  "taker": 0.0025},
    "kraken":  {"maker": 0.0025,  "taker": 0.0040},
    "okx":     {"maker": 0.0020,  "taker": 0.0035},
    "hl_perp": {"maker": 0.00015, "taker": 0.00045},
    "hl_spot": {"maker": 0.0004,  "taker": 0.0007},
}

# Withdrawal-fee PLANNING ESTIMATES for the rebalance-cost leg (verified
# 2026-06-10; Kraken/OKX crypto fees are dynamic and only authoritative at
# withdrawal-confirmation time — parameterize in the gate config, don't trust
# these as exact). None = not available on that venue (e.g. Bitvavo has no
# USDT under MiCA; HL bridges USDC not USDT — flat 1 USDC via Arbitrum).
WITHDRAW_FEES_EST: Dict[str, Dict[str, Optional[float]]] = {
    "bitvavo": {"BTC": 0.000024, "ETH": 0.0005,    "USDT": None, "EUR": 0.0},
    "kraken":  {"BTC": 0.0001,   "ETH": 0.0035,    "USDT": 1.0,  "EUR": 1.0},
    "okx":     {"BTC": 0.00001,  "ETH": 0.000095,  "USDT": 1.0,  "EUR": None},
    "hl":      {"BTC": None,     "ETH": None,      "USDT": None, "EUR": None,
                "USDC": 1.0},   # Unit-bridge mins: 0.002 BTC / 0.05 ETH / 2 USDC
}


# =========================== pure parsing helpers ===========================
# Each parser: raw venue JSON -> (bids, asks) as [(px, qty), ...] floats,
# best-first. Raise ValueError on anything unexpected — the caller turns that
# into a per-book {"err": ...} record.

def parse_bitvavo(j) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """{"market","nonce","bids":[[px,sz],..],"asks":[..],"timestamp":<ns>}."""
    if not isinstance(j, dict) or "bids" not in j or "asks" not in j:
        raise ValueError(f"unexpected bitvavo shape: {str(j)[:120]}")
    bids = [(float(p), float(q)) for p, q in j["bids"]]
    asks = [(float(p), float(q)) for p, q in j["asks"]]
    return bids, asks


def parse_kraken(j) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """{"error":[],"result":{"<KEY>":{"bids":[[px,vol,ts],..],"asks":[..]}}}.

    The result KEY is Kraken's classic name (XXBTZEUR for XBTEUR) — take the
    single key present, never the request pair.
    """
    if not isinstance(j, dict):
        raise ValueError(f"unexpected kraken shape: {str(j)[:120]}")
    if j.get("error"):
        raise ValueError(f"kraken error: {j['error']}")
    result = j.get("result") or {}
    if not result:
        raise ValueError("kraken: empty result")
    book = result[next(iter(result))]
    bids = [(float(l[0]), float(l[1])) for l in book["bids"]]
    asks = [(float(l[0]), float(l[1])) for l in book["asks"]]
    return bids, asks


def parse_okx(j) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """{"code":"0","data":[{"bids":[[px,sz,"0",nOrders],..],"asks":[..],"ts":..}]}."""
    if not isinstance(j, dict) or j.get("code") != "0" or not j.get("data"):
        raise ValueError(f"okx error: {str(j)[:160]}")
    book = j["data"][0]
    bids = [(float(l[0]), float(l[1])) for l in book["bids"]]
    asks = [(float(l[0]), float(l[1])) for l in book["asks"]]
    return bids, asks


def parse_hl(j) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """{"coin","time","levels":[[bids..],[asks..]]}, level {"px","sz","n"}.

    HL returns literal null for an unknown coin (e.g. the composed spot name
    "UBTC/USDC" instead of "@<index>") — that lands here as j=None and raises.
    """
    if not isinstance(j, dict) or not isinstance(j.get("levels"), list) \
            or len(j["levels"]) != 2:
        raise ValueError(f"unexpected hl shape: {str(j)[:120]}")
    bids = [(float(l["px"]), float(l["sz"])) for l in j["levels"][0]]
    asks = [(float(l["px"]), float(l["sz"])) for l in j["levels"][1]]
    return bids, asks


def resolve_hl_spot(spot_meta, wanted: Tuple[str, ...] = HL_SPOT_BOOKS) -> Dict[str, str]:
    """spotMeta -> {composed "BASE/QUOTE" name: universe coin name ("@142")}.

    Same composition as hl_adapter.spot_pairs(): tokens by index, name =
    f"{base}/{quote}". Only canonical pairs (PURR/USDC) use the name form;
    UBTC/UETH are non-canonical -> "@<spot-universe-index>". Missing pairs are
    simply absent from the result (caller errs those books per cycle).
    """
    if not isinstance(spot_meta, dict):
        return {}
    tok = {t["index"]: t for t in spot_meta.get("tokens", []) or []
           if isinstance(t, dict) and "index" in t}
    out: Dict[str, str] = {}
    for u in spot_meta.get("universe", []) or []:
        try:
            base, quote = u["tokens"]
            name = f'{tok[base]["name"]}/{tok[quote]["name"]}'
            if name in wanted:
                out[name] = u["name"]
        except (KeyError, TypeError, ValueError):
            continue
    return out


def depth_px(levels: List[Tuple[float, float]],
             notionals: Tuple[Tuple[str, float], ...] = DEPTH_NOTIONALS
             ) -> Dict[str, Optional[float]]:
    """Price at which cumulative notional (px*qty, quote ccy) reaches each
    threshold, walking best-first. None where the visible book is too shallow."""
    out: Dict[str, Optional[float]] = {label: None for label, _ in notionals}
    pending = list(notionals)
    cum = 0.0
    for px, qty in levels:
        cum += px * qty
        while pending and cum >= pending[0][1]:
            out[pending[0][0]] = px
            pending.pop(0)
        if not pending:
            break
    return out


def summarize(bids: List[Tuple[float, float]],
              asks: List[Tuple[float, float]]) -> dict:
    """Best bid/ask px+qty plus per-side depth prices. Raises on an empty side
    (a one-sided book is unusable for spread math — better an err record)."""
    if not bids or not asks:
        raise ValueError("empty book side")
    return {
        "bid": bids[0][0], "ask": asks[0][0],
        "bid_qty": bids[0][1], "ask_qty": asks[0][1],
        "depth": {"bid": depth_px(bids), "ask": depth_px(asks)},
    }


def gzip_file(path: Path) -> bool:
    """Best-effort gzip+remove. Never raises (rollover must not kill a cycle)."""
    try:
        if not path.exists():
            return False
        gz = path.with_name(path.name + ".gz")
        with open(path, "rb") as src, gzip.open(gz, "wb") as dst:
            shutil.copyfileobj(src, dst)
        path.unlink()
        return True
    except Exception:
        return False


# ================================ collector =================================

class Collector:
    def __init__(self, state_dir: Path = STATE_DIR, cycle_sec: float = CYCLE_SEC):
        self.state_dir = Path(state_dir)
        self.cycle_sec = float(cycle_sec)
        self.health_path = self.state_dir / "health.json"
        self.cycle_no = 0
        self.started_ts = int(time.time() * 1000)
        self.counts = {v: {"ok": 0, "err": 0, "skip": 0} for v in VENUES}
        self.venue_state = {v: {"consec": 0, "skip_until": 0} for v in VENUES}
        self._current_path: Optional[Path] = None
        self._cycle_t0_perf = time.perf_counter()    # reset at each run_cycle
        self._spot_map: Dict[str, str] = {}
        self._spot_resolve_cycle: Optional[int] = None
        self._pool = ThreadPoolExecutor(max_workers=len(VENUES),
                                        thread_name_prefix="lanec")
        self.sessions: Dict[str, requests.Session] = {}
        for v in VENUES:
            s = requests.Session()
            s.headers.update({"User-Agent": USER_AGENT})
            self.sessions[v] = s

    # ------------------------------------------------------------- HTTP
    def _get_json(self, venue: str, url: str, params: Optional[dict] = None):
        r = self.sessions[venue].get(url, params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _post_json(self, venue: str, url: str, body: dict):
        r = self.sessions[venue].post(url, json=body, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _safe_record(self, http_call: Callable[[], object],
                     parser: Callable) -> dict:
        """Time the HTTP call, parse, summarize. Any failure -> {"err": str}.
        off_ms (fetch-start offset from the cycle clock) is recorded on BOTH
        success and error records, so the per-book sample-skew chain stays
        measurable even when an earlier book in the same venue thread fails."""
        off_ms = int(round((time.perf_counter() - self._cycle_t0_perf) * 1000))
        try:
            t0 = time.perf_counter()
            j = http_call()
            lat_ms = int(round((time.perf_counter() - t0) * 1000))
            rec = summarize(*parser(j))
            rec["lat_ms"] = lat_ms
            rec["off_ms"] = off_ms
            return rec
        except Exception as e:                       # noqa: BLE001 — contained by design
            return {"err": f"{type(e).__name__}: {e}"[:200], "off_ms": off_ms}

    # ------------------------------------------------------------- venues
    def _fx_due(self) -> bool:
        return (self.cycle_no - 1) % USDT_EVERY == 0

    def _expected_keys(self, venue: str) -> List[str]:
        if venue == "bitvavo":
            return [f"bitvavo:{m}" for m in BITVAVO_BOOKS]
        if venue == "kraken":
            keys = [f"kraken:{k}" for k, _ in KRAKEN_BOOKS]
            if self._fx_due():
                keys.append(f"kraken:{KRAKEN_FX[0]}")
            return keys
        if venue == "okx":
            return [f"okx:{i}" for i in OKX_BOOKS]
        return ([f"hl_spot:{p}" for p in HL_SPOT_BOOKS]
                + [f"hl_perp:{c}" for c in HL_PERP_BOOKS])

    def fetch_bitvavo(self) -> Dict[str, dict]:
        out = {}
        for market in BITVAVO_BOOKS:
            out[f"bitvavo:{market}"] = self._safe_record(
                lambda m=market: self._get_json(
                    "bitvavo", f"{BITVAVO_BASE}/{m}/book", {"depth": 10}),
                parse_bitvavo)
        return out

    def fetch_kraken(self) -> Dict[str, dict]:
        books = list(KRAKEN_BOOKS)
        if self._fx_due():
            books.append(KRAKEN_FX)
        out = {}
        for i, (key, req_pair) in enumerate(books):
            if i:                                   # ~1 req/s instantaneous max
                time.sleep(KRAKEN_STAGGER_SEC)
            out[f"kraken:{key}"] = self._safe_record(
                lambda p=req_pair: self._get_json(
                    "kraken", KRAKEN_DEPTH_URL, {"pair": p, "count": 10}),
                parse_kraken)
        return out

    def fetch_okx(self) -> Dict[str, dict]:
        out = {}
        for inst in OKX_BOOKS:
            out[f"okx:{inst}"] = self._safe_record(
                lambda i=inst: self._get_json(
                    "okx", OKX_BOOKS_URL, {"instId": i, "sz": 10}),
                parse_okx)
        return out

    def _hl_spot_map(self) -> Dict[str, str]:
        """Resolved {composed name: "@idx"} — cached; re-resolves at most every
        RESOLVE_RETRY_CYCLES while incomplete (spotMeta is a heavy info call)."""
        if all(p in self._spot_map for p in HL_SPOT_BOOKS):
            return self._spot_map
        if (self._spot_resolve_cycle is not None
                and self.cycle_no - self._spot_resolve_cycle < RESOLVE_RETRY_CYCLES):
            return self._spot_map
        self._spot_resolve_cycle = self.cycle_no
        try:
            sm = self._post_json("hl", HL_INFO_URL, {"type": "spotMeta"})
            resolved = resolve_hl_spot(sm)
            if resolved:
                self._spot_map.update(resolved)
        except Exception:                            # keep old map; books err this cycle
            pass
        return self._spot_map

    def fetch_hl(self) -> Dict[str, dict]:
        out = {}
        spot_map = self._hl_spot_map()
        for pair in HL_SPOT_BOOKS:
            coin = spot_map.get(pair)
            if coin is None:
                out[f"hl_spot:{pair}"] = {"err": "spot pair unresolved (spotMeta)"}
                continue
            out[f"hl_spot:{pair}"] = self._safe_record(
                lambda c=coin: self._post_json(
                    "hl", HL_INFO_URL, {"type": "l2Book", "coin": c}),
                parse_hl)
        for coin in HL_PERP_BOOKS:
            out[f"hl_perp:{coin}"] = self._safe_record(
                lambda c=coin: self._post_json(
                    "hl", HL_INFO_URL, {"type": "l2Book", "coin": c}),
                parse_hl)
        return out

    # ------------------------------------------------------------- cycle
    def run_cycle(self) -> dict:
        self.cycle_no += 1
        ts = int(time.time() * 1000)                 # ONE wall clock per cycle
        self._cycle_t0_perf = time.perf_counter()    # off_ms reference for every book
        venues: Dict[str, dict] = {}

        active: List[str] = []
        for v in VENUES:
            vs = self.venue_state[v]
            if self.cycle_no < vs["skip_until"]:
                for k in self._expected_keys(v):
                    venues[k] = {"err": f"backoff until cycle {vs['skip_until']}"}
                self.counts[v]["skip"] += 1
                continue
            active.append(v)

        futures = {self._pool.submit(getattr(self, f"fetch_{v}")): v
                   for v in active}
        for fut, v in futures.items():
            keys = self._expected_keys(v)
            try:
                res = fut.result()
            except Exception as e:                   # noqa: BLE001 — never kills the cycle
                res = {k: {"err": f"{type(e).__name__}: {e}"[:200]} for k in keys}
            venues.update(res)

            errs = sum(1 for k in keys if "err" in venues.get(k, {"err": "missing"}))
            self.counts[v]["ok"] += len(keys) - errs
            self.counts[v]["err"] += errs
            vs = self.venue_state[v]
            if keys and errs == len(keys):           # total venue failure
                vs["consec"] += 1
                if vs["consec"] >= ERR_CYCLES_BEFORE_BACKOFF:
                    vs["skip_until"] = self.cycle_no + 1 + BACKOFF_CYCLES
                    vs["consec"] = 0
            else:
                vs["consec"] = 0

        return {"ts": ts, "venues": venues}

    # ------------------------------------------------------------- persistence
    def path_for_ts(self, ts_ms: int) -> Path:
        d = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        return self.state_dir / f"quotes-{d.strftime('%Y%m%d')}.jsonl"

    def gzip_stale(self, now_ms: Optional[int] = None) -> None:
        """Startup sweep: gzip any prior-day plain files (restart-across-midnight)
        and delete .gz history older than RETAIN_GZ_DAYS (bounded disk; see the
        Disk note in the module docstring). Best-effort, never raises."""
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        today = self.path_for_ts(now).name
        for p in sorted(self.state_dir.glob("quotes-*.jsonl")):
            if p.name != today:
                gzip_file(p)
        cutoff = now - RETAIN_GZ_DAYS * 86_400_000
        for p in sorted(self.state_dir.glob("quotes-*.jsonl.gz")):
            try:
                d = datetime.strptime(p.name[len("quotes-"):len("quotes-") + 8],
                                      "%Y%m%d").replace(tzinfo=timezone.utc)
                if int(d.timestamp() * 1000) < cutoff:
                    p.unlink()
            except (ValueError, OSError):
                continue

    def append_line(self, line: dict) -> Path:
        path = self.path_for_ts(line["ts"])
        if self._current_path is not None and path != self._current_path:
            gzip_file(self._current_path)            # date rollover, best-effort
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(line, separators=(",", ":")) + "\n")
        self._current_path = path
        return path

    def write_health(self, ts: int, current: Path) -> None:
        try:
            bytes_today = current.stat().st_size if current.exists() else 0
        except OSError:
            bytes_today = 0
        h = {
            "ts": ts,
            "ts_iso": datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc).isoformat(),
            "started_ts": self.started_ts,
            "cycles": self.cycle_no,
            "venues": self.counts,
            "current_file": str(current),
            "bytes_today": bytes_today,
        }
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.health_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(h, indent=2))
        tmp.replace(self.health_path)

    # ------------------------------------------------------------- run modes
    def once(self, strict: bool = False) -> int:
        """One cycle. Default (lenient): exit 0 if AT LEAST ONE book succeeded —
        a liveness check. --strict: exit 0 only when EVERY expected book is
        error-free — the deploy/smoke gate."""
        line = self.run_cycle()
        path = self.append_line(line)
        self.write_health(line["ts"], path)
        print(json.dumps(line))
        errs = sum(1 for r in line["venues"].values() if "err" in r)
        ok = len(line["venues"]) - errs
        if strict:
            return 0 if (ok and errs == 0) else 1
        return 0 if ok else 1

    def loop(self) -> int:
        self.gzip_stale()
        while True:
            try:
                t0 = time.monotonic()
                line = self.run_cycle()
                path = self.append_line(line)
                self.write_health(line["ts"], path)
                time.sleep(max(0.0, self.cycle_sec - (time.monotonic() - t0)))
            except KeyboardInterrupt:
                return 0
            except Exception as e:                   # noqa: BLE001 — process must survive
                print(f"cycle error (continuing): {type(e).__name__}: {e}",
                      file=sys.stderr)
                time.sleep(self.cycle_sec)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true",
                      help="one cycle: write + print the JSONL line, exit. "
                           "Exit 0 if >=1 book succeeded (lenient liveness); "
                           "combine with --strict for the all-books deploy gate")
    mode.add_argument("--loop", action="store_true",
                      help="run forever (default)")
    ap.add_argument("--strict", action="store_true",
                    help="with --once: exit 0 only when EVERY book is error-free")
    ap.add_argument("--cycle-sec", type=float, default=CYCLE_SEC)
    ap.add_argument("--state-dir", default=str(STATE_DIR),
                    help="output dir (default state/lanec)")
    args = ap.parse_args(argv)

    c = Collector(state_dir=Path(args.state_dir), cycle_sec=args.cycle_sec)
    if args.once:
        return c.once(strict=args.strict)
    return c.loop()


if __name__ == "__main__":
    sys.exit(main())
