"""
Capture-file tests: pcap writing and reading (classic, pcapng, foreign link
types), replay timing on real sockets, and the S3 path of capture/replay
against an in-memory stand-in for the bucket.

Run: python test_pcap.py
"""
import io
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

import pcap
from pyver import require_python

require_python()

HERE = os.path.dirname(os.path.abspath(__file__))
TS = 1_790_000_000_123_456_789


def _udp_ip(payload, src=("10.1.2.3", 4000), dst=("224.0.31.1", 14310), frag=0):
    """Raw IPv4+UDP bytes (no link layer)."""
    udp = struct.pack("!HHHH", src[1], dst[1], 8 + len(payload), 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, frag, 64, 17, 0,
                     socket.inet_aton(src[0]), socket.inet_aton(dst[0]))
    return ip + udp


def _classic(linktype, frames, endian="<", nanos=False):
    out = struct.pack(endian + "IHHiIII", pcap.MAGIC_NS if nanos else pcap.MAGIC_US,
                      2, 4, 0, 0, 65535, linktype)
    for ts, fr in frames:
        sec, sub = divmod(ts, 1_000_000_000)
        out += struct.pack(endian + "IIII", sec, sub if nanos else sub // 1000,
                           len(fr), len(fr)) + fr
    return out


def test_write_read_roundtrip():
    for nanos in (True, False):
        buf = io.BytesIO()
        w = pcap.PcapWriter(buf, nanos=nanos)
        w.write(b"\x01\x02payload", TS, ("127.0.0.1", 55555), ("224.0.32.1", 15310))
        w.flush()
        (d,) = list(pcap.read_packets(buf.getvalue()))
        assert d.payload == b"\x01\x02payload"
        assert d.src == ("127.0.0.1", 55555) and d.dst == ("224.0.32.1", 15310)
        assert d.ts_ns == (TS if nanos else TS // 1000 * 1000)
    hdr = pcap.global_header(True)
    assert struct.unpack("<I", hdr[:4])[0] == 0xA1B23C4D and len(hdr) == 24
    fr = pcap.frame(b"x", "127.0.0.1", 1, "224.0.31.1", 2)
    assert fr[:6] == bytes.fromhex("01005e001f01"), "RFC 1112 multicast MAC"
    assert pcap.ip_checksum(fr[14:34]) == 0, "IPv4 header checksum must verify"
    print("ok  pcap write/read: ns + us resolution, multicast MAC, IP checksum")


def test_foreign_link_types():
    pay = b"MDP"
    ip = _udp_ip(pay)
    eth_vlan = (bytes(6) + bytes(6) + struct.pack("!HH", 0x8100, 42) +
                struct.pack("!H", 0x0800) + ip)
    sll = struct.pack("!HHH8sH", 0, 1, 6, bytes(8), 0x0800) + ip
    sll2 = struct.pack("!HHIHBB8s", 0x0800, 0, 1, 1, 0, 6, bytes(8)) + ip
    null = struct.pack("<I", 2) + ip
    cases = {"ethernet+vlan": (1, eth_vlan), "raw": (101, ip), "linux-sll": (113, sll),
             "linux-sll2": (276, sll2), "bsd-loopback": (0, null)}
    for name, (lt, fr) in cases.items():
        for endian in "<>":
            (d,) = list(pcap.read_packets(_classic(lt, [(TS, fr)], endian)))
            assert d.payload == pay and d.dst == ("224.0.31.1", 14310), name
    st = pcap.ReadStats()
    frag = _udp_ip(pay, frag=0x2000)                 # more-fragments set
    assert list(pcap.read_packets(_classic(101, [(TS, frag)]), st)) == []
    assert st.fragments == 1
    print(f"ok  reader: {', '.join(cases)}; both byte orders; fragments skipped")


def test_pcapng():
    pay = b"pcapng-payload"
    eth = bytes(12) + struct.pack("!H", 0x0800) + _udp_ip(pay)

    def block(btype, body):
        body += bytes((-len(body)) % 4)
        n = 12 + len(body)
        return struct.pack("<II", btype, n) + body + struct.pack("<I", n)

    shb = block(0x0A0D0D0A, struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1))
    # if_tsresol = 9 (nanoseconds)
    idb = block(1, struct.pack("<HHI", 1, 0, 65535) + struct.pack("<HHB3x", 9, 1, 9)
                + struct.pack("<HH", 0, 0))
    epb = block(6, struct.pack("<IIIII", 0, TS >> 32, TS & 0xFFFFFFFF, len(eth), len(eth)) + eth)
    (d,) = list(pcap.read_packets(shb + idb + epb))
    assert d.payload == pay and d.ts_ns == TS
    print("ok  pcapng: section, interface (ns tsresol), enhanced packet")


def _listener(group, port):
    from receiver import join_group
    return join_group(group, port, "127.0.0.1", timeout=2.0)


