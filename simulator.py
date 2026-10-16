"""
CME MDP 3.0 futures market-data simulator.

Emits wire-accurate SBE-encoded MDP 3.0 packets over UDP multicast: a random
walk of each product's price bounded to a price range, running for a submitted
duration.

--product picks up to four futures products from CME's config.xml (default
ES). Each product brings its channel, multicast feeds, tick size, tick value
and front-month contract; products that share a channel share its feeds and its
packet sequence, as on the real exchange. Every channel publishes:

  * Incremental feed A and B — identical packets, for A/B arbitration
  * Snapshot feed A and B    — a SnapshotFullRefresh loop, for book recovery
  * Instrument Replay A, B   — an instrument-definition loop, so a handler
                               learns each security id from the wire

Each walk opens at that product's previous close (fetched and cached by
settlement.py) unless you pass --start, and its band is derived around that
opening price unless you pass --low/--high.

Example:
    python simulator.py --duration 3600 --rate 20
    python simulator.py --product ES,NQ,CL,ZN --duration 3600
    python simulator.py --product NQ,RTY --pcap session.pcap    # one channel, two products
    python simulator.py --start ES=7772.50,CL=92 --product ES,CL
    python simulator.py --loss-a 0.05                          # make feed A lossy

By default the socket is bound to loopback (TTL 0) so packets never leave this
machine. Run `python products.py` to list what you can simulate.
"""
from __future__ import annotations

import argparse
import time

from channel import (INCREMENTAL, INSTRUMENT, SNAPSHOT, ChannelPublisher,
                     build_instruments, build_publishers)
from pcap import PcapWriter
from products import parse_product_list
from wire import add_schema_argument, open_wire
from transport import Transport
from lifecycle import stop_on_signals
from pyver import require_python

require_python()



def describe_origin(origin: dict) -> str:
    """Human-readable provenance for the opening price."""
    src = origin["source"]
    if src == "explicit":
        return "explicit --start"
    if src == "priorClose":
        who = f"{origin['symbol']} close {origin['closeDate']}"
        return who + " — CACHED, may be out of date" if origin["stale"] else who
    if src == "fallback":
        return "offline fallback reference level — NOT a quote"
    if origin["outOfBand"]:
        return "prior close is outside the band; opening at the center"
    return "no prior close available; opening at the band center"


def per_product(raw: str | None, codes: list[str], flag: str) -> dict:
    """'7772.5' (one product) or 'ES=7772.5,CL=92' -> {code: float}."""
    if raw is None:
        return {}
    if "=" not in raw:
        if len(codes) > 1:
            raise SystemExit(f"{flag} needs CODE=value pairs with several products, "
                             f"e.g. {flag} {codes[0]}=…,{codes[1]}=…")
        return {codes[0]: float(raw)}
    out = {}
    for part in raw.split(","):
        code, _, val = part.partition("=")
        code = code.strip().upper()
        if code not in codes:
            raise SystemExit(f"{flag}: {code} is not one of --product {','.join(codes)}")
        out[code] = float(val)
    return out


def instruments_from_args(args, codes):
    return build_instruments(
        codes, starts=per_product(args.start, codes, "--start"),
        lows=per_product(args.low, codes, "--low"),
        highs=per_product(args.high, codes, "--high"),
        bands=per_product(args.band, codes, "--band"),
        depth=args.depth, volatility=args.volatility, reversion=args.reversion,
        jump_prob=args.jump_prob, jump_size=args.jump_size, seed=args.seed,
        offline=args.offline, refresh=args.refresh, security_id=args.security_id)


def override_feed(pub: ChannelPublisher, group: str | None, port: int | None) -> None:
    """--group/--port replace incremental feed A (single-product runs)."""
    if not (group or port):
        return
    from dataclasses import replace
    feeds = pub.feeds[INCREMENTAL]
    a = next((f for f in feeds if f.feed_id == "A"), None)
    if a is None:
        return
    pub.feeds[INCREMENTAL] = [replace(a, ip=group or a.ip, port=port or a.port)
                              if f is a else f for f in feeds]


