#!/usr/bin/env python3
"""
MDP 3.0 multicast capture service: every feed of up to four products, to pcap,
flushed to S3 (VAST) and/or a local directory every N seconds.

Joins each feed of the chosen products' channels — incremental A/B, snapshot
A/B, instrument replay A/B by default — timestamps each datagram as close to
the wire as the platform allows (kernel SO_TIMESTAMPNS / SO_TIMESTAMP, see
ptp.py), and writes it into an in-memory nanosecond pcap. Every `--interval`
seconds that window becomes one standalone .pcap object. No root needed: the
capture is at the socket layer and the frame headers are synthesized (pcap.py).

    python3 capture.py --product ES,NQ --local-dir ./pcaps
    python3 capture.py --product ES,CL --interval 3          # to S3 via config.env
    python3 capture.py --kinds incremental --feeds A         # just incremental A
    python3 capture.py --join 224.0.31.1:14310               # any group, no product

Without S3 configured it needs --local-dir. Object keys:
    <S3_PREFIX>/<host>/YYYY/MM/DD/mdp3-YYYYMMDDTHHMMSSZ-mmm.pcap
"""
from __future__ import annotations

import argparse
import io
import os
import selectors
import socket
import sys
import time
from datetime import datetime, timezone

import pcap
import s3store
from feedhandler import INCREMENTAL, INSTRUMENT, KINDS, SNAPSHOT, FeedHandler
from lifecycle import stop_on_signals
from products import get_product, parse_product_list
from ptp import ANC_BUFSIZE, enable_rx_timestamps, receive_timestamp
from receiver import join_group, parse_ifaces
from pyver import require_python

require_python()

KIND_NAMES = {"incremental": INCREMENTAL, "snapshot": SNAPSHOT, "instrument": INSTRUMENT}


class Sink:
    """Where finalized pcap windows go: S3 if configured, local files if asked."""

    def __init__(self, local_dir: str | None):
        self.host = socket.gethostname().split(".")[0]
        self.local_dir = local_dir
        self.s3 = s3store.client() if s3store.configured() else None
        self.bucket = s3store.bucket() if self.s3 else None
        self.prefix = s3store.prefix()
        if local_dir:
            os.makedirs(local_dir, exist_ok=True)

    def describe(self) -> str:
        parts = []
        if self.s3:
            parts.append(f"s3://{self.bucket}/{self.prefix}/{self.host}/")
        if self.local_dir:
            parts.append(f"local:{self.local_dir}")
        return "  ".join(parts)

    def key_for(self, dt: datetime) -> str:
        return (f"{self.prefix}/{self.host}/{dt:%Y/%m/%d}/"
                f"mdp3-{dt:%Y%m%dT%H%M%S}Z-{dt.microsecond // 1000:03d}.pcap")

    def emit(self, blob: bytes, dt: datetime, npkts: int) -> str:
        key = self.key_for(dt)
        wrote = []
        if self.s3:
            s3store.put(self.s3, self.bucket, key, blob)
            wrote.append(f"s3://{self.bucket}/{key}")
        if self.local_dir:
            path = os.path.join(self.local_dir, os.path.basename(key))
            with open(path, "wb") as fh:
                fh.write(blob)
            wrote.append(path)
        return f"{npkts} pkt, {len(blob):,}B -> " + " , ".join(wrote)


