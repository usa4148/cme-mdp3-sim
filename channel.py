"""
One CME channel's worth of feeds, published from one or more simulated products.

CME carries each channel on three kinds of multicast feed, each sent twice
(A and B) on separate groups so a handler can survive loss on either:

=================  =========================================================
Incremental        Real-time book and trade updates (templates 46, 48). One
                   packet sequence per channel, shared by every instrument on
                   it; an instrument's own `rptSeq` orders its entries.
Snapshot           A loop of SnapshotFullRefresh (52), one per instrument,
                   each naming the incremental packet and `rptSeq` it is
                   current to. Packet sequence restarts at 1 every loop.
Instrument Replay  A loop of MDInstrumentDefinitionFuture (54), one per
                   instrument: security id, symbol, tick, contract, limits.
=================  =========================================================

`ChannelPublisher` owns that: every instrument on the channel steps its own
`MarketEngine`, and the publisher frames the results onto the channel's feeds
through a `Transport`. Products on the same channel share its feeds and its
incremental sequence, as they do on a real channel.

Messages are built in the canonical vocabulary and encoded by a `wire.Wire`, so
the same publisher emits CME's official layout or the working subset. Time comes
from `clock` (default the wall clock), which lets the dashboard record a
deterministic session through exactly this code.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone

from engine import MarketEngine
from packet import build_packet
from products import Product, by_channel, get_product
from settlement import resolve_start_band

INCREMENTAL, SNAPSHOT, INSTRUMENT = "Incremental", "Snapshot", "Instrument Replay"

# ISO 10962 MIC per listing exchange
_MIC = {"CME": "XCME", "CBOT": "XCBT", "NYMEX": "XNYM", "COMEX": "XCEC"}

# Per asset class: CFI code, CME product complex (tag 462), unit of measure.
# CFI codes are representative of each class rather than looked up per product.
_CLASS = {
    "Equity": ("FFICSX", 5, "IPNT"),
    "FX":     ("FFCPSX", 4, "USD"),
    "Rates":  ("FFDPSX", 14, "USD"),
    "Grains": ("FCAPSX", 2, "BU"),
    "Energy": ("FCEPSX", 16, "BBL"),
    "Metals": ("FCMPSX", 17, "OZTR"),
}
_UNIT = {"NG": "MMBTU", "RB": "GAL", "HO": "GAL", "HG": "LBS", "ZM": "SHTONS",
         "ZL": "LBS", "6E": "EUR", "6J": "JPY", "6B": "GBP", "6A": "AUD", "6C": "CAD"}

_EPOCH = date(1970, 1, 1)


def local_mkt_date(d: date | str | None) -> int | None:
    """LocalMktDate: days since the Unix epoch."""
    if d is None:
        return None
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return (d - _EPOCH).days


def _ns(d: date, at: dtime) -> int:
    return int(datetime.combine(d, at, tzinfo=timezone.utc).timestamp() * 1e9)


def entries_from_increments(engine, security_id, incs):
    return [{
        "mdEntryPx": inc.price,
        "mdEntrySize": inc.size,
        "securityId": security_id,
        "rptSeq": engine.next_rpt_seq(),
        "numberOfOrders": inc.orders,
        "mdPriceLevel": inc.level,
        "mdUpdateAction": inc.action,
        "mdEntryType": inc.side,
    } for inc in incs]


def entries_from_trades(engine, security_id, trades):
    return [{
        "mdEntryPx": t.price,
        "mdEntrySize": t.size,
        "securityId": security_id,
        "rptSeq": engine.next_rpt_seq(),
        "numberOfOrders": 0,
        "aggressorSide": t.aggressor,
        "mdUpdateAction": "New",
        "mdEntryType": "Trade",
    } for t in trades]


@dataclass
class Instrument:
    """One product being simulated: its engine plus the session statistics a
    snapshot reports (open, high, low, last, volume)."""
    product: Product
    engine: MarketEngine
    origin: dict                      # settlement.resolve_start_band() result
    security_id: int
    created_ns: int = field(default_factory=time.time_ns)
    last_trade: tuple | None = None   # (price, size)
    volume: int = 0
    open_px: float | None = None
    high_px: float | None = None
    low_px: float | None = None
    transact_ns: int = 0

    @property
    def code(self) -> str:
        return self.product.code

    @property
    def contract(self):
        return self.product.front_month()

    def on_trades(self, trades, ts_ns: int) -> None:
        for t in trades:
            self.last_trade = (t.price, t.size)
            self.volume += t.size
            if self.open_px is None:
                self.open_px = t.price
            self.high_px = t.price if self.high_px is None else max(self.high_px, t.price)
            self.low_px = t.price if self.low_px is None else min(self.low_px, t.price)
        self.transact_ns = ts_ns

    # ---- template 52 ----
    def snapshot_entries(self) -> list[dict]:
        eng = self.engine
        out = []
        for side, levels, best_first in (("Bid", eng.bids, True), ("Offer", eng.offers, False)):
            for i, px in enumerate(sorted(levels, reverse=best_first)):
                lv = levels[px]
                out.append({"mdEntryPx": px, "mdEntrySize": lv.size,
                            "numberOfOrders": lv.orders, "mdPriceLevel": i + 1,
                            "mdEntryType": side})
        if self.last_trade:
            out.append({"mdEntryPx": self.last_trade[0], "mdEntrySize": self.last_trade[1],
                        "mdEntryType": "Trade"})
        if self.open_px is not None:
            out.append({"mdEntryPx": self.open_px, "mdEntryType": "OpenPrice",
                        "openCloseSettlFlag": "DailyOpenPrice"})
        if self.high_px is not None:
            out.append({"mdEntryPx": self.high_px, "mdEntryType": "TradingSessionHighPrice"})
            out.append({"mdEntryPx": self.low_px, "mdEntryType": "TradingSessionLowPrice"})
        if self.volume:
            out.append({"mdEntrySize": self.volume, "mdEntryType": "ElectronicVolume"})
        if self.origin.get("source") == "priorClose":
            out.append({"mdEntryPx": self.origin["start"], "mdEntryType": "SettlementPrice",
                        "settlPriceType": ["FinalDaily", "Actual"],
                        "tradingReferenceDate": local_mkt_date(self.origin.get("closeDate"))})
        return out

    def snapshot_root(self, last_seq: int, tot: int, trade_date: int) -> dict:
        eng = self.engine
        return {"lastMsgSeqNumProcessed": last_seq, "totNumReports": tot,
                "securityId": self.security_id, "rptSeq": eng.rpt_seq,
                "transactTime": self.transact_ns, "lastUpdateTime": self.created_ns,
                "tradeDate": trade_date, "mdSecurityTradingStatus": "ReadyToTrade",
                "highLimitPrice": eng.high, "lowLimitPrice": eng.low,
                "maxPriceVariation": None}

    # ---- template 54 ----
    def definition(self, tot: int | None, depth: int) -> tuple[dict, dict]:
        p, spec, c = self.product, self.product.spec, self.contract
        cfi, complex_, unit = _CLASS.get(p.group, ("FFXXXX", 12, ""))
        closed = self.origin.get("source") == "priorClose"
        root = {
            "matchEventIndicator": ["LastQuoteMsg", "EndOfEvent"],
            "totNumReports": tot,
            "securityUpdateAction": "Add",
            "lastUpdateTime": self.created_ns,
            "mdSecurityTradingStatus": "ReadyToTrade",
            "applId": p.channel_id,
            "marketSegmentId": 0,                       # not modeled
            "underlyingProduct": complex_,
            "securityExchange": _MIC.get(p.exchange, "XCME"),
            "securityGroup": p.group_code or p.code,
            "asset": p.code,
            "symbol": c.symbol,
            "securityId": self.security_id,
            "securityType": "FUT",
            "cfiCode": cfi,
            "maturityMonthYear": {"year": c.year, "month": c.month},
            "currency": "USD",
            "settlCurrency": "USD",
            "matchAlgorithm": "F",                      # FIFO
            "minTradeVol": 1,
            "maxTradeVol": 2000,
            "minPriceIncrement": spec.tick,
            "displayFactor": 1.0,
            "priceDisplayFormat": spec.decimals,
            "unitOfMeasure": _UNIT.get(p.code, unit),
            "unitOfMeasureQty": spec.multiplier,
            "tradingReferencePrice": self.origin["start"] if closed else None,
            "settlPriceType": ["FinalDaily", "Actual"] if closed else ["NullValue"],
            "highLimitPrice": self.engine.high,
            "lowLimitPrice": self.engine.low,
            "minPriceIncrementAmount": spec.tick_value,
            "userDefinedInstrument": "N",
            "tradingReferenceDate": local_mkt_date(self.origin.get("closeDate")) if closed else None,
        }
        # Activation a year before expiry and the last trade at 13:30 UTC on the
        # expiry date: plausible shapes, not CME's listing calendar.
        groups = {
            "Events": [
                {"eventType": "Activation",
                 "eventTime": _ns(c.expiry - timedelta(days=365), dtime(23, 0))},
                {"eventType": "LastEligibleTradeDate", "eventTime": _ns(c.expiry, dtime(13, 30))},
            ],
            "MDFeedTypes": [{"mdFeedType": "GBX", "marketDepth": depth}],
            "InstAttrib": [{"instAttribValue": [
                "ElectronicMatchEligible", "OrderCrossEligible", "BlockTradeEligible",
                "EFPEligible", "EBFEligible", "EFSEligible", "EFREligible", "OTCEligible",
                "GTOrdersEligibility", "ImpliedMatchingEligibility"]}],
            "LotTypeRules": [{"lotType": 2, "minLotSize": 1.0}],   # round lot
        }
        return root, groups


def build_instruments(codes, *, starts=None, lows=None, highs=None, bands=None,
                      depth=10, volatility=0.6, reversion=0.004, jump_prob=0.0,
                      jump_size=8.0, seed=None, offline=False, refresh=False,
                      security_id=None) -> list[Instrument]:
    """One Instrument per product code: opening price, band and engine.

    Price settings are {code: value} maps; a product without one opens at its
    prior close in a band derived around it. Product i's engine is seeded
    `seed + i`, so a run is reproducible and the products move independently.
    """
    starts, lows, highs, bands = (d or {} for d in (starts, lows, highs, bands))
    out = []
    for i, code in enumerate(codes):
        prod = get_product(code)
        spec = prod.spec
        origin = resolve_start_band(starts.get(prod.code), lows.get(prod.code),
                                    highs.get(prod.code), bands.get(prod.code),
                                    product=prod.code, offline=offline, refresh=refresh)
        engine = MarketEngine(origin["low"], origin["high"], depth=depth,
                              volatility=volatility, reversion=reversion,
                              jump_prob=jump_prob, jump_size=jump_size,
                              seed=None if seed is None else seed + i,
                              start=origin["start"], tick=spec.tick,
                              tick_value=spec.tick_value)
        sid = security_id if (security_id is not None and len(codes) == 1) else spec.security_id
        out.append(Instrument(prod, engine, origin, sid))
    return out


def build_publishers(instruments, wire, transport, depth=10, feeds="AB",
                     clock=time.time_ns) -> list["ChannelPublisher"]:
    """One publisher per channel the instruments are on, in first-seen order."""
    pubs = []
    for ch, prods in by_channel([ins.product for ins in instruments]).items():
        members = [ins for ins in instruments if ins.product.channel_id == ch]
        pubs.append(ChannelPublisher(prods[0], members, wire, transport,
                                     depth=depth, feeds=feeds, clock=clock))
    return pubs


class ChannelPublisher:
    def __init__(self, product: Product, instruments: list[Instrument], wire,
                 transport, depth: int = 10, feeds: str = "AB", clock=time.time_ns):
        self.channel_id = product.channel_id
        self.label = product.channel_label
        self.instruments = instruments
        self.wire = wire
        self.clock = clock
        self.out = transport
        self.depth = depth
        self.feeds = {}
        for kind in (INCREMENTAL, SNAPSHOT, INSTRUMENT):
            a, b = product.feed_pair(kind)
            self.feeds[kind] = [f for f in (a, b)
                                if f is not None and f.feed_id in feeds.upper()]
        self.seq = 0                  # incremental packet sequence (per channel)
        self.snapshot_loops = 0
        self.definition_loops = 0
        self.packets = {INCREMENTAL: 0, SNAPSHOT: 0, INSTRUMENT: 0}

    def _send(self, kind: str, seq: int, ts_ns: int, messages: list[bytes]) -> bytes:
        pkt = build_packet(seq, ts_ns, messages)
        for feed in self.feeds[kind]:
            self.out.send(feed, pkt, ts_ns)   # identical bytes on A and on B
        self.packets[kind] += 1
        return pkt

    def step(self) -> list[tuple]:
        """One book update per instrument. Returns (instrument, incs, trades, pkt)."""
        results = []
        for ins in self.instruments:
            incs, trades = ins.engine.step()
            ts = self.clock()
            messages = []
            if incs:
                messages.append(self.wire.encode(
                    "MDIncrementalRefreshBook",
                    {"transactTime": ts, "matchEventIndicator": ["LastQuoteMsg", "EndOfEvent"]},
                    entries_from_increments(ins.engine, ins.security_id, incs)))
            if trades:
                messages.append(self.wire.encode(
                    "MDIncrementalRefreshTradeSummary",
                    {"transactTime": ts, "matchEventIndicator": ["LastTradeMsg", "EndOfEvent"]},
                    entries_from_trades(ins.engine, ins.security_id, trades)))
                ins.on_trades(trades, ts)
            elif incs:
                ins.transact_ns = ts
            if not messages:
                continue
            self.seq += 1
            results.append((ins, incs, trades, self._send(INCREMENTAL, self.seq, ts, messages)))
        return results

    def snapshot_loop(self) -> int:
        """One full snapshot loop: a SnapshotFullRefresh per instrument."""
        if not self.feeds[SNAPSHOT]:
            return 0
        tot = len(self.instruments)
        now = self.clock()
        trade_date = local_mkt_date(datetime.fromtimestamp(now / 1e9, timezone.utc).date())
        for i, ins in enumerate(self.instruments, start=1):
            msg = self.wire.encode(
                "SnapshotFullRefresh", ins.snapshot_root(self.seq, tot, trade_date),
                ins.snapshot_entries())
            self._send(SNAPSHOT, i, self.clock(), [msg])       # seq restarts each loop
        self.snapshot_loops += 1
        return tot

    def definition_loop(self) -> int:
        """One Instrument Replay loop: a definition per instrument."""
        if not self.feeds[INSTRUMENT]:
            return 0
        tot = len(self.instruments)
        for i, ins in enumerate(self.instruments, start=1):
            root, groups = ins.definition(tot, self.depth)
            msg = self.wire.encode("MDInstrumentDefinitionFuture", root, groups=groups)
            self._send(INSTRUMENT, i, self.clock(), [msg])
        self.definition_loops += 1
        return tot
