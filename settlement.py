"""
Prior-session close lookup for the ES future.

The simulator starts its random walk at the previous session's closing price.
This module resolves that price using only the standard library:

  1. a price you pass explicitly always wins (``--start 7772.50``);
  2. otherwise the last *completed* daily bar for the symbol is fetched from
     Yahoo Finance's public chart endpoint and cached on disk;
  3. if the fetch fails or is skipped, the cached value is reused and flagged
     stale;
  4. if there is no cache either, the caller falls back to the band center, so
     the simulator still runs with no network at all.

CME's own settlements endpoint is deliberately *not* used: its Data Terms of Use
prohibit automated access. Yahoo's continuous front-month quote (``ES=F``) is a
close proxy, but it is a consolidated close, not CME's official settlement
price — pass ``--start`` if you need the exact settle.

    python3 settlement.py               # print yesterday's ES close
    python3 settlement.py --refresh     # ignore the cache
    python3 settlement.py --offline     # cache only, never touch the network
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from contracts import DEFAULT_BAND_TICKS, ContractSpec, get_spec
from engine import TICK, round_to_tick
from pyver import require_python

require_python()

DEFAULT_SYMBOL = "ES=F"          # front-month E-mini S&P 500, continuous
CHART_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
             "{symbol}?range=10d&interval=1d")
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          ".prior_close_cache.json")
CACHE_TTL = 4 * 3600             # seconds before we try the network again
CACHE_VERSION = 2                # bump when cached values change meaning
MAX_FETCH_WORKERS = 8            # polite ceiling on concurrent lookups

# One cache file is shared by every product, and prefetch() writes to it from
# several threads, so read-modify-write has to be serialized or entries are lost.
_CACHE_LOCK = threading.RLock()
DEFAULT_TIMEOUT = 5.0
USER_AGENT = "cme-mdp3-sim/1.0 (local market-data simulator; stdlib urllib)"


@dataclass
class PriorClose:
    """The previous completed session's close for one symbol."""
    price: float
    date: str                    # exchange-local session date, YYYY-MM-DD
    symbol: str
    source: str                  # 'yahoo' | 'cache'
    stale: bool = False          # served from cache after a skipped/failed fetch

    def describe(self) -> str:
        via = self.source + (", stale" if self.stale else "")
        return f"{self.symbol} {self.date} close {self.price:.2f} ({via})"


# ---------------------------------------------------------------- network ----
def _local_date(epoch_s: float, gmtoffset: int) -> str:
    """Date at the exchange, from a UTC epoch and the venue's UTC offset."""
    return datetime.fromtimestamp(epoch_s + gmtoffset, timezone.utc).strftime("%Y-%m-%d")


def fetch_prior_close(symbol: str = DEFAULT_SYMBOL,
                      timeout: float = DEFAULT_TIMEOUT) -> PriorClose:
    """Fetch the last completed daily bar. Raises on any failure."""
    req = urllib.request.Request(CHART_URL.format(symbol=urllib.parse.quote(symbol)),
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)
    return parse_chart_payload(payload, symbol)


def parse_chart_payload(payload: dict, symbol: str = DEFAULT_SYMBOL,
                        now: float | None = None) -> PriorClose:
    """Pull the most recent *completed* session close out of a chart payload.

    The final bar is the session in progress, so we walk backwards to the first
    bar whose exchange-local date is strictly before today at the exchange.

    The price is returned raw: snapping it to a tick grid happens in
    `resolve_start_band`, which knows the product. Rounding here would quantize
    every product to the ES tick — a Japanese Yen close of 0.0064 would become 0.
    """
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError("chart payload has no result")
    res = result[0]
    meta = res.get("meta") or {}
    gmtoffset = int(meta.get("gmtoffset") or 0)
    stamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    if not stamps or not closes:
        raise ValueError("chart payload has no daily bars")

    today = _local_date(time.time() if now is None else now, gmtoffset)
    for ts, close in zip(reversed(stamps), reversed(closes)):
        if close is None or float(close) <= 0:     # empty or nonsensical bar
            continue
        session = _local_date(ts, gmtoffset)
        if session < today:
            return PriorClose(price=float(close), date=session,
                              symbol=meta.get("symbol") or symbol, source="yahoo")
    raise ValueError("no completed session found in the last 10 days of bars")


