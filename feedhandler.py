"""
A CME MDP 3.0 feed handler: A/B arbitration, snapshot recovery, instrument
definitions — for any set of channels.

Arbitration (incremental feeds A and B)
    Both feeds carry byte-identical packets with the same per-channel
    MsgSeqNum. The first copy of each sequence number is processed, the second
    is a duplicate. A packet that arrives ahead of sequence is held briefly
    (`window` packets / `hold_ms`) in case its predecessor is still in flight on
    the slower feed; only when neither feed delivers it is it a real gap.
    `ArbStats` records which feed won each packet and how many arrived on one
    feed only — loss the other feed covered.

Recovery (snapshot feed, instrument level)
    Every incremental entry carries its instrument's `rptSeq`, which increases
    by exactly one per entry. An instrument whose next entry skips ahead has
    missed something (a lost packet, or a late join); it goes RECOVERING and
    buffers its entries. The next SnapshotFullRefresh for it replaces the book
    as of the snapshot's `rptSeq`, the buffered entries after that are replayed,
    and it is LIVE again. Instruments the gap did not touch are unaffected —
    there is no need to stall a whole channel for one instrument's loss.

    An instrument first seen at `rptSeq` 1 is live from the start: nothing came
    before it. A late joiner sees a higher number and recovers from a snapshot.

Definitions (instrument replay feed)
    MDInstrumentDefinitionFuture tells the handler each security id's symbol,
    tick, channel, decimals and limits, so nothing about an instrument has to
    be configured.

`FeedSet` joins the sockets for a set of products and polls them on one thread;
consumer.py runs the same handler behind its receive threads.

Decoding goes through a `wire.Wire` (or `AutoWire`), so the handler works on
CME's official schema and the working subset alike; a frame that fits neither is
counted, never applied.
"""
from __future__ import annotations

import selectors
import time
from dataclasses import dataclass, field

from book import Book
from packet import parse_packet
from receiver import join_group
from wire import SchemaMismatch

INCREMENTAL, SNAPSHOT, INSTRUMENT = "Incremental", "Snapshot", "Instrument Replay"
KINDS = (INCREMENTAL, SNAPSHOT, INSTRUMENT)
SHORT = {INCREMENTAL: "I", SNAPSHOT: "S", INSTRUMENT: "N"}

WAITING, RECOVERING, LIVE = "waiting", "recovering", "live"
MAX_PENDING = 50_000          # buffered entries per recovering instrument


@dataclass
class Definition:
    security_id: int
    symbol: str
    asset: str
    group: str
    exchange: str
    channel: int
    tick: float
    decimals: int
    tick_value: float | None
    multiplier: float | None
    maturity: str
    high_limit: float | None
    low_limit: float | None
    reference_price: float | None
    depth: int | None

    @classmethod
    def from_message(cls, root: dict, groups: dict) -> "Definition":
        mmy = root.get("maturityMonthYear") or {}
        depth = next((g.get("marketDepth") for g in groups.get("MDFeedTypes", [])
                      if g.get("mdFeedType") == "GBX"), None)
        tick = root.get("minPriceIncrement")
        dec = root.get("priceDisplayFormat")
        if dec is None and tick:
            dec = max(0, len(f"{tick:.10f}".rstrip("0").split(".")[1]))
        return cls(root["securityId"], root.get("symbol", ""), root.get("asset", ""),
                   root.get("securityGroup", ""), root.get("securityExchange", ""),
                   root.get("applId"), tick, dec if dec is not None else 2,
                   root.get("minPriceIncrementAmount"), root.get("unitOfMeasureQty"),
                   f"{mmy.get('year') or 0:04d}-{mmy.get('month') or 0:02d}",
                   root.get("highLimitPrice"), root.get("lowLimitPrice"),
                   root.get("tradingReferencePrice"), depth)


@dataclass
class InstrumentState:
    security_id: int
    channel: int
    book: Book = field(default_factory=Book)
    definition: Definition | None = None
    state: str = WAITING
    rpt_seq: int | None = None
    pending: list = field(default_factory=list)   # (rptSeq, entry, transactTime)
    gaps: int = 0
    recoveries: int = 0
    snapshots_checked: int = 0
    snapshot_mismatches: int = 0
    entries: int = 0
    overflowed: int = 0

    @property
    def name(self) -> str:
        return self.definition.symbol if self.definition else str(self.security_id)


