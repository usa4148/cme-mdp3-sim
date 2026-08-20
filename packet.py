"""
MDP 3.0 transport framing.

A UDP payload (packet) is:
    Binary Packet Header (12 bytes, little-endian):
        MsgSeqNum   uint32   (packet sequence number, starts at 1)
        SendingTime uint64   (nanoseconds since Unix epoch)
    Then 1..N messages, each framed as:
        MsgSize     uint16   (size of the SBE message that follows, in bytes)
        <SBE message header + body>
"""
from __future__ import annotations

import struct

PACKET_HEADER = struct.Struct("<IQ")   # MsgSeqNum, SendingTime


def build_packet(seq_num: int, sending_time_ns: int, messages: list[bytes]) -> bytes:
    out = bytearray(PACKET_HEADER.pack(seq_num, sending_time_ns))
    for m in messages:
        out += struct.pack("<H", len(m))
        out += m
    return bytes(out)


def parse_packet(buf: bytes):
    seq_num, sending_time_ns = PACKET_HEADER.unpack_from(buf, 0)
    off = PACKET_HEADER.size
    frames = []
    while off + 2 <= len(buf):
        (msg_size,) = struct.unpack_from("<H", buf, off)
        off += 2
        if msg_size == 0 or off + msg_size > len(buf):
            break
        frames.append(buf[off:off + msg_size])
        off += msg_size
    return seq_num, sending_time_ns, frames
