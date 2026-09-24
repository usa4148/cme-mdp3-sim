"""
Tests for PTP-style receive timestamping and the threaded receive path.

These run anywhere. Where the kernel supports receive timestamping the real
path is exercised against a loopback socket; where it does not, the same test
falls back to synthesized control messages — the bytes the kernel would have
attached — so the parsing and measurement path is still covered. Each test says
which path it took.

Run: python3 test_timestamps.py
"""
import queue
import socket
import struct
import threading
import time

import ptp
from ptp import (ANC_BUFSIZE, DEFAULT_TAI_OFFSET, ClockSource, LatencyStats,
                 NS_PER_S, decode_ptp_timestamp, enable_rx_timestamps,
                 encode_ptp_timestamp, fmt_ns, format_ptp, ptp_to_utc_ns,
                 receive_timestamp, synthesize_ancdata, timestamp_from_ancdata,
                 utc_ns_to_ptp)
from pyver import require_python

require_python()

SAMPLE_NS = 1_790_290_256_335_946_000          # a real capture from this sim


def _udp_pair(clock="auto"):
    """A bound receiver with timestamping enabled, plus a sender."""
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2.0)
    source = enable_rx_timestamps(rx, clock)
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    return rx, tx, source


def _capture_one(rx, tx, source, payload=b"ping"):
    """Send one datagram to ourselves and timestamp it, faking if we must."""
    tx.sendto(payload, rx.getsockname())
    data, ancdata, _flags, _addr = rx.recvmsg(4096, ANC_BUFSIZE)
    now = time.clock_gettime_ns(time.CLOCK_REALTIME)
    if source.synthetic:
        ancdata = synthesize_ancdata(now, source)
    ts, from_kernel = receive_timestamp(ancdata, source, now)
    return data, ts, from_kernel


# --------------------------------------------------------------- PTP format ----
def test_tai_offset_is_applied():
    sec, nsec = utc_ns_to_ptp(SAMPLE_NS, tai_offset=37)
    assert sec == SAMPLE_NS // NS_PER_S + 37, sec
    assert nsec == SAMPLE_NS % NS_PER_S, nsec
    # PTP runs ahead of UTC, and the trip back is exact
    assert ptp_to_utc_ns(sec, nsec, 37) == SAMPLE_NS
    assert utc_ns_to_ptp(SAMPLE_NS, 0)[0] + 37 == sec
    print("ok  UTC -> PTP applies the TAI offset and round-trips")


def test_format_ptp_keeps_all_nine_digits():
    s = format_ptp(SAMPLE_NS, 37)
    assert s == "1790290293.335946000", s
    # a sub-microsecond value must not lose its trailing zeros or digits
    assert format_ptp(1_000_000_000_000_000_001, 0) == "1000000000.000000001"
    print(f"ok  PTP formatting keeps nanosecond precision: {s}")


def test_ieee1588_wire_roundtrip():
    raw = encode_ptp_timestamp(SAMPLE_NS, 37)
    assert len(raw) == 10, len(raw)                 # 48-bit sec + 32-bit ns
    sec = int.from_bytes(raw[:6], "big")
    nsec = int.from_bytes(raw[6:], "big")
    assert sec == SAMPLE_NS // NS_PER_S + 37 and nsec == SAMPLE_NS % NS_PER_S
    assert decode_ptp_timestamp(raw, 37) == SAMPLE_NS
    print(f"ok  IEEE 1588 Timestamp encodes to 10 bytes: {raw.hex()}")


def test_wire_rejects_bad_input():
    for bad in (b"", b"\x00" * 9, b"\x00" * 11):
        try:
            decode_ptp_timestamp(bad)
        except ValueError:
            continue
        raise AssertionError(f"should have rejected {len(bad)} bytes")
    print("ok  a malformed PTP Timestamp is rejected")