def print_banner(pubs, transport, args, wire):
    print(f"MDP 3.0 simulator — {sum(len(p.instruments) for p in pubs)} product(s) "
          f"on {len(pubs)} channel(s)")
    for pub in pubs:
        print(f"\n  channel {pub.channel_id} — {pub.label}")
        for kind in (INCREMENTAL, SNAPSHOT, INSTRUMENT):
            fs = "  ".join(f"{f.feed_id} {f.ip}:{f.port}" for f in pub.feeds[kind]) or "off"
            print(f"    {kind:<18} {fs}")
        for ins in pub.instruments:
            p, spec, eng, o = ins.product, ins.product.spec, ins.engine, ins.origin
            d, c = spec.decimals, ins.contract
            print(f"    {p.code:<4} {c.symbol:<7} {p.label}  security_id={ins.security_id}"
                  f"  last trade {c.expiry.isoformat()}")
            print(f"         range=[{eng.low:.{d}f}, {eng.high:.{d}f}]  tick={spec.tick:.10g} "
                  f"(${spec.tick_value:.4g}/tick)  open={eng.open_bid_px:.{d}f} bid"
                  f"  ({describe_origin(o)})")
            if o["clamped"]:
                print("         note: start price clamped into the band")
            if o["outOfBand"]:
                print("         note: the prior close is outside the band — opening at "
                      "the band center instead; pass --start to override")
    loss = "  ".join(f"loss {k}={v:.1%}" for k, v in transport.loss.items())
    print(f"\n  via {args.iface} ttl={args.ttl}  rate={args.rate}/s  duration={args.duration}s"
          f"  snapshot every {args.snapshot_interval or '—'}s"
          f"  definitions every {args.secdef_interval or '—'}s  {loss}")
    if transport.pcap is not None:
        print(f"  pcap  {args.pcap}")
    d = wire.describe()
    print(f"  schema {d['label']}  id={d['id']} version={d['version']}\n")


