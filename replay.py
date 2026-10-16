#!/usr/bin/env python3
"""
Replay packet captures onto UDP multicast, or decode them without sending.

Sources are .pcap/.pcapng files, directories of them (replayed in name order,
which for capture.py windows is time order), or S3 prefixes written by
capture.py (`s3://bucket/prefix` or `s3:prefix` with S3_BUCKET from
config.env). Each datagram goes back to its original group and port, so every
receive-side tool — consumer, ladder, capture — works on a replay exactly as on
the live simulator. Captures from a real CME line work too.

Timing follows the capture's timestamps: `--speed 1` is real time, `--speed 10`
ten times faster, `--speed 0` as fast as the socket allows. Long silences can
be cut with `--max-gap`.

    python3 replay.py session.pcap
    python3 replay.py ./pcaps --speed 5 --loop 0          # forever
    python3 replay.py s3:mdp3-pcap/myhost/2026/10/16 --product ES
    python3 replay.py session.pcap --dry-run              # decode + report, send nothing
"""
from __future__ import annotations

import argparse
import time

import pcap
import s3store
from feedhandler import FeedHandler
from lifecycle import stop_on_signals
from products import get_product, parse_product_list, simulatable
from transport import make_socket
from wire import add_schema_argument, open_wire
from pyver import require_python

require_python()



def sources(paths: list[str]):
    """Yield (name, bytes-or-path) for every capture behind `paths`."""
    s3 = None
    for p in paths:
        if p.startswith("s3:"):
            if s3 is None:
                s3store.load_env_file()
                s3 = s3store.client()
            bkt, pfx = s3store.parse_url(p)
            for o in s3store.list_keys(s3, bkt, pfx):
                if o["Key"].endswith((".pcap", ".pcapng")):
                    yield f"s3://{bkt}/{o['Key']}", s3store.get(s3, bkt, o["Key"])
        else:
            for f in pcap.capture_files(p):
                yield f, f


def all_routes_handler(wire, products=None) -> FeedHandler:
    """A feed handler that knows every simulatable channel's feeds."""
    h = FeedHandler(wire, hold_ms=1e9)         # replay order is final: hold until window
    h.add_routes_for(products or simulatable())
    return h


def summarize(blobs, products=None, schema: str = "auto") -> FeedHandler:
    """Decode captures through the feed handler and print what they contain."""
    wire = open_wire(schema)
    h = all_routes_handler(wire, products)
    events = []
    h.kw["on_event"] = None
    for ch in h.channels.values():
        ch.on_event = lambda k, t: events.append((k, t))
    stats = pcap.ReadStats()
    unrouted: dict[tuple, int] = {}
    first = last = None
    n = 0
    for blob in blobs:
        for d in pcap.read_packets(blob, stats):
            n += 1
            first = d.ts_ns if first is None else first
            last = d.ts_ns
            if d.dst not in h.routes:
                unrouted[d.dst] = unrouted.get(d.dst, 0) + 1
                continue
            h.on_datagram(d.dst, d.payload, d.ts_ns)
    for ch in h.channels.values():
        ch.tick(1 << 62)                        # flush anything still held
    span = (last - first) / 1e9 if first is not None else 0
    print(f"{n} datagrams over {span:.3f}s  ({stats.skipped} non-UDP frames, "
          f"{stats.fragments} fragments)  schema {wire.name}")
    for kind, text in events:
        if kind in ("gap", "recovered", "mismatch", "schema"):
            print(f"  ** {kind:<10} {text}")
    for chid, ch in sorted(h.channels.items()):
        if not ch.instruments and not ch.arb.accepted:
            continue
        a = ch.arb
        only = ch.one_feed_only()
        print(f"  channel {chid}: {a.accepted} incremental packets (seq ..{ch.last_seq}), "
              f"won A {a.won.get('A', 0)} / B {a.won.get('B', 0)}, dup {a.duplicates}, "
              f"one feed only A {only.get('A', 0)} / B {only.get('B', 0)}, "
              f"lost on both {a.lost} · {ch.snapshot_packets} snapshot, "
              f"{ch.definition_packets} definition pkts")
        for ins in sorted(ch.instruments.values(), key=lambda i: i.name):
            dec = ins.definition.decimals if ins.definition else 4
            px = lambda v: "--" if v is None else f"{v:.{dec}f}"
            print(f"    {ins.name:<8} {ins.state:<10} rptSeq {ins.rpt_seq}  "
                  f"bid {px(ins.book.best_bid())} / offer {px(ins.book.best_offer())}  "
                  f"last {px(ins.book.last_trade.price) if ins.book.last_trade else '--'}  "
                  f"gaps {ins.gaps}  snapshot checks {ins.snapshots_checked} "
                  f"({ins.snapshot_mismatches} mismatch)")
    for (ip, port), c in sorted(unrouted.items()):
        print(f"  {ip}:{port}  {c} datagrams on no known MDP feed")
    return h