@dataclass
class ArbStats:
    accepted: int = 0
    duplicates: int = 0
    won: dict = field(default_factory=lambda: {"A": 0, "B": 0})
    only: dict = field(default_factory=lambda: {"A": 0, "B": 0})   # other feed lost it
    lost: int = 0                 # packets neither feed delivered
    gap_events: int = 0
    late: int = 0                 # arrived after its gap was already declared
    reordered: int = 0            # held, then released in order


class ChannelHandler:
    """All feeds of one channel."""

    def __init__(self, channel: int, wire, window: int = 64, hold_ms: float = 20.0,
                 recover: bool = True, on_event=None):
        self.channel = channel
        self.wire = wire
        self.window = window
        self.hold_ns = int(hold_ms * 1e6)
        self.recover = recover
        self.on_event = on_event or (lambda kind, text: None)
        self.instruments: dict[int, InstrumentState] = {}
        self.arb = ArbStats()
        self.last_seq: int | None = None
        self._held: dict[int, tuple] = {}            # seq -> (feed, frames, ts, rx)
        self._seen: dict[int, set] = {}              # seq -> feeds it arrived on
        self.snapshot_packets = 0
        self.definition_packets = 0
        self.decode_errors = 0
        self.last_sending_ns: int | None = None

    def _decode(self, frame: bytes):
        try:
            return self.wire.decode(frame)
        except SchemaMismatch as e:
            self.decode_errors += 1
            if self.decode_errors == 1:
                self.on_event("schema", f"ch {self.channel}: {e} — is the sender using "
                              f"another --schema?")
            return None

    # ---- entry point ----
    def on_packet(self, kind: str, feed_id: str, data: bytes, rx_ns: int | None = None) -> int | None:
        """Handle one datagram. Returns its sequence number if it was the copy
        that got processed (incremental), else None."""
        seq, ts, frames = parse_packet(data)
        if kind == INCREMENTAL:
            return self._arbitrate(seq, feed_id, frames, ts, rx_ns or time.time_ns())
        if kind == SNAPSHOT:
            self.snapshot_packets += 1
            for f in frames:
                d = self._decode(f)
                if d and d["id"] == 52:
                    self._on_snapshot(d)
        elif kind == INSTRUMENT:
            self.definition_packets += 1
            for f in frames:
                d = self._decode(f)
                if d and d["id"] == 54:
                    self._on_definition(d)
        return None

    def one_feed_only(self) -> dict:
        """Packets that arrived on one feed only: the other feed lost them."""
        out = dict(self.arb.only)
        for feeds in self._seen.values():
            if len(feeds) == 1:
                f = next(iter(feeds))
                out[f] = out.get(f, 0) + 1
        return out

    def tick(self, now_ns: int | None = None) -> None:
        """Release held packets whose predecessor never came. Call periodically."""
        if self._held:
            self._release_expired(now_ns or time.time_ns())

    # ---- A/B arbitration ----
    def _note_seen(self, seq: int, feed_id: str) -> None:
        self._seen.setdefault(seq, set()).add(feed_id)
        if len(self._seen) > 4 * self.window + 256:
            # anything well behind the head has had its chance on both feeds
            horizon = (self.last_seq or seq) - 2 * self.window
            for s in [s for s in self._seen if s < horizon]:
                feeds = self._seen.pop(s)
                if len(feeds) == 1:
                    self.arb.only[next(iter(feeds))] += 1

    def _arbitrate(self, seq, feed_id, frames, ts, rx_ns):
        last = self.last_seq
        self._note_seen(seq, feed_id)
        if last is not None and seq <= last:
            if seq in self._seen and len(self._seen[seq]) > 1:
                self.arb.duplicates += 1
            else:
                self.arb.late += 1          # its gap was declared before it came
            return None
        if seq in self._held:
            self.arb.duplicates += 1
            return None
        if last is None or seq == last + 1:
            self._process(seq, feed_id, frames, ts)
            self._drain_held()
            return seq
        # ahead of sequence: hold it, the predecessor may be on the other feed
        self._held[seq] = (feed_id, frames, ts, rx_ns)
        self.arb.reordered += 1
        while len(self._held) > self.window:
            self._skip_to(min(self._held))
        self._release_expired(rx_ns)
        return None

    def _drain_held(self) -> None:
        while self.last_seq is not None and self.last_seq + 1 in self._held:
            s = self.last_seq + 1
            feed_id, frames, ts, _rx = self._held.pop(s)
            self._process(s, feed_id, frames, ts)

    def _release_expired(self, now_ns: int) -> None:
        # each skip drains up to the next hole; keep going while what is left
        # has also waited long enough
        while self._held and now_ns - min(v[3] for v in self._held.values()) >= self.hold_ns:
            self._skip_to(min(self._held))

    def _skip_to(self, seq: int) -> None:
        missing = seq - (self.last_seq or 0) - 1
        if missing > 0:
            self.arb.lost += missing
            self.arb.gap_events += 1
            self.on_event("gap", f"ch {self.channel} packets {self.last_seq + 1}"
                          f"{'' if missing == 1 else '..' + str(seq - 1)} lost on both feeds")
        self.last_seq = seq - 1
        self._drain_held()

    def _process(self, seq, feed_id, frames, ts) -> None:
        self.last_seq = seq
        self.last_sending_ns = ts
        self.arb.accepted += 1
        self.arb.won[feed_id] = self.arb.won.get(feed_id, 0) + 1
        for f in frames:
            d = self._decode(f)
            if d and d["id"] in (46, 48):
                tt = d["root"].get("transactTime")
                for e in d["groups"].get("MDEntries", []):
                    self._on_entry(e, tt)

    # ---- per-instrument sequencing ----
    def instrument(self, sid: int) -> InstrumentState:
        ins = self.instruments.get(sid)
        if ins is None:
            ins = self.instruments[sid] = InstrumentState(sid, self.channel)
        return ins

    def _on_entry(self, e: dict, tt) -> None:
        sid, r = e.get("securityId"), e.get("rptSeq")
        ins = self.instrument(sid)
        if ins.state == LIVE:
            if r <= ins.rpt_seq:
                return                                  # already reflected
            if r == ins.rpt_seq + 1:
                ins.book.apply(e, tt)
                ins.rpt_seq = r
                ins.entries += 1
                return
            ins.gaps += 1
            self.on_event("gap", f"{ins.name} rptSeq {ins.rpt_seq} -> {r}: "
                          f"{r - ins.rpt_seq - 1} entries missed"
                          f"{', recovering from snapshot' if self.recover else ''}")
            if not self.recover:                        # apply and carry on
                ins.book.apply(e, tt)
                ins.rpt_seq = r
                return
            ins.state = RECOVERING
            ins.pending = [(r, e, tt)]
            return
        if ins.state == WAITING and (r == 1 or not self.recover):
            ins.state, ins.rpt_seq = LIVE, r            # nothing came before it
            ins.book.apply(e, tt)
            ins.entries += 1
            return
        if ins.state == WAITING:
            ins.state = RECOVERING
            self.on_event("join", f"{ins.name} joined at rptSeq {r}: "
                          f"waiting for a snapshot")
        if len(ins.pending) < MAX_PENDING:
            ins.pending.append((r, e, tt))
        else:
            ins.overflowed += 1

    # ---- snapshot ----
    def _on_snapshot(self, d: dict) -> None:
        root = d["root"]
        sid, r = root["securityId"], root["rptSeq"]
        entries = d["groups"].get("MDEntries", [])
        ins = self.instrument(sid)
        if ins.state == LIVE:
            if r == ins.rpt_seq:                        # cross-check our book
                ins.snapshots_checked += 1
                if not ins.book.levels_equal(entries):
                    ins.snapshot_mismatches += 1
                    self.on_event("mismatch", f"{ins.name} book differs from snapshot at "
                                  f"rptSeq {r}")
            return
        if not self.recover:
            return
        pending = sorted((p for p in ins.pending if p[0] > r), key=lambda p: p[0])
        if pending and pending[0][0] != r + 1:
            # the snapshot is older than what we've buffered past a hole; keep
            # waiting for a newer loop
            ins.pending = pending
            return
        ins.book.load_snapshot(root, entries)
        ins.rpt_seq = r
        replayed = 0
        for i, (pr, e, tt) in enumerate(pending):
            if pr != ins.rpt_seq + 1:
                ins.pending = pending[i:]
                return                                  # another hole; next snapshot
            ins.book.apply(e, tt)
            ins.rpt_seq = pr
            replayed += 1
        was = ins.state
        ins.pending = []
        ins.state = LIVE
        if was == RECOVERING:
            ins.recoveries += 1
            self.on_event("recovered", f"{ins.name} recovered from snapshot at rptSeq {r} "
                          f"(+{replayed} buffered entries replayed)")
        else:
            self.on_event("synced", f"{ins.name} synced from snapshot at rptSeq {r}")

    # ---- instrument definitions ----
    def _on_definition(self, d: dict) -> None:
        defn = Definition.from_message(d["root"], d["groups"])
        ins = self.instrument(defn.security_id)
        known = ins.definition is not None
        ins.definition = defn
        if not known:
            self.on_event("definition", f"{defn.symbol} security_id={defn.security_id} "
                          f"tick={defn.tick:.10g} ch={defn.channel} {defn.exchange}")