def feed_list(args) -> list[tuple]:
    """[((ip, port), label)] to join."""
    out = []
    for j in args.join or []:
        ip, _, port = j.rpartition(":")
        out.append(((ip, int(port)), f"{ip}:{port}"))
    if args.product or not out:
        kinds = [KIND_NAMES[k.strip().lower()] for k in args.kinds.split(",")] \
            if args.kinds != "all" else list(KINDS)
        fh = FeedHandler(None)
        fh.add_routes_for([get_product(c) for c in parse_product_list(args.product)],
                          kinds, args.feeds)
        out += [(dst, f"ch {ch} {kind} {fid}") for dst, (ch, kind, fid) in fh.routes.items()]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="MDP 3.0 multicast capture -> pcap -> S3/local")
    ap.add_argument("--product", "--products", dest="product", action="append",
                    help="product code(s) whose channels to capture, up to 4 (default ES)")
    ap.add_argument("--kinds", default="all",
                    help="feed kinds: all, or a list of incremental,snapshot,instrument")
    ap.add_argument("--feeds", default="AB", choices=("AB", "A", "B"))
    ap.add_argument("--join", action="append", metavar="GROUP:PORT",
                    help="also capture this group (repeatable)")
    ap.add_argument("--iface", default="0.0.0.0",
                    help="interface IP(s) to receive on, comma-separated (e.g. 127.0.0.1,10.1.2.3)")
    ap.add_argument("--interval", type=float, default=3.0, help="flush period in seconds")
    ap.add_argument("--local-dir", default=None, help="also/instead write .pcap files here")
    ap.add_argument("--env-file", default=s3store.DEFAULT_ENV)
    ap.add_argument("--empty-windows", action="store_true",
                    help="write a (headers-only) pcap even for windows with no packets")
    ap.add_argument("--usec", action="store_true", help="microsecond pcap timestamps")
    args = ap.parse_args()
    stop_on_signals()
    try:
        args.iface = ",".join(parse_ifaces(args.iface))
    except ValueError as e:
        ap.error(str(e))

    s3store.load_env_file(args.env_file)
    if not s3store.configured() and not args.local_dir:
        ap.error("no S3_BUCKET configured and no --local-dir given; nothing to write to")
    try:
        feeds = feed_list(args)
    except ValueError as e:
        ap.error(str(e))

    sink = Sink(args.local_dir)
    sel = selectors.DefaultSelector()
    source = None
    for (ip, port), label in feeds:
        s = join_group(ip, port, args.iface)
        s.setblocking(False)
        source = enable_rx_timestamps(s, "auto")
        sel.register(s, selectors.EVENT_READ, (ip, port))

    print(f"capture: {len(feeds)} feed(s) on iface {args.iface}  flush every "
          f"{args.interval}s -> {sink.describe()}", flush=True)
    for (ip, port), label in feeds:
        print(f"  {ip}:{port:<6} {label}", flush=True)
    print(f"  clock {source.describe()}", flush=True)

    nanos = not args.usec

    def new_buf() -> io.BytesIO:
        b = io.BytesIO()
        b.write(pcap.global_header(nanos))
        return b

    buf, npkts, ip_id = new_buf(), 0, 0
    window_start = time.time()
    totals = {"pkts": 0, "windows": 0}

    def flush(now: float) -> None:
        nonlocal buf, npkts, window_start
        if npkts or args.empty_windows:
            dt = datetime.fromtimestamp(window_start, tz=timezone.utc)
            try:
                msg = sink.emit(buf.getvalue(), dt, npkts)
                print(f"  flush {dt:%H:%M:%S}  {msg}", flush=True)
                totals["windows"] += 1
            except Exception as exc:  # keep capturing even if a flush fails
                print(f"  flush FAILED: {exc!r}", file=sys.stderr, flush=True)
        buf, npkts, window_start = new_buf(), 0, now

    try:
        while True:
            timeout = max(0.0, window_start + args.interval - time.time())
            for key, _ in sel.select(min(timeout, 0.25)):
                while True:
                    try:
                        data, anc, _fl, addr = key.fileobj.recvmsg(65535, ANC_BUFSIZE)
                    except (BlockingIOError, InterruptedError):
                        break
                    now_ns = time.clock_gettime_ns(time.CLOCK_REALTIME)
                    rx_ns, _k = receive_timestamp(anc, source, now_ns)
                    ip_id = (ip_id + 1) & 0xFFFF
                    buf.write(pcap.record(data, rx_ns, addr[0], addr[1], key.data[0],
                                          key.data[1], ip_id, nanos))
                    npkts += 1
                    totals["pkts"] += 1
            now = time.time()
            if now - window_start >= args.interval:
                flush(now)
    except KeyboardInterrupt:
        flush(time.time())
        print(f"\ncapture stopped: {totals['pkts']} packets in {totals['windows']} "
              f"window(s).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
