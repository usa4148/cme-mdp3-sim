"""
Live mode for the dashboard: the real multicast feed, through the feed handler.

A `LiveMonitor` joins every feed of up to four products' channels (exactly as
consumer.py and ladder.py do), runs a `FeedHandler` on its own thread, and
publishes a compact snapshot several times a second: each instrument's book,
new trades, recovery state and `rptSeq`, each channel's A/B arbitration tallies
and per-feed packet rates, and any new events (gaps, recoveries, definitions,
schema detection).

Monitors are shared: every browser watching the same products/feeds/interface
gets the same one (`LiveRegistry`), and a monitor stops a short grace period
after its last viewer leaves, so a page reload doesn't rejoin every group.
"""
from __future__ import annotations

import threading
import time
from collections import deque

from feedhandler import KINDS, SHORT, FeedHandler, FeedSet
from products import get_product, parse_product_list
from receiver import parse_ifaces
from wire import open_wire

SNAPSHOT_HZ = 5
GRACE_S = 10.0


class LiveMonitor(threading.Thread):
    def __init__(self, codes: list[str], schema: str = "auto", iface: str = "0.0.0.0",
                 feeds: str = "AB", depth: int = 10, hz: float = SNAPSHOT_HZ):
        super().__init__(name=f"live {','.join(codes)}", daemon=True)
        self.codes = codes
        self.products = [get_product(c) for c in codes]
        self.depth = depth
        self.interval = 1.0 / hz
        self.wire = open_wire(schema)
        self._events: deque = deque(maxlen=500)
        self.event_seq = 0
        if hasattr(self.wire, "on_detect"):
            self.wire.on_detect = lambda n: self._event("detected", f"feed is using the {n} schema")
        self.handler = FeedHandler(self.wire, on_event=self._event)
        self.handler.add_routes_for(self.products, KINDS, feeds)
        self.feedset = FeedSet(self.handler, iface)
        self.iface, self.feeds = iface, feeds
        self.started = time.time()
        self.stop_event = threading.Event()
        self.cond = threading.Condition()
        self.version = 0
        self.latest: dict | None = None
        self.error: str | None = None
        self._trade_marks: dict[int, int] = {}
        self._feed_counts: dict[tuple, int] = {}
        self._rates: dict[tuple, float] = {}
        self._last_rate_t = time.monotonic()

    def _event(self, kind: str, text: str) -> None:
        self.event_seq += 1
        self._events.append({"n": self.event_seq, "t": round(time.time() - self.started, 3),
                             "kind": kind, "text": text})

    def run(self) -> None:
        next_snap = time.monotonic()
        counts = self.feedset.counts
        try:
            while not self.stop_event.is_set():
                self.feedset.poll(0.02)
                now = time.monotonic()
                if now - self._last_rate_t >= 1.0:
                    dt = now - self._last_rate_t
                    self._rates = {k: (v - self._feed_counts.get(k, 0)) / dt
                                   for k, v in counts.items()}
                    self._feed_counts = dict(counts)
                    self._last_rate_t = now
                if now >= next_snap:
                    snap = self.snapshot()
                    with self.cond:
                        self.latest = snap
                        self.version += 1
                        self.cond.notify_all()
                    next_snap = now + self.interval
        except Exception as e:                      # surface it to the browser
            self.error = repr(e)
            with self.cond:
                self.version += 1
                self.cond.notify_all()
        finally:
            self.feedset.close()

    def snapshot(self) -> dict:
        instruments = []
        for p in self.products:
            ch = self.handler.channels[p.channel_id]
            ins = next((i for i in ch.instruments.values()
                        if i.definition and i.definition.asset == p.code), None) \
                or ch.instruments.get(p.spec.security_id)
            spec, c = p.spec, p.front_month()
            row = {"code": p.code, "label": p.label, "channel": p.channel_id,
                   "symbol": c.symbol, "securityId": spec.security_id,
                   "tick": spec.tick, "decimals": spec.decimals, "state": "waiting",
                   "rptSeq": None, "gaps": 0, "recoveries": 0, "checks": 0,
                   "mismatches": 0, "bb": None, "bo": None, "m": None,
                   "b": [], "o": [], "tr": [], "volume": None, "last": None,
                   "definition": None}
            if ins is not None:
                b, d = ins.book, ins.definition
                bids = sorted(b.bids.items(), key=lambda kv: -kv[0])[:self.depth]
                offers = sorted(b.offers.items(), key=lambda kv: kv[0])[:self.depth]
                mark = self._trade_marks.get(ins.security_id, 0)
                fresh = min(len(b.trades), b.trade_count - mark)
                self._trade_marks[ins.security_id] = b.trade_count
                bb = bids[0][0] if bids else None
                bo = offers[0][0] if offers else None
                row.update({
                    "securityId": ins.security_id, "state": ins.state, "rptSeq": ins.rpt_seq,
                    "gaps": ins.gaps, "recoveries": ins.recoveries,
                    "checks": ins.snapshots_checked, "mismatches": ins.snapshot_mismatches,
                    "bb": bb, "bo": bo,
                    "m": (bb + bo) / 2 if bb is not None and bo is not None else None,
                    "b": [[px, lv.size, lv.orders] for px, lv in bids],
                    "o": [[px, lv.size, lv.orders] for px, lv in offers],
                    "tr": [[t.price, t.size, t.aggressor]
                           for t in list(b.trades)[len(b.trades) - fresh:]] if fresh else [],
                    "volume": b.volume,
                    "last": [b.last_trade.price, b.last_trade.size, b.last_trade.aggressor]
                    if b.last_trade else None})
                if d:
                    row.update(symbol=d.symbol, tick=d.tick, decimals=d.decimals,
                               definition={"symbol": d.symbol, "tick": d.tick,
                                           "decimals": d.decimals, "maturity": d.maturity,
                                           "exchange": d.exchange, "highLimit": d.high_limit,
                                           "lowLimit": d.low_limit,
                                           "referencePrice": d.reference_price})
            instruments.append(row)
        channels = []
        for chid, h in self.handler.channels.items():
            feeds = []
            for (ip, port), (c, kind, fid) in self.handler.routes.items():
                if c == chid:
                    feeds.append({"kind": SHORT[kind], "feed": fid, "ip": ip, "port": port,
                                  "packets": self.feedset.counts.get((ip, port), 0),
                                  "rate": round(self._rates.get((ip, port), 0.0), 1)})
            channels.append({"id": chid, "feeds": feeds,
                             "arb": {"accepted": h.arb.accepted, "duplicates": h.arb.duplicates,
                                     "won": h.arb.won, "only": h.one_feed_only(),
                                     "lost": h.arb.lost, "gaps": h.arb.gap_events,
                                     "held": h.arb.reordered, "lastSeq": h.last_seq},
                             "decodeErrors": h.decode_errors})
        return {"t": round(time.time() - self.started, 3), "wall": time.time(),
                "schema": self.wire.describe(), "iface": self.iface, "feeds": self.feeds,
                "packets": self.feedset.packets, "instruments": instruments,
                "channels": channels, "eventSeq": self.event_seq}

    def events_since(self, n: int) -> list[dict]:
        return [e for e in list(self._events) if e["n"] > n]

    def wait(self, after_version: int, timeout: float) -> int:
        with self.cond:
            self.cond.wait_for(lambda: self.version > after_version or self.error,
                               timeout=timeout)
            return self.version

    def stop(self) -> None:
        self.stop_event.set()


