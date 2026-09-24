# CME MDP 3.0 Market-Data Simulator

A wire-accurate simulator that emulates the **CME Group MDP 3.0** market-data
feed. It encodes real **SBE (Simple Binary Encoding)** messages and sends them
over **UDP multicast**, driven by a bounded random walk of the price.

It defaults to the **E-mini S&P 500 (ES)** front month; `--product` switches to
any futures product in CME's `config.xml` — 29 of them out of the box, across
equity index, FX, rates, grains, energy and metals. Choosing a product sets the
channel, multicast feed, tick size, tick value and front-month contract.

The walk opens at **yesterday's closing price** for that product, looked up
automatically and cached. You submit a **duration** (and optionally a price
range); the mid-price does a mean-reverting random walk inside the band, the book
updates around it, and the feed streams as genuine MDP 3.0 packets. The opening
price is editable everywhere — `--start` on the CLI, a field in the dashboard,
`?start=` on the URL.

> **New here?** Run the dashboard: `python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt`,
> then `.venv/bin/python app.py`, and open <http://127.0.0.1:8000>.
> For the byte-level design, price model, and diagrams see **[ARCHITECTURE.md](ARCHITECTURE.md)**.

## Requirements

**Python 3.14 or newer** — check with `python3 --version`. Every entry point
enforces it (see `pyver.py`) rather than failing later on something obscure. The
only third-party dependency is `flask`, and only for the dashboard host; the
simulator, codec, engine, consumer and tests are pure standard library.