def test_replay_timing_and_filter():
    """Replay a written capture onto loopback multicast at 4x and at full speed."""
    from products import get_product
    es, cl = get_product("ES").incremental, get_product("CL").incremental
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.pcap")
        with pcap.PcapWriter(path) as w:
            for i in range(5):                        # 5 packets, 200ms apart
                w.write(f"es-{i}".encode(), TS + i * 200_000_000,
                        ("127.0.0.1", 1), (es.ip, es.port))
                w.write(f"cl-{i}".encode(), TS + i * 200_000_000 + 1,
                        ("127.0.0.1", 1), (cl.ip, cl.port))
        rx = _listener(es.ip, es.port)
        try:
            t0 = time.monotonic()
            r = subprocess.run([sys.executable, os.path.join(HERE, "replay.py"), path,
                                "--speed", "4", "--product", "ES", "--quiet"],
                               capture_output=True, text=True, timeout=20)
            took = time.monotonic() - t0
            assert r.returncode == 0, r.stderr
            got = [rx.recv(100) for _ in range(5)]
            assert got == [f"es-{i}".encode() for i in range(5)], got
            assert "replayed 5 datagrams" in r.stdout, r.stdout   # CL filtered out
            # 0.8s of capture at 4x is 0.2s of sends (+ interpreter start)
            assert took >= 0.2, took
            r = subprocess.run([sys.executable, os.path.join(HERE, "replay.py"), path,
                                "--speed", "0", "--quiet", "--loop", "2"],
                               capture_output=True, text=True, timeout=20)
            assert "replayed 20 datagrams" in r.stdout, r.stdout
        finally:
            rx.close()
    print(f"ok  replay: original order, --speed 4 paced ({took:.2f}s), --product "
          f"filter, --loop")


def test_simulator_pcap_decodes():
    """simulator --pcap writes a capture the replay decoder fully understands."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sim.pcap")
        r = subprocess.run([sys.executable, os.path.join(HERE, "simulator.py"),
                            "--product", "ES,CL", "--duration", "1.2", "--rate", "20",
                            "--offline", "--quiet", "--pcap", path, "--loss-a", "0.2",
                            "--seed", "4"], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        r = subprocess.run([sys.executable, os.path.join(HERE, "replay.py"), path,
                            "--dry-run"], capture_output=True, text=True, timeout=30)
        out = r.stdout
        assert r.returncode == 0, r.stderr
        assert "channel 310" in out and "channel 382" in out, out
        assert "lost on both 0" in out, out
        assert "live" in out and "mismatch)" in out and "1 mismatch" not in out, out
    print("ok  simulator --pcap: both channels decode, B covers A's loss, books check out")


class FakeS3:
    """Just enough of boto3's S3 client for capture/replay/s3store."""

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[(Bucket, Key)] = bytes(Body)

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def get_paginator(self, _name):
        store = self

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k, "Size": len(v)}
                                    for (b, k), v in sorted(store.objects.items())
                                    if b == Bucket and k.startswith(Prefix)]}
        return P()


def test_s3_capture_and_replay_sources():
    import capture
    import replay
    import s3store
    fake = FakeS3()
    saved = {k: os.environ.get(k) for k in ("S3_BUCKET", "S3_PREFIX")}
    real_client = s3store.client
    os.environ.update(S3_BUCKET="bkt", S3_PREFIX="mdp3-pcap")
    s3store.client = lambda: fake
    try:
        with tempfile.TemporaryDirectory() as d:
            sink = capture.Sink(d)
            buf = io.BytesIO()
            w = pcap.PcapWriter(buf)
            w.write(b"one", TS, ("127.0.0.1", 1), ("224.0.31.1", 14310))
            dt = datetime(2026, 10, 16, 12, 30, 1, 250_000, tzinfo=timezone.utc)
            msg = sink.emit(buf.getvalue(), dt, 1)
            key = f"mdp3-pcap/{sink.host}/2026/10/16/mdp3-20261016T123001Z-250.pcap"
            assert ("bkt", key) in fake.objects, fake.objects.keys()
            assert os.path.exists(os.path.join(d, os.path.basename(key)))
            assert "s3://bkt/" in msg
            got = list(replay.sources(["s3:mdp3-pcap/"]))
            assert [n for n, _ in got] == [f"s3://bkt/{key}"]
            (dg,) = list(pcap.read_packets(got[0][1]))
            assert dg.payload == b"one"
            assert s3store.parse_url("s3://other/x/y") == ("other", "x/y")
    finally:
        s3store.client = real_client
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("ok  S3: capture window key layout, local copy, replay reads s3: prefixes")


def test_env_file():
    import s3store
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
        fh.write("# comment\nMDP3_TEST_A='quoted'\nMDP3_TEST_B = plain\n")
    try:
        os.environ["MDP3_TEST_B"] = "already-set"
        s3store.load_env_file(fh.name)
        assert os.environ["MDP3_TEST_A"] == "quoted"
        assert os.environ["MDP3_TEST_B"] == "already-set", "env must win over the file"
    finally:
        os.unlink(fh.name)
        os.environ.pop("MDP3_TEST_A", None)
        os.environ.pop("MDP3_TEST_B", None)
    print("ok  config.env: comments, quotes, never overrides the environment")


if __name__ == "__main__":
    test_write_read_roundtrip()
    test_foreign_link_types()
    test_pcapng()
    test_replay_timing_and_filter()
    test_simulator_pcap_decodes()
    test_s3_capture_and_replay_sources()
    test_env_file()
    print("\nALL PASS — capture files, replay, and the S3 path.")