def main():
    ap = argparse.ArgumentParser(description="CME MDP 3.0 futures market-data simulator")
    ap.add_argument("--product", "--products", dest="product", action="append",
                    help="CME product code(s), comma-separated or repeated, up to 4 "
                         "(default ES)")
    ap.add_argument("--low", default=None,
                    help="lower price bound; CODE=value,… with several products")
    ap.add_argument("--high", default=None,
                    help="upper price bound; CODE=value,… with several products")
    ap.add_argument("--start", default=None,
                    help="opening price (default: previous session's close); "
                         "CODE=value,… with several products")
    ap.add_argument("--band", default=None,
                    help="width of the band derived around --start (default: 100 ticks)")
    ap.add_argument("--offline", action="store_true",
                    help="never fetch the prior close; use the cache only")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch the prior close even if cached")
    ap.add_argument("--duration", type=float, default=60, help="run time in seconds")
    ap.add_argument("--rate", type=float, default=10, help="book updates per second, per product")
    ap.add_argument("--depth", type=int, default=10, help="book depth (levels/side)")
    ap.add_argument("--volatility", type=float, default=0.6, help="gaussian shock stddev, in ticks")
    ap.add_argument("--reversion", type=float, default=0.004, help="mean-reversion pull to center")
    ap.add_argument("--jump-prob", type=float, default=0.0, help="per-step probability of a shock")
    ap.add_argument("--jump-size", type=float, default=8.0, help="mean jump magnitude, in ticks")
    ap.add_argument("--feeds", default="AB", choices=("AB", "A", "B"),
                    help="which feed letters to publish (default both)")
    ap.add_argument("--snapshot-interval", type=float, default=1.0,
                    help="seconds between snapshot loops (0 = no snapshot feed)")
    ap.add_argument("--secdef-interval", type=float, default=5.0,
                    help="seconds between instrument-definition loops (0 = off)")
    ap.add_argument("--loss-a", type=float, default=0.0,
                    help="fraction of feed-A packets to drop before the wire")
    ap.add_argument("--loss-b", type=float, default=0.0,
                    help="fraction of feed-B packets to drop before the wire")
    add_schema_argument(ap)
    ap.add_argument("--pcap", default=None, help="also write every packet sent to this .pcap")
    ap.add_argument("--pcap-usec", action="store_true",
                    help="microsecond pcap timestamps (default nanosecond)")
    ap.add_argument("--security-id", type=int, default=None,
                    help="instrument security id (single product; default: the product's)")
    ap.add_argument("--group", default=None,
                    help="override incremental feed A's group (single product)")
    ap.add_argument("--port", type=int, default=None,
                    help="override incremental feed A's port (single product)")
    ap.add_argument("--iface", default="127.0.0.1", help="outbound interface ip")
    ap.add_argument("--ttl", type=int, default=0, help="multicast TTL (0 = this host only)")
    ap.add_argument("--seed", type=int, default=None, help="rng seed (reproducible walk)")
    ap.add_argument("--quiet", action="store_true", help="suppress per-packet logging")
    args = ap.parse_args()
    stop_on_signals()

    try:
        codes = parse_product_list(args.product)
    except ValueError as e:
        ap.error(str(e))
    if len(codes) > 1 and (args.group or args.port or args.security_id is not None):
        ap.error("--group/--port/--security-id apply to a single --product")

    try:
        wire = open_wire(args.schema)
    except (FileNotFoundError, ValueError) as e:
        ap.error(str(e))
    instruments = instruments_from_args(args, codes)
    writer = PcapWriter(args.pcap, nanos=not args.pcap_usec) if args.pcap else None
    transport = Transport(args.iface, args.ttl, pcap=writer,
                          loss={"A": args.loss_a, "B": args.loss_b}, seed=args.seed)

    pubs = build_publishers(instruments, wire, transport, depth=args.depth, feeds=args.feeds)
    for pub in pubs:
        if args.snapshot_interval <= 0:
            pub.feeds[SNAPSHOT] = []
        if args.secdef_interval <= 0:
            pub.feeds[INSTRUMENT] = []
        override_feed(pub, args.group, args.port)

    print_banner(pubs, transport, args, wire)

    # Definitions first, so a handler started before us learns the instruments
    # before the first book update arrives.
    for pub in pubs:
        pub.definition_loop()
        pub.snapshot_loop()

    interval = 1.0 / args.rate
    start = time.time()
    t_end = start + args.duration
    next_tick = start
    next_snap = start + args.snapshot_interval if args.snapshot_interval > 0 else float("inf")
    next_defs = start + args.secdef_interval if args.secdef_interval > 0 else float("inf")
    multi = len(instruments) > 1
    try:
        while time.time() < t_end:
            now = time.time()
            if now >= next_snap:
                for pub in pubs:
                    pub.snapshot_loop()
                next_snap += args.snapshot_interval
            if now >= next_defs:
                for pub in pubs:
                    pub.definition_loop()
                next_defs += args.secdef_interval
            if now < next_tick:
                time.sleep(min(next_tick - now, 0.005))
                continue
            next_tick += interval
            for pub in pubs:
                for ins, _incs, trades, pkt in pub.step():
                    if args.quiet:
                        continue
                    eng, d = ins.engine, ins.product.spec.decimals
                    bb, bo = eng.best_bid(), eng.best_offer()
                    trd = (f"  TRADE {trades[0].size}@{trades[0].price:.{d}f} "
                           f"{trades[0].aggressor}") if trades else ""
                    tag = f"{ins.code:<4}" if multi else ""
                    print(f"{tag}ch={pub.channel_id} seq={pub.seq:<6} "
                          f"mid={eng.mid:<12.{d + 1}f} bid={bb:.{d}f} "
                          f"offer={bo:.{d}f} len={len(pkt)}B{trd}")
    except KeyboardInterrupt:
        print()
    finally:
        transport.close()

    print(f"\nDone. Sent {transport.sent} datagrams ({transport.bytes:,} B).")
    for pub in pubs:
        print(f"  channel {pub.channel_id}: {pub.seq} incremental packets, "
              f"{pub.snapshot_loops} snapshot loops, {pub.definition_loops} definition loops"
              f"  (×{len(pub.feeds[INCREMENTAL])} feeds)")
    if transport.dropped:
        print("  dropped by --loss: " + ", ".join(f"feed {k} {v}"
                                                   for k, v in sorted(transport.dropped.items())))
    if writer is not None:
        print(f"  pcap: {writer.count} packets -> {args.pcap}")


if __name__ == "__main__":
    main()
