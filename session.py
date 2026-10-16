"""
Dashboard sessions: a stretch of MDP 3.0 feed, decoded from the wire.

A session is what the browser dashboard renders. There are two sources, and one
pipeline behind both:

    simulate(params)        ChannelPublisher (the live simulator's own code)
                               -> Transport (its loss model, no socket)
                               -> datagrams ─┐
    from_capture(datagrams)  pcap / pcapng ─┴─> Recorder: FeedHandler decodes
                                                every datagram; books, events,
                                                feed timeline, samples

So a simulated session goes through exactly the publisher, loss model and feed
handler that run on the network, and a capture — the simulator's or a real CME
line's — renders the same way. Every book in a session is the *feed handler's*
book, rebuilt from bytes: when loss forces a recovery, the dashboard shows the
instrument recovering, not the engine's private state.

`SessionParams` is the validated parameter set, shared by the Flask app
(query strings), record_session.py and build_dashboard.py (CLI flags).

Session JSON (version 2), compact by design (frames dominate):

    source       {kind: simulated|capture, name, params?}
    schema       wire description + per-template layout (the byte model)
    schemas      schemas available on this host
    catalog      the product chooser's products
    channels     [{id, label, feeds[], arb{...}, decodeErrors}]
    instruments  [{key, code, symbol, label, securityId, channel, tick, decimals,
                   low, high, center, start, startSource, ..., definition, summary}]
    frames       [{t: ms, i: [{m, bb, bo, b, o, tr, s, r} per instrument]}]
    packets      [[t_ms, channel_idx, kind I|S|N, feed A|B, seq, bytes, status, tpl]]
    events       [{t, kind, text}]
    samples      {template_id: {hex, segments, kind, feed, seq}}
"""
from __future__ import annotations

import io
import math
import os
from dataclasses import asdict, dataclass, field

from channel import build_instruments, build_publishers
from contracts import DEFAULT_PRODUCT, get_spec
from feedhandler import INCREMENTAL, INSTRUMENT, KINDS, LIVE, SHORT, SNAPSHOT, FeedHandler
from packet import PACKET_HEADER, parse_packet
from pcap import PcapWriter, ReadStats, read_packets
from products import MAX_PRODUCTS, get_product, parse_product_list, product_catalog, simulatable
from transport import Transport
from wire import LABELS, SchemaMismatch, available, default_name, open_wire

STEP_NS = 100_000_000                       # one engine step = 100 ms of feed
BASE_TS = 1_787_000_000_000_000_000          # fixed epoch: sessions are reproducible
MAX_STEPS = 3000
MAX_FRAMES = 3000
STATE_CODE = {"live": 0, "recovering": 1, "waiting": 2}


class ParamError(ValueError):
    """A parameter the caller can fix (HTTP 400)."""


def _per_product(raw, codes: list[str], name: str) -> dict:
    """'7772.5' (single product) or 'ES=7772.5,CL=92' -> {code: float}."""
    if raw in (None, "", {}):
        return {}
    if isinstance(raw, dict):
        return {k.upper(): float(v) for k, v in raw.items() if v not in (None, "")}
    raw = str(raw)
    if "=" not in raw and ":" not in raw:
        if len(codes) > 1:
            raise ParamError(f"{name} needs CODE=value pairs with several products")
        return {codes[0]: _num(raw, name)}
    out = {}
    for part in raw.split(","):
        code, _, val = part.replace(":", "=").partition("=")
        code = code.strip().upper()
        if code not in codes:
            raise ParamError(f"{name}: {code} is not one of {','.join(codes)}")
        if val.strip():
            out[code] = _num(val, name)
    return out


def _num(v, name, lo=None, hi=None, cast=float):
    try:
        x = cast(v)
    except (TypeError, ValueError):
        raise ParamError(f"{name} must be a number, got {v!r}") from None
    if isinstance(x, float) and not math.isfinite(x):
        raise ParamError(f"{name} must be finite")
    if lo is not None and x < lo or hi is not None and x > hi:
        raise ParamError(f"{name} must be between {lo} and {hi}, got {x}")
    return x


