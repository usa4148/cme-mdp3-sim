"""
Multi-feed tests: the snapshot and definition messages, multi-product channels,
A/B arbitration, snapshot recovery and instrument definitions — the behavioural
ones under every schema present (CME's official file and the working subset).

Run: python test_feeds.py
"""
import os
import socket
import time

from channel import INCREMENTAL, INSTRUMENT, SNAPSHOT, ChannelPublisher, Instrument
from engine import MarketEngine
from feedhandler import LIVE, RECOVERING, FeedHandler, FeedSet
from packet import parse_packet
from products import by_channel, get_product, parse_product_list
from receiver import join_group
from sbe import Schema
from settlement import resolve_start_band
from wire import available, open_wire
from pyver import require_python

require_python()

HERE = os.path.dirname(os.path.abspath(__file__))
SUBSET = open_wire("subset").schema
OFFICIAL_PATH = os.path.join(HERE, "schema", "templates_FixBinary.xml")
WIRES = [open_wire(n) for n in available()]
WIRE = WIRES[0]                       # rebound per schema by __main__


class FakeTransport:
    """Records what the publisher sends instead of putting it on the wire."""

    def __init__(self):
        self.sent = []                     # (feed, pkt)

    def send(self, feed, pkt, ts_ns=None):
        self.sent.append((feed, pkt))
        return True

    def of(self, kind, feed_id=None):
        return [(f, p) for f, p in self.sent
                if f.type == kind and (feed_id is None or f.feed_id == feed_id)]


def make_channel(codes, seed=7):
    """A publisher for products that share one channel, plus its transport."""
    instruments = []
    for i, code in enumerate(codes):
        p = get_product(code)
        o = resolve_start_band(product=code, offline=True)
        eng = MarketEngine(o["low"], o["high"], depth=5, seed=seed + i, start=o["start"],
                           tick=p.spec.tick, tick_value=p.spec.tick_value)
        instruments.append(Instrument(p, eng, o, p.spec.security_id))
    tr = FakeTransport()
    return ChannelPublisher(instruments[0].product, instruments, WIRE, tr, depth=5), tr


def deliver(handler, sent, drop=lambda feed, seq: False):
    for feed, pkt in sent:
        seq = parse_packet(pkt)[0]
        if not drop(feed, seq):
            handler.on_datagram((feed.ip, feed.port), pkt, time.time_ns())


def new_handler(codes, **kw):
    events = []
    h = FeedHandler(WIRE, on_event=lambda k, t: events.append((k, t)), **kw)
    h.add_routes_for([get_product(c) for c in codes])
    return h, events


def assert_book_matches(ins_state, engine):
    got_b = {p: (lv.size, lv.orders) for p, lv in ins_state.book.bids.items()}
    got_o = {p: (lv.size, lv.orders) for p, lv in ins_state.book.offers.items()}
    want_b = {p: (lv.size, lv.orders) for p, lv in engine.bids.items()}
    want_o = {p: (lv.size, lv.orders) for p, lv in engine.offers.items()}
    assert got_b == want_b, (got_b, want_b)
    assert got_o == want_o, (got_o, want_o)


# ------------------------------------------------------------------ codec ----
def test_snapshot_and_definition_layout():
    m52, m54 = SUBSET.messages[52], SUBSET.messages[54]
    assert m52.block_length == 59 and m52.groups[0].block_length == 22
    assert m54.block_length == 224
    assert [g.block_length for g in m54.groups] == [9, 4, 4, 5]
    # constant fields take no wire space: securityIdSource sits between
    # securityId (55..59) and securityType (59)
    f = {x.name: x for x in m54.fields}
    assert f["securityIdSource"].size == 0 and f["securityType"].offset == 59
    print("ok  templates 52/54: block lengths, group sizes, constant fields")


def test_definition_roundtrip():
    pub, _ = make_channel(["ES"])
    root, groups = pub.instruments[0].definition(1, 5)
    msg = WIRE.encode("MDInstrumentDefinitionFuture", root, groups=groups)
    assert len(msg) == 8 + 224 + 3 + 2 * 9 + 3 + 4 + 3 + 4 + 3 + 5
    d = WIRE.decode(msg)                 # raises SchemaMismatch unless it fits exactly
    r = d["root"]
    assert r["symbol"] == get_product("ES").front_month().symbol
    assert r["securityId"] == 42003 and r["applId"] == 310 and r["asset"] == "ES"
    assert r["securityIdSource"] == "8"                       # constant from the schema
    assert r["minPriceIncrement"] == 0.25 and r["unitOfMeasureQty"] == 50.0
    assert r["maturityMonthYear"]["day"] is None              # optional, null on wire
    assert r["securityExchange"] == "XCME" and r["securityType"] == "FUT"
    assert [e["eventType"] for e in d["groups"]["Events"]] == ["Activation", "LastEligibleTradeDate"]
    assert d["groups"]["MDFeedTypes"] == [{"mdFeedType": "GBX", "marketDepth": 5}]
    assert "ElectronicMatchEligible" in d["groups"]["InstAttrib"][0]["instAttribValue"]
    assert d["groups"]["LotTypeRules"][0]["minLotSize"] == 1.0
    print(f"ok  [{WIRE.name}] instrument definition round trip: {len(msg)}B, 4 groups, "
          f"strings + nulls")