class FeedHandler:
    """Channel handlers keyed by channel id, plus the (group, port) routing."""

    def __init__(self, wire, window: int = 64, hold_ms: float = 20.0,
                 recover: bool = True, on_event=None):
        self.wire = wire
        self.kw = dict(window=window, hold_ms=hold_ms, recover=recover, on_event=on_event)
        self.channels: dict[int, ChannelHandler] = {}
        self.routes: dict[tuple, tuple] = {}            # (ip, port) -> (channel, kind, feed)

    def channel(self, ch: int) -> ChannelHandler:
        h = self.channels.get(ch)
        if h is None:
            h = self.channels[ch] = ChannelHandler(ch, self.wire, **self.kw)
        return h

    def add_routes_for(self, products, kinds=KINDS, feeds="AB") -> list[tuple]:
        """Route every feed of these products' channels; returns the new routes."""
        added = []
        for p in products:
            self.channel(p.channel_id)
            for kind in kinds:
                for f in p.feed_pair(kind):
                    if f is None or f.feed_id not in feeds:
                        continue
                    key = (f.ip, f.port)
                    if key not in self.routes:
                        self.routes[key] = (p.channel_id, kind, f.feed_id)
                        added.append((key, self.routes[key]))
        return added

    def on_datagram(self, dst: tuple, data: bytes, rx_ns: int | None = None):
        route = self.routes.get(dst)
        if route is None:
            return None
        ch, kind, feed_id = route
        return self.channels[ch].on_packet(kind, feed_id, data, rx_ns)

    def tick(self, now_ns: int | None = None) -> None:
        for h in self.channels.values():
            h.tick(now_ns)

    def instruments(self) -> list[InstrumentState]:
        return [ins for h in self.channels.values() for ins in h.instruments.values()]


