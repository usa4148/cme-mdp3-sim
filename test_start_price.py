"""
Tests for the opening price: engine start, band derivation, and the prior-close
lookup (parsing + cache). No network — the chart payload is a fixture.

Run: python3 test_start_price.py
"""
import json
import os
import tempfile
from datetime import datetime, timezone

from engine import TICK, MarketEngine
from record_session import build_session
import threading

from contracts import get_spec
from settlement import (PriorClose, band_around, parse_chart_payload, prefetch,
                        prior_close, resolve_start_band, _load_cache_file,
                        _write_cache)
from pyver import require_python

require_python()

MISSING_CACHE = os.path.join(tempfile.gettempdir(), "cme-mdp3-sim-no-such-cache.json")


def _epoch(y, m, d, hh=4):
    return datetime(y, m, d, hh, tzinfo=timezone.utc).timestamp()


# ------------------------------------------------------------------ engine ----
def test_engine_defaults_to_center():
    # unchanged behaviour when no start price is given
    eng = MarketEngine(5000, 5025)
    assert eng.start is None
    assert eng.best_bid_px == eng.center - TICK, eng.best_bid_px
    print("ok  no start price -> book still opens straddling the band center")


def test_engine_opens_at_start():
    eng = MarketEngine(7760, 7785, start=7772.50)
    assert eng.start == 7772.50
    assert eng.best_bid_px == 7772.50, eng.best_bid_px
    assert eng.mid == 7772.625, eng.mid          # 1-tick market: mid is half a tick up
    assert eng.open_bid_px == 7772.50
    print(f"ok  opens at 7772.50 -> best bid 7772.50 / offer 7772.75, mid {eng.mid}")


def test_engine_start_snaps_to_tick():
    assert MarketEngine(7760, 7785, start=7772.40).start == 7772.50
    assert MarketEngine(7760, 7785, start=7772.60).start == 7772.50
    print("ok  start price snaps to the 0.25 tick grid")


def test_engine_start_clamped_into_band():
    hi = MarketEngine(5000, 5025, start=9999)
    assert hi.best_bid_px == 5025 - TICK, hi.best_bid_px
    lo = MarketEngine(5000, 5025, start=1)
    assert lo.best_bid_px == 5000, lo.best_bid_px
    print("ok  a start price outside the band is clamped into it")


# -------------------------------------------------------------------- band ----
def test_band_around():
    assert band_around(7772.50, 25) == (7760.0, 7785.0)
    assert band_around(7772.50, 40) == (7752.5, 7792.5)
    assert band_around(92.16, tick=0.01) == (91.66, 92.66)       # crude, 100 ticks
    lo, hi = band_around(7772.50, 0)             # degenerate width still leaves room
    assert hi - lo >= 4 * TICK, (lo, hi)
    print("ok  band_around centers a tick-aligned band on the price")


def test_resolve_explicit_start_derives_band():
    got = resolve_start_band(start=7772.50, band=25, offline=True,
                             cache_path=MISSING_CACHE, product="ES")
    assert got["source"] == "explicit"
    assert (got["start"], got["low"], got["high"]) == (7772.50, 7760.0, 7785.0), got
    print("ok  explicit start derives a 25-point band around itself")


def test_resolve_explicit_bounds_win():
    got = resolve_start_band(start=7772.50, low=7700, high=7800, band=25,
                             offline=True, cache_path=MISSING_CACHE, product="ES")
    assert (got["low"], got["high"]) == (7700.0, 7800.0), got
    assert got["start"] == 7772.50 and not got["clamped"]
    print("ok  explicit low/high override the derived band")


def test_resolve_one_bound_fills_the_other():
    got = resolve_start_band(start=7772.50, low=7750, band=30, offline=True,
                             cache_path=MISSING_CACHE, product="ES")
    assert (got["low"], got["high"]) == (7750.0, 7780.0), got
    print("ok  one bound plus --band fills in the other")


def test_resolve_clamps_start_into_explicit_band():
    got = resolve_start_band(start=9999, low=5000, high=5025, offline=True,
                             cache_path=MISSING_CACHE, product="ES")
    assert got["clamped"] is True
    assert got["start"] == 5025 - TICK, got
    print("ok  a start outside an explicit band is clamped and flagged")


def test_resolve_prior_close_outside_explicit_band_uses_center():
    # a prior close IS available here (7772.50, cached) but the caller asked for
    # a band it cannot sit in: pinning the open to the band edge would
    # misrepresent both, so fall back to the old center-open behaviour
    path = os.path.join(tempfile.mkdtemp(), "cache.json")
    _write_cache(path, PriorClose(price=7772.50, date="2026-09-23",
                                  symbol="ES=F", source="yahoo"))
    assert prior_close(offline=True, cache_path=path).price == 7772.50   # available
    got = resolve_start_band(low=5000, high=5025, offline=True, cache_path=path,
                             product="ES")
    assert got["start"] is None and got["source"] == "center", got
    assert got["outOfBand"] is True and got["clamped"] is False, got
    # ...while an explicit start in the same situation is clamped, not dropped
    exp = resolve_start_band(start=7772.50, low=5000, high=5025, offline=True,
                             cache_path=path, product="ES")
    assert exp["start"] == 5025 - TICK and exp["clamped"] is True, exp
    print("ok  a prior close outside an explicit band opens at center; "
          "an explicit start clamps")