# ------------------------------------------------------------------ cache ----
def _load_cache_file(path: str) -> dict:
    """The whole cache, keyed by symbol. Any problem reads as an empty cache.

    A cache written by an older version is discarded rather than trusted: v1
    stored prices already snapped to the ES tick, which is wrong for every other
    product.
    """
    try:
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
        if blob.get("version") != CACHE_VERSION:
            return {}
        symbols = blob.get("symbols")
        return symbols if isinstance(symbols, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def _read_cache(path: str, symbol: str) -> tuple[PriorClose | None, float]:
    """Return (cached close for `symbol`, age in seconds)."""
    with _CACHE_LOCK:
        entry = _load_cache_file(path).get(symbol)
    if not isinstance(entry, dict):
        return None, float("inf")
    try:
        pc = PriorClose(price=float(entry["price"]), date=str(entry["date"]),
                        symbol=symbol, source="cache")
    except (KeyError, TypeError, ValueError):
        return None, float("inf")
    return pc, max(0.0, time.time() - float(entry.get("fetched_at", 0) or 0))


def _write_cache(path: str, pc: PriorClose) -> None:
    """Best-effort cache write; a read-only checkout must not break the run.

    One file holds every product, so switching products does not evict the
    close already fetched for the last one.
    """
    with _CACHE_LOCK:
        symbols = _load_cache_file(path)
        symbols[pc.symbol] = {"price": pc.price, "date": pc.date,
                              "fetched_at": time.time()}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"version": CACHE_VERSION, "symbols": symbols}, f)
        except OSError:
            pass


def prior_close(symbol: str = DEFAULT_SYMBOL, *, offline: bool = False,
                refresh: bool = False, timeout: float = DEFAULT_TIMEOUT,
                cache_path: str = CACHE_PATH) -> PriorClose | None:
    """Yesterday's close, from cache or the network. None if neither works.

    Never raises: a simulator with no internet still has to start.
    """
    cached, age = _read_cache(cache_path, symbol)
    if cached and not refresh and age < CACHE_TTL:
        return cached
    if offline:
        if cached:
            cached.stale = True
        return cached
    try:
        fresh = fetch_prior_close(symbol, timeout=timeout)
    except Exception:                              # network, DNS, JSON, shape…
        if cached:
            cached.stale = True
        return cached
    _write_cache(cache_path, fresh)
    return fresh


def prefetch(symbols, *, offline: bool = False, refresh: bool = False,
             timeout: float = DEFAULT_TIMEOUT, cache_path: str = CACHE_PATH,
             max_workers: int = MAX_FETCH_WORKERS) -> dict:
    """Warm the cache for several symbols at once.

    Each lookup is a blocking HTTPS round trip, so this is I/O-bound and threads
    help regardless of the GIL — 29 products drop from tens of seconds to about
    one. Results come back keyed by symbol, with None where nothing was found.
    """
    unique = list(dict.fromkeys(symbols))
    if not unique:
        return {}
    workers = max(1, min(max_workers, len(unique)))
    out: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers,
                                               thread_name_prefix="close") as pool:
        futures = {pool.submit(prior_close, sym, offline=offline, refresh=refresh,
                               timeout=timeout, cache_path=cache_path): sym
                   for sym in unique}
        for fut in concurrent.futures.as_completed(futures):
            sym = futures[fut]
            try:
                out[sym] = fut.result()
            except Exception:              # prior_close swallows its own errors
                out[sym] = None
    return out


# ----------------------------------------------------- start price + band ----
def band_width(tick: float = TICK, band_ticks: int = DEFAULT_BAND_TICKS) -> float:
    """Default band expressed in ticks, so it scales across products.

    100 ticks is 25 points of ES, 1 point of crude, 1.5625 of a 10-year note.
    """
    return abs(band_ticks) * tick


def band_around(price: float, width: float = None,
                tick: float = TICK) -> tuple[float, float]:
    """A tick-aligned [low, high] band of `width` centered on `price`."""
    width = band_width(tick) if width is None else width
    width = max(abs(width), 4 * tick)
    return round_to_tick(price - width / 2, tick), round_to_tick(price + width / 2, tick)


