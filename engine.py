"""
ES price + order-book engine.

Price path: a mean-reverting (Ornstein-Uhlenbeck-style) random walk in tick
space, bounded to a user-supplied [low, high] price range. The mid-price is
pulled gently toward the center of the range, takes discrete tick steps, and
reflects off the range boundaries so it always stays in-band.

Book: a symmetric N-level Market-by-Price book built around the mid. Each tick
the engine produces incremental MD entries (New / Change / Delete) describing
how the book changed, plus occasional trades that cross the spread.

ES contract facts: tick = 0.25 index points, $12.50 per tick.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

TICK = 0.25
TICK_VALUE = 12.50


def round_to_tick(px: float) -> float:
    return round(px / TICK) * TICK


@dataclass
class BookLevel:
    price: float
    size: int
    orders: int


@dataclass
class Increment:
    """One incremental MD entry to be emitted."""
    action: str          # 'New' | 'Change' | 'Delete'
    side: str            # 'Bid' | 'Offer'
    price: float
    size: int
    orders: int
    level: int           # 1-based price level


@dataclass
class Trade:
    price: float
    size: int
    aggressor: str       # 'Buy' | 'Sell'


class MarketEngine:
    def __init__(self, low: float, high: float, depth: int = 10,
                 volatility: float = 0.6, reversion: float = 0.004,
                 jump_prob: float = 0.0, jump_size: float = 8.0,
                 seed: int | None = None):
        assert high > low, "high must exceed low"
        self.low = round_to_tick(low)
        self.high = round_to_tick(high)
        self.center = round_to_tick((self.low + self.high) / 2)
        self.depth = depth
        self.volatility = volatility           # gaussian shock stddev, in ticks
        self.reversion = reversion             # OU pull toward center per step
        self.jump_prob = jump_prob             # per-step probability of a shock
        self.jump_size = jump_size             # mean jump magnitude, in ticks
        self.rng = random.Random(seed)

        # Track the best bid on the tick grid; best offer sits one tick above it,
        # so the book is never locked or crossed and the mid falls between them.
        self.best_bid_px = round_to_tick(self.center) - TICK
        self.bids: dict[float, BookLevel] = {}
        self.offers: dict[float, BookLevel] = {}
        self.rpt_seq = 0

    @property
    def mid(self) -> float:
        return round(self.best_bid_px + TICK / 2, 4)   # midpoint of a 1-tick market

    # ---- price path ----
    def _step_best_bid(self) -> float:
        # OU drift toward center + gaussian shock, quantized to whole ticks
        drift = -self.reversion * (self.best_bid_px - self.center) / TICK
        shock = self.rng.gauss(0, self.volatility)
        # occasional large gap move (news / liquidity event)
        if self.jump_prob and self.rng.random() < self.jump_prob:
            mag = abs(self.rng.gauss(self.jump_size, self.jump_size * 0.3))
            shock += mag if self.rng.random() < 0.5 else -mag
        step_ticks = round(drift + shock)
        new_bid = self.best_bid_px + step_ticks * TICK
        # reflect off boundaries; keep best offer (bid + 1 tick) inside [low, high]
        lo, hi = self.low, self.high - TICK
        if new_bid < lo:
            new_bid = lo + (lo - new_bid)
        if new_bid > hi:
            new_bid = hi - (new_bid - hi)
        return round_to_tick(min(max(new_bid, lo), hi))

    def _target_levels(self, side: str) -> dict[float, BookLevel]:
        """The book we *want*, best bid/offer one tick apart around the mid."""
        levels = {}
        best = self.best_bid_px if side == "Bid" else self.best_bid_px + TICK
        for i in range(self.depth):
            price = best - i * TICK if side == "Bid" else best + i * TICK
            if price < self.low or price > self.high:
                continue
            # size fattens deeper in the book, with noise
            base = 20 + i * 15
            size = max(1, int(self.rng.gauss(base, base * 0.3)))
            orders = max(1, int(size / self.rng.uniform(3, 9)))
            levels[round_to_tick(price)] = BookLevel(round_to_tick(price), size, orders)
        return levels

    def _diff_side(self, side: str, current: dict, target: dict) -> list[Increment]:
        incs: list[Increment] = []
        # sort prices best-first for level numbering
        ranked = sorted(target.keys(), reverse=(side == "Bid"))
        level_of = {p: i + 1 for i, p in enumerate(ranked)}
        for price, lvl in target.items():
            old = current.get(price)
            if old is None:
                incs.append(Increment("New", side, price, lvl.size, lvl.orders, level_of[price]))
            elif old.size != lvl.size or old.orders != lvl.orders:
                incs.append(Increment("Change", side, price, lvl.size, lvl.orders, level_of[price]))
        for price, old in current.items():
            if price not in target:
                # deleted level; level number best-effort (0 => was pushed out)
                incs.append(Increment("Delete", side, price, old.size, old.orders, 0))
        return incs

    def step(self) -> tuple[list[Increment], list[Trade]]:
        """Advance one tick of simulated time. Returns (increments, trades)."""
        self.best_bid_px = self._step_best_bid()
        tgt_bids = self._target_levels("Bid")
        tgt_offers = self._target_levels("Offer")

        incs = []
        incs += self._diff_side("Bid", self.bids, tgt_bids)
        incs += self._diff_side("Offer", self.offers, tgt_offers)
        self.bids, self.offers = tgt_bids, tgt_offers

        # occasional aggressor trade at the touch
        trades: list[Trade] = []
        if self.rng.random() < 0.35:
            buy = self.rng.random() < 0.5
            if buy and tgt_offers:
                px = min(tgt_offers)
                trades.append(Trade(px, max(1, int(self.rng.expovariate(1 / 4))), "Buy"))
            elif not buy and tgt_bids:
                px = max(tgt_bids)
                trades.append(Trade(px, max(1, int(self.rng.expovariate(1 / 4))), "Sell"))
        return incs, trades

    def next_rpt_seq(self) -> int:
        self.rpt_seq += 1
        return self.rpt_seq

    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    def best_offer(self) -> float | None:
        return min(self.offers) if self.offers else None
