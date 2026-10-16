"""
Dashboard tests: sessions, the Flask API (simulated, pcap out, captures in,
live server-sent events), input validation, the static build, and that the
in-page engine reproduces engine.py exactly (under node, when installed).

Run: python test_dashboard.py          (needs flask: .venv/bin/python)
"""
import io
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time

from pyver import require_python

require_python()

try:
    import flask  # noqa: F401
except ImportError:                                  # the venv has it; bare python may not
    print("skip  flask is not installed: run with .venv/bin/python")
    raise SystemExit(0)

from app import create_app, render_index
from engine import MarketEngine
from pcap import read_packets
from session import ParamError, SessionParams, simulate
from wire import available

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = next(iter(available()))


def _params(**kw):
    base = dict(products="ES,NQ,CL", schema=SCHEMA, steps=120, offline=True)
    base.update(kw)
    return SessionParams(**base).validate()


# --------------------------------------------------------------- sessions ----
def test_session_shape_and_wire_truth():
    s, dgs = simulate(_params(loss_a=0.15, loss_b=0.15, seed=3), want_datagrams=True)
    assert s["version"] == 2 and s["source"]["kind"] == "simulated"
    assert [i["code"] for i in s["instruments"]] == ["ES", "NQ", "CL"]
    assert len(s["frames"]) == 120 and all(len(f["i"]) == 3 for f in s["frames"])
    assert {c["id"] for c in s["channels"]} == {310, 318, 382}
    assert set(s["samples"]) >= {"46", "48", "52", "54"}
    kinds = {p[6] for p in s["packets"]}
    assert {"win", "dup", "drop", "loop"} <= kinds, kinds
    assert all(i["definition"] for i in s["instruments"]), "definitions learned from the wire"
    for i in s["instruments"]:
        assert i["summary"]["mismatches"] == 0, i
    assert s["totals"]["dropped"] == sum(1 for d in dgs if not d.sent)
    print(f"ok  session: 3 products, {len(s['packets'])} packets ({s['totals']['dropped']} "
          f"dropped), samples {sorted(s['samples'])}, books checked against snapshots")


def test_session_preview_inputs_match_engine():
    """The meta the page re-simulates from reproduces the server's books exactly."""
    s = simulate(_params(products="ES,CL", loss_a=0, loss_b=0, seed=11))
    for k, i in enumerate(s["instruments"]):
        e = MarketEngine(i["low"], i["high"], depth=10, seed=11 + k, tick=i["tick"],
                         start=i["start"] if i["startSource"] != "center" else None)
        for f in s["frames"][:60]:
            e.step()
            row = f["i"][k]
            assert row["bb"] == e.best_bid() and row["bo"] == e.best_offer(), (i["code"], row)
    print("ok  frames decoded from the wire equal the engine stepped from the session meta")


def test_params_validation():
    bad = [dict(products="ES,NQ,CL,ZN,GC"), dict(products="ZZ"), dict(steps=5),
           dict(loss_a=2), dict(schema="nope"), dict(products="ES", start="NQ=1"),
           dict(products="ES,CL", start="7700"), dict(low="ES=10", high="ES=5"),
           dict(volatility="nan"), dict(feeds="C")]
    for kw in bad:
        try:
            _params(**kw)
        except ParamError:
            continue
        raise AssertionError(f"accepted {kw}")
    p = _params(products="ES,CL", start="ES=7772.5,CL=92")
    assert p.start == {"ES": 7772.5, "CL": 92.0}
    assert "--start ES=7772.5,CL=92" in p.simulator_command()
    print(f"ok  parameters: {len(bad)} bad inputs refused; per-product prices parse")


# ------------------------------------------------------------------ flask ----
def _client(capture_dir):
    app = create_app(capture_dir)
    app.testing = True
    return app, app.test_client()