@dataclass
class SessionParams:
    products: list = field(default_factory=lambda: [DEFAULT_PRODUCT])
    schema: str = field(default_factory=default_name)
    steps: int = 400
    depth: int = 10
    volatility: float = 0.6
    reversion: float = 0.004
    jump_prob: float = 0.0
    jump_size: float = 8.0
    seed: int = 20260820
    start: dict = field(default_factory=dict)
    low: dict = field(default_factory=dict)
    high: dict = field(default_factory=dict)
    band: dict = field(default_factory=dict)
    loss_a: float = 0.0
    loss_b: float = 0.0
    snapshot_every: int = 10                 # steps between snapshot loops (10 = 1 s)
    secdef_every: int = 50                   # steps between definition loops
    feeds: str = "AB"
    offline: bool = False
    refresh: bool = False
    security_id: int | None = None

    def validate(self) -> "SessionParams":
        try:
            self.products = parse_product_list(self.products)
        except ValueError as e:
            raise ParamError(str(e)) from None
        known = {p.code for p in simulatable()}
        bad = [c for c in self.products if c not in known]
        if bad:
            raise ParamError(f"unknown product(s) {', '.join(bad)}")
        if self.schema not in available() and not os.path.exists(str(self.schema)):
            raise ParamError(f"schema {self.schema!r} is not available here "
                             f"(have: {', '.join(available())})")
        self.steps = _num(self.steps, "steps", 10, MAX_STEPS, int)
        self.depth = _num(self.depth, "depth", 1, 20, int)
        self.volatility = _num(self.volatility, "volatility", 0, 20)
        self.reversion = _num(self.reversion, "reversion", 0, 1)
        self.jump_prob = _num(self.jump_prob, "jump_prob", 0, 1)
        self.jump_size = _num(self.jump_size, "jump_size", 0, 500)
        self.seed = _num(self.seed, "seed", 0, 2 ** 31 - 1, int)
        self.loss_a = _num(self.loss_a, "loss_a", 0, 0.9)
        self.loss_b = _num(self.loss_b, "loss_b", 0, 0.9)
        self.snapshot_every = _num(self.snapshot_every, "snapshot_every", 0, 10_000, int)
        self.secdef_every = _num(self.secdef_every, "secdef_every", 0, 10_000, int)
        if self.feeds not in ("AB", "A", "B"):
            raise ParamError("feeds must be AB, A or B")
        for name in ("start", "low", "high", "band"):
            setattr(self, name, _per_product(getattr(self, name), self.products, name))
        for code in self.products:
            lo, hi = self.low.get(code), self.high.get(code)
            if lo is not None and hi is not None and hi <= lo:
                raise ParamError(f"{code}: high must be greater than low")
        return self

    @classmethod
    def from_query(cls, q) -> "SessionParams":
        """From a request's query args (werkzeug MultiDict or a plain dict)."""
        g = q.get
        products = g("products") or g("product") or DEFAULT_PRODUCT
        p = cls(products=products, schema=g("schema") or default_name())
        for name in ("steps", "depth", "volatility", "reversion", "jump_prob", "jump_size",
                     "seed", "loss_a", "loss_b", "snapshot_every", "secdef_every"):
            if g(name) not in (None, ""):
                setattr(p, name, g(name))
        for name in ("start", "low", "high", "band"):
            p.__dict__[name] = g(name) or {}
        p.feeds = (g("feeds") or "AB").upper()
        p.offline = g("offline") in ("1", "true", "yes")
        if g("security_id") not in (None, ""):
            p.security_id = _num(g("security_id"), "security_id", 1, 2 ** 31 - 1, int)
        return p.validate()

    def to_query(self) -> dict:
        q = {"products": ",".join(self.products), "schema": self.schema}
        for k, v in asdict(self).items():
            if k in ("products", "schema", "offline", "refresh", "security_id"):
                continue
            if isinstance(v, dict):
                if v:
                    q[k] = ",".join(f"{c}={x:g}" for c, x in v.items())
            else:
                q[k] = v
        return q

    def simulator_command(self) -> str:
        """The `simulator.py` flags that put these dynamics on the live feed."""
        parts = ["python3 simulator.py", f"--product {','.join(self.products)}",
                 f"--schema {self.schema}", "--duration 3600 --rate 10",
                 f"--volatility {self.volatility:g}", f"--reversion {self.reversion:g}"]
        if self.jump_prob:
            parts.append(f"--jump-prob {self.jump_prob:g} --jump-size {self.jump_size:g}")
        for flag, d in (("--start", self.start), ("--low", self.low),
                        ("--high", self.high), ("--band", self.band)):
            if d:
                val = (f"{next(iter(d.values())):g}" if len(self.products) == 1
                       else ",".join(f"{c}={v:g}" for c, v in d.items()))
                parts.append(f"{flag} {val}")
        if self.loss_a:
            parts.append(f"--loss-a {self.loss_a:g}")
        if self.loss_b:
            parts.append(f"--loss-b {self.loss_b:g}")
        if self.feeds != "AB":
            parts.append(f"--feeds {self.feeds}")
        parts.append(f"--depth {self.depth} --seed {self.seed}")
        return " ".join(parts)


