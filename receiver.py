"""
Multicast group membership for every receive-side tool.

CME puts a channel's incremental, snapshot and instrument feeds on *different
groups that share one port* (channel 310 feed A: 224.0.31.1, .22 and .43, all
on 14310). A socket bound to the wildcard address receives every group joined
on that port by anyone on the host on Linux, so each socket here binds to its
group address instead — that is the filter — and also clears
IP_MULTICAST_ALL where Linux has it.

SO_REUSEADDR/SO_REUSEPORT let several processes (capture, ladder, consumer)
join the same feed on one host and each get every packet.

A socket can join its group on several interfaces at once
(`iface="127.0.0.1,10.1.2.3"`): one membership per interface, so a capture box
hears both feeds it sends to itself over loopback and feeds arriving on its
LAN interface. A datagram arrives on one interface, so this never duplicates
packets.
"""
from __future__ import annotations

import ipaddress
import socket
import sys

IP_MULTICAST_ALL = 49          # Linux; Python does not export it


def parse_ifaces(spec) -> list[str]:
    """'127.0.0.1,10.1.2.3' (or a list) -> validated, de-duplicated IPv4 addresses.

    '0.0.0.0' (or empty) means the kernel's default interface for the group.
    """
    items = spec if isinstance(spec, (list, tuple)) else str(spec or "0.0.0.0").split(",")
    out = []
    for item in items:
        item = str(item).strip() or "0.0.0.0"
        try:
            addr = str(ipaddress.IPv4Address(item))
        except ValueError:
            raise ValueError(f"interface {item!r} is not an IPv4 address") from None
        if addr not in out:
            out.append(addr)
    return out or ["0.0.0.0"]


def first_iface(spec) -> str:
    """The interface a *sender* uses when given a receive-side list."""
    return parse_ifaces(spec)[0]


def join_group(group: str, port: int, iface="0.0.0.0",
               timeout: float | None = None, bind_group: bool = True) -> socket.socket:
    """A socket bound to (`group`, `port`) and joined to `group` on each of `iface`
    (one address, a comma-separated list, or a list)."""
    ifaces = parse_ifaces(iface)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    if sys.platform.startswith("linux"):
        try:
            s.setsockopt(socket.IPPROTO_IP, IP_MULTICAST_ALL, 0)
        except OSError:
            pass
    try:
        s.bind((group if bind_group else "", port))
    except OSError:
        s.bind(("", port))                 # platforms that refuse a group bind
    for addr in ifaces:
        mreq = socket.inet_aton(group) + socket.inet_aton(addr)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as e:
            s.close()
            raise OSError(e.errno, f"cannot join {group} on interface {addr}: {e.strerror}"
                          f" (is {addr} an address of this host?)") from None
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    except OSError:
        pass
    if timeout is not None:
        s.settimeout(timeout)
    return s