def test_api_session_pcap_and_capture_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        app, c = _client(d)
        r = c.get("/?products=ES,NQ&steps=60&offline=1")
        assert r.status_code == 200 and b'id="session-data"' in r.data
        assert "script-src 'self'" in r.headers["Content-Security-Policy"]
        r = c.get("/api/session?products=ES,NQ&steps=60&offline=1&loss_a=0.2&schema=" + SCHEMA)
        s = r.get_json()
        assert r.status_code == 200 and [i["code"] for i in s["instruments"]] == ["ES", "NQ"]
        assert c.get("/api/session?products=ES,XX").status_code == 400
        assert c.get("/api/session?steps=99999").status_code == 400
        r = c.get("/api/session.pcap?products=ES,NQ&steps=60&offline=1&schema=" + SCHEMA)
        assert r.status_code == 200 and r.mimetype == "application/vnd.tcpdump.pcap"
        assert "attachment" in r.headers["Content-Disposition"]
        pcap_bytes = r.data
        n = sum(1 for _ in read_packets(pcap_bytes))
        assert n > 100, n
        # upload it back
        r = c.post("/api/capture", data={"file": (io.BytesIO(pcap_bytes), "sim.pcap")},
                   content_type="multipart/form-data")
        cap = r.get_json()
        assert r.status_code == 200, cap
        assert sorted(i["symbol"] for i in cap["instruments"]) == \
            sorted(i["symbol"] for i in s["instruments"])
        assert cap["schema"]["detected"] == SCHEMA
        assert all(i["summary"]["state"] == "live" for i in cap["instruments"])
        # and from the capture directory, by src
        with open(os.path.join(d, "a.pcap"), "wb") as f:
            f.write(pcap_bytes)
        lst = c.get("/api/captures").get_json()
        assert [x["src"] for x in lst["local"]] == ["local:a.pcap"]
        r = c.get("/api/capture?src=local:a.pcap&instruments=42004")
        one = r.get_json()
        assert [i["securityId"] for i in one["instruments"]] == [42004], one["instruments"]
        assert len(one["available"]) == 2
        for evil in ("local:../app.py", "local:/etc/passwd", "file:/etc/passwd", "s3:../x"):
            assert c.get("/api/capture?src=" + evil).status_code in (400, 404), evil
        r = c.post("/api/capture", data={"file": (io.BytesIO(b"not a pcap at all......."), "x.pcap")},
                   content_type="multipart/form-data")
        assert r.status_code == 400
    print(f"ok  api: session, pcap download ({n} datagrams), upload and src round-trip to the "
          f"same instruments; traversal and junk refused")


def test_api_live_stream():
    """A real publisher on loopback multicast, watched through /api/live."""
    from channel import build_instruments, build_publishers
    from transport import Transport
    from wire import open_wire
    app, c = _client(None)
    stop = threading.Event()

    def publish():
        tr = Transport("127.0.0.1", 0)
        pubs = build_publishers(build_instruments(["ES"], offline=True, seed=2), open_wire(SCHEMA), tr)
        k = 0
        while not stop.is_set():
            for p in pubs:
                if k % 25 == 0:              # repeat it, like the simulator: the viewer
                    p.definition_loop()       # may join after the first loop
                p.step()
                if k % 10 == 0:
                    p.snapshot_loop()
            k += 1
            time.sleep(0.02)
        tr.close()

    th = threading.Thread(target=publish, daemon=True)
    th.start()
    try:
        assert c.get("/api/live?iface=not-an-ip").status_code == 400
        r = c.get("/api/live?products=ES&iface=127.0.0.1", buffered=False)
        assert r.mimetype == "text/event-stream"
        it = iter(r.response)
        snap, deadline = None, time.monotonic() + 8
        while time.monotonic() < deadline:
            chunk = next(it)
            chunk = chunk.decode() if isinstance(chunk, bytes) else chunk
            m = re.search(r"^data: (.*)$", chunk, re.M)
            if not m:
                continue
            snap = json.loads(m.group(1))
            es = snap["instruments"][0]
            if es["state"] == "live" and es["definition"] and snap["schema"].get("detected"):
                break
        r.close()
        es = snap["instruments"][0]
        assert es["state"] == "live" and es["bb"] is not None and es["definition"], es
        assert snap["schema"]["detected"] == SCHEMA
        assert snap["channels"][0]["arb"]["accepted"] > 0
    finally:
        stop.set()
        app.extensions["live"].stop_all()
    print(f"ok  live: SSE snapshots — ES {es['state']} at rptSeq {es['rptSeq']}, schema "
          f"detected as {snap['schema']['detected']}, definition from the wire")


