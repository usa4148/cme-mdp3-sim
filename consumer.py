"""
MDP 3.0 consumer / feed handler.

Joins the multicast group, receives packets, decodes the binary packet header
and every SBE message inside, and prints them. This proves the simulator's
output is wire-correct: an independent decoder reconstructs bids/offers/trades
purely from the bytes on the wire.

It also behaves like a real feed handler in two ways:

**PTP-style timestamps.** Every datagram is stamped as close to the wire as the
platform allows — a kernel `SO_TIMESTAMPNS`/`SO_TIMESTAMP` control message where
available, a userspace clock read otherwise — and reported on the IEEE 1588 TAI
timescale. Transit is measured against the `sendingTime` in the packet header.
See ptp.py; the timestamp's true provenance and resolution are always printed,
and a synthesized one is labelled as such.

**A split receive path.** The receive thread does nothing but `recvmsg`, stamp
and enqueue, so decoding never delays the next packet — the mistake that makes a
naive handler's latency numbers measure its own decoder. Decoding, printing and
statistics run on the main thread, fed by a bounded queue whose overruns are
counted the way a real handler counts kernel drops.

**A real handler's recovery path.** By default it joins every feed of the
chosen products' channels — incremental A and B, snapshot, instrument replay —
and runs them through `feedhandler.py`: A/B arbitration, per-instrument gap
detection on `rptSeq`, snapshot recovery, and instrument definitions learned
from the wire. `--raw` (or an explicit `--group`) listens on one group and
decodes everything on it, as before.

    python3 consumer.py --summary
    python3 consumer.py --product ES,NQ,CL --summary
    python3 consumer.py --product CL --count 100
    python3 consumer.py --feeds A              # no arbitration: feed A alone
    python3 consumer.py --raw --product ES     # one group, every message printed
    python3 consumer.py --clock synthetic      # demo on a kernel without support
"""
from __future__ import annotations

import argparse
import queue
import selectors
import socket
import threading
import time
from dataclasses import dataclass

from feedhandler import KINDS, SHORT, FeedHandler
from packet import parse_packet
from products import get_product, parse_product_list
from receiver import join_group, parse_ifaces
from wire import add_schema_argument, open_wire
from ptp import (ANC_BUFSIZE, DEFAULT_TAI_OFFSET, ClockSource, LatencyStats,
                 enable_rx_timestamps, fmt_ns, format_ptp, format_wall,
                 receive_timestamp, synthesize_ancdata)
from lifecycle import stop_on_signals
from pyver import require_python, runtime_banner

require_python()

DEFAULT_GROUP = "224.0.31.1"
DEFAULT_PORT = 14310
RECV_TIMEOUT_S = 0.2          # lets the receive thread notice a stop request


@dataclass
class Capture:
    """One datagram, as the receive thread hands it over."""
    data: bytes
    rx_ns: int
    from_kernel: bool
    feed: tuple | None = None     # (group, port) it arrived on


class Receiver(threading.Thread):
    """Receive, timestamp, enqueue. Deliberately does no parsing.

    Anything slower than a memcpy belongs on the other side of the queue: while
    this thread is decoding, it is not receiving, and the next packet either
    waits in the socket buffer (inflating the latency it will later report) or
    is dropped by the kernel.
    """

    def __init__(self, sock: socket.socket, source: ClockSource,
                 out: queue.Queue, stop: threading.Event, label: tuple | None = None):
        super().__init__(name=f"rx {label[0]}:{label[1]}" if label else "rx", daemon=True)
        self.sock = sock
        self.label = label
        self.source = source
        self.out = out
        self.stop = stop
        self.received = 0
        self.dropped = 0          # queue overruns: the decoder fell behind
        self.max_depth = 0
        self.error: BaseException | None = None

    def run(self) -> None:
        sock, source, out = self.sock, self.source, self.out
        synthetic = source.synthetic
        try:
            while not self.stop.is_set():
                try:
                    data, ancdata, _flags, _addr = sock.recvmsg(65535, ANC_BUFSIZE)
                except TimeoutError:
                    continue
                except OSError:
                    break                      # socket closed during shutdown
                now = time.clock_gettime_ns(time.CLOCK_REALTIME)
                if synthetic:
                    # No kernel support: fabricate the control message the kernel
                    # would have attached, so the parsing path below is identical.
                    ancdata = synthesize_ancdata(now, source)
                rx_ns, from_kernel = receive_timestamp(ancdata, source, now)
                self.received += 1
                try:
                    out.put_nowait(Capture(data, rx_ns, from_kernel, self.label))
                except queue.Full:
                    self.dropped += 1
                else:
                    self.max_depth = max(self.max_depth, out.qsize())
        except BaseException as exc:            # never die silently
            self.error = exc


