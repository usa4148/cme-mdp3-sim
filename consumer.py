"""
MDP 3.0 consumer / feed handler.

Joins the multicast group, receives packets, decodes the binary packet header
and every SBE message inside, and prints them. This proves the simulator's
output is wire-correct: an independent decoder reconstructs bids/offers/trades
purely from the bytes on the wire.
"""
from __future__ import annotations

import argparse
import os
import socket
import struct

from packet import parse_packet
from sbe import Schema
from pyver import require_python

require_python()

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema", "mdp3.xml")
DEFAULT_GROUP = "224.0.31.1"
DEFAULT_PORT = 14310


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


def fmt_entry(e):
    action = e.get("mdUpdateAction")
    etype = e.get("mdEntryType")
    px = e.get("mdEntryPx")
    sz = e.get("mdEntrySize")
    lvl = e.get("mdPriceLevel")
    extra = f" L{lvl}" if lvl is not None else ""
    if etype == "Trade":
        return f"    TRADE  {sz}@{px}  aggressor={e.get('aggressorSide')}  rpt={e.get('rptSeq')}"
    return f"    {action:<7}{etype:<6} {sz}@{px}{extra}  ord={e.get('numberOfOrders')} rpt={e.get('rptSeq')}"


def main():
    ap = argparse.ArgumentParser(description="MDP 3.0 multicast consumer/decoder")
    ap.add_argument("--group", default=DEFAULT_GROUP)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--iface", default="127.0.0.1")
    ap.add_argument("--count", type=int, default=0, help="stop after N packets (0 = forever)")
    ap.add_argument("--summary", action="store_true", help="one line per packet")
    args = ap.parse_args()

    schema = Schema(SCHEMA_PATH)
    sock = join_group(args.group, args.port, args.iface)
    print(f"Listening on {args.group}:{args.port} (iface {args.iface})  "
          f"schema id={schema.schema_id} v{schema.version}\n")

    n, last_seq = 0, None
    while True:
        buf, _ = sock.recvfrom(65535)
        seq, ts_ns, frames = parse_packet(buf)
        gap = "" if last_seq is None or seq == last_seq + 1 else f"  !! GAP (prev {last_seq})"
        last_seq = seq
        decoded = [schema.decode_message(f)[0] for f in frames]
        if args.summary:
            kinds = ",".join(d["template"].replace("MDIncrementalRefresh", "") for d in decoded)
            print(f"seq={seq:<6} ts={ts_ns} msgs={len(frames)} [{kinds}] {len(buf)}B{gap}")
        else:
            print(f"── packet seq={seq}  sendingTime={ts_ns}ns  {len(frames)} msg(s)  {len(buf)}B{gap}")
            for d in decoded:
                print(f"  {d['template']} (t{d['id']})  transactTime={d['root'].get('transactTime')}"
                      f"  evt={d['root'].get('matchEventIndicator')}")
                for e in d["groups"].get("MDEntries", []):
                    print(fmt_entry(e))
        n += 1
        if args.count and n >= args.count:
            break
    print(f"\nReceived {n} packets.")


if __name__ == "__main__":
    main()
