"""
Schema-neutral wire tests: the canonical vocabulary encoded with CME's official
templates_FixBinary.xml and with the working subset, translation of names and
enum values, mismatch detection, auto-detection, and the inspector's byte map.

Run: python test_wire.py
"""
import os

from packet import parse_packet
from sbe import Schema
from wire import AutoWire, SchemaMismatch, available, open_wire
from pyver import require_python

require_python()

HERE = os.path.dirname(os.path.abspath(__file__))
HAVE_CME = "cme" in available()

BOOK = [{"mdEntryPx": 5000.25, "mdEntrySize": 40, "securityId": 42003, "rptSeq": 7,
         "numberOfOrders": 5, "mdPriceLevel": 1, "mdUpdateAction": "Change",
         "mdEntryType": "Offer"}]
ROOT = {"transactTime": 1_787_000_000_000_000_123,
        "matchEventIndicator": ["LastQuoteMsg", "EndOfEvent"]}
TRADE = [{"mdEntryPx": 5000.5, "mdEntrySize": 3, "securityId": 42003, "rptSeq": 8,
          "numberOfOrders": 2, "aggressorSide": "Sell", "mdUpdateAction": "New",
          "mdEntryType": "Trade"}]


def _skip(name):
    print(f"skip  {name}: CME templates_FixBinary.xml not present")


def test_cme_bytes_are_cmes_layout():
    if not HAVE_CME:
        return _skip("cme layout")
    w = open_wire("cme")
    raw = Schema(os.path.join(HERE, "schema", "templates_FixBinary.xml"))
    msg = w.encode("MDIncrementalRefreshBook", ROOT, BOOK)
    d, end = raw.decode_message(msg)
    assert end == len(msg) and d["template"] == "MDIncrementalRefreshBook46"
    e = d["groups"]["NoMDEntries"][0]
    assert e["MDEntryPx"] == 5000.25 and e["SecurityID"] == 42003 and e["RptSeq"] == 7
    assert e["MDUpdateAction"] == "Change" and e["MDEntryType"] == "Offer"
    assert e["TradeableSize"] is None, "a field only CME has must be null, not 0"
    assert d["groups"]["NoOrderIDEntries"] == []
    # CME prices at exponent -9: 5000.25 -> 5000250000000
    assert int.from_bytes(msg[8 + 11 + 3:8 + 11 + 3 + 8], "little", signed=True) == 5_000_250_000_000
    print(f"ok  cme: a canonical book entry encodes as CME's own 46 ({len(msg)}B, exponent -9, "
          f"empty order-id group)")


def test_roundtrip_both_schemas_same_canonical():
    for name in available():
        w = open_wire(name)
        for tpl, entries in (("MDIncrementalRefreshBook", BOOK),
                             ("MDIncrementalRefreshTradeSummary", TRADE)):
            d = w.decode(w.encode(tpl, ROOT, entries))
            assert d["template"] == tpl
            assert d["root"]["matchEventIndicator"] == ROOT["matchEventIndicator"]
            got = d["groups"]["MDEntries"][0]
            for k, v in entries[0].items():
                assert got[k] == v, (name, tpl, k, got[k], v)
    print(f"ok  {' and '.join(available())}: book and trade round-trip to identical "
          f"canonical messages")


def test_enum_translation_by_wire_value():
    """'TradingSessionHighPrice' in both now, but values map by raw char either way."""
    for name in available():
        w = open_wire(name)
        root = {"lastMsgSeqNumProcessed": 1, "totNumReports": 1, "securityId": 1,
                "rptSeq": 1, "transactTime": 1, "lastUpdateTime": 1, "tradeDate": 20000,
                "mdSecurityTradingStatus": "ReadyToTrade"}
        entries = [{"mdEntryPx": 1.5, "mdEntryType": "TradingSessionHighPrice"},
                   {"mdEntrySize": 99, "mdEntryType": "ElectronicVolume"},
                   {"mdEntryPx": 1.25, "mdEntryType": "SettlementPrice",
                    "settlPriceType": ["FinalDaily", "Actual"]}]
        d = w.decode(w.encode("SnapshotFullRefresh", root, entries))
        got = d["groups"]["MDEntries"]
        assert [e["mdEntryType"] for e in got] == ["TradingSessionHighPrice", "ElectronicVolume",
                                                   "SettlementPrice"], (name, got)
        assert got[2]["settlPriceType"] == ["FinalDaily", "Actual"]
        assert got[0]["mdEntrySize"] is None              # optional, absent -> null
        assert d["root"]["mdSecurityTradingStatus"] == "ReadyToTrade"
    print("ok  enums and sets map by wire value; absent optional fields decode as null")