```bash
python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Name the interpreter explicitly: a bare `python3` may well be an older build,
and the venv inherits whichever one created it. If `python3 --version` already
reports 3.14+, `python3 -m venv .venv` is equivalent.

Everything except the dashboard runs straight from the checkout with no install
at all — `python3 simulator.py`, `python3 consumer.py`, `python3 products.py`.

### Platforms

Verified on **macOS** (Apple Silicon) and **Linux** (aarch64, `python:3.14-slim`):
all four test suites, multicast end to end, the network lookup, the static build
and the Flask dashboard.

The visible difference is timestamp quality, and each run reports what it
actually got:

| | mechanism | resolution measured |
|---|---|---|
| Linux | `SO_TIMESTAMPNS` | ~41ns |
| macOS | `SO_TIMESTAMP` | 1µs |

Python exports none of those socket constants on *either* platform, which is why
`ptp.py` carries them by value.

`consumer.py` targets Unix: its kernel timestamping reads ancillary data via
`socket.recvmsg()`.

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
| `engine.py`         | Price random walk + order-book / increment generation |
| `contracts.py`      | Contract specs (tick, tick value, cycle) + front-month dates |
| `products.py`       | Reads CME's `config.xml`; joins channels/feeds to contract specs |
| `schema/config.xml` | CME's channel configuration (download it; gitignored) |
| `schema/config.sample.xml` | Committed fallback extract of the above |
| `settlement.py`     | Looks up & caches the prior session's close; derives the band |
| `simulator.py`      | Main sender — encodes MDP 3.0 packets, UDP multicast |
| `consumer.py`       | Receiver/decoder — proves the wire; PTP timestamps, threaded receive |
| `ptp.py`            | IEEE 1588 receive timestamping, TAI formatting, latency stats |
| `pyver.py`          | Python version floor, shared by every entry point |
| `record_session.py` | Records a session to JSON (`build_session()` reused by the app) |
| `app.py`            | Flask app — hosts the dashboard locally, generates real-SBE sessions |
| `viz_template.html` | Dashboard template (`__SESSION_JSON__` placeholder + live tuner) |
| `build_dashboard.py`| Bakes a static, serverless `dashboard.html` from the template |
| `dashboard.html`    | Static dashboard — *generated* by `build_dashboard.py`, not in the repo |
| `session.json`      | Recorded session JSON — *generated*, not in the repo |
| `test_roundtrip.py` | Wire-correctness tests (encode → bytes → decode) |
| `test_start_price.py`| Opening-price tests (engine start, band, close lookup) |
| `test_products.py`  | Product chooser tests (specs, front months, config parsing) |
| `test_timestamps.py`| Timestamping + threaded receive tests (real kernel or faked) |
| `requirements.txt`  | Python deps (only `flask`, for `app.py`) |
| `ARCHITECTURE.md`   | Byte-level wire format, codec design, price model, diagrams |

## Messages implemented

- `MDIncrementalRefreshBook` (template 46) — bid/offer book updates (New/Change/Delete)
- `MDIncrementalRefreshTradeSummary` (template 48) — trade prints with aggressor side
- `SecurityStatus` (template 30)

## Choosing a product

The simulator reads CME's **`config.xml`** — the channel configuration mapping
products to channels and multicast feeds — and joins it to the contract specs in
`contracts.py`. List what you can simulate:

```bash
python3 products.py                 # every product, grouped
python3 products.py --show CL       # one product in detail
```

```
code  product                  group    exch   ch    front    feed
ES    E-mini S&P 500           Equity   CME    310   ESZ6     224.0.31.1:14310
CL    WTI Crude Oil            Energy   NYMEX  382   CLX6     224.0.31.130:14382
ZN    10-Year T-Note           Rates    CBOT   344   ZNZ6     224.0.31.68:14344
```

Then point anything at it — ES stays the default:

```bash
python3 simulator.py --product CL --duration 3600
python3 record_session.py --product GC --steps 400
python3 build_dashboard.py --product ZN
```

The dashboard has a **product chooser** in the header, grouped by asset class.

### Getting CME's config.xml

CME publishes it at
<https://www.cmegroup.com/ftp/SBEFix/Production/Configuration/config.xml>, behind
a CME Group login. Download it to **`schema/config.xml`** and every tool picks it
up automatically (42 channels, ~5,000 products). It is gitignored — it is CME's
file, not this repo's.

Without it, the committed **`schema/config.sample.xml`** is used: a small
unmodified extract of the same file covering the 9 futures channels that host the
products below. Both parse identically, so nothing changes when you swap them.
Point elsewhere with `--config /path/to/config.xml` or `$CME_CONFIG_XML`.

**Options channels are ignored.** A code like `ES` is listed on its futures
channel (310) *and* on two options channels; only channels whose label says
Futures are considered, so `ES` always resolves to 310.

### Front month

The front month is always assumed — the nearest listed contract whose last trade
date is on or after today, so it rolls the day after expiry:

```bash
python3 contracts.py            # every product's front month and expiry
# ES    E-mini S&P 500    ESZ6   2026-12-18   0.25   12.5
# CL    WTI Crude Oil     CLX6   2026-10-20   0.01     10
```

Expiry rules follow CME's rulebook in shape (third Friday for equity index, N
business days before month end for treasuries, and so on) but **ignore exchange
holidays**, so a date can be a day off when a holiday falls in the window. That
is fine for picking a front month; it is not a settlement calendar.

### Contract economics

CME's `config.xml` carries no tick sizes — those live in `contracts.py`, keyed by
the same product codes. Tick size and tick value are the real contract specs;
**security IDs are synthetic** (on a real feed they arrive in the security
definition messages), and ES keeps `42003`, the id this simulator has always
used. To simulate a product not in that table, add a `ContractSpec` for it.

## Starting price

By default the simulator opens at the **previous session's close** for the
selected product and builds a **100-tick** band around it — 25 points of ES, $1.00
of crude, 1.5625 of a 10-year note — so a bare `python3 simulator.py` starts at a
realistic price with no arguments:

```bash
python3 settlement.py
# ES=F 2026-09-23 close 7772.50 (yahoo)
#   suggested band: [7760.0, 7785.0]
```

The close comes from Yahoo Finance's public chart endpoint for the product's
continuous front-month symbol (`ES=F`, `CL=F`, …), fetched with the standard
library and cached per symbol in `.prior_close_cache.json` (re-fetched at most
every 4 hours). If the network is unavailable the cached value is reused and
flagged **stale**; with no cache either, the product opens at a rough **fallback
reference level** — labelled as such, never presented as a quote — so the
simulator always runs offline. (Yahoo has no usable history for `EMD`, which
therefore always opens at its reference level.)

> CME's own settlements endpoint is deliberately **not** used — its Data Terms of
> Use prohibit automated access. Yahoo's number is a consolidated close, not
> CME's official settlement price; pass `--start` when you need the exact settle.

Override the opening price anywhere:

```bash
python3 simulator.py --start 7772.50                 # explicit open, band derived
python3 simulator.py --start 7772.50 --band 40       # 40-point band around it
python3 simulator.py --low 7700 --high 7800          # explicit band, open at close
python3 simulator.py --product CL --start 92.00      # any product, same flags
python3 simulator.py --offline                       # never touch the network
python3 simulator.py --refresh                       # force a re-fetch
```

Rules, in order: an explicit `--start` always wins; explicit `--low`/`--high`
always win over the derived band; anything left over is filled in around the
opening price. An explicit `--start` outside the band is clamped into it. If the
*prior close* falls outside a band you set explicitly, the walk opens at the band
center instead — the price and the band you asked for are both left honest.

The opening price becomes the opening **best bid**; with a one-tick-wide market
the opening mid therefore sits half a tick above it (7772.50 → mid 7772.625).
Prices snap to the *selected product's* tick grid, so a Japanese Yen close of
0.006332 stays 0.006332 rather than being quantized to the ES tick.

## Dashboard (local Flask app)

The interactive dashboard runs locally, and is the only part that needs the
venv from [Requirements](#requirements):

```bash
python3.14 -m venv .venv && .venv/bin/pip install -r requirements.txt
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