class LiveRegistry:
    """One monitor per (products, schema, iface, feeds), shared by viewers."""

    def __init__(self, grace: float = GRACE_S):
        self.grace = grace
        self.lock = threading.Lock()
        self.monitors: dict[tuple, list] = {}       # key -> [monitor, viewers, idle_since]

    @staticmethod
    def key(products, schema, iface, feeds) -> tuple:
        codes = tuple(parse_product_list(products))
        iface = ",".join(parse_ifaces(iface))         # ValueError for anything else
        if feeds not in ("AB", "A", "B"):
            raise ValueError("feeds must be AB, A or B")
        if schema not in ("auto", "cme", "subset"):
            raise ValueError("schema must be auto, cme or subset")
        return codes, schema, iface, feeds

    def acquire(self, key: tuple) -> LiveMonitor:
        with self.lock:
            self._reap()
            ent = self.monitors.get(key)
            if ent is None or not ent[0].is_alive():
                codes, schema, iface, feeds = key
                mon = LiveMonitor(list(codes), schema, iface, feeds)
                mon.start()
                ent = self.monitors[key] = [mon, 0, None]
            ent[1] += 1
            ent[2] = None
            return ent[0]

    def release(self, key: tuple) -> None:
        with self.lock:
            ent = self.monitors.get(key)
            if ent:
                ent[1] -= 1
                if ent[1] <= 0:
                    ent[2] = time.monotonic()
            self._reap()

    def _reap(self) -> None:
        now = time.monotonic()
        for k, (mon, viewers, idle) in list(self.monitors.items()):
            if viewers <= 0 and idle is not None and now - idle >= self.grace:
                mon.stop()
                del self.monitors[k]

    def stop_all(self) -> None:
        with self.lock:
            for mon, _v, _i in self.monitors.values():
                mon.stop()
            self.monitors.clear()