# ------------------------------------------------------------ cmsg parsing ----
def test_every_ancillary_layout_parses():
    """timespec, 8+8 timeval and BSD's 8+4 timeval all decode to the same ns."""
    for layout, expect in (("timespec", SAMPLE_NS),
                           ("timeval", SAMPLE_NS // 1000 * 1000),
                           ("timeval32", SAMPLE_NS // 1000 * 1000)):
        src = ClockSource("test", layout, 1, kernel=True, scm_type=2)
        anc = synthesize_ancdata(SAMPLE_NS, src)
        got = timestamp_from_ancdata(anc, src)
        assert got == expect, (layout, got, expect)
    print("ok  timespec / timeval / BSD timeval32 layouts all parse")


def test_bsd_timeval32_layout_is_twelve_bytes_of_sixteen():
    """Regression: reading tv_usec as 8 bytes works only while padding is zero."""
    src = ClockSource("test", "timeval32", 1000, kernel=True, scm_type=2)
    (_level, _type, buf), = synthesize_ancdata(SAMPLE_NS, src)
    assert len(buf) == 16, len(buf)
    sec, usec = struct.unpack_from("@qi", buf)
    assert sec == SAMPLE_NS // NS_PER_S
    assert usec == SAMPLE_NS % NS_PER_S // 1000
    print("ok  BSD timeval is 8-byte sec + 4-byte usec, padded to 16")


def test_foreign_control_messages_are_ignored():
    src = ClockSource("test", "timespec", 1, kernel=True, scm_type=2)
    anc = [(socket.SOL_SOCKET, 999, b"\xff" * 16)] + synthesize_ancdata(SAMPLE_NS, src)
    assert timestamp_from_ancdata(anc, src) == SAMPLE_NS
    assert timestamp_from_ancdata([(socket.SOL_SOCKET, 999, b"x" * 16)], src) is None
    print("ok  unrelated control messages are skipped")


def test_receive_timestamp_falls_back_to_userspace():
    src = ClockSource("test", "timespec", 1, kernel=True, scm_type=2)
    ts, from_kernel = receive_timestamp([], src, fallback_ns=SAMPLE_NS)
    assert ts == SAMPLE_NS and from_kernel is False
    ts, from_kernel = receive_timestamp(synthesize_ancdata(SAMPLE_NS, src), src, 0)
    assert ts == SAMPLE_NS and from_kernel is True
    print("ok  a missing kernel timestamp falls back to the userspace clock")


# ------------------------------------------------------------ live capture ----
def test_live_capture_timestamps_a_real_datagram():
    """Real loopback capture — kernel timestamps if available, else synthetic."""
    rx, tx, source = _udp_pair("auto")
    try:
        if not source.kernel:
            # this kernel cannot timestamp: fake it, and say so
            rx.close()
            rx, tx, source = _udp_pair("synthetic")
            assert source.synthetic
        before = time.clock_gettime_ns(time.CLOCK_REALTIME)
        data, ts, from_kernel = _capture_one(rx, tx, source, b"mdp3")
        after = time.clock_gettime_ns(time.CLOCK_REALTIME)
        assert data == b"mdp3"
        # the stamp must sit inside the window we bracketed, allowing for the
        # clock's own granularity at both ends
        slack = max(source.resolution_ns, 1000)
        assert before - slack <= ts <= after + slack, (before, ts, after)
        assert from_kernel == source.kernel
        print(f"ok  live capture stamped at {format_ptp(ts)} via "
              f"{source.describe()}")
    finally:
        rx.close()
        tx.close()


def test_synthetic_mode_works_without_kernel_support():
    """The fallback must stand on its own on a kernel with no timestamping."""
    rx, tx, source = _udp_pair("synthetic")
    try:
        assert source.synthetic and not source.kernel
        assert "FABRICATED" in source.describe()
        _data, ts, from_kernel = _capture_one(rx, tx, source)
        assert from_kernel is True          # parsed out of the (fabricated) cmsg
        assert abs(ts - time.clock_gettime_ns(time.CLOCK_REALTIME)) < NS_PER_S
        print("ok  synthetic timestamps work and are labelled FABRICATED")
    finally:
        rx.close()
        tx.close()


def test_userspace_mode_needs_no_kernel_feature():
    rx, tx, source = _udp_pair("userspace")
    try:
        assert not source.kernel and not source.synthetic
        _data, ts, from_kernel = _capture_one(rx, tx, source)
        assert from_kernel is False
        assert abs(ts - time.clock_gettime_ns(time.CLOCK_REALTIME)) < NS_PER_S
        print("ok  userspace mode timestamps without any kernel support")
    finally:
        rx.close()
        tx.close()


def test_transit_is_measurable_end_to_end():
    """sendingTime -> receive timestamp, the number a feed handler reports."""
    rx, tx, source = _udp_pair("auto")
    if not source.kernel:
        rx.close()
        rx, tx, source = _udp_pair("synthetic")
    try:
        sent_ns = time.clock_gettime_ns(time.CLOCK_REALTIME)
        _data, ts, _k = _capture_one(rx, tx, source, b"x")
        transit = ts - sent_ns
        # loopback on any sane machine is well under a second, and the receive
        # cannot precede the send by more than the clock's granularity
        assert -max(source.resolution_ns, 1000) <= transit < NS_PER_S, transit
        print(f"ok  loopback transit measured: {fmt_ns(transit)}")
    finally:
        rx.close()
        tx.close()


# ---------------------------------------------------------------- threading ----
def test_receive_thread_captures_every_packet():
    from consumer import Receiver

    rx, tx, source = _udp_pair("auto")
    q: queue.Queue = queue.Queue(maxsize=256)
    stop = threading.Event()
    rx.settimeout(0.2)
    thread = Receiver(rx, source, q, stop)
    thread.start()
    try:
        n = 50
        for i in range(n):
            tx.sendto(f"pkt-{i}".encode(), rx.getsockname())
        deadline = time.monotonic() + 5
        got = []
        while len(got) < n and time.monotonic() < deadline:
            try:
                got.append(q.get(timeout=0.2))
            except queue.Empty:
                pass
        assert thread.error is None, thread.error
        assert len(got) == n, f"captured {len(got)} of {n}"
        assert thread.dropped == 0, thread.dropped
        assert [c.data for c in got] == [f"pkt-{i}".encode() for i in range(n)]
        # timestamps advance with arrival order
        stamps = [c.rx_ns for c in got]
        assert stamps == sorted(stamps), "timestamps out of order"
        print(f"ok  receive thread captured {n}/{n} datagrams in order, "
              f"high-water {thread.max_depth}")
    finally:
        stop.set()
        thread.join(timeout=2)
        rx.close()
        tx.close()


def test_queue_overrun_is_counted_not_fatal():
    """A slow decoder must cost packets, not wedge or kill the receive thread."""
    from consumer import Receiver

    rx, tx, source = _udp_pair("auto")
    q: queue.Queue = queue.Queue(maxsize=4)        # deliberately tiny
    stop = threading.Event()
    rx.settimeout(0.2)
    thread = Receiver(rx, source, q, stop)
    thread.start()
    try:
        for i in range(400):
            tx.sendto(b"flood-%d" % i, rx.getsockname())
        time.sleep(1.0)                            # never drain the queue
        assert thread.error is None, thread.error
        assert thread.is_alive(), "receive thread died on overrun"
        assert thread.dropped > 0, "expected drops with a size-4 queue"
        assert thread.received >= thread.dropped
        print(f"ok  overrun counted: received {thread.received}, "
              f"dropped {thread.dropped}, thread still alive")
    finally:
        stop.set()
        thread.join(timeout=2)
        rx.close()
        tx.close()


def test_receive_thread_stops_cleanly():
    from consumer import Receiver

    rx, tx, source = _udp_pair("auto")
    rx.settimeout(0.2)
    stop = threading.Event()
    thread = Receiver(rx, source, queue.Queue(), stop)
    thread.start()
    stop.set()
    thread.join(timeout=3)
    assert not thread.is_alive(), "receive thread ignored the stop event"
    assert thread.error is None, thread.error
    rx.close()
    tx.close()
    print("ok  the receive thread honours the stop event")


# -------------------------------------------------------------------- stats ----
def test_latency_stats():
    st = LatencyStats("transit")
    for v in range(1, 101):
        st.add(v * 1000)
    assert len(st) == 100
    assert st.percentile(50) == 50_000 or st.percentile(50) == 51_000
    assert st.percentile(0) == 1000 and st.percentile(100) == 100_000
    assert st.jitter_ns > 0
    assert "transit" in st.describe() and "p50" in st.describe()
    assert "no samples" in LatencyStats("empty").describe()
    print(f"ok  latency stats: {st.describe()}")


def test_duration_formatting():
    assert fmt_ns(500) == "500ns"
    assert fmt_ns(1500) == "1.5µs"
    assert fmt_ns(2_500_000) == "2.50ms"
    assert fmt_ns(3_000_000_000) == "3.000s"
    assert fmt_ns(-1500) == "-1.5µs"
    print("ok  durations scale from ns to s")


def test_clock_resolution_is_plausible():
    res = ptp.clock_resolution_ns()
    assert 1 <= res <= 10_000_000, res
    print(f"ok  measured realtime clock resolution: {fmt_ns(res)}")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nALL PASS — PTP timestamping and the threaded receive path.")
