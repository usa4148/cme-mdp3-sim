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

    python3 consumer.py --summary
    python3 consumer.py --product CL --count 100
    python3 consumer.py --clock synthetic     # demo on a kernel without support
"""
from __future__ import annotations

import argparse
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass

from packet import parse_packet
from products import get_product
from ptp import (ANC_BUFSIZE, DEFAULT_TAI_OFFSET, ClockSource, LatencyStats,
                 enable_rx_timestamps, fmt_ns, format_ptp, format_wall,
                 receive_timestamp, synthesize_ancdata)
from pyver import require_python, runtime_banner
from sbe import Schema

require_python()

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema", "mdp3.xml")
DEFAULT_GROUP = "224.0.31.1"
DEFAULT_PORT = 14310
RECV_TIMEOUT_S = 0.2          # lets the receive thread notice a stop request


def join_group(group: str, port: int, iface: str) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    s.bind(("", port))
    mreq = socket.inet_aton(group) + socket.inet_aton(iface)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    return s


@dataclass
class Capture:
    """One datagram, as the receive thread hands it over."""
    data: bytes
    rx_ns: int
    from_kernel: bool


class Receiver(threading.Thread):
    """Receive, timestamp, enqueue. Deliberately does no parsing.

    Anything slower than a memcpy belongs on the other side of the queue: while
    this thread is decoding, it is not receiving, and the next packet either
    waits in the socket buffer (inflating the latency it will later report) or
    is dropped by the kernel.
    """

    def __init__(self, sock: socket.socket, source: ClockSource,
                 out: queue.Queue, stop: threading.Event):
        super().__init__(name="rx", daemon=True)
        self.sock = sock
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
                    out.put_nowait(Capture(data, rx_ns, from_kernel))
                except queue.Full:
                    self.dropped += 1
                else:
                    self.max_depth = max(self.max_depth, out.qsize())
        except BaseException as exc:            # never die silently
            self.error = exc


class Session:
    """Decodes captures and accumulates feed-handler statistics."""

    def __init__(self, schema: Schema, source: ClockSource, args):
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

        decoded = [self.schema.decode_message(f)[0] for f in frames]
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


def run_threaded(sock, schema, source, args) -> Session:
    """Receive on its own thread; decode on this one."""
    stop = threading.Event()
    q: queue.Queue = queue.Queue(maxsize=args.queue)
    rx = Receiver(sock, source, q, stop)
    session = Session(schema, source, args)
    rx.start()
    try:
        while True:
            if args.count and session.n >= args.count:
                break
            try:
                cap = q.get(timeout=RECV_TIMEOUT_S)
            except queue.Empty:
                if rx.error or not rx.is_alive():
                    break
                continue
            session.handle(cap)
    except KeyboardInterrupt:
        print()
    finally:
        stop.set()
        rx.join(timeout=2.0)
    if rx.error:
        print(f"  receive thread failed: {rx.error!r}")
    if rx.dropped:
        print(f"  queue drops   {rx.dropped} (decoder fell behind the feed)")
    if rx.max_depth:
        print(f"  queue high-water {rx.max_depth}/{args.queue}")
    return session


def run_inline(sock, schema, source, args) -> Session:
    """Single-threaded path: decode between receives (--no-threads)."""
    session = Session(schema, source, args)
    try:
        while not (args.count and session.n >= args.count):
            try:
                data, ancdata, _flags, _addr = sock.recvmsg(65535, ANC_BUFSIZE)
            except TimeoutError:
                continue
            now = time.clock_gettime_ns(time.CLOCK_REALTIME)
            if source.synthetic:
                ancdata = synthesize_ancdata(now, source)
            rx_ns, from_kernel = receive_timestamp(ancdata, source, now)
            session.handle(Capture(data, rx_ns, from_kernel))
    except KeyboardInterrupt:
        print()
    return session


def main():
    ap = argparse.ArgumentParser(description="MDP 3.0 multicast consumer/decoder")
    ap.add_argument("--product", default=None,
                    help="listen on this product's feed (default: --group/--port)")
    ap.add_argument("--group", default=None, help="multicast group")
    ap.add_argument("--port", type=int, default=None, help="multicast port")
    ap.add_argument("--iface", default="127.0.0.1")
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

    group, port = args.group, args.port
    if args.product:
        feed = get_product(args.product).incremental
        group = group or feed.ip
        port = port or feed.port
    group = group or DEFAULT_GROUP
    port = port or DEFAULT_PORT

    schema = Schema(SCHEMA_PATH)
    sock = join_group(group, port, args.iface)
    sock.settimeout(RECV_TIMEOUT_S)
    try:
        source = enable_rx_timestamps(sock, args.clock)
    except OSError as e:
        raise SystemExit(f"--clock kernel: {e}\nUse --clock synthetic to "
                         f"fabricate timestamps instead.")

    print(f"Listening on {group}:{port} (iface {args.iface})  "
          f"schema id={schema.schema_id} v{schema.version}")
    print(f"  clock  {source.describe()}")
    print(f"  {runtime_banner()}  ·  "
          f"{'receive thread + decode thread' if not args.no_threads else 'inline decode'}\n")

    runner = run_inline if args.no_threads else run_threaded
    session = runner(sock, schema, source, args)
    session.report()


if __name__ == "__main__":
    main()
