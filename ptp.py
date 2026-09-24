"""
PTP-style receive timestamping for the feed handler.

A real market-data handler timestamps every packet as close to the wire as it
can get, then measures one-way transit against the `sendingTime` in the packet
header. This module provides that, in IEEE 1588 (PTP) terms:

* **Capture.** `enable_rx_timestamps()` asks the kernel to attach a receive
  timestamp to each datagram and reports which mechanism it got. Python does not
  export the socket constants on every platform, so they are supplied by value:

  | Platform | Option | Ancillary payload | Resolution |
  |----------|--------|-------------------|------------|
  | Linux | `SO_TIMESTAMPNS` (35) | `struct timespec` | nanoseconds |
  | Linux | `SO_TIMESTAMP` (29) | `struct timeval` (8+8) | microseconds |
  | macOS/BSD | `SO_TIMESTAMP` (0x0400) | `struct timeval` (8+4) | microseconds |
  | anywhere | — | none; read the clock after `recvmsg` | userspace |

* **Representation.** PTP counts seconds and nanoseconds from the 1970 epoch on
  the **TAI** timescale, which runs ahead of UTC by a whole number of leap
  seconds (37 since 2017). `format_ptp()` and `encode_ptp_timestamp()` apply
  that offset and produce the on-wire 10-byte IEEE 1588 `Timestamp`.

* **Honesty.** This is PTP *formatting and measurement*, not PTP *sync*: no
  grandmaster disciplines this host's clock. `ClockSource.describe()` always
  states where the timestamp came from and what it is actually worth, and a
  synthesized timestamp is always labelled as such.

When the kernel offers nothing, `synthesize_ancdata()` fabricates the very bytes
the kernel would have produced, so the parsing path is exercised identically on
a machine that cannot timestamp — which is also how the tests run everywhere.
"""
from __future__ import annotations

import socket
import statistics
import struct
import sys
import time
from dataclasses import dataclass, field

from pyver import require_python

require_python()

# PTP epoch is 1970-01-01 00:00:00 TAI. TAI has been ahead of UTC by 37s since
# 2017-01-01; there is no portable OS call for it, so it is a parameter.
DEFAULT_TAI_OFFSET = 37
NS_PER_S = 1_000_000_000

_IS_LINUX = sys.platform.startswith("linux")
_IS_BSD = sys.platform == "darwin" or "bsd" in sys.platform

# Socket option / control-message numbers Python does not always export.
_SO_TIMESTAMP = getattr(socket, "SO_TIMESTAMP", 0x0400 if _IS_BSD else 29)
_SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35 if _IS_LINUX else None)
_SCM_TIMESTAMP = getattr(socket, "SCM_TIMESTAMP", 0x02 if _IS_BSD else 29)
_SCM_TIMESTAMPNS = getattr(socket, "SCM_TIMESTAMPNS", 35 if _IS_LINUX else None)

ANC_BUFSIZE = 256          # ample for one timestamp control message


@dataclass(frozen=True)
class ClockSource:
    """Where a receive timestamp came from, and what it is worth."""
    name: str                      # SO_TIMESTAMPNS | SO_TIMESTAMP | userspace | synthetic
    layout: str                    # timespec | timeval | timeval32 | none
    resolution_ns: int
    kernel: bool
    scm_type: int | None = None
    synthetic: bool = False

    def describe(self) -> str:
        where = ("kernel" if self.kernel else
                 "fabricated" if self.synthetic else "userspace")
        res = (f"{self.resolution_ns}ns" if self.resolution_ns < 1000 else
               f"{self.resolution_ns // 1000}µs")
        note = " — FABRICATED, not a real capture" if self.synthetic else ""
        return f"{self.name} ({where}, {res} resolution){note}"


USERSPACE = ClockSource("userspace", "none", 1000, kernel=False)


# ------------------------------------------------------------------ capture ----
def clock_resolution_ns(samples: int = 2000) -> int:
    """Smallest non-zero step the realtime clock actually advances by."""
    reads = [time.clock_gettime_ns(time.CLOCK_REALTIME) for _ in range(samples)]
    steps = {b - a for a, b in zip(reads, reads[1:]) if b > a}
    return min(steps) if steps else 1