class Session:
    """Decodes captures and accumulates feed-handler statistics."""

    def __init__(self, schema, source: ClockSource, args):
        self.schema = schema
        self.source = source
        self.args = args
        self.n = 0
        self.gaps = 0
        self.userspace_fallbacks = 0
        self.last_seq: int | None = None
        self.last_rx: int | None = None
        self.transit = LatencyStats("transit")
        self.arrival = LatencyStats("inter-arrival")
        self.first_rx: int | None = None

    def handle(self, cap: Capture) -> None:
        a = self.args
        seq, ts_ns, frames = parse_packet(cap.data)
        gap = "" if self.last_seq is None or seq == self.last_seq + 1 else \
              f"  !! GAP (prev {self.last_seq})"
        if gap:
            self.gaps += 1
        self.last_seq = seq
        if not cap.from_kernel and self.source.kernel:
            self.userspace_fallbacks += 1

        transit = cap.rx_ns - ts_ns              # sendingTime -> our timestamp
        self.transit.add(transit)
        if self.last_rx is not None:
            self.arrival.add(cap.rx_ns - self.last_rx)
        self.last_rx = cap.rx_ns
        if self.first_rx is None:
            self.first_rx = cap.rx_ns
        self.n += 1

        decoded = [self.schema.decode(f) for f in frames]
        stamp = format_ptp(cap.rx_ns, a.tai_offset)
        if a.summary:
            kinds = ",".join(d["template"].replace("MDIncrementalRefresh", "")
                             for d in decoded)
            print(f"seq={seq:<6} ptp={stamp}  transit={fmt_ns(transit):>9}  "
                  f"{len(frames)} msg [{kinds}] {len(cap.data)}B{gap}")
            return

        print(f"── packet seq={seq}  {len(frames)} msg(s)  {len(cap.data)}B{gap}")
        print(f"   rx  PTP {stamp}  ({format_wall(cap.rx_ns)} UTC"
              f"{'' if cap.from_kernel else ', userspace'})")
        print(f"   tx  PTP {format_ptp(ts_ns, a.tai_offset)}  "
              f"transit {fmt_ns(transit)}")
        for d in decoded:
            print(f"  {d['template']} (t{d['id']})  "
                  f"transactTime={d['root'].get('transactTime')}"
                  f"  evt={d['root'].get('matchEventIndicator')}")
            for e in d["groups"].get("MDEntries", []):
                print(fmt_entry(e))

    def report(self) -> None:
        a = self.args
        span = (self.last_rx - self.first_rx) / 1e9 if self.n > 1 else 0.0
        print(f"\nReceived {self.n} packets in {span:.2f}s  ·  "
              f"{self.gaps} sequence gap(s)")
        print(f"  clock         {self.source.describe()}")
        print(f"  PTP epoch     TAI (UTC + {a.tai_offset}s) — this is PTP "
              f"formatting and measurement, not grandmaster sync")
        if self.userspace_fallbacks:
            print(f"  fallbacks     {self.userspace_fallbacks} packet(s) had no "
                  f"kernel timestamp and used a userspace read")
        if len(self.transit):
            print(f"  {self.transit.describe()}")
        if len(self.arrival):
            print(f"  {self.arrival.describe()}")
            print(f"  jitter        {fmt_ns(self.arrival.jitter_ns)} (p99 - p50 "
                  f"of inter-arrival)")
        if self.n and self.transit.percentile(50) < 0:
            print("  note: negative transit means the sender's clock is ahead of "
                  "this host's — expected without a shared PTP source")


