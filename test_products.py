"""
Tests for the product chooser: contract specs, front-month dates, and parsing
CME's config.xml (both the real file and the bundled extract).

Run: python3 test_products.py
"""
import os
import tempfile
from datetime import date

from contracts import (DEFAULT_PRODUCT, SPECS, contract_symbol, expiry_date,
                       front_month, get_spec, month_code)
from engine import MarketEngine
from products import (FULL_CONFIG, SAMPLE_CONFIG, default_config_path,
                      get_product, is_futures_channel, load_products,
                      product_catalog, simulatable)
from pyver import require_python

require_python()

SAMPLE = SAMPLE_CONFIG


# ------------------------------------------------------------------ symbols ----
def test_month_codes():
    assert month_code(1) == "F" and month_code(12) == "Z" and month_code(6) == "M"
    assert contract_symbol("ES", 2026, 12) == "ESZ6"
    assert contract_symbol("CL", 2027, 3) == "CLH7"
    print("ok  month codes and Globex symbols (ESZ6, CLH7)")


# ------------------------------------------------------------------ expiry ----
def test_equity_index_expiry_is_the_third_friday():
    assert expiry_date(get_spec("ES"), 2026, 12) == date(2026, 12, 18)
    assert expiry_date(get_spec("ES"), 2027, 3) == date(2027, 3, 19)
    assert expiry_date(get_spec("NQ"), 2026, 9) == date(2026, 9, 18)
    print("ok  equity index expires on the third Friday")


def test_expiries_land_on_business_days():
    for code, spec in SPECS.items():
        for month in spec.cycle:
            d = expiry_date(spec, 2027, month)
            assert d.weekday() < 5, (code, month, d)
    print("ok  every product's computed expiry lands on a weekday")


def test_front_month_rolls_the_day_after_expiry():
    # ES Dec 2026 stops trading 2026-12-18
    assert front_month("ES", date(2026, 12, 17)).symbol == "ESZ6"
    assert front_month("ES", date(2026, 12, 18)).symbol == "ESZ6"   # expiry day
    assert front_month("ES", date(2026, 12, 19)).symbol == "ESH7"   # rolled
    print("ok  the front month rolls the day after last trade, not before")


def test_front_month_is_always_ahead():
    today = date(2026, 9, 24)
    for code in SPECS:
        fm = front_month(code, today)
        assert fm.expiry >= today, (code, fm)
        assert fm.month in get_spec(code).cycle, (code, fm)
        assert fm.symbol.startswith(code), fm
    print(f"ok  all {len(SPECS)} products resolve a front month on or after today")


def test_crude_front_month_leads_the_delivery_month():
    # CL stops trading ~3 business days before the 25th of the PRIOR month, so
    # late September is already trading November crude.
    fm = front_month("CL", date(2026, 9, 24))
    assert fm.symbol == "CLX6" and fm.month == 11, fm
    print(f"ok  crude leads its delivery month: {fm.label}")


# ------------------------------------------------------------------ config ----
def test_futures_channels_only():
    assert is_futures_channel("CME Globex Equity Futures - E-mini S&P 500 futures")
    assert not is_futures_channel("CME Globex Equity Options - E-mini S&P 500 outrights")
    assert not is_futures_channel("")
    print("ok  options channels are excluded, futures channels kept")


def test_es_resolves_to_the_futures_channel():
    # ES is listed on channel 310 (futures) and on two options channels
    p = get_product("ES", SAMPLE)
    assert p.channel_id == 310, p
    assert "Options" not in p.channel_label
    feed = p.incremental
    assert (feed.ip, feed.port) == ("224.0.31.1", 14310), feed
    assert feed.type == "Incremental" and feed.feed_id == "A"
    print(f"ok  ES -> channel 310, {feed}")


def test_every_spec_product_is_in_the_config():
    have = {p.code for p in load_products(SAMPLE)}
    missing = sorted(set(SPECS) - have)
    assert not missing, f"not in config.xml: {missing}"
    print(f"ok  all {len(SPECS)} spec'd products are present in the config")