def enable_rx_timestamps(sock: socket.socket, mode: str = "auto") -> ClockSource:
    """Turn on kernel receive timestamping; report what was actually obtained.

    `mode` is auto (best available), kernel (fail if unavailable), userspace
    (don't ask the kernel) or synthetic (fabricate — for demos and tests on a
    kernel with no support). Never raises in auto mode: a handler that cannot
    timestamp in the kernel still runs, it just says so.
    """
    if mode == "synthetic":
        return ClockSource("synthetic", "timespec", clock_resolution_ns(),
                           kernel=False, scm_type=_SCM_TIMESTAMPNS or _SCM_TIMESTAMP,
                           synthetic=True)
    if mode == "userspace":
        return ClockSource("userspace", "none", clock_resolution_ns(), kernel=False)

    attempts = []
    if _SO_TIMESTAMPNS is not None:          # nanosecond timespec, Linux
        attempts.append(("SO_TIMESTAMPNS", _SO_TIMESTAMPNS, _SCM_TIMESTAMPNS,
                         "timespec", 1))
    attempts.append(("SO_TIMESTAMP", _SO_TIMESTAMP, _SCM_TIMESTAMP,
                     "timeval32" if _IS_BSD else "timeval", 1000))

    for name, opt, scm, layout, res in attempts:
        if opt is None or scm is None:
            continue
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, 1)
        except OSError:
            continue
        # the clock cannot be finer than the OS can actually report
        return ClockSource(name, layout, max(res, clock_resolution_ns()),
                           kernel=True, scm_type=scm)

    if mode == "kernel":
        raise OSError("no kernel receive timestamping available on this platform")
    return ClockSource("userspace", "none", clock_resolution_ns(), kernel=False)


# ------------------------------------------------------------------ parsing ----
def _parse_timespec(buf: bytes) -> int:
    sec, nsec = struct.unpack_from("@qq", buf)
    return sec * NS_PER_S + nsec


def _parse_timeval(buf: bytes) -> int:
    sec, usec = struct.unpack_from("@qq", buf)
    return sec * NS_PER_S + usec * 1000


def _parse_timeval32(buf: bytes) -> int:
    """BSD/macOS timeval: 8-byte tv_sec, 4-byte tv_usec (padded to 16)."""
    sec, usec = struct.unpack_from("@qi", buf)
    return sec * NS_PER_S + usec * 1000


_PARSERS = {"timespec": _parse_timespec, "timeval": _parse_timeval,
            "timeval32": _parse_timeval32}


def timestamp_from_ancdata(ancdata, source: ClockSource) -> int | None:
    """Pull the receive timestamp (ns since the UTC epoch) out of `recvmsg` data."""
    parse = _PARSERS.get(source.layout)
    if parse is None:
        return None
    for level, cmsg_type, buf in ancdata:
        if level != socket.SOL_SOCKET or cmsg_type != source.scm_type:
            continue
        try:
            return parse(buf)
        except struct.error:
            return None
    return None


