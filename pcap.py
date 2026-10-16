"""
Packet capture files: write the feed out, read it back in.

Writing. The simulator and the capture service see datagrams at the socket
layer (no root, no raw sniffing), so the link/network/transport headers are not
available. To produce a genuinely valid .pcap that Wireshark and tcpdump decode
as UDP multicast, Ethernet + IPv4 + UDP headers are synthesized around each
payload from what is known: the source (ip, port), the multicast group/port and
the timestamp. Files are classic libpcap, link type EN10MB, with nanosecond
timestamps by default (microsecond on request, for old tools).

Reading. `read_packets()` yields every IPv4 UDP datagram in a capture, whether
the simulator wrote it or it came off a real network: classic pcap in either
byte order and either resolution, and pcapng (Wireshark's default). Link types:
Ethernet (with 802.1Q/QinQ tags), Linux cooked v1/v2, raw IP, and BSD loopback
(`tcpdump -i lo0` on macOS). IP fragments are skipped and counted — MDP packets
fit one datagram, and a fragment's UDP header is in another frame.

    python3 pcap.py capture.pcap            # summarize: feeds, packets, span
"""
from __future__ import annotations

import argparse
import os
import socket
import struct
from dataclasses import dataclass
from typing import BinaryIO, Iterator

from pyver import require_python

require_python()

MAGIC_US = 0xA1B2C3D4            # microsecond timestamps
MAGIC_NS = 0xA1B23C4D            # nanosecond timestamps
PCAPNG_SHB = 0x0A0D0D0A
DLT_NULL, DLT_EN10MB, DLT_RAW, DLT_LINUX_SLL = 0, 1, 101, 113
DLT_IPV4, DLT_LINUX_SLL2 = 228, 276
SNAPLEN = 65535

_REC_HEADER = struct.Struct("<IIII")
SRC_MAC = bytes([0x02, 0x00, 0x00, 0x00, 0x00, 0x01])   # locally administered


# ------------------------------------------------------------------ write ----
def global_header(nanos: bool = True, snaplen: int = SNAPLEN) -> bytes:
    return struct.pack("<IHHiIII", MAGIC_NS if nanos else MAGIC_US, 2, 4, 0, 0,
                       snaplen, DLT_EN10MB)


def mcast_mac(group_ip: str) -> bytes:
    """01:00:5e + the low 23 bits of the group address (RFC 1112)."""
    o = socket.inet_aton(group_ip)
    return bytes([0x01, 0x00, 0x5E, o[1] & 0x7F, o[2], o[3]])


def ip_checksum(hdr: bytes) -> int:
    s = sum(struct.unpack(f"!{len(hdr) // 2}H", hdr))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return (~s) & 0xFFFF


def frame(payload: bytes, src_ip: str, src_port: int, dst_ip: str, dst_port: int,
          ip_id: int = 0, ttl: int = 64) -> bytes:
    """Ethernet + IPv4 + UDP around a payload. UDP checksum 0 = not computed."""
    eth = mcast_mac(dst_ip) + SRC_MAC + struct.pack("!H", 0x0800)
    udp = struct.pack("!HHHH", src_port, dst_port, 8 + len(payload), 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), ip_id & 0xFFFF, 0,
                     max(1, ttl), socket.IPPROTO_UDP, 0,
                     socket.inet_aton(src_ip), socket.inet_aton(dst_ip))
    ip = ip[:10] + struct.pack("!H", ip_checksum(ip)) + ip[12:]
    return eth + ip + udp


def record(payload: bytes, ts_ns: int, src_ip: str, src_port: int, dst_ip: str,
           dst_port: int, ip_id: int = 0, nanos: bool = True,
           snaplen: int = SNAPLEN) -> bytes:
    """One pcap record (header + synthesized frame) for a datagram."""
    fr = frame(payload, src_ip, src_port, dst_ip, dst_port, ip_id)
    sec, sub = divmod(ts_ns, 1_000_000_000)
    if not nanos:
        sub //= 1000
    incl = min(len(fr), snaplen)
    return _REC_HEADER.pack(sec, sub, incl, len(fr)) + fr[:incl]