def test_snapshot_and_definition_decode_with_official_schema():
    """What either wire encodes for 52/54 must decode with CME's own file."""
    if not os.path.exists(OFFICIAL_PATH):
        print("skip  CME templates_FixBinary.xml not present")
        return
    official = Schema(OFFICIAL_PATH)
    pub, tr = make_channel(["CL"])
    for _ in range(5):
        pub.step()
    pub.snapshot_loop()
    pub.definition_loop()
    eng = pub.instruments[0].engine
    (_, snap), = tr.of(SNAPSHOT, "A")
    (_, defn), = tr.of(INSTRUMENT, "A")
    d52, _ = official.decode_message(parse_packet(snap)[2][0])
    assert d52["template"] == "SnapshotFullRefresh52"
    assert d52["root"]["SecurityID"] == 42401 and d52["root"]["RptSeq"] == eng.rpt_seq
    bids = [e for e in d52["groups"]["NoMDEntries"] if e["MDEntryType"] == "Bid"]
    assert bids[0]["MDEntryPx"] == eng.best_bid() and bids[0]["MDPriceLevel"] == 1
    d54, _ = official.decode_message(parse_packet(defn)[2][0])
    r = d54["root"]
    assert d54["template"] == "MDInstrumentDefinitionFuture54"
    assert r["Symbol"] == get_product("CL").front_month().symbol
    assert r["MinPriceIncrement"] == 0.01 and r["SecurityExchange"] == "XNYM"
    assert r["SecurityIDSource"] == "8"
    print(f"ok  [{WIRE.name}] templates 52 and 54 decode with CME's official "
          f"templates_FixBinary.xml")


# ---------------------------------------------------------- multi-product ----
def test_product_list():
    assert parse_product_list(None) == ["ES"]
    assert parse_product_list("es, nq,ES") == ["ES", "NQ"]
    assert parse_product_list(["ES,CL", "ZN"]) == ["ES", "CL", "ZN"]
    try:
        parse_product_list("ES,NQ,CL,ZN,GC")
    except ValueError as e:
        assert "at most 4" in str(e)
    else:
        raise AssertionError("five products accepted")
    groups = by_channel([get_product(c) for c in ("NQ", "ES", "RTY", "CL")])
    assert [p.code for p in groups[318]] == ["NQ", "RTY"]
    assert set(groups) == {310, 318, 382}
    print("ok  products: parsing, 4-product limit, NQ+RTY share channel 318")


def test_shared_channel_sequence_and_ab_identical():
    pub, tr = make_channel(["NQ", "RTY"])
    for _ in range(10):
        pub.step()
    a = tr.of(INCREMENTAL, "A")
    b = tr.of(INCREMENTAL, "B")
    assert [p for _, p in a] == [p for _, p in b], "A and B must carry identical bytes"
    assert a[0][0].ip != b[0][0].ip
    seqs = [parse_packet(p)[0] for _, p in a]
    assert seqs == list(range(1, len(seqs) + 1)), "one sequence per channel"
    sids = set()
    for _, p in a:
        for f in parse_packet(p)[2]:
            d = WIRE.decode(f)
            sids |= {e["securityId"] for e in d["groups"]["MDEntries"]}
    assert sids == {42004, 42005}
    print(f"ok  channel 318: NQ+RTY share seq 1..{seqs[-1]}; A and B byte-identical")


def test_snapshot_loop_semantics():
    pub, tr = make_channel(["NQ", "RTY"])
    for _ in range(3):
        pub.step()
    pub.snapshot_loop()
    pub.step()
    pub.snapshot_loop()
    snaps = tr.of(SNAPSHOT, "A")
    seqs = [parse_packet(p)[0] for _, p in snaps]
    assert seqs == [1, 2, 1, 2], f"snapshot seq must restart each loop: {seqs}"
    d = WIRE.decode(parse_packet(snaps[-1][1])[2][0])
    assert d["root"]["totNumReports"] == 2
    assert d["root"]["lastMsgSeqNumProcessed"] == pub.seq
    print("ok  snapshot loop: seq restarts per loop, totNumReports, lastMsgSeqNumProcessed")