The **product** chooser in the header switches instrument; it reloads with
`?product=` so the authoritative Python session is regenerated (tick size,
channel and prior close all change), carrying your dynamics over and dropping the
price-specific ones. A static `file://` dashboard has no server to regenerate
against, so the chooser is disabled there — rebuild with `--product`.

The **start** field in the tuner edits the opening price live. Yesterday's close
is shown as a chip in the header and as a dashed reference line on the chart, the
big header delta reads against it, and **↺ prior close** snaps back to it. Typing
a price outside the current band slides the band along with it (same width);
`--start` is added to the copied simulator command. Clear the field to go back to
opening at the band center.

Set the opening price and dynamics via query string:

```
http://127.0.0.1:8000/?volatility=1.2&jump_prob=0.04&jump_size=12&seed=7
http://127.0.0.1:8000/?product=CL
http://127.0.0.1:8000/?start=7772.50&band=40
```

`GET /api/session?...` returns the authoritative Python-generated session as JSON,
`GET /api/products` lists the chooser's products, and
`GET /api/prior-close?product=GC` returns the close a product starts from.

## Timestamps & latency

`consumer.py` behaves like a real feed handler. Every datagram is stamped as
close to the wire as the platform allows and reported on the IEEE 1588 (PTP)
TAI timescale, and transit is measured against the `sendingTime` in the packet
header:

```bash
python3 consumer.py --summary
python3 consumer.py --product CL --summary     # follow a different feed
```

```
Listening on 224.0.31.1:14310 (iface 127.0.0.1)  schema id=1 v13
  clock  SO_TIMESTAMP (kernel, 1µs resolution)
  Python 3.14.3 (GIL)  ·  receive thread + decode thread

seq=1      ptp=1790290256.335946000  transit=  214.0µs  2 msg [Book,TradeSummary] 732B
seq=2      ptp=1790290256.461729000  transit=  253.0µs  1 msg [Book] 676B
...
Received 12 packets in 1.38s  ·  0 sequence gap(s)
  clock         SO_TIMESTAMP (kernel, 1µs resolution)
  PTP epoch     TAI (UTC + 37s) — this is PTP formatting and measurement, not grandmaster sync
  transit: min 214.0µs  p50 560.0µs  p99 680.0µs  max 680.0µs  mean 498.6µs
  inter-arrival: min 123.98ms  p50 125.06ms  p99 126.03ms  max 126.03ms
  jitter        968.0µs (p99 - p50 of inter-arrival)
```