def test_resolve_without_data_uses_the_reference_level():
    got = resolve_start_band(offline=True, cache_path=MISSING_CACHE, product="CL")
    spec = get_spec("CL")
    assert got["source"] == "fallback", got
    assert got["start"] == spec.ref_price, got
    # the band is 100 ticks of CRUDE, not of ES
    assert round(got["high"] - got["low"], 10) == 100 * spec.tick, got
    print("ok  offline with no cache opens at the product's reference level")


def test_band_scales_with_the_product_tick():
    widths = {}
    for code in ("ES", "CL", "ZN", "6J"):
        got = resolve_start_band(start=get_spec(code).ref_price, product=code)
        widths[code] = round(got["high"] - got["low"], 10)
        assert widths[code] == round(100 * get_spec(code).tick, 10), (code, got)
    assert widths["ES"] == 25.0                    # unchanged from before
    print(f"ok  the default band is 100 ticks per product: {widths}")


# --------------------------------------------------------------- settlement ----
def test_parse_chart_payload_picks_last_completed_session():
    payload = {"chart": {"result": [{
        "meta": {"symbol": "ES=F", "gmtoffset": -14400},
        "timestamp": [_epoch(2026, 9, 21), _epoch(2026, 9, 22),
                      _epoch(2026, 9, 23), _epoch(2026, 9, 24)],
        "indicators": {"quote": [{"close": [7833.5, 7831.75, 7772.53, 7759.5]}]},
    }]}}
    # "now" is mid-session on the 24th, so the 24th's bar is still in progress
    pc = parse_chart_payload(payload, now=_epoch(2026, 9, 24, 18))
    assert pc.date == "2026-09-23", pc.date
    # the raw close is kept; snapping to a tick grid happens per product later
    assert pc.price == 7772.53, pc.price
    assert pc.symbol == "ES=F" and pc.source == "yahoo" and not pc.stale
    print(f"ok  parses the last completed session: {pc.describe()}")


def test_parse_chart_payload_does_not_round_to_the_es_tick():
    """Regression: rounding at fetch time quantized every product to 0.25."""
    payload = {"chart": {"result": [{
        "meta": {"symbol": "6J=F", "gmtoffset": -14400},
        "timestamp": [_epoch(2026, 9, 23), _epoch(2026, 9, 24)],
        "indicators": {"quote": [{"close": [0.006332, 0.006340]}]},
    }]}}
    pc = parse_chart_payload(payload, "6J=F", now=_epoch(2026, 9, 24, 18))
    assert pc.price == 0.006332, pc.price         # not 0.0
    # and the per-product snap happens on the way into the band
    got = resolve_start_band(start=pc.price, product="6J")
    assert got["start"] == 0.006332 and got["tick"] == 5e-07, got
    print("ok  a Yen close survives intact (0.006332, not rounded to the ES tick)")


def test_parse_chart_payload_skips_nonpositive_closes():
    payload = {"chart": {"result": [{
        "meta": {"symbol": "ES=F", "gmtoffset": -14400},
        "timestamp": [_epoch(2026, 9, 22), _epoch(2026, 9, 23), _epoch(2026, 9, 24)],
        "indicators": {"quote": [{"close": [7831.75, 0.0, 7759.5]}]},
    }]}}
    pc = parse_chart_payload(payload, now=_epoch(2026, 9, 24, 18))
    assert pc.date == "2026-09-22" and pc.price == 7831.75, pc
    print("ok  a zero close is treated as an empty bar")


def test_parse_chart_payload_skips_empty_bars():
    payload = {"chart": {"result": [{
        "meta": {"symbol": "ES=F", "gmtoffset": -14400},
        "timestamp": [_epoch(2026, 9, 22), _epoch(2026, 9, 23), _epoch(2026, 9, 24)],
        "indicators": {"quote": [{"close": [7831.75, None, 7759.5]}]},
    }]}}
    pc = parse_chart_payload(payload, now=_epoch(2026, 9, 24, 18))
    assert pc.date == "2026-09-22" and pc.price == 7831.75, pc
    print("ok  a bar with no close is skipped")


def test_parse_chart_payload_rejects_junk():
    for bad in ({}, {"chart": {"result": []}},
                {"chart": {"result": [{"meta": {}, "timestamp": [], "indicators": {}}]}}):
        try:
            parse_chart_payload(bad)
        except ValueError:
            continue
        raise AssertionError(f"should have raised on {bad}")
    print("ok  malformed payloads raise ValueError")