# ------------------------------------------------------------ arbitration ----
def test_arbitration_covers_single_feed_loss():
    pub, tr = make_channel(["ES"])
    h, events = new_handler(["ES"])
    pub.definition_loop()
    for _ in range(200):
        pub.step()
    # A loses every 3rd packet, B every 5th; only multiples of 15 are lost on both
    deliver(h, tr.sent, drop=lambda f, s: f.type == INCREMENTAL and (
        (f.feed_id == "A" and s % 3 == 0) or (f.feed_id == "B" and s % 5 == 0)))
    h.tick(1 << 62)
    ch = h.channels[310]
    lost = len([s for s in range(1, pub.seq + 1) if s % 15 == 0])
    assert ch.arb.lost == lost, (ch.arb.lost, lost)
    only = ch.one_feed_only()
    assert only["B"] == len([s for s in range(1, pub.seq + 1) if s % 3 == 0 and s % 15])
    assert only["A"] == len([s for s in range(1, pub.seq + 1) if s % 5 == 0 and s % 15])
    assert ch.arb.accepted == pub.seq - lost
    print(f"ok  arbitration: {only['B']} covered by B, {only['A']} by A, "
          f"{lost} lost on both of {pub.seq}")


def test_arbitration_reorder_across_feeds():
    """Feed A misses a packet that feed B delivers late: hold, don't gap."""
    pub, tr = make_channel(["ES"])
    h, _ = new_handler(["ES"])
    for _ in range(6):
        pub.step()
    a = {parse_packet(p)[0]: (f, p) for f, p in tr.of(INCREMENTAL, "A")}
    b = {parse_packet(p)[0]: (f, p) for f, p in tr.of(INCREMENTAL, "B")}
    order = [a[1], a[2], a[4], a[5], b[3], b[4], a[6]]      # A lost 3; B's 3 is late
    deliver(h, order)
    ch = h.channels[310]
    assert ch.arb.lost == 0 and ch.arb.reordered == 2, ch.arb
    assert ch.last_seq == 6
    ins = ch.instruments[42003]
    assert ins.state == LIVE and ins.gaps == 0
    assert_book_matches(ins, pub.instruments[0].engine)
    print("ok  arbitration: out-of-order copy from B fills A's hole without a gap")


# --------------------------------------------------------------- recovery ----
def test_gap_recovers_from_snapshot():
    pub, tr = make_channel(["NQ", "RTY"])
    h, events = new_handler(["NQ", "RTY"])
    for _ in range(20):
        pub.step()
    lost_seq = 9
    deliver(h, tr.sent, drop=lambda f, s: f.type == INCREMENTAL and s == lost_seq)
    h.tick(1 << 62)                 # the hold expires: neither feed had packet 9
    tr.sent.clear()
    pub.snapshot_loop()
    for _ in range(5):
        pub.step()
    deliver(h, tr.sent)
    ch = h.channels[318]
    assert ch.arb.lost == 1
    victims = [i for i in ch.instruments.values() if i.gaps]
    assert len(victims) == 1, "only the instrument in the lost packet has a gap"
    v = victims[0]
    assert v.state == LIVE and v.recoveries == 1, (v.state, v.recoveries)
    for ins_state in ch.instruments.values():
        eng = next(i.engine for i in pub.instruments if i.security_id == ins_state.security_id)
        assert ins_state.rpt_seq == eng.rpt_seq
        assert_book_matches(ins_state, eng)
    assert any(k == "recovered" for k, _ in events)
    print(f"ok  recovery: packet {lost_seq} lost on both feeds -> {v.name} recovered "
          f"from snapshot, the other instrument untouched, books exact")


def test_gap_without_snapshot_stays_recovering():
    pub, tr = make_channel(["ES"])
    h, _ = new_handler(["ES"])
    for _ in range(10):
        pub.step()
    deliver(h, [(f, p) for f, p in tr.sent if f.type == INCREMENTAL],
            drop=lambda f, s: s == 4)
    h.tick(1 << 62)
    ins = h.channels[310].instruments[42003]
    assert ins.state == RECOVERING and ins.pending, ins.state
    pub.snapshot_loop()
    deliver(h, tr.of(SNAPSHOT))
    assert ins.state == LIVE
    assert_book_matches(ins, pub.instruments[0].engine)
    print("ok  recovery: an instrument buffers until a snapshot arrives, then goes live")


def test_late_join_recovers():
    pub, tr = make_channel(["CL"])
    for _ in range(30):
        pub.step()
    tr.sent.clear()                                  # we joined after all of that
    h, events = new_handler(["CL"])
    for _ in range(3):
        pub.step()
    pub.snapshot_loop()
    for _ in range(3):
        pub.step()
    deliver(h, tr.sent)
    ins = h.channels[382].instruments[42401]
    assert ins.state == LIVE and ins.recoveries == 1
    assert_book_matches(ins, pub.instruments[0].engine)
    assert any(k == "join" for k, _ in events)
    print("ok  late join: buffered from mid-stream, synced from the snapshot, book exact")


