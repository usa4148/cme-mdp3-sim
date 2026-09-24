"""
Wire-correctness tests: encode -> raw bytes -> decode, and full packet framing.
Run: python test_roundtrip.py
"""
import os
from sbe import Schema
from packet import build_packet, parse_packet
from pyver import require_python

require_python()

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema", "mdp3.xml")
schema = Schema(SCHEMA_PATH)


def test_header_layout():
    # SBE message header must be 8 bytes: blockLength,templateId,schemaId,version
    assert schema.header.wire_size == 8
    # CME group dimension must be 3 bytes: blockLength(u16)+numInGroup(u8)
    assert schema.group_dim.wire_size == 3
    print("ok  header=8B  groupDim=3B")


def test_price_scaling():
    # 5000.25 with exponent -7 -> mantissa 50002500000
    msg = schema.encode_message(
        "MDIncrementalRefreshBook",
        {"transactTime": 111, "matchEventIndicator": ["EndOfEvent"]},
        [{"mdEntryPx": 5000.25, "mdEntrySize": 40, "securityId": 42003,
          "rptSeq": 1, "numberOfOrders": 5, "mdPriceLevel": 1,
          "mdUpdateAction": "New", "mdEntryType": "Bid"}])
    import struct
    # header(8) + block(11) + dim(3) + entry offset 0 = mantissa int64
    mant_off = 8 + 11 + 3
    (mant,) = struct.unpack_from("<q", msg, mant_off)
    assert mant == 50002500000, mant
    print(f"ok  price 5000.25 -> mantissa {mant} (exp -7)")


def test_book_roundtrip():
    entries = [
        {"mdEntryPx": 5001.00, "mdEntrySize": 40, "securityId": 42003, "rptSeq": 10,
         "numberOfOrders": 5, "mdPriceLevel": 1, "mdUpdateAction": "New", "mdEntryType": "Bid"},
        {"mdEntryPx": 5001.50, "mdEntrySize": 33, "securityId": 42003, "rptSeq": 11,
         "numberOfOrders": 4, "mdPriceLevel": 1, "mdUpdateAction": "Change", "mdEntryType": "Offer"},
        {"mdEntryPx": 4999.75, "mdEntrySize": 12, "securityId": 42003, "rptSeq": 12,
         "numberOfOrders": 2, "mdPriceLevel": 3, "mdUpdateAction": "Delete", "mdEntryType": "Bid"},
    ]
    root = {"transactTime": 1_700_000_000_000_000_000,
            "matchEventIndicator": ["LastQuoteMsg", "EndOfEvent"]}
    msg = schema.encode_message("MDIncrementalRefreshBook", root, entries)
    dec, end = schema.decode_message(msg)
    assert end == len(msg), (end, len(msg))
    assert dec["template"] == "MDIncrementalRefreshBook"
    assert dec["root"]["transactTime"] == root["transactTime"]
    assert set(dec["root"]["matchEventIndicator"]) == set(root["matchEventIndicator"])
    got = dec["groups"]["MDEntries"]
    assert len(got) == 3
    for orig, back in zip(entries, got):
        assert abs(back["mdEntryPx"] - orig["mdEntryPx"]) < 1e-9, (orig, back)
        assert back["mdEntrySize"] == orig["mdEntrySize"]
        assert back["mdUpdateAction"] == orig["mdUpdateAction"]
        assert back["mdEntryType"] == orig["mdEntryType"]
        assert back["rptSeq"] == orig["rptSeq"]
    print(f"ok  book roundtrip: {len(entries)} entries, {len(msg)}B, exact match")


def test_trade_and_packet():
    trade = [{"mdEntryPx": 5000.25, "mdEntrySize": 7, "securityId": 42003, "rptSeq": 99,
              "numberOfOrders": 0, "aggressorSide": "Buy", "mdUpdateAction": "New",
              "mdEntryType": "Trade"}]
    m1 = schema.encode_message("MDIncrementalRefreshBook",
                               {"transactTime": 1, "matchEventIndicator": ["EndOfEvent"]},
                               [{"mdEntryPx": 5000.0, "mdEntrySize": 10, "securityId": 42003,
                                 "rptSeq": 1, "numberOfOrders": 1, "mdPriceLevel": 1,
                                 "mdUpdateAction": "New", "mdEntryType": "Bid"}])
    m2 = schema.encode_message("MDIncrementalRefreshTradeSummary",
                               {"transactTime": 2, "matchEventIndicator": ["LastTradeMsg"]},
                               trade)
    pkt = build_packet(7, 123456789, [m1, m2])
    seq, ts, frames = parse_packet(pkt)
    assert seq == 7 and ts == 123456789 and len(frames) == 2
    d1, _ = schema.decode_message(frames[0])
    d2, _ = schema.decode_message(frames[1])
    assert d1["template"] == "MDIncrementalRefreshBook"
    assert d2["template"] == "MDIncrementalRefreshTradeSummary"
    t = d2["groups"]["MDEntries"][0]
    assert t["mdEntryType"] == "Trade" and t["aggressorSide"] == "Buy" and t["mdEntrySize"] == 7
    print(f"ok  packet: 2 msgs framed+decoded, seq={seq}, {len(pkt)}B")


if __name__ == "__main__":
    test_header_layout()
    test_price_scaling()
    test_book_roundtrip()
    test_trade_and_packet()
    print("\nALL PASS — wire encode/decode is self-consistent.")