@dataclass
class Datagram:
    ts_ns: int
    dst: tuple
    payload: bytes
    sent: bool = True           # False: dropped by the loss model before the wire
    src: tuple = ("127.0.0.1", 30000)


def _r(x, nd=10):
    return None if x is None else round(x, nd)


class Recorder:
    """Datagrams in, a session dict out. Owns one FeedHandler."""

    def __init__(self, wire, routes_for, instrument_keys=None, depth: int = 10):
        self.wire = wire
        self.depth = depth
        self.events: list[dict] = []
        self.handler = FeedHandler(wire, on_event=self._event)
        self.handler.add_routes_for(routes_for)
        self.keys = list(instrument_keys or [])     # security ids to track, in order
        self.frames: list[dict] = []
        self.packets: list[list] = []
        self.samples: dict[str, dict] = {}
        self.t0: int | None = None
        self.now = 0
        self.unrouted = 0
        self.total_bytes = 0
        self.channel_order = list(self.handler.channels)

    def _event(self, kind: str, text: str) -> None:
        self.events.append({"t": self._ms(self.now), "kind": kind, "text": text})

    def _ms(self, ts):
        return 0 if self.t0 is None else round((ts - self.t0) / 1e6, 3)

    def feed(self, dg: Datagram) -> None:
        if self.t0 is None:
            self.t0 = dg.ts_ns
        self.now = dg.ts_ns
        route = self.handler.routes.get(dg.dst)
        if route is None:
            self.unrouted += 1
            return
        ch, kind, feed_id = route
        h = self.handler.channels[ch]
        seq, _ts, frames = parse_packet(dg.payload)
        tpls = []
        for f in frames:
            if len(f) >= 4:
                tpls.append(int.from_bytes(f[2:4], "little"))
        status = "drop"
        if dg.sent:
            self.total_bytes += len(dg.payload)
            h.tick(dg.ts_ns)
            before = h.last_seq
            got = self.handler.on_datagram(dg.dst, dg.payload, dg.ts_ns)
            if kind != INCREMENTAL:
                status = "loop"
            elif got is not None:
                status = "win"
            elif before is not None and seq > before + 1:
                status = "held"
            else:
                status = "dup"
            self._sample(dg, kind, feed_id, seq, frames, tpls)
        self.packets.append([self._ms(dg.ts_ns), self.channel_order.index(ch),
                             SHORT[kind], feed_id, seq, len(dg.payload), status,
                             ",".join(map(str, tpls))])

    def _sample(self, dg, kind, feed_id, seq, frames, tpls) -> None:
        """Keep the first packet carrying each template, explained byte by byte."""
        new = [t for t in tpls if str(t) not in self.samples]
        if not new:
            return
        try:
            segs = [{"off": 0, "len": 4, "path": "packet.MsgSeqNum", "kind": "packet",
                     "field": "MsgSeqNum", "type": "uint32", "value": seq},
                    {"off": 4, "len": 8, "path": "packet.SendingTime", "kind": "packet",
                     "field": "SendingTime", "type": "uint64 ns", "value": _ts_of(dg.payload)}]
            off = PACKET_HEADER.size
            for i, f in enumerate(frames):
                segs.append({"off": off, "len": 2, "path": f"message[{i}].MsgSize",
                             "kind": "size", "field": "MsgSize", "type": "uint16",
                             "value": len(f), "msg": i})
                off += 2
                for s in self.wire.explain(f):
                    s = dict(s, off=s["off"] + off, msg=i)
                    s["value"] = _jsonable(s["value"])
                    segs.append(s)
                off += len(f)
        except (SchemaMismatch, KeyError, ValueError, IndexError):
            return
        sample = {"hex": dg.payload.hex(), "segments": segs, "kind": kind, "feed": feed_id,
                  "seq": seq, "templates": tpls,
                  "names": [self.wire.template_name(t) for t in tpls]}
        for t in new:
            self.samples[str(t)] = sample

    def frame(self, ts_ns: int) -> None:
        """Record every tracked instrument's state as of `ts_ns`."""
        self.now = ts_ns
        self.handler.tick(ts_ns)
        row = []
        for sid in self.keys:
            ins = self._find(sid)
            if ins is None:
                row.append(None)
                continue
            b = ins.book
            bids = sorted(b.bids.items(), key=lambda kv: -kv[0])[:self.depth]
            offers = sorted(b.offers.items(), key=lambda kv: kv[0])[:self.depth]
            bb, bo = (bids[0][0] if bids else None), (offers[0][0] if offers else None)
            new_trades = list(b.trades)[len(b.trades) - min(len(b.trades),
                                        b.trade_count - getattr(ins, "_seen_trades", 0)):]
            ins._seen_trades = b.trade_count
            row.append({
                "m": _r((bb + bo) / 2) if bb is not None and bo is not None else None,
                "bb": bb, "bo": bo,
                "b": [[p, lv.size, lv.orders] for p, lv in bids],
                "o": [[p, lv.size, lv.orders] for p, lv in offers],
                "tr": [[t.price, t.size, t.aggressor] for t in new_trades],
                "s": STATE_CODE.get(ins.state, 2), "r": ins.rpt_seq})
        self.frames.append({"t": self._ms(ts_ns), "i": row})

    def _find(self, sid):
        for h in self.handler.channels.values():
            if sid in h.instruments:
                return h.instruments[sid]
        return None

    # ---- output ----
    def result(self, instruments_meta: list[dict], source: dict) -> dict:
        # only channels that carried traffic or hold a shown instrument; packet
        # rows refer to channels by index, so renumber them to match
        tracked = {m.get("channel") for m in instruments_meta}
        busy = {self.channel_order[p[1]] for p in self.packets}
        keep = [ch for ch in self.channel_order if ch in busy or ch in tracked]
        remap = {self.channel_order.index(ch): i for i, ch in enumerate(keep)}
        packets = [[p[0], remap[p[1]], *p[2:]] for p in self.packets if p[1] in remap]
        channels = []
        for ch in keep:
            h = self.handler.channels[ch]
            feeds = [{"kind": SHORT[k], "feed": f, "ip": ip, "port": port}
                     for (ip, port), (c, k, f) in self.handler.routes.items() if c == ch]
            p = next((p for p in simulatable() if p.channel_id == ch), None)
            channels.append({
                "id": ch, "label": p.channel_label if p else str(ch), "feeds": feeds,
                "arb": {"accepted": h.arb.accepted, "duplicates": h.arb.duplicates,
                        "won": h.arb.won, "only": h.one_feed_only(), "lost": h.arb.lost,
                        "gaps": h.arb.gap_events, "held": h.arb.reordered,
                        "late": h.arb.late, "lastSeq": h.last_seq},
                "snapshotPackets": h.snapshot_packets,
                "definitionPackets": h.definition_packets,
                "decodeErrors": h.decode_errors})
        for meta in instruments_meta:
            ins = self._find(meta["securityId"])
            meta["definition"] = _definition(ins.definition) if ins and ins.definition else None
            meta["summary"] = None if ins is None else {
                "state": ins.state, "rptSeq": ins.rpt_seq, "gaps": ins.gaps,
                "recoveries": ins.recoveries, "checks": ins.snapshots_checked,
                "mismatches": ins.snapshot_mismatches, "entries": ins.entries}
        wire = self.wire
        return {
            "version": 2, "source": source,
            "schema": dict(wire.describe(), layout=wire.layout()),
            "schemas": [{"name": n, "label": LABELS.get(n, n)} for n in available()],
            "catalog": product_catalog(), "maxProducts": MAX_PRODUCTS,
            "depth": self.depth, "stepMs": STEP_NS / 1e6,
            "channels": channels, "instruments": instruments_meta,
            "frames": self.frames, "packets": packets, "events": self.events,
            "samples": self.samples,
            "totals": {"datagrams": len(self.packets), "bytes": self.total_bytes,
                       "unrouted": self.unrouted,
                       "dropped": sum(1 for p in self.packets if p[6] == "drop")},
        }