class PcapWriter:
    """Append datagrams to a .pcap file (or any binary stream)."""

    def __init__(self, target: str | BinaryIO, nanos: bool = True):
        self._own = isinstance(target, str)
        self.fh = open(target, "wb") if self._own else target
        self.nanos = nanos
        self.count = 0
        self._ip_id = 0
        self.fh.write(global_header(nanos))

    def write(self, payload: bytes, ts_ns: int, src: tuple, dst: tuple) -> None:
        self._ip_id = (self._ip_id + 1) & 0xFFFF
        self.fh.write(record(payload, ts_ns, src[0], src[1], dst[0], dst[1],
                             self._ip_id, self.nanos))
        self.count += 1

    def flush(self) -> None:
        self.fh.flush()

    def close(self) -> None:
        if self._own:
            self.fh.close()
        else:
            self.fh.flush()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ------------------------------------------------------------------- read ----
@dataclass
class Datagram:
    ts_ns: int
    src: tuple            # (ip, port)
    dst: tuple            # (ip, port)
    payload: bytes


@dataclass
class ReadStats:
    frames: int = 0
    udp: int = 0
    skipped: int = 0      # not IPv4/UDP
    fragments: int = 0


class PcapError(ValueError):
    pass


def _ipv4_udp(buf: bytes, off: int, stats: ReadStats):
    """Parse IPv4 + UDP at `off`; None if it isn't an unfragmented UDP datagram."""
    if len(buf) < off + 20 or buf[off] >> 4 != 4:
        return None
    ihl = (buf[off] & 0x0F) * 4
    total_len, flags_frag = struct.unpack_from("!H2xH", buf, off + 2)
    if flags_frag & 0x3FFF:                          # MF set or offset != 0
        stats.fragments += 1
        return None
    if buf[off + 9] != socket.IPPROTO_UDP:
        return None
    src_ip = socket.inet_ntoa(buf[off + 12:off + 16])
    dst_ip = socket.inet_ntoa(buf[off + 16:off + 20])
    u = off + ihl
    if len(buf) < u + 8:
        return None
    sport, dport, ulen = struct.unpack_from("!HHH", buf, u)
    end = min(len(buf), u + ulen, off + total_len if total_len else len(buf))
    return (src_ip, sport), (dst_ip, dport), bytes(buf[u + 8:end])


def _l3_offset(linktype: int, fr: bytes) -> int | None:
    """Byte offset of the IPv4 header in a frame, or None."""
    if linktype == DLT_EN10MB:
        off, etype = 14, struct.unpack_from("!H", fr, 12)[0] if len(fr) >= 14 else 0
        while etype in (0x8100, 0x88A8, 0x9100) and len(fr) >= off + 4:   # VLAN tags
            etype = struct.unpack_from("!H", fr, off + 2)[0]
            off += 4
        return off if etype == 0x0800 else None
    if linktype in (DLT_RAW, DLT_IPV4):
        return 0
    if linktype == DLT_LINUX_SLL:
        return 16 if len(fr) >= 16 and struct.unpack_from("!H", fr, 14)[0] == 0x0800 else None
    if linktype == DLT_LINUX_SLL2:
        return 20 if len(fr) >= 20 and struct.unpack_from("!H", fr, 0)[0] == 0x0800 else None
    if linktype == DLT_NULL:
        if len(fr) < 4:
            return None
        fam = struct.unpack_from("<I", fr, 0)[0]
        if fam > 0xFFFF:                              # written big-endian
            fam = struct.unpack_from(">I", fr, 0)[0]
        return 4 if fam == 2 else None               # AF_INET
    return None


def _frames_classic(data: bytes) -> Iterator[tuple[int, int, bytes]]:
    (magic,) = struct.unpack_from("<I", data, 0)
    for endian in "<>":
        (magic,) = struct.unpack_from(endian + "I", data, 0)
        if magic in (MAGIC_US, MAGIC_NS):
            break
    else:
        raise PcapError("not a pcap file")
    scale = 1 if magic == MAGIC_NS else 1000
    (linktype,) = struct.unpack_from(endian + "I", data, 20)
    linktype &= 0x0FFFFFFF                          # high bits: FCS info
    rec = struct.Struct(endian + "IIII")
    off = 24
    while off + 16 <= len(data):
        sec, sub, incl, _orig = rec.unpack_from(data, off)
        off += 16
        yield linktype, sec * 1_000_000_000 + sub * scale, data[off:off + incl]
        off += incl