def test_static_build_is_self_contained():
    s = simulate(_params(products="ES", steps=30))
    html = render_index(s, inline_assets=True)
    assert 'src="static/' not in html and 'href="static/' not in html
    assert "MDPEngine" in html and "</script" not in html.split("<script>", 1)[1].split("\n</script>")[0]
    block = re.search(r'<script type="application/json" id="session-data">(.*?)</script>', html, re.S)
    data = json.loads(block.group(1))
    assert data["instruments"][0]["code"] == "ES"
    hostile = render_index({"x": "</script><script>alert(1)</script>"})
    assert "<script>alert" not in hostile
    print("ok  static build: assets inlined, session JSON parses, '</script>' cannot break out")


def test_js_engine_matches_python():
    node = shutil.which("node")
    if not node:
        print("skip  node not installed: JS engine parity not checked")
        return
    runner = (
        "const E=require(process.argv[1]);const cs=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "process.stdout.write(JSON.stringify(cs.map(c=>{const r=new E.PyRandom(c.seed);"
        "return {rnd:[r.random(),r.random()],frames:E.simulate(c.eng).frames.map(f=>[f.bb,f.bo,f.b,f.o,f.tr])};})));")
    rng = random.Random(5)
    cases, want = [], []
    specs = [(7757.75, 7782.75, 0.25, 7770.25), (91.66, 92.66, 0.01, 92.16),
             (0.006, 0.0067, 0.0000005, 0.0063335), (104.0, 105.5, 0.015625, None)]
    for k in range(24):
        lo, hi, tick, start = specs[k % 4]
        seed = rng.randrange(0, 2 ** 40)
        vol, rev = rng.uniform(0.1, 2.5), rng.choice([0, 0.004, 0.02])
        jp, js = rng.choice([0, 0.03, 0.1]), rng.uniform(2, 15)
        cases.append({"seed": seed, "eng": dict(low=lo, high=hi, tick=tick, start=start, volatility=vol,
                     reversion=rev, jumpProb=jp, jumpSize=js, depth=10, steps=300, seed=seed)})
        r = random.Random(seed)
        e = MarketEngine(lo, hi, depth=10, volatility=vol, reversion=rev, jump_prob=jp,
                         jump_size=js, seed=seed, start=start, tick=tick)
        frames = []
        for _ in range(300):
            _i, trades = e.step()
            frames.append([e.best_bid(), e.best_offer(),
                           [[l.price, l.size, l.orders] for l in sorted(e.bids.values(), key=lambda l: -l.price)],
                           [[l.price, l.size, l.orders] for l in sorted(e.offers.values(), key=lambda l: l.price)],
                           [[t.price, t.size, t.aggressor] for t in trades]])
        want.append({"rnd": [r.random(), r.random()], "frames": frames})
    out = subprocess.run([node, "-e", runner, os.path.join(HERE, "web", "engine.js")],
                         input=json.dumps(cases), capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    for k, (g, w) in enumerate(zip(got, want)):
        assert g["rnd"] == w["rnd"], f"case {k}: random() differs"
        for step, (gf, wf) in enumerate(zip(g["frames"], w["frames"])):
            assert gf == wf, f"case {k} step {step}: {gf[:2]} vs {wf[:2]}"
    print(f"ok  web/engine.js reproduces engine.py: {len(cases)} random cases x 300 steps, "
          f"books, sizes and trades identical")


if __name__ == "__main__":
    test_session_shape_and_wire_truth()
    test_session_preview_inputs_match_engine()
    test_params_validation()
    test_api_session_pcap_and_capture_roundtrip()
    test_api_live_stream()
    test_static_build_is_self_contained()
    test_js_engine_matches_python()
    print("\nALL PASS — dashboard sessions, API, live stream, static build, engine parity.")