def test_snapshot_cross_check_when_live():
    pub, tr = make_channel(["ES"])
    h, _ = new_handler(["ES"])
    for _ in range(15):
        pub.step()
        pub.snapshot_loop()
    deliver(h, tr.sent)
    ins = h.channels[310].instruments[42003]
    assert ins.snapshots_checked >= 14 and ins.snapshot_mismatches == 0
    print(f"ok  live book matched {ins.snapshots_checked} snapshots exactly")


def test_definitions_learned():
    pub, tr = make_channel(["NQ", "RTY"])
    h, _ = new_handler(["NQ", "RTY"])
    pub.definition_loop()
    deliver(h, tr.sent)
    ch = h.channels[318]
    rty = ch.instruments[42005].definition
    assert rty is not None and rty.tick == 0.1 and rty.decimals == 1
    assert rty.symbol == get_product("RTY").front_month().symbol and rty.channel == 318
    assert ch.instruments[42004].definition.asset == "NQ"
    print(f"ok  definitions: {rty.symbol} tick {rty.tick} decimals {rty.decimals} from the wire")


# ------------------------------------------------------------ live wire ----
def test_group_bind_isolates_feeds_sharing_a_port():
    """Incremental A, snapshot A and instrument A of a channel share one port."""
    es = get_product("ES")
    inc, snap = es.feed("Incremental", "A"), es.feed("Snapshot", "A")
    assert inc.port == snap.port and inc.ip != snap.ip
    r_inc = join_group(inc.ip, inc.port, "127.0.0.1", timeout=1.0)
    r_snap = join_group(snap.ip, snap.port, "127.0.0.1", timeout=1.0)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    try:
        tx.sendto(b"incremental", (inc.ip, inc.port))
        tx.sendto(b"snapshot", (snap.ip, snap.port))
        assert r_inc.recv(100) == b"incremental"
        assert r_snap.recv(100) == b"snapshot"
        r_inc.settimeout(0.3)
        try:
            extra = r_inc.recv(100)
        except (TimeoutError, socket.timeout):
            extra = None
        assert extra is None, f"incremental socket also got {extra!r}"
    finally:
        for s in (r_inc, r_snap, tx):
            s.close()
    print("ok  sockets bound per group: same port, no cross-delivery")


def test_live_multicast_two_channels():
    """Real sockets end to end: publisher -> loopback multicast -> FeedSet."""
    from transport import Transport
    codes = ["ES", "NQ"]
    h, _ = new_handler(codes)
    fs = FeedSet(h, "127.0.0.1")
    tr = Transport("127.0.0.1", 0, loss={"A": 0.3}, seed=1)
    pubs = []
    for code in codes:
        p, _ = make_channel([code])
        p.out = tr
        pubs.append(p)
    try:
        for p in pubs:
            p.definition_loop()
        for _ in range(60):
            for p in pubs:
                p.step()
            fs.poll(0.002)
        for p in pubs:
            p.snapshot_loop()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            fs.poll(0.05)
            if all(h.channels[p.channel_id].last_seq == p.seq for p in pubs):
                break
        for p in pubs:
            ch = h.channels[p.channel_id]
            ins = ch.instruments[p.instruments[0].security_id]
            assert ch.last_seq == p.seq, (ch.last_seq, p.seq)
            assert ch.arb.lost == 0, "feed B should cover feed A's 30% loss"
            assert ins.state == LIVE and ins.definition is not None
            assert_book_matches(ins, p.instruments[0].engine)
        print(f"ok  live multicast: 2 channels, 30% loss on A, B covered "
              f"{sum(h.channels[p.channel_id].one_feed_only()['B'] for p in pubs)} packets, "
              f"books exact")
    finally:
        fs.close()
        tr.close()


PER_SCHEMA = (
    test_definition_roundtrip,
    test_snapshot_and_definition_decode_with_official_schema,
    test_shared_channel_sequence_and_ab_identical,
    test_snapshot_loop_semantics,
    test_arbitration_covers_single_feed_loss,
    test_arbitration_reorder_across_feeds,
    test_gap_recovers_from_snapshot,
    test_gap_without_snapshot_stays_recovering,
    test_late_join_recovers,
    test_snapshot_cross_check_when_live,
    test_definitions_learned,
    test_live_multicast_two_channels,
)

if __name__ == "__main__":
    test_snapshot_and_definition_layout()
    test_product_list()
    test_group_bind_isolates_feeds_sharing_a_port()
    for WIRE in WIRES:
        print(f"\n— schema: {WIRE.describe()['label']}")
        for t in PER_SCHEMA:
            t()
    print(f"\nALL PASS — snapshot/definition wire, multi-product channels, A/B "
          f"arbitration and recovery, under {len(WIRES)} schema(s).")