def test_cache_is_reused_offline_and_flagged_stale():
    path = os.path.join(tempfile.mkdtemp(), "cache.json")
    _write_cache(path, PriorClose(price=7772.50, date="2026-09-23",
                                  symbol="ES=F", source="yahoo"))
    pc = prior_close(offline=True, refresh=True, cache_path=path)
    assert pc is not None and pc.price == 7772.50
    assert pc.source == "cache" and pc.stale is True
    # a symbol we have not fetched is a miss, not the wrong product's price
    assert prior_close("NQ=F", offline=True, refresh=True, cache_path=path) is None
    print("ok  offline reuse of the cache works and is flagged stale")


def test_cache_holds_several_products_at_once():
    path = os.path.join(tempfile.mkdtemp(), "cache.json")
    _write_cache(path, PriorClose(7772.50, "2026-09-23", "ES=F", "yahoo"))
    _write_cache(path, PriorClose(92.16, "2026-09-23", "CL=F", "yahoo"))
    es = prior_close("ES=F", offline=True, refresh=True, cache_path=path)
    cl = prior_close("CL=F", offline=True, refresh=True, cache_path=path)
    assert es.price == 7772.50 and cl.price == 92.16, (es, cl)
    print("ok  one cache file holds every product; switching does not evict")


def test_stale_format_cache_is_discarded():
    """A v1 cache stored ES-tick-rounded prices — must not be trusted."""
    path = os.path.join(tempfile.mkdtemp(), "cache.json")
    with open(path, "w") as f:
        json.dump({"symbols": {"6J=F": {"price": 0.0, "date": "2026-09-23",
                                        "fetched_at": 9e9}}}, f)   # no version
    assert prior_close("6J=F", offline=True, cache_path=path) is None
    print("ok  a cache from an older format version is ignored")


def test_prior_close_offline_without_cache_is_none():
    assert prior_close(offline=True, cache_path=MISSING_CACHE) is None
    print("ok  offline with no cache returns None instead of raising")


# --------------------------------------------------------------- concurrency ----
def test_prefetch_returns_every_symbol():
    """Offline prefetch against a seeded cache — no network, all symbols back."""
    path = os.path.join(tempfile.mkdtemp(), "cache.json")
    symbols = ["ES=F", "CL=F", "GC=F", "ZN=F", "6J=F"]
    for i, sym in enumerate(symbols):
        _write_cache(path, PriorClose(100.0 + i, "2026-09-23", sym, "yahoo"))
    got = prefetch(symbols, offline=True, refresh=True, cache_path=path)
    assert set(got) == set(symbols), got
    assert all(pc is not None for pc in got.values()), got
    assert got["GC=F"].price == 102.0, got["GC=F"]
    assert prefetch([], offline=True, cache_path=path) == {}
    # duplicates collapse to one lookup each
    assert set(prefetch(["ES=F", "ES=F"], offline=True, refresh=True,
                        cache_path=path)) == {"ES=F"}
    print(f"ok  prefetch returns all {len(symbols)} symbols offline")


def test_concurrent_cache_writes_lose_nothing():
    """The cache is read-modify-write; threads must not clobber each other."""
    path = os.path.join(tempfile.mkdtemp(), "cache.json")
    symbols = [f"T{i}=F" for i in range(40)]
    barrier = threading.Barrier(len(symbols))

    def writer(sym, price):
        barrier.wait()                      # maximize the overlap
        _write_cache(path, PriorClose(price, "2026-09-23", sym, "yahoo"))

    threads = [threading.Thread(target=writer, args=(s, float(i)))
               for i, s in enumerate(symbols)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    stored = _load_cache_file(path)
    missing = sorted(set(symbols) - set(stored))
    assert not missing, f"{len(missing)} entries lost to a write race: {missing[:5]}"
    assert stored["T7=F"]["price"] == 7.0, stored["T7=F"]
    print(f"ok  {len(symbols)} concurrent cache writes all survived")


# ------------------------------------------------------------------ session ----
def test_session_meta_carries_the_opening_price():
    sess = build_session(start=7772.50, steps=25, offline=True)
    m = sess["meta"]
    assert m["product"] == "ES" and m["symbol"].startswith("ES")
    assert m["start"] == 7772.50 and m["startSource"] == "explicit"
    assert (m["low"], m["high"]) == (7760.0, 7785.0), m
    assert m["band"] == 25.0
    assert sess["frames"], "session produced no frames"
    for f in sess["frames"]:                      # the walk stays inside the band
        assert m["low"] <= f["mid"] <= m["high"], f
    print(f"ok  build_session opens at {m['start']} in band [{m['low']}, {m['high']}]")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL PASS — opening price resolves, clamps, and reaches the wire.")
