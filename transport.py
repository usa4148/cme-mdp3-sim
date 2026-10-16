"""
Outbound multicast transport for the simulator.

One UDP socket sends every feed (incremental, snapshot, instrument replay, A and
B) — the destination address is per packet. Two optional taps sit on the send
path:

* **Loss** (`loss={"A": 0.02}`) drops that fraction of packets on one feed
  letter before they reach the wire, so A/B arbitration has something to do:
  on loopback nothing is ever lost on its own. A packet dropped on A still goes
  out on B unless B is dropped too, which is the only way a real gap happens.
* **PCAP** writes every datagram that actually went out, with the source
  address the socket really used, so the file is what a sniffer would have seen.

`deliver` replaces the socket (the dashboard records a session without touching
the network) and `observer(feed, pkt, ts_ns, sent)` sees every packet, including
the ones loss dropped.
"""
from __future__ import annotations

import random
import socket
import time

from pcap import PcapWriter


def make_socket(iface: str, ttl: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    if iface and iface != "0.0.0.0":
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
    # allow local consumers on the same host to receive our packets
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    except OSError:
        pass
    # bind now so the source port is known before the first send (for the pcap)
    s.bind((iface if iface and iface != "0.0.0.0" else "", 0))
    return s


class Transport:
    def __init__(self, iface: str = "127.0.0.1", ttl: int = 0,
                 pcap: PcapWriter | None = None, loss: dict | None = None,
                 seed: int | None = None, sock: socket.socket | None = None,
                 deliver=None, observer=None, src: tuple = ("127.0.0.1", 30000)):
        self.deliver = deliver
        self.observer = observer
        if deliver is not None:
            self.sock = None
            self.src = src
        else:
            self.sock = sock or make_socket(iface, ttl)
            host, port = self.sock.getsockname()[:2]
            self.src = (iface if host == "0.0.0.0" and iface else host, port)
        self.pcap = pcap
        self.loss = {k.upper(): v for k, v in (loss or {}).items() if v}
        self.rng = random.Random(None if seed is None else seed ^ 0x5EED)
        self.sent = 0
        self.bytes = 0
        self.dropped: dict[str, int] = {}

    def send(self, feed, pkt: bytes, ts_ns: int | None = None) -> bool:
        """Send `pkt` to `feed` (a products.Feed). False if loss dropped it."""
        ts_ns = ts_ns if ts_ns is not None else time.time_ns()
        p = self.loss.get(feed.feed_id)
        if p and self.rng.random() < p:
            self.dropped[feed.feed_id] = self.dropped.get(feed.feed_id, 0) + 1
            if self.observer:
                self.observer(feed, pkt, ts_ns, False)
            return False
        dst = (feed.ip, feed.port)
        if self.deliver is not None:
            self.deliver(pkt, dst)
        else:
            self.sock.sendto(pkt, dst)
        self.sent += 1
        self.bytes += len(pkt)
        if self.pcap is not None:
            self.pcap.write(pkt, ts_ns, self.src, dst)
        if self.observer:
            self.observer(feed, pkt, ts_ns, True)
        return True

    def close(self) -> None:
        if self.pcap is not None:
            self.pcap.close()
        if self.sock is not None:
            self.sock.close()