def synthesize_ancdata(ts_ns: int, source: ClockSource) -> list:
    """Build the control message the kernel *would* have produced.

    Used when the running kernel cannot timestamp, and by the tests, so the same
    parsing path runs either way.
    """
    sec, rem = divmod(int(ts_ns), NS_PER_S)
    layout = source.layout if source.layout in _PARSERS else "timespec"
    if layout == "timespec":
        buf = struct.pack("@qq", sec, rem)
    elif layout == "timeval":
        buf = struct.pack("@qq", sec, rem // 1000)
    else:                                     # timeval32, padded as the kernel pads
        buf = struct.pack("@qi", sec, rem // 1000) + b"\x00" * 4
    scm = source.scm_type if source.scm_type is not None else _SCM_TIMESTAMP
    return [(socket.SOL_SOCKET, scm, buf)]


def receive_timestamp(ancdata, source: ClockSource, fallback_ns: int | None = None) -> tuple:
    """(timestamp_ns, from_cmsg) for one datagram, with a userspace fallback.

    Synthetic sources are parsed too, not short-circuited: the whole point of
    fabricating the control message is that the same parsing path runs on a
    kernel that cannot timestamp. `from_cmsg` says the value came out of a
    control message; whether that message was real is `source.synthetic`.
    """
    ts = (timestamp_from_ancdata(ancdata, source)
          if (source.kernel or source.synthetic) else None)
    if ts is not None:
        return ts, True
    return (fallback_ns if fallback_ns is not None
            else time.clock_gettime_ns(time.CLOCK_REALTIME)), False


# ---------------------------------------------------------------- PTP format ----
def utc_ns_to_ptp(ns: int, tai_offset: int = DEFAULT_TAI_OFFSET) -> tuple:
    """UTC nanoseconds -> PTP (seconds, nanoseconds) on the TAI timescale."""
    return divmod(int(ns) + tai_offset * NS_PER_S, NS_PER_S)


def ptp_to_utc_ns(sec: int, nsec: int, tai_offset: int = DEFAULT_TAI_OFFSET) -> int:
    return sec * NS_PER_S + nsec - tai_offset * NS_PER_S


def format_ptp(ns: int, tai_offset: int = DEFAULT_TAI_OFFSET) -> str:
    """PTP timestamp as seconds.nanoseconds, e.g. 1790289946.320409000."""
    sec, nsec = utc_ns_to_ptp(ns, tai_offset)
    return f"{sec}.{nsec:09d}"


def format_wall(ns: int) -> str:
    """The same instant as readable UTC, nanoseconds kept."""
    sec, nsec = divmod(int(ns), NS_PER_S)
    return time.strftime("%H:%M:%S", time.gmtime(sec)) + f".{nsec:09d}"


def encode_ptp_timestamp(ns: int, tai_offset: int = DEFAULT_TAI_OFFSET) -> bytes:
    """The 10-byte IEEE 1588 Timestamp: 48-bit seconds + 32-bit nanoseconds, BE."""
    sec, nsec = utc_ns_to_ptp(ns, tai_offset)
    if not 0 <= sec < 1 << 48:
        raise ValueError(f"seconds field out of PTP range: {sec}")
    return sec.to_bytes(6, "big") + nsec.to_bytes(4, "big")


def decode_ptp_timestamp(raw: bytes, tai_offset: int = DEFAULT_TAI_OFFSET) -> int:
    """Inverse of `encode_ptp_timestamp`, back to UTC nanoseconds."""
    if len(raw) != 10:
        raise ValueError(f"PTP Timestamp is 10 bytes, got {len(raw)}")
    return ptp_to_utc_ns(int.from_bytes(raw[:6], "big"),
                         int.from_bytes(raw[6:], "big"), tai_offset)


def fmt_ns(ns: float) -> str:
    """Human-scaled duration."""
    a = abs(ns)
    if a < 1_000:
        return f"{ns:.0f}ns"
    if a < 1_000_000:
        return f"{ns / 1_000:.1f}µs"
    if a < 1_000_000_000:
        return f"{ns / 1_000_000:.2f}ms"
    return f"{ns / 1_000_000_000:.3f}s"


# ------------------------------------------------------------------- stats ----
@dataclass
class LatencyStats:
    """Running distribution of a nanosecond measurement."""
    name: str
    samples: list = field(default_factory=list)

    def add(self, ns: float) -> None:
        self.samples.append(float(ns))

    def __len__(self) -> int:
        return len(self.samples)

    def percentile(self, p: float) -> float:
        if not self.samples:
            return float("nan")
        ordered = sorted(self.samples)
        k = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
        return ordered[k]

    def describe(self) -> str:
        if not self.samples:
            return f"{self.name}: no samples"
        return (f"{self.name}: min {fmt_ns(min(self.samples))}  "
                f"p50 {fmt_ns(self.percentile(50))}  "
                f"p99 {fmt_ns(self.percentile(99))}  "
                f"max {fmt_ns(max(self.samples))}  "
                f"mean {fmt_ns(statistics.fmean(self.samples))}")

    @property
    def jitter_ns(self) -> float:
        """p99 - p50: how much the tail strays from the typical case."""
        return self.percentile(99) - self.percentile(50) if self.samples else float("nan")