class FeedSession:
    """Runs captures from every feed through the feed handler."""

    def __init__(self, handler: FeedHandler, source: ClockSource, args, products):
        self.handler = handler
        self.source = source
        self.args = args
        self.products = products
        self.n = 0                    # incremental packets processed (first copies)
        self.datagrams = 0
        self.userspace_fallbacks = 0
        self.transit = LatencyStats("transit")
        self.arrival = LatencyStats("inter-arrival")   # per feed: one socket, one clock order
        self._feed_rx: dict[tuple, int] = {}
        self.last_rx: int | None = None
        self.first_rx: int | None = None

    def event(self, kind: str, text: str) -> None:
        print(f"  ** {kind:<10} {text}", flush=True)

    def handle(self, cap: Capture) -> None:
        a = self.args
        self.datagrams += 1
        if not cap.from_kernel and self.source.kernel:
            self.userspace_fallbacks += 1
        route = self.handler.routes.get(cap.feed)
        if route is None:
            return
        ch, kind, feed_id = route
        if kind == "Incremental":
            prev = self._feed_rx.get(cap.feed)
            if prev is not None:
                self.arrival.add(cap.rx_ns - prev)
            self._feed_rx[cap.feed] = cap.rx_ns
        seq = self.handler.on_datagram(cap.feed, cap.data, cap.rx_ns)
        self.handler.tick(cap.rx_ns)
        if seq is None:
            return                     # duplicate, held, snapshot or definition
        _, ts_ns, frames = parse_packet(cap.data)
        transit = cap.rx_ns - ts_ns
        self.transit.add(transit)
        self.last_rx = cap.rx_ns if self.last_rx is None else max(self.last_rx, cap.rx_ns)
        if self.first_rx is None:
            self.first_rx = cap.rx_ns
        self.n += 1
        if a.quiet:
            return
        h = self.handler.channels[ch]
        stamp = format_ptp(cap.rx_ns, a.tai_offset)
        if a.summary:
            names = []
            for f in frames:
                d = h.wire.decode(f)
                sids = {e.get("securityId") for e in d["groups"].get("MDEntries", [])}
                for sid in sids:
                    ins = h.instruments.get(sid)
                    nm = ins.name if ins else str(sid)
                    if nm not in names:
                        names.append(nm)
            print(f"ch={ch} {SHORT[kind]}-{feed_id} seq={seq:<6} ptp={stamp}  "
                  f"transit={fmt_ns(transit):>9}  {len(frames)} msg "
                  f"[{','.join(names)}] {len(cap.data)}B")
            return
        print(f"── ch {ch} {kind} feed {feed_id}  seq={seq}  {len(frames)} msg(s)  "
              f"{len(cap.data)}B")
        print(f"   rx  PTP {stamp}  transit {fmt_ns(transit)}")
        for f in frames:
            d = h.wire.decode(f)
            print(f"  {d['template']} (t{d['id']})  transactTime={d['root'].get('transactTime')}")
            for e in d["groups"].get("MDEntries", []):
                ins = h.instruments.get(e.get("securityId"))
                print(f"  {ins.name if ins else e.get('securityId'):>8}" + fmt_entry(e))

    def report(self) -> None:
        a = self.args
        span = (self.last_rx - self.first_rx) / 1e9 if self.n > 1 else 0.0
        print(f"\nProcessed {self.n} incremental packets in {span:.2f}s  ·  "
              f"{self.datagrams} datagrams across {len(self.handler.routes)} feeds")
        both = "A" in a.feeds and "B" in a.feeds
        for ch, h in sorted(self.handler.channels.items()):
            arb = h.arb
            line = (f"  channel {ch}: won A {arb.won.get('A', 0)} / B {arb.won.get('B', 0)}"
                    f"  dup {arb.duplicates}  held {arb.reordered}"
                    f"  lost on both {arb.lost} ({arb.gap_events} gap(s))")
            if both:
                only = h.one_feed_only()
                line += f"  covered by B {only.get('B', 0)}  covered by A {only.get('A', 0)}"
            if arb.late:
                line += f"  late {arb.late}"
            print(line + f"  · {h.snapshot_packets} snapshot, "
                  f"{h.definition_packets} definition pkts")
            for ins in sorted(h.instruments.values(), key=lambda i: i.name):
                b, dfn = ins.book, ins.definition
                dec = dfn.decimals if dfn else 2
                px = lambda v: "--" if v is None else f"{v:.{dec}f}"
                learned = (f"{dfn.symbol} tick {dfn.tick:.10g}" if dfn
                           else "no definition received")
                print(f"    {ins.name:<8} {ins.state:<10} rptSeq {ins.rpt_seq}"
                      f"  bid {px(b.best_bid())} / offer {px(b.best_offer())}"
                      f"  gaps {ins.gaps}  recoveries {ins.recoveries}"
                      f"  snapshot checks {ins.snapshots_checked}"
                      f" ({ins.snapshot_mismatches} mismatch)  [{learned}]")
        print(f"  clock         {self.source.describe()}")
        print(f"  PTP epoch     TAI (UTC + {a.tai_offset}s) — this is PTP "
              f"formatting and measurement, not grandmaster sync")
        if self.userspace_fallbacks:
            print(f"  fallbacks     {self.userspace_fallbacks} packet(s) had no "
                  f"kernel timestamp and used a userspace read")
        if len(self.transit):
            print(f"  {self.transit.describe()}")
        if len(self.arrival):
            print(f"  {self.arrival.describe()}  (per incremental feed)")
        if self.n and self.transit.percentile(50) < 0:
            print("  note: negative transit means the sender's clock is ahead of "
                  "this host's — expected without a shared PTP source")