def _ts_of(payload: bytes) -> int:
    return PACKET_HEADER.unpack_from(payload, 0)[1]


def _jsonable(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


def _definition(d) -> dict:
    return {"symbol": d.symbol, "asset": d.asset, "group": d.group, "exchange": d.exchange,
            "channel": d.channel, "tick": d.tick, "decimals": d.decimals,
            "tickValue": d.tick_value, "multiplier": d.multiplier, "maturity": d.maturity,
            "highLimit": d.high_limit, "lowLimit": d.low_limit,
            "referencePrice": d.reference_price, "depth": d.depth,
            "securityId": d.security_id}


def _instrument_meta(ins) -> dict:
    """What the dashboard needs about a simulated instrument (its configuration)."""
    p, spec, eng, o, c = ins.product, ins.product.spec, ins.engine, ins.origin, ins.contract
    feed = p.incremental
    return {"key": p.code, "code": p.code, "symbol": c.symbol, "label": p.label,
            "group": p.group, "exchange": p.exchange, "securityId": ins.security_id,
            "channel": p.channel_id, "channelLabel": p.channel_label,
            "tick": spec.tick, "tickValue": spec.tick_value, "decimals": spec.decimals,
            "low": eng.low, "high": eng.high, "center": eng.center,
            "start": o["start"], "startSource": o["source"], "closeDate": o["closeDate"],
            "closeSymbol": o["symbol"], "startStale": o["stale"],
            "clamped": o["clamped"], "outOfBand": o["outOfBand"],
            "contract": c.label, "expiry": c.expiry.isoformat(),
            "multicast": f"{feed.ip}:{feed.port}", "band": round(eng.high - eng.low, 10)}


# ---------------------------------------------------------------- simulate ----
def simulate(params: SessionParams, want_datagrams: bool = False):
    """Run a session through the real publisher, loss model and feed handler.

    Returns the session dict, or (session, datagrams) with `want_datagrams`.
    """
    p = params.validate() if not isinstance(params.products, list) else params
    wire = open_wire(p.schema)
    instruments = build_instruments(
        p.products, starts=p.start, lows=p.low, highs=p.high, bands=p.band, depth=p.depth,
        volatility=p.volatility, reversion=p.reversion, jump_prob=p.jump_prob,
        jump_size=p.jump_size, seed=p.seed, offline=p.offline, refresh=p.refresh,
        security_id=p.security_id)
    rec = Recorder(wire, [ins.product for ins in instruments],
                   [ins.security_id for ins in instruments], depth=p.depth)
    datagrams: list[Datagram] = []
    clock = {"ts": BASE_TS}

    def tick_clock():
        clock["ts"] += 1_000                     # 1 µs per packet keeps them ordered
        return clock["ts"]

    def observe(feed, pkt, ts, sent):
        dg = Datagram(ts, (feed.ip, feed.port), pkt, sent)
        datagrams.append(dg)
        rec.feed(dg)

    transport = Transport(deliver=lambda pkt, dst: None, observer=observe,
                          loss={"A": p.loss_a, "B": p.loss_b}, seed=p.seed)
    pubs = build_publishers(instruments, wire, transport, depth=p.depth, feeds=p.feeds,
                            clock=tick_clock)
    for ins in instruments:
        ins.created_ns = BASE_TS
    for step in range(p.steps):
        clock["ts"] = BASE_TS + step * STEP_NS
        if p.secdef_every and step % p.secdef_every == 0:
            for pub in pubs:
                pub.definition_loop()
        if p.snapshot_every and step % p.snapshot_every == 0:
            for pub in pubs:
                pub.snapshot_loop()
        for pub in pubs:
            pub.step()
        rec.frame(BASE_TS + step * STEP_NS + STEP_NS - 1)
    session = rec.result([_instrument_meta(ins) for ins in instruments],
                         {"kind": "simulated",
                          "name": ", ".join(ins.contract.symbol for ins in instruments),
                          "params": p.to_query(), "command": p.simulator_command()})
    return (session, datagrams) if want_datagrams else session


def to_pcap(datagrams: list[Datagram], nanos: bool = True) -> bytes:
    """The datagrams that reached the wire, as a pcap file."""
    buf = io.BytesIO()
    w = PcapWriter(buf, nanos=nanos)
    for dg in datagrams:
        if dg.sent:
            w.write(dg.payload, dg.ts_ns, dg.src, dg.dst)
    w.flush()
    return buf.getvalue()


# ----------------------------------------------------------------- capture ----
def scan_instruments(datagrams, wire, routes) -> list[dict]:
    """Instruments in a capture by activity: [{securityId, entries, symbol?, channel}]."""
    counts: dict[int, dict] = {}
    for dg in datagrams:
        route = routes.get(dg.dst)
        if route is None:
            continue
        _seq, _ts, frames = parse_packet(dg.payload)
        for f in frames:
            try:
                d = wire.decode(f)
            except SchemaMismatch:
                continue
            if d["id"] in (46, 48):
                for e in d["groups"].get("MDEntries", []):
                    c = counts.setdefault(e.get("securityId"), {"entries": 0, "channel": route[0]})
                    c["entries"] += 1
            elif d["id"] == 54:
                c = counts.setdefault(d["root"]["securityId"], {"entries": 0, "channel": route[0]})
                c["symbol"] = d["root"].get("symbol")
                c["asset"] = d["root"].get("asset")
    return sorted(({"securityId": k, **v} for k, v in counts.items() if k is not None),
                  key=lambda x: -x["entries"])


def from_capture(datagrams: list[Datagram], name: str, schema: str = "auto",
                 instruments: list[int] | None = None, depth: int = 10) -> dict:
    """A session from captured datagrams: the busiest (or chosen) four instruments."""
    datagrams = sorted(datagrams, key=lambda d: d.ts_ns)
    wire = open_wire(schema)
    products = list(simulatable())
    probe = FeedHandler(wire)
    probe.add_routes_for(products)
    found = scan_instruments(datagrams, wire, probe.routes)
    keys = instruments or [f["securityId"] for f in found if f["entries"]][:MAX_PRODUCTS]
    if not keys:
        keys = [f["securityId"] for f in found][:MAX_PRODUCTS]
    rec = Recorder(wire, products, keys, depth=depth)
    if datagrams:
        t0, t1 = datagrams[0].ts_ns, datagrams[-1].ts_ns
        bucket = max(STEP_NS, (t1 - t0) // MAX_FRAMES + 1)
        edge = t0 + bucket
        for dg in datagrams:
            while dg.ts_ns >= edge:
                rec.frame(edge - 1)
                edge += bucket
            rec.feed(dg)
        rec.frame(edge - 1)
    by_sid = {f["securityId"]: f for f in found}
    metas = []
    for idx, sid in enumerate(keys):
        ins = rec._find(sid)
        dfn = ins.definition if ins else None
        info = by_sid.get(sid, {})
        code = (dfn.asset if dfn else None) or info.get("asset") or _code_for_sid(sid)
        spec = _spec_or_none(code)
        tick = (dfn.tick if dfn else None) or (spec.tick if spec else None) or _guess_tick(ins)
        decimals = (dfn.decimals if dfn else None)
        if decimals is None:
            decimals = spec.decimals if spec else _decimals_of(tick)
        prices = [p for fr in rec.frames if fr["i"][idx]
                  for p in (fr["i"][idx]["bb"], fr["i"][idx]["bo"]) if p is not None]
        lo, hi = (min(prices), max(prices)) if prices else (None, None)
        if dfn and dfn.low_limit is not None and dfn.high_limit is not None:
            lo, hi = min(lo, dfn.low_limit) if lo else dfn.low_limit, \
                max(hi, dfn.high_limit) if hi else dfn.high_limit
        ch = ins.channel if ins else info.get("channel")
        metas.append({
            "key": str(sid), "code": code or str(sid),
            "symbol": (dfn.symbol if dfn else None) or info.get("symbol") or f"sid {sid}",
            "label": spec.name if spec else (dfn.symbol if dfn else f"security {sid}"),
            "group": spec.group if spec else "", "exchange": dfn.exchange if dfn else "",
            "securityId": sid, "channel": ch, "channelLabel": "",
            "tick": tick, "tickValue": (dfn.tick_value if dfn else None) or
                                        (spec.tick_value if spec else None),
            "decimals": decimals, "low": lo, "high": hi,
            "center": None if lo is None else (lo + hi) / 2,
            "start": None, "startSource": "capture", "closeDate": None, "closeSymbol": None,
            "startStale": False, "contract": dfn.symbol if dfn else "", "expiry": "",
            "multicast": "", "band": None if lo is None else hi - lo})
    session = rec.result(metas, {"kind": "capture", "name": name})
    session["available"] = found[:200]
    return session


def datagrams_from_pcap(blob: bytes) -> tuple[list[Datagram], ReadStats]:
    stats = ReadStats()
    out = [Datagram(d.ts_ns, d.dst, d.payload, True, d.src) for d in read_packets(blob, stats)]
    return out, stats


def _code_for_sid(sid):
    for p in simulatable():
        if p.spec.security_id == sid:
            return p.code
    return None


def _spec_or_none(code):
    try:
        return get_spec(code) if code else None
    except ValueError:
        return None


def _guess_tick(ins) -> float:
    if ins is None:
        return 0.01
    prices = sorted(set(ins.book.bids) | set(ins.book.offers))
    diffs = [round(b - a, 10) for a, b in zip(prices, prices[1:]) if b - a > 1e-12]
    return min(diffs) if diffs else 0.01


def _decimals_of(tick: float) -> int:
    s = f"{tick:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def selected_products(session: dict) -> list:
    return [i["code"] for i in session["instruments"]]


__all__ = ["SessionParams", "ParamError", "Recorder", "simulate", "from_capture",
           "datagrams_from_pcap", "to_pcap", "Datagram", "KINDS", "LIVE", "SNAPSHOT",
           "INSTRUMENT", "get_product"]
