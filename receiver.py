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
"""
from __future__ import annotations

import socket
import sys

IP_MULTICAST_ALL = 49          # Linux; Python does not export it


def join_group(group: str, port: int, iface: str = "0.0.0.0",
               timeout: float | None = None, bind_group: bool = True) -> socket.socket:
    """A socket bound to (`group`, `port`) and joined to `group` on `iface`."""
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
    mreq = socket.inet_aton(group) + socket.inet_aton(iface or "0.0.0.0")
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
    except OSError:
        pass
    if timeout is not None:
        s.settimeout(timeout)
    return s