def fmt_entry(e):
    action = e.get("mdUpdateAction")
    etype = e.get("mdEntryType")
    px = e.get("mdEntryPx")
    sz = e.get("mdEntrySize")
    lvl = e.get("mdPriceLevel")
    extra = f" L{lvl}" if lvl is not None else ""
    if etype == "Trade":
        return (f"    TRADE  {sz}@{px}  aggressor={e.get('aggressorSide')}  "
                f"rpt={e.get('rptSeq')}")
    return (f"    {action:<7}{etype:<6} {sz}@{px}{extra}  "
            f"ord={e.get('numberOfOrders')} rpt={e.get('rptSeq')}")


def run_threaded(socks, session, source, args):
    """One receive thread per socket; decode on this one, in arrival order."""
    stop = threading.Event()
    q: queue.Queue = queue.Queue(maxsize=args.queue)
    rxs = [Receiver(sock, source, q, stop, label) for sock, label in socks]
    for rx in rxs:
        rx.start()
    tick = getattr(getattr(session, "handler", None), "tick", None)
    try:
        while True:
            if args.count and session.n >= args.count:
                break
            try:
                cap = q.get(timeout=RECV_TIMEOUT_S)
            except queue.Empty:
                if tick:
                    tick()
                if any(rx.error for rx in rxs) or not any(rx.is_alive() for rx in rxs):
                    break
                continue
            session.handle(cap)
    except KeyboardInterrupt:
        print()
    finally:
        stop.set()
        for rx in rxs:
            rx.join(timeout=2.0)
    for rx in rxs:
        if rx.error:
            print(f"  receive thread {rx.name} failed: {rx.error!r}")
    dropped = sum(rx.dropped for rx in rxs)
    if dropped:
        print(f"  queue drops   {dropped} (decoder fell behind the feed)")
    high = max((rx.max_depth for rx in rxs), default=0)
    if high:
        print(f"  queue high-water {high}/{args.queue}")
    return session


def run_inline(socks, session, source, args):
    """Single-threaded path: decode between receives (--no-threads)."""
    sel = selectors.DefaultSelector()
    for sock, label in socks:
        sel.register(sock, selectors.EVENT_READ, label)
    tick = getattr(getattr(session, "handler", None), "tick", None)
    try:
        while not (args.count and session.n >= args.count):
            events = sel.select(RECV_TIMEOUT_S)
            if not events and tick:
                tick()
            for key, _ in events:
                try:
                    data, ancdata, _flags, _addr = key.fileobj.recvmsg(65535, ANC_BUFSIZE)
                except (TimeoutError, BlockingIOError):
                    continue
                now = time.clock_gettime_ns(time.CLOCK_REALTIME)
                if source.synthetic:
                    ancdata = synthesize_ancdata(now, source)
                rx_ns, from_kernel = receive_timestamp(ancdata, source, now)
                session.handle(Capture(data, rx_ns, from_kernel, key.data))
    except KeyboardInterrupt:
        print()
    return session