def test_products_are_deduped_across_channels():
    codes = [p.code for p in load_products(SAMPLE)]
    assert len(codes) == len(set(codes)), "duplicate product codes"
    print("ok  a product listed on several channels appears once")


def test_catalog_shape():
    cat = product_catalog(SAMPLE)
    assert {c["code"] for c in cat} == set(SPECS)
    es = next(c for c in cat if c["code"] == "ES")
    assert es["label"] == "E-mini S&P 500" and es["group"] == "Equity"
    assert es["symbol"].startswith("ES") and es["channel"] == 310
    assert cat[0]["group"] == "Equity", "equity should lead the chooser"
    print(f"ok  catalog exposes {len(cat)} products for the chooser")


def test_unknown_product_is_a_clear_error():
    try:
        get_product("NOPE", SAMPLE)
    except ValueError as e:
        assert "unknown product" in str(e) and "ES" in str(e), e
        print("ok  an unknown product lists what is available")
        return
    raise AssertionError("should have raised")


def test_parser_accepts_the_attribute_form():
    """CME nests fields; tolerate the flatter attribute form too."""
    xml = """<?xml version="1.0"?>
    <configuration>
      <channel id="999" label="Test Globex Futures">
        <products><product code="ES" group="Equity"/></products>
        <connections>
          <connection id="999IA" type="Incremental" feed-id="A"
                      ip="224.0.99.1" port="14999" protocol="UDP/IP"/>
        </connections>
      </channel>
    </configuration>"""
    path = os.path.join(tempfile.mkdtemp(), "alt.xml")
    open(path, "w").write(xml)
    p = get_product("ES", path)
    assert p.channel_id == 999 and p.incremental.ip == "224.0.99.1", p
    assert p.incremental.feed_id == "A" and p.incremental.port == 14999
    print("ok  attribute-style config parses as well as CME's nested form")


def test_tcp_connections_are_skipped():
    """Historical replay entries have a port but no multicast ip."""
    for p in simulatable(SAMPLE):
        for f in p.feeds:
            assert f.ip, f
    print("ok  non-multicast connections are filtered out")


def test_real_cme_config_if_downloaded():
    """When CME's full config.xml is present it is the default — check it works."""
    if not os.path.exists(FULL_CONFIG):
        print("ok  (skipped) CME's full config.xml not downloaded")
        return
    assert default_config_path() == FULL_CONFIG
    full = simulatable(FULL_CONFIG)
    assert {p.code for p in full} == set(SPECS), "spec products missing from CME config"
    es = get_product("ES", FULL_CONFIG)
    assert es.channel_id == 310 and es.incremental.ip == "224.0.31.1", es
    # the real file lists thousands of products across futures and options
    assert len(load_products(FULL_CONFIG)) > len(load_products(SAMPLE))
    print(f"ok  CME's real config.xml parses: {len(load_products(FULL_CONFIG))} "
          f"futures products, {len(full)} simulatable")


# ------------------------------------------------------------------ engine ----
def test_engine_honours_each_product_tick():
    for code in ("ES", "CL", "ZN", "6J", "ZB"):
        spec = get_spec(code)
        px = spec.ref_price
        eng = MarketEngine(px - 50 * spec.tick, px + 50 * spec.tick, start=px,
                           tick=spec.tick, tick_value=spec.tick_value, seed=3)
        assert eng.tick == spec.tick
        assert abs(eng.best_bid_px - px) < spec.tick, (code, eng.best_bid_px, px)
        incs, _ = eng.step()
        for inc in incs:                       # every price stays on the grid
            n = inc.price / spec.tick
            assert abs(n - round(n)) < 1e-6, (code, inc.price)
    print("ok  the engine quantizes to each product's own tick grid")


def test_default_product_is_es():
    assert DEFAULT_PRODUCT == "ES"
    assert get_product(None, SAMPLE).code == "ES"
    print("ok  the default product is still ES")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL PASS — product chooser, front months, and config parsing.")