### Where the timestamp comes from

`--clock` picks the source; `auto` takes the best available and always reports
what it actually got:

| Platform | Mechanism | Payload | Resolution |
|----------|-----------|---------|-----------|
| Linux | `SO_TIMESTAMPNS` | `struct timespec` | nanoseconds |
| macOS / BSD | `SO_TIMESTAMP` | `struct timeval` | microseconds |
| anywhere | userspace clock read after `recvmsg` | — | clock resolution |
| anywhere | `--clock synthetic` | fabricated control message | clock resolution |

Python does not export these socket constants on every platform, so `ptp.py`
supplies them by value. **If the running kernel cannot timestamp, it is faked:**
`--clock synthetic` fabricates exactly the control message the kernel would have
attached, so the parsing and measurement path is identical — and every line of
output labels it `FABRICATED`. That is also how the tests cover the feature on
machines with no kernel support.

This is PTP *formatting and measurement*, **not** PTP *sync*: nothing disciplines
this host's clock to a grandmaster. Across two hosts without a shared time
source, transit is dominated by clock offset, and the consumer says so when the
median goes negative. `--tai-offset` sets the UTC→TAI leap seconds (37 today).

### Threading

The receive thread does nothing but `recvmsg`, stamp and enqueue. Decoding runs
on the main thread behind a bounded queue, so a slow decoder cannot delay the
next packet — the mistake that makes a naive handler's latency numbers measure
its own decoder. Queue overruns are counted the way a real handler counts kernel
drops, and `--queue` sets the depth. `--no-threads` decodes inline instead, which
is useful for comparison.

Threads earn their place in two other spots: `settlement.prefetch()` looks up
many products' closes at once (29 symbols in ~0.4s instead of ~10s serially, with
a lock around the shared cache file), and the Flask app serves `threaded=True`
so one slow session build does not block every other request.

```bash
python3 settlement.py --all          # every product's close, concurrently
```

## Quick start

Run the tests:

```bash
python3 test_roundtrip.py     # wire: encode -> bytes -> decode
python3 test_start_price.py   # opening price: engine, band, close lookup
python3 test_products.py      # products: specs, front months, config parsing
python3 test_timestamps.py    # PTP timestamps + threaded receive path
```

Terminal 1 — start a consumer (joins the multicast group and decodes):

```bash
python3 consumer.py --summary
```

Terminal 2 — run the simulator for an hour, opening at yesterday's ES close in a
25-point band around it:

```bash
python3 simulator.py --duration 3600 --rate 20
```

Defaults match CME **channel 310** (E-mini S&P 500), Incremental feed A,
multicast `224.0.31.1:14310` — read from `config.xml`, not hardcoded. TTL defaults to 0 (this host only) so nothing
leaves your machine; raise `--ttl` to reach the LAN.

### Static, serverless dashboard

To bake a self-contained `dashboard.html` you can open directly (file://), with
the dynamics of your choice:

```bash
python3 build_dashboard.py --volatility 1.2 --jump-prob 0.04 --jump-size 12
python3 build_dashboard.py --start 7772.50 --band 40
```

## Key options

`simulator.py`: `--product --start --band --low --high --offline --refresh
--duration --rate --depth --volatility --reversion --jump-prob --jump-size
--security-id --group --port --iface --ttl --seed --quiet`

`consumer.py`: `--product --group --port --iface --count --summary --clock
--tai-offset --queue --no-threads`

`products.py`: `--config --group --show`  ·  `contracts.py`: `[codes…]`

`settlement.py`: `--product --symbol --all --offline --refresh --json`

`--group`/`--port`/`--security-id` default to the selected product's values and
override them when passed.
