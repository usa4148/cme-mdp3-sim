"""
CME MDP 3.0 ES futures market-data simulator.

Emits wire-accurate SBE-encoded MDP 3.0 packets over UDP multicast: a random
walk of the E-mini S&P 500 (ES) mid-price bounded to a submitted price range,
running for a submitted duration.

Example:
    python simulator.py --low 5000 --high 5025 --duration 3600 --rate 20

Defaults match CME channel 310 (E-mini S&P 500) Incremental feed A. By default
the socket is bound to loopback (TTL 0) so packets never leave this machine.
"""
from __future__ import annotations

import argparse
import os
import socket
import struct
import time

from engine import MarketEngine, TICK_VALUE
from packet import build_packet
from sbe import Schema

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema", "mdp3.xml")

# CME channel 310 (E-mini S&P 500), Incremental feed A
DEFAULT_GROUP = "224.0.31.1"
DEFAULT_PORT = 14310


def make_socket(iface: str, ttl: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
    # allow local consumers on the same host to receive our packets
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    except OSError:
        pass
    return s


def entries_from_increments(engine, security_id, incs):
    entries = []
    for inc in incs:
        entries.append({
            "mdEntryPx": inc.price,
            "mdEntrySize": inc.size,
            "securityId": security_id,
            "rptSeq": engine.next_rpt_seq(),
            "numberOfOrders": inc.orders,
            "mdPriceLevel": inc.level,
            "mdUpdateAction": inc.action,
            "mdEntryType": inc.side,
        })
    return entries


def entries_from_trades(engine, security_id, trades):
    entries = []
    for t in trades:
        entries.append({
            "mdEntryPx": t.price,
            "mdEntrySize": t.size,
            "securityId": security_id,
            "rptSeq": engine.next_rpt_seq(),
            "numberOfOrders": 0,
            "aggressorSide": t.aggressor,
            "mdUpdateAction": "New",
            "mdEntryType": "Trade",
        })
    return entries


def main():
    ap = argparse.ArgumentParser(description="CME MDP 3.0 ES market-data simulator")
    ap.add_argument("--low", type=float, required=True, help="lower price bound")
    ap.add_argument("--high", type=float, required=True, help="upper price bound")
    ap.add_argument("--duration", type=float, default=60, help="run time in seconds")
    ap.add_argument("--rate", type=float, default=10, help="book updates per second")
    ap.add_argument("--depth", type=int, default=10, help="book depth (levels/side)")
    ap.add_argument("--volatility", type=float, default=0.6, help="gaussian shock stddev, in ticks")
    ap.add_argument("--reversion", type=float, default=0.004, help="mean-reversion pull to center")
    ap.add_argument("--jump-prob", type=float, default=0.0, help="per-step probability of a shock")
    ap.add_argument("--jump-size", type=float, default=8.0, help="mean jump magnitude, in ticks")
    ap.add_argument("--security-id", type=int, default=42003, help="ES instrument security id")
    ap.add_argument("--group", default=DEFAULT_GROUP, help="multicast group")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="multicast port")
    ap.add_argument("--iface", default="127.0.0.1", help="outbound interface ip")
    ap.add_argument("--ttl", type=int, default=0, help="multicast TTL (0 = this host only)")
    ap.add_argument("--seed", type=int, default=None, help="rng seed (reproducible walk)")
    ap.add_argument("--quiet", action="store_true", help="suppress per-packet logging")
    args = ap.parse_args()

    schema = Schema(SCHEMA_PATH)
    engine = MarketEngine(args.low, args.high, depth=args.depth,
                          volatility=args.volatility, reversion=args.reversion,
                          jump_prob=args.jump_prob, jump_size=args.jump_size,
                          seed=args.seed)
    sock = make_socket(args.iface, args.ttl)
    dest = (args.group, args.port)

    print(f"MDP 3.0 ES simulator  channel=310  security_id={args.security_id}")
    print(f"  range=[{args.low}, {args.high}]  tick=0.25 (${TICK_VALUE}/tick)"
          f"  center={engine.center}")
    print(f"  multicast={args.group}:{args.port} via {args.iface} ttl={args.ttl}"
          f"  rate={args.rate}/s  duration={args.duration}s")
    print(f"  schema id={schema.schema_id} version={schema.version}\n")

    seq = 0
    interval = 1.0 / args.rate
    t_end = time.time() + args.duration
    next_tick = time.time()
    pkts = 0
    while time.time() < t_end:
        now = time.time()
        if now < next_tick:
            time.sleep(min(next_tick - now, 0.005))
            continue
        next_tick += interval

        incs, trades = engine.step()
        messages = []
        now_ns = time.time_ns()

        if incs:
            entries = entries_from_increments(engine, args.security_id, incs)
            messages.append(schema.encode_message(
                "MDIncrementalRefreshBook",
                {"transactTime": now_ns, "matchEventIndicator": ["LastQuoteMsg", "EndOfEvent"]},
                entries))
        if trades:
            entries = entries_from_trades(engine, args.security_id, trades)
            messages.append(schema.encode_message(
                "MDIncrementalRefreshTradeSummary",
                {"transactTime": now_ns, "matchEventIndicator": ["LastTradeMsg", "EndOfEvent"]},
                entries))
        if not messages:
            continue

        seq += 1
        pkt = build_packet(seq, now_ns, messages)
        sock.sendto(pkt, dest)
        pkts += 1
        if not args.quiet:
            bb, bo = engine.best_bid(), engine.best_offer()
            trd = f"  TRADE {trades[0].size}@{trades[0].price} {trades[0].aggressor}" if trades else ""
            print(f"seq={seq:<6} mid={engine.mid:<8} bid={bb} offer={bo} "
                  f"len={len(pkt)}B{trd}")

    print(f"\nDone. Sent {pkts} packets ({seq} sequenced).")


if __name__ == "__main__":
    main()