def main():
    ap = argparse.ArgumentParser(description="MDP 3.0 multicast consumer / feed handler")
    ap.add_argument("--product", "--products", dest="product", action="append",
                    help="product code(s) whose channels to follow, up to 4 (default ES)")
    ap.add_argument("--feeds", default="AB", choices=("AB", "A", "B"),
                    help="which feed letters to join (default both: arbitration)")
    ap.add_argument("--no-recovery", action="store_true",
                    help="apply entries across gaps instead of recovering from snapshots")
    ap.add_argument("--window", type=int, default=64,
                    help="packets to hold ahead of sequence before declaring a gap")
    ap.add_argument("--hold-ms", type=float, default=20.0,
                    help="how long to wait for a missing packet on the other feed")
    ap.add_argument("--quiet", action="store_true", help="no per-packet lines, events only")
    add_schema_argument(ap, receive=True)
    ap.add_argument("--raw", action="store_true",
                    help="one group only (incremental A), no feed handler")
    ap.add_argument("--group", default=None, help="multicast group (implies --raw)")
    ap.add_argument("--port", type=int, default=None, help="multicast port (implies --raw)")
    ap.add_argument("--iface", default="127.0.0.1",
                    help="interface IP(s) to receive on, comma-separated")
    ap.add_argument("--count", type=int, default=0,
                    help="stop after N packets (0 = forever)")
    ap.add_argument("--summary", action="store_true", help="one line per packet")
    ap.add_argument("--clock", default="auto",
                    choices=("auto", "kernel", "userspace", "synthetic"),
                    help="receive timestamp source (default: best available)")
    ap.add_argument("--tai-offset", type=int, default=DEFAULT_TAI_OFFSET,
                    help=f"UTC->TAI leap seconds (default {DEFAULT_TAI_OFFSET})")
    ap.add_argument("--queue", type=int, default=4096,
                    help="receive queue depth before packets are dropped")
    ap.add_argument("--no-threads", action="store_true",
                    help="decode inline instead of on a separate thread")
    args = ap.parse_args()
    stop_on_signals()
    try:
        args.iface = ",".join(parse_ifaces(args.iface))
    except ValueError as e:
        ap.error(str(e))

    try:
        codes = parse_product_list(args.product)
    except ValueError as e:
        ap.error(str(e))
    try:
        schema = open_wire(args.schema)
    except (FileNotFoundError, ValueError) as e:
        ap.error(str(e))
    threads = "receive thread(s) + decode thread" if not args.no_threads else "inline decode"

    if args.raw or args.group or args.port:
        group, port = args.group, args.port
        if args.product or not (group and port):
            feed = get_product(codes[0]).incremental
            group = group or feed.ip
            port = port or feed.port
        sock = join_group(group, port, args.iface)
        sock.settimeout(RECV_TIMEOUT_S)
        source = _enable(sock, args.clock)
        print(f"Listening on {group}:{port} (iface {args.iface})  "
              f"schema {schema.name} id={schema.schema_id} v{schema.version}")
        print(f"  clock  {source.describe()}")
        print(f"  {runtime_banner()}  ·  {threads}\n")
        runner = run_inline if args.no_threads else run_threaded
        session = runner([(sock, (group, port))], Session(schema, source, args), source, args)
        session.report()
        return

    products = [get_product(c) for c in codes]
    session = None
    handler = FeedHandler(schema, window=args.window, hold_ms=args.hold_ms,
                          recover=not args.no_recovery,
                          on_event=lambda k, t: session.event(k, t))
    routes = handler.add_routes_for(products, KINDS, args.feeds)
    socks = []
    for (ip, port), _route in routes:
        s = join_group(ip, port, args.iface)
        s.settimeout(RECV_TIMEOUT_S)
        socks.append((s, (ip, port)))
    source = None
    for s, _ in socks:
        source = _enable(s, args.clock)
    session = FeedSession(handler, source, args, products)

    if hasattr(schema, "on_detect"):
        schema.on_detect = lambda n: session.event("detected", f"feed is using the {n} schema")
    print(f"Feed handler for {', '.join(codes)}  (iface {args.iface})  "
          f"schema {schema.name}")
    for (ip, port), (ch, kind, feed_id) in routes:
        print(f"  ch {ch:<4} {kind:<18} {feed_id}  {ip}:{port}")
    print(f"  recovery {'off' if args.no_recovery else 'snapshot, per instrument'}"
          f"  ·  arbitration {'A/B' if len(args.feeds) == 2 else 'off (feed ' + args.feeds + ' only)'}")
    print(f"  clock  {source.describe()}")
    print(f"  {runtime_banner()}  ·  {threads}\n")
    runner = run_inline if args.no_threads else run_threaded
    runner(socks, session, source, args)
    session.report()


def _enable(sock, mode):
    try:
        return enable_rx_timestamps(sock, mode)
    except OSError as e:
        raise SystemExit(f"--clock kernel: {e}\nUse --clock synthetic to "
                         f"fabricate timestamps instead.")


if __name__ == "__main__":
    main()
