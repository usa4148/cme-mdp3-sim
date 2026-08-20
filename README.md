# CME MDP 3.0 ES Market-Data Simulator

A wire-accurate simulator that emulates the **CME Group MDP 3.0** market-data feed
for the **E-mini S&P 500 (ES)** future. It encodes real **SBE (Simple Binary
Encoding)** messages and sends them over **UDP multicast**, driven by a bounded
random walk of the ES price.

You submit a **price range** and a **duration**; the mid-price does a
mean-reverting random walk inside that band (tick = 0.25, $12.50/tick), the book
updates around it, and the feed streams as genuine MDP 3.0 packets.

> **New here?** Run the dashboard: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`,
> then `.venv/bin/python app.py`, and open <http://127.0.0.1:8000>.
> For the byte-level design, price model, and diagrams see **[ARCHITECTURE.md](ARCHITECTURE.md)**.

## Why it's wire-accurate

The SBE codec (`sbe.py`) is **schema-driven** — the exact byte layout lives in
`schema/mdp3.xml`, a faithful subset of CME's official `templates_FixBinary.xml`.
Encoder and decoder both read that schema, so:

- Little-endian, `blockLength/templateId/schemaId/version` SBE headers.
- CME's 3-byte repeating-group dimension (`blockLength` u16 + `numInGroup` u8).
- Prices as `int64` mantissa with constant exponent **-7** (5000.25 → 50002500000).

For bit-for-bit production parity, drop in CME's official
`templates_FixBinary.xml` and the codec adapts automatically.

## Layout

| File | Purpose |
|------|---------|
| `schema/mdp3.xml`   | SBE schema — messages, composites, enums, sets, groups |
| `sbe.py`            | Schema-driven SBE encoder/decoder |
| `packet.py`         | Binary Packet Header + message framing |
| `engine.py`         | ES price random walk + order-book / increment generation |
| `simulator.py`      | Main sender — encodes MDP 3.0 packets, UDP multicast |
| `consumer.py`       | Receiver/decoder — proves the wire by reconstructing from bytes |
| `record_session.py` | Records a session to JSON (`build_session()` reused by the app) |
| `app.py`            | Flask app — hosts the dashboard locally, generates real-SBE sessions |
| `viz_template.html` | Dashboard template (`__SESSION_JSON__` placeholder + live tuner) |
| `build_dashboard.py`| Bakes a static, serverless `dashboard.html` from the template |
| `dashboard.html`    | Pre-built static dashboard (generated; open with no server) |
| `test_roundtrip.py` | Wire-correctness tests (encode → bytes → decode) |
| `requirements.txt`  | Python deps (only `flask`, for `app.py`) |
| `ARCHITECTURE.md`   | Byte-level wire format, codec design, price model, diagrams |

## Messages implemented

- `MDIncrementalRefreshBook` (template 46) — bid/offer book updates (New/Change/Delete)
- `MDIncrementalRefreshTradeSummary` (template 48) — trade prints with aggressor side
- `SecurityStatus` (template 30)

## Dashboard (local Flask app)

The interactive dashboard runs locally. First-time setup (project-local venv):

```bash
python3 -m venv .venv && .venv/bin/pip install flask
```

Run it:

```bash
.venv/bin/python app.py
```

By default it binds to **localhost only** (`127.0.0.1`). To reach it from other
machines on your LAN, bind to all interfaces:

```bash
.venv/bin/python app.py --host 0.0.0.0 --port 8000
```

> ⚠️ `--host 0.0.0.0` exposes the dashboard to your whole network. It's a debug
> server with no auth — only do this on a trusted LAN.

Then open **http://127.0.0.1:8000**. Each load runs the Python simulator (real
MDP 3.0 SBE encode/decode) and serves the result; the in-page **Price Dynamics**
tuner (volatility σ, mean reversion θ, jump probability, jump size) re-simulates
client-side instantly, and **Copy simulator command** gives you the matching
`simulator.py` flags to reproduce the dynamics on the live multicast feed.

Set the initial dynamics via query string:

```
http://127.0.0.1:8000/?volatility=1.2&jump_prob=0.04&jump_size=12&seed=7
```

`GET /api/session?...` returns the authoritative Python-generated session as JSON.

## Quick start

Run the tests:

```bash
python3 test_roundtrip.py
```

Terminal 1 — start a consumer (joins the multicast group and decodes):

```bash
python3 consumer.py --summary
```

Terminal 2 — run the simulator for an hour over a 25-point band:

```bash
python3 simulator.py --low 5000 --high 5025 --duration 3600 --rate 20
```

Defaults match CME **channel 310** (E-mini S&P 500), Incremental feed A,
multicast `224.0.31.1:14310`. TTL defaults to 0 (this host only) so nothing
leaves your machine; raise `--ttl` to reach the LAN.

### Static, serverless dashboard

To bake a self-contained `dashboard.html` you can open directly (file://), with
the dynamics of your choice:

```bash
python3 build_dashboard.py --volatility 1.2 --jump-prob 0.04 --jump-size 12
```

## Key options

`simulator.py`: `--low --high --duration --rate --depth --volatility --security-id
--group --port --iface --ttl --seed --quiet`