class FeedSet:
    """Sockets for a FeedHandler's routes, polled on the calling thread."""

    def __init__(self, handler: FeedHandler, iface: str = "0.0.0.0"):
        self.handler = handler
        self.sel = selectors.DefaultSelector()
        self.socks = []
        for (ip, port), route in handler.routes.items():
            s = join_group(ip, port, iface)
            s.setblocking(False)
            self.sel.register(s, selectors.EVENT_READ, (ip, port))
            self.socks.append(s)
        self.packets = 0
        self.counts: dict[tuple, int] = {}          # datagrams per (group, port)

    def poll(self, timeout: float = 0.05) -> int:
        """Process whatever arrives within `timeout` seconds. Returns count."""
        n = 0
        end = time.monotonic() + timeout
        while True:
            remaining = end - time.monotonic()
            events = self.sel.select(max(0.0, remaining))
            for key, _ in events:
                while True:
                    try:
                        data = key.fileobj.recv(65535)
                    except (BlockingIOError, InterruptedError):
                        break
                    self.handler.on_datagram(key.data, data, time.time_ns())
                    self.counts[key.data] = self.counts.get(key.data, 0) + 1
                    n += 1
            if remaining <= 0 or not events:
                break
        self.handler.tick()
        self.packets += n
        return n

    def close(self) -> None:
        for s in self.socks:
            self.sel.unregister(s)
            s.close()