def resolve_start_band(start: float | None = None, low: float | None = None,
                       high: float | None = None, band: float | None = None, *,
                       product: str = "ES", tick: float | None = None,
                       symbol: str | None = None, offline: bool = False,
                       refresh: bool = False,
                       cache_path: str = CACHE_PATH) -> dict:
    """Work out where the walk starts and which band it runs in, for one product.

    `start` wins if given; otherwise the product's prior close is used; offline
    with no cache it falls back to the product's rough reference level. An
    explicit `low`/`high` always wins over the derived band; whatever is left is
    filled in around the start price. Returns the resolved start/low/high plus
    provenance for display.
    """
    spec: ContractSpec = get_spec(product)
    tick = spec.tick if tick is None else tick
    symbol = symbol or spec.yahoo
    info = {"start": None, "low": low, "high": high, "source": "center",
            "closeDate": None, "symbol": symbol, "stale": False,
            "clamped": False, "outOfBand": False,
            "product": spec.code, "tick": tick}

    if start is not None:
        info["start"] = round_to_tick(float(start), tick)
        info["source"] = "explicit"
    else:
        pc = prior_close(symbol, offline=offline, refresh=refresh,
                         cache_path=cache_path)
        if pc is not None:
            info.update(start=round_to_tick(pc.price, tick), source="priorClose",
                        closeDate=pc.date, symbol=pc.symbol, stale=pc.stale)
        elif spec.ref_price:
            # no network and nothing cached: a rough anchor, clearly labelled
            info.update(start=round_to_tick(spec.ref_price, tick), source="fallback")

    px, lo, hi = info["start"], low, high
    if lo is None and hi is None:
        if px is None:
            raise ValueError(
                f"no starting price for {spec.code}: pass --start, or --low/--high")
        lo, hi = band_around(px, band, tick)
    elif lo is None:
        lo = round_to_tick(hi - abs(band if band is not None else band_width(tick)), tick)
    elif hi is None:
        hi = round_to_tick(lo + abs(band if band is not None else band_width(tick)), tick)
    lo, hi = round_to_tick(min(lo, hi), tick), round_to_tick(max(lo, hi), tick)
    if hi - lo < 4 * tick:                         # keep room for a 1-tick market
        hi = round_to_tick(lo + 4 * tick, tick)

    if px is not None and not lo <= px <= hi - tick:
        if info["source"] == "explicit":
            # An explicit start price is a deliberate choice: honour it as far as
            # the band allows and say that it moved.
            info.update(start=round_to_tick(min(max(px, lo), hi - tick), tick),
                        clamped=True)
        else:
            # The inferred price sits outside a band the caller chose explicitly.
            # Pinning the open to the band edge would misrepresent both, so fall
            # back to opening at the center, as before there was a start price.
            info.update(start=None, source="center", outOfBand=True)
    info["low"], info["high"] = lo, hi
    return info


def main():
    ap = argparse.ArgumentParser(description="Look up the prior session's close")
    ap.add_argument("--product", default=None, help="CME product code (e.g. ES, CL, GC)")
    ap.add_argument("--symbol", default=None, help="Yahoo symbol (default: the product's)")
    ap.add_argument("--offline", action="store_true", help="use the cache only")
    ap.add_argument("--refresh", action="store_true", help="ignore a fresh cache")
    ap.add_argument("--json", action="store_true", help="print JSON")
    ap.add_argument("--all", action="store_true",
                    help="look up every product's close, concurrently")
    args = ap.parse_args()

    if args.all:
        from contracts import SPECS
        t0 = time.time()
        got = prefetch([sp.yahoo for sp in SPECS.values()],
                       offline=args.offline, refresh=args.refresh)
        if args.json:
            print(json.dumps({sym: (None if pc is None else
                                    {"price": pc.price, "date": pc.date,
                                     "source": pc.source, "stale": pc.stale})
                              for sym, pc in got.items()}, indent=2))
            return
        print(f"{'code':<5} {'symbol':<7} {'close':>16} {'date':<12} source")
        for code, sp in SPECS.items():
            pc = got.get(sp.yahoo)
            if pc is None:
                print(f"{code:<5} {sp.yahoo:<7} {'—':>16} {'—':<12} unavailable")
            else:
                src = pc.source + (" (stale)" if pc.stale else "")
                print(f"{code:<5} {sp.yahoo:<7} {pc.price:>16.7g} {pc.date:<12} {src}")
        ok = sum(1 for pc in got.values() if pc is not None)
        print(f"\n{ok}/{len(got)} symbols in {time.time() - t0:.2f}s "
              f"(up to {MAX_FETCH_WORKERS} concurrent lookups)")
        return

    spec = get_spec(args.product) if args.product else None
    symbol = args.symbol or (spec.yahoo if spec else DEFAULT_SYMBOL)
    tick = spec.tick if spec else TICK
    pc = prior_close(symbol, offline=args.offline, refresh=args.refresh)
    if pc is None:
        raise SystemExit(f"no prior close available for {symbol} "
                         f"(no network and no cache)")
    if args.json:
        print(json.dumps({"price": pc.price, "date": pc.date, "symbol": pc.symbol,
                          "source": pc.source, "stale": pc.stale}))
    else:
        lo, hi = band_around(pc.price, tick=tick)
        print(pc.describe())
        print(f"  suggested band: [{lo}, {hi}]")


if __name__ == "__main__":
    main()
