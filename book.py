"""
Market-by-price book for one instrument, rebuilt from decoded MDP 3.0 entries.

`apply()` takes one incremental entry (template 46 or 48), `load_snapshot()`
replaces the whole book from a SnapshotFullRefresh (52). Levels are keyed by
price, the authoritative field; `mdPriceLevel` is informational. Prices arrive
already rounded to their exponent by the codec, so equal prices compare equal.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass
class Level:
    size: int
    orders: int


@dataclass
class Trade:
    price: float
    size: int
    aggressor: str          # 'Buy' | 'Sell' | 'NoAggressor'
    transact_time: int      # ns


class Book:
    def __init__(self, tape_len: int = 64):
        self.bids: dict[float, Level] = {}
        self.offers: dict[float, Level] = {}
        self.trades: deque[Trade] = deque(maxlen=tape_len)
        self.trade_count = 0          # trades ever applied (the deque only keeps the tail)
        self.last_trade: Trade | None = None
        self.volume: int | None = None
        self.settlement: float | None = None
        self.open: float | None = None
        self.high: float | None = None
        self.low: float | None = None
        self.transact_time: int | None = None

    # ---- incremental ----
    def apply(self, e: dict, transact_time: int | None = None) -> None:
        if transact_time is not None:
            self.transact_time = transact_time
        etype = e.get("mdEntryType")
        if etype in ("Bid", "Offer"):
            self._apply_level(e, etype)
        elif etype == "Trade":
            self._apply_trade(e)

    def _apply_level(self, e: dict, etype: str) -> None:
        side = self.bids if etype == "Bid" else self.offers
        action, px = e.get("mdUpdateAction"), e.get("mdEntryPx")
        if action == "DeleteThru":
            side.clear()
            return
        if px is None:
            return
        if action in ("New", "Change", "Overlay"):
            side[px] = Level(int(e.get("mdEntrySize") or 0), int(e.get("numberOfOrders") or 0))
        elif action == "Delete":
            side.pop(px, None)
        elif action == "DeleteFrom":
            # this level and everything worse (further from the touch)
            for p in [p for p in side if (p <= px if etype == "Bid" else p >= px)]:
                side.pop(p, None)

    def _apply_trade(self, e: dict) -> None:
        if e.get("mdEntryPx") is None:
            return
        t = Trade(e["mdEntryPx"], int(e.get("mdEntrySize") or 0),
                  e.get("aggressorSide") or "NoAggressor", self.transact_time or 0)
        self.trades.append(t)
        self.trade_count += 1
        self.last_trade = t
        self.volume = (self.volume or 0) + t.size
        self.high = t.price if self.high is None else max(self.high, t.price)
        self.low = t.price if self.low is None else min(self.low, t.price)
        if self.open is None:
            self.open = t.price

    # ---- snapshot ----
    def load_snapshot(self, root: dict, entries: list[dict]) -> None:
        self.bids.clear()
        self.offers.clear()
        self.transact_time = root.get("transactTime") or self.transact_time
        for e in entries:
            et, px = e.get("mdEntryType"), e.get("mdEntryPx")
            if et in ("Bid", "Offer") and px is not None:
                side = self.bids if et == "Bid" else self.offers
                side[px] = Level(int(e.get("mdEntrySize") or 0), int(e.get("numberOfOrders") or 0))
            elif et == "Trade" and px is not None:
                self.last_trade = Trade(px, int(e.get("mdEntrySize") or 0), "NoAggressor",
                                        self.transact_time or 0)
            elif et == "ElectronicVolume":
                self.volume = e.get("mdEntrySize")
            elif et == "SettlementPrice":
                self.settlement = px
            elif et == "OpenPrice":
                self.open = px
            elif et == "TradingSessionHighPrice":
                self.high = px
            elif et == "TradingSessionLowPrice":
                self.low = px

    def levels_equal(self, entries: list[dict]) -> bool:
        """Does this book match a snapshot's levels exactly?"""
        bids = {e["mdEntryPx"]: (e["mdEntrySize"], e["numberOfOrders"])
                for e in entries if e.get("mdEntryType") == "Bid"}
        offers = {e["mdEntryPx"]: (e["mdEntrySize"], e["numberOfOrders"])
                  for e in entries if e.get("mdEntryType") == "Offer"}
        mine_b = {p: (lv.size, lv.orders) for p, lv in self.bids.items()}
        mine_o = {p: (lv.size, lv.orders) for p, lv in self.offers.items()}
        return bids == mine_b and offers == mine_o

    # ---- derived views ----
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_offer(self) -> float | None:
        return min(self.offers) if self.offers else None

    def mid(self) -> float | None:
        bb, bo = self.best_bid(), self.best_offer()
        return None if bb is None or bo is None else (bb + bo) / 2

    def spread(self) -> float | None:
        bb, bo = self.best_bid(), self.best_offer()
        return None if bb is None or bo is None else bo - bb

    def ladder_rows(self, tick: float, max_levels: int | None = None, decimals: int = 2):
        """Rows from highest price at top to lowest at bottom (classic DOM).

        Returns (price, bid_level_or_None, offer_level_or_None) on a continuous
        tick grid between the extreme live prices. The grid is anchored to the
        best bid/offer (on-grid prices), never the mid, which can sit on a half
        tick.
        """
        if not self.bids and not self.offers:
            return []
        prices = list(self.bids) + list(self.offers)
        top, bot = max(prices), min(prices)
        if max_levels is not None:
            bb, bo = self.best_bid(), self.best_offer()
            hi_anchor = bo if bo is not None else top
            lo_anchor = bb if bb is not None else bot
            top = min(top, hi_anchor + (max_levels - 1) * tick)
            bot = max(bot, lo_anchor - (max_levels - 1) * tick)
        nd = decimals + 2
        rows = []
        for k in range(int(round((top - bot) / tick)) + 1):
            p = round(top - k * tick, nd)
            b = self.bids.get(p) or self._near(self.bids, p, tick)
            o = self.offers.get(p) or self._near(self.offers, p, tick)
            rows.append((p, b, o))
        return rows

    @staticmethod
    def _near(side: dict, p: float, tick: float):
        for q, lv in side.items():
            if abs(q - p) < tick / 4:
                return lv
        return None