def wanted_destinations(codes) -> set | None:
    if not codes:
        return None
    h = FeedHandler(None)
    h.add_routes_for([get_product(c) for c in codes])
    return set(h.routes)


def replay(args) -> int:
    codes = parse_product_list(args.product) if args.product else None
    keep = wanted_destinations(codes)
    sock = make_socket(args.iface, args.ttl)
    max_gap_ns = int(args.max_gap * 1e9) if args.max_gap else None
    sent = nbytes = 0
    loops = 0
    t0 = time.monotonic()
    try:
        while True:
            loops += 1
            base_cap = base_wall = prev_cap = None
            shift = 0                                   # silence removed by --max-gap
            for name, src in sources(args.paths):
                if not args.quiet:
                    print(f"  {name}", flush=True)
                for d in pcap.read_packets(src):
                    if keep is not None and d.dst not in keep:
                        continue
                    if args.speed > 0 and d.ts_ns:
                        if base_cap is None:
                            base_cap, base_wall = d.ts_ns, time.monotonic_ns()
                        if max_gap_ns and prev_cap is not None and d.ts_ns - prev_cap > max_gap_ns:
                            shift += d.ts_ns - prev_cap - max_gap_ns
                        prev_cap = d.ts_ns
                        due = base_wall + (d.ts_ns - base_cap - shift) / args.speed
                        wait = (due - time.monotonic_ns()) / 1e9
                        if wait > 0:
                            time.sleep(wait)
                    sock.sendto(d.payload, d.dst)
                    sent += 1
                    nbytes += len(d.payload)
            if args.loop and loops >= args.loop:
                break
    except KeyboardInterrupt:
        print()
    finally:
        sock.close()
    dt = time.monotonic() - t0
    print(f"replayed {sent} datagrams ({nbytes:,} B) in {dt:.2f}s"
          f"{f' over {loops} loop(s)' if loops > 1 else ''} via {args.iface} ttl={args.ttl}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay MDP 3.0 captures onto multicast")
    ap.add_argument("paths", nargs="+",
                    help=".pcap/.pcapng files, directories, or s3://bucket/prefix / s3:prefix")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="timing multiplier (1 = real time, 0 = as fast as possible)")
    ap.add_argument("--loop", type=int, default=1, help="passes over the input (0 = forever)")
    ap.add_argument("--max-gap", type=float, default=None, metavar="SECONDS",
                    help="cut any silence longer than this")
    ap.add_argument("--product", "--products", dest="product", action="append",
                    help="replay only these products' channel feeds")
    ap.add_argument("--iface", default="127.0.0.1", help="outbound interface ip")
    ap.add_argument("--ttl", type=int, default=0, help="multicast TTL (0 = this host only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="decode through the feed handler and report; send nothing")
    ap.add_argument("--quiet", action="store_true", help="don't list each capture file")
    add_schema_argument(ap, receive=True)
    args = ap.parse_args()
    stop_on_signals()
    if args.dry_run:
        codes = parse_product_list(args.product) if args.product else None
        try:
            summarize((src for _name, src in sources(args.paths)),
                      [get_product(c) for c in codes] if codes else None, args.schema)
        except KeyboardInterrupt:
            pass
        return 0
    return replay(args)


if __name__ == "__main__":
    raise SystemExit(main())