def _frames_pcapng(data: bytes) -> Iterator[tuple[int, int, bytes]]:
    off, endian, ifaces = 0, "<", []
    while off + 12 <= len(data):
        (btype,) = struct.unpack_from(endian + "I", data, off)
        if btype == PCAPNG_SHB:
            bom = struct.unpack_from("<I", data, off + 8)[0]
            endian = "<" if bom == 0x1A2B3C4D else ">"
            ifaces = []                               # new section, new interfaces
        (blen,) = struct.unpack_from(endian + "I", data, off + 4)
        if blen < 12 or off + blen > len(data):
            break
        body = data[off + 8:off + blen - 4]
        if btype == 1:                                # Interface Description
            linktype = struct.unpack_from(endian + "H", body, 0)[0]
            per_sec = 10 ** 6                         # default: microseconds
            o = 8
            while o + 4 <= len(body):
                code, ln = struct.unpack_from(endian + "HH", body, o)
                if code == 0:
                    break
                if code == 9 and ln >= 1:             # if_tsresol
                    v = body[o + 4]
                    per_sec = 2 ** (v & 0x7F) if v & 0x80 else 10 ** (v & 0x7F)
                o += 4 + ((ln + 3) & ~3)
            ifaces.append((linktype, per_sec))
        elif btype == 6 and ifaces:                   # Enhanced Packet
            iface, hi, lo, cap, _orig = struct.unpack_from(endian + "IIIII", body, 0)
            linktype, per_sec = ifaces[iface] if iface < len(ifaces) else ifaces[0]
            ts = (hi << 32) | lo
            # integer math: an epoch in ns needs 61 bits, more than a float holds
            yield linktype, ts * 1_000_000_000 // per_sec, body[20:20 + cap]
        elif btype == 3 and ifaces:                   # Simple Packet (no timestamp)
            (orig,) = struct.unpack_from(endian + "I", body, 0)
            yield ifaces[0][0], 0, body[4:4 + orig]
        off += blen


def read_packets(source: str | bytes, stats: ReadStats | None = None) -> Iterator[Datagram]:
    """Every IPv4/UDP datagram in a capture, in file order."""
    data = source if isinstance(source, (bytes, bytearray)) else open(source, "rb").read()
    stats = stats if stats is not None else ReadStats()
    if len(data) < 24:
        raise PcapError("file too short to be a capture")
    is_ng = struct.unpack_from("<I", data, 0)[0] == PCAPNG_SHB
    frames = _frames_pcapng(data) if is_ng else _frames_classic(data)
    for linktype, ts_ns, fr in frames:
        stats.frames += 1
        l3 = _l3_offset(linktype, fr)
        parsed = _ipv4_udp(fr, l3, stats) if l3 is not None else None
        if parsed is None:
            stats.skipped += 1
            continue
        stats.udp += 1
        src, dst, payload = parsed
        yield Datagram(ts_ns, src, dst, payload)


def capture_files(path: str) -> list[str]:
    """A file, or every .pcap/.pcapng under a directory, in name order."""
    if os.path.isdir(path):
        out = []
        for root, _dirs, files in os.walk(path):
            out += [os.path.join(root, f) for f in files
                    if f.endswith((".pcap", ".pcapng", ".cap"))]
        return sorted(out)
    return [path]


def main():
    ap = argparse.ArgumentParser(description="Summarize a capture's UDP feeds")
    ap.add_argument("paths", nargs="+", help=".pcap/.pcapng files or directories")
    args = ap.parse_args()
    feeds: dict[tuple, list] = {}
    stats = ReadStats()
    first = last = None
    for path in args.paths:
        for f in capture_files(path):
            for d in read_packets(f, stats):
                ent = feeds.setdefault(d.dst, [0, 0])
                ent[0] += 1
                ent[1] += len(d.payload)
                first = d.ts_ns if first is None else min(first, d.ts_ns)
                last = d.ts_ns if last is None else max(last, d.ts_ns)
    span = (last - first) / 1e9 if first is not None else 0.0
    print(f"{stats.udp} UDP datagrams over {span:.3f}s  ·  {stats.frames} frames, "
          f"{stats.skipped} skipped, {stats.fragments} fragments")
    for (ip, port), (n, nbytes) in sorted(feeds.items()):
        print(f"  {ip}:{port:<6} {n:>8} pkts {nbytes:>12,} B")


if __name__ == "__main__":
    main()