def test_wrong_schema_is_detected():
    if not HAVE_CME:
        return _skip("mismatch")
    cme, sub = open_wire("cme"), open_wire("subset")
    c = cme.encode("MDIncrementalRefreshBook", ROOT, BOOK)
    s = sub.encode("MDIncrementalRefreshBook", ROOT, BOOK)
    for w, frame in ((sub, c), (cme, s)):
        try:
            w.decode(frame)
        except SchemaMismatch:
            continue
        raise AssertionError(f"{w.name} decoded the other schema's frame silently")
    print("ok  a 46 from one schema is refused by the other (length check), never misread")


def test_auto_detects_each_schema():
    if not HAVE_CME:
        return _skip("auto")
    for sender in ("cme", "subset"):
        frame = open_wire(sender).encode("MDIncrementalRefreshTradeSummary", ROOT, TRADE)
        a = open_wire("auto")
        assert isinstance(a, AutoWire)
        seen = []
        a.on_detect = seen.append
        d = a.decode(frame)
        assert a.detected == sender and seen == [sender]
        assert d["groups"]["MDEntries"][0]["aggressorSide"] == "Sell"
    print("ok  auto: settles on cme or subset from the first incremental frame")


def test_explain_covers_every_byte():
    for name in available():
        w = open_wire(name)
        for frame in (w.encode("MDIncrementalRefreshBook", ROOT, BOOK * 3),
                      w.encode("MDIncrementalRefreshTradeSummary", ROOT, TRADE)):
            segs = w.explain(frame)
            pos = 0
            for g in segs:
                assert g["off"] == pos, (name, g, pos)
                pos += g["len"]
            assert pos == len(frame), (name, pos, len(frame))
            assert segs[1]["value"] in (46, 48)
            px = next(g for g in segs if g["kind"] == "field" and g.get("canonical", g["field"]) == "mdEntryPx")
            assert px["value"] in (5000.25, 5000.5)
    print("ok  inspector map: contiguous, covers every byte, canonical names attached")


def test_simulator_defaults_to_cme_when_present():
    from wire import default_name
    old = os.environ.pop("MDP3_SCHEMA", None)
    try:
        assert default_name() == ("cme" if HAVE_CME else "subset")
        os.environ["MDP3_SCHEMA"] = "subset"
        assert default_name() == "subset"
    finally:
        os.environ.pop("MDP3_SCHEMA", None)
        if old is not None:
            os.environ["MDP3_SCHEMA"] = old
    print(f"ok  default schema: {'cme' if HAVE_CME else 'subset'}; $MDP3_SCHEMA overrides")


def test_packets_from_publisher_parse_under_their_schema():
    from channel import build_instruments, build_publishers

    class Tr:
        def __init__(self):
            self.pkts = []

        def send(self, feed, pkt, ts=None):
            self.pkts.append(pkt)

    for name in available():
        w, tr = open_wire(name), Tr()
        ins = build_instruments(["ES"], offline=True, seed=1, depth=5)
        (pub,) = build_publishers(ins, w, tr, depth=5)
        pub.definition_loop()
        pub.snapshot_loop()
        for _ in range(5):
            pub.step()
        n = 0
        for pkt in tr.pkts:
            for f in parse_packet(pkt)[2]:
                w.decode(f)                  # raises on any byte out of place
                n += 1
        assert n > 10
    print("ok  every frame the publisher emits decodes exactly under its schema")


if __name__ == "__main__":
    test_cme_bytes_are_cmes_layout()
    test_roundtrip_both_schemas_same_canonical()
    test_enum_translation_by_wire_value()
    test_wrong_schema_is_detected()
    test_auto_detects_each_schema()
    test_explain_covers_every_byte()
    test_simulator_defaults_to_cme_when_present()
    test_packets_from_publisher_parse_under_their_schema()
    print("\nALL PASS — one vocabulary, either schema on the wire.")
