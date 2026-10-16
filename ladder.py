#!/usr/bin/env python3
"""
Live CME MDP 3.0 futures ladder (terminal DOM), for up to four products.

Joins every feed of the chosen products' channels and runs them through the
feed handler (A/B arbitration, snapshot recovery, instrument definitions), then
renders a classic depth-of-market ladder: bid qty | price | ask qty, highest
price at top. Best bid/offer and the last trade are highlighted; quantity bars
scale with resting size. Tick and decimals come from each instrument's
definition on the wire, falling back to contracts.py until one arrives.

    python3 ladder.py                              # ES
    python3 ladder.py --product ES,NQ,CL,ZN        # four products
    python3 ladder.py --product ES,CL --print-after 3   # one text frame, no curses

Keys: 1-4 or Tab pick a product, v toggles single/split view, q quits.
"""
from __future__ import annotations

import argparse
import curses
import time

from feedhandler import KINDS, LIVE, FeedHandler, FeedSet
from products import get_product, parse_product_list
from wire import add_schema_argument, open_wire
from pyver import require_python

require_python()

BAR_CH = "█"

# color pair ids
CP_BID, CP_OFFER, CP_PRICE, CP_BEST_BID, CP_BEST_OFF, CP_LAST, CP_HDR, CP_DIM, \
    CP_OK, CP_WARN, CP_TAB = range(1, 12)


class View:
    """One product as the ladder sees it: its instrument state, once known."""

    def __init__(self, product, handler: FeedHandler):
        self.product = product
        self.handler = handler

    @property
    def channel(self):
        return self.handler.channels[self.product.channel_id]

    @property
    def ins(self):
        ch = self.channel
        for ins in ch.instruments.values():
            if ins.definition and ins.definition.asset == self.product.code:
                return ins
        return ch.instruments.get(self.product.spec.security_id)

    @property
    def tick(self) -> float:
        ins = self.ins
        return ins.definition.tick if ins and ins.definition else self.product.spec.tick

    @property
    def decimals(self) -> int:
        ins = self.ins
        return ins.definition.decimals if ins and ins.definition else self.product.spec.decimals

    @property
    def symbol(self) -> str:
        ins = self.ins
        return ins.definition.symbol if ins and ins.definition else self.product.front_month().symbol

    def px(self, v) -> str:
        return "--" if v is None else f"{v:.{self.decimals}f}"


def ladder_lines(view: View, width: int, rows_avail: int):
    """[(text, kind)] for one ladder column. kind picks the color per segment."""
    ins = view.ins
    if ins is None or (not ins.book.bids and not ins.book.offers):
        return [([(" waiting for feed ...", "dim")])]
    book = ins.book
    bb, bo, last = book.best_bid(), book.best_offer(), book.last_trade
    rows = book.ladder_rows(view.tick, max_levels=max(1, rows_avail // 2),
                            decimals=view.decimals)[:rows_avail]
    max_sz = max([lv.size for _, b, o in rows for lv in (b, o) if lv] or [1])
    px_w = max(9, len(view.px(rows[0][0])) + 2) if rows else 9
    side_w = max(4, (width - px_w - 2) // 2)
    out = []
    for p, b, o in rows:
        left = " " * side_w
        if b:
            qty = str(b.size)
            n = max(0, min(int(round(b.size / max_sz * (side_w - len(qty) - 1))),
                           side_w - len(qty) - 1))
            left = " " * (side_w - n - len(qty) - 1) + BAR_CH * n + " " + qty
        right = " " * side_w
        if o:
            qty = str(o.size)
            n = max(0, min(int(round(o.size / max_sz * (side_w - len(qty) - 1))),
                           side_w - len(qty) - 1))
            right = (qty + " " + BAR_CH * n).ljust(side_w)
        tol = view.tick / 4
        kind = "price"
        if last is not None and abs(p - last.price) < tol:
            kind = "last"
        elif bb is not None and abs(p - bb) < tol:
            kind = "bestbid"
        elif bo is not None and abs(p - bo) < tol:
            kind = "bestoff"
        out.append([(left[:side_w], "bid"), (" ", ""),
                    (f"{view.px(p):^{px_w}}", kind), (" ", ""), (right[:side_w], "offer")])
    return out


def header_lines(view: View, views: list[View], sel: int):
    ins, ch = view.ins, view.channel
    book = ins.book if ins else None
    tabs = []
    for i, v in enumerate(views):
        vi = v.ins
        st = vi.state if vi else "waiting"
        bb = v.px(vi.book.best_bid()) if vi else "--"
        bo = v.px(vi.book.best_offer()) if vi else "--"
        tabs.append((f" {i + 1} {v.symbol} {bb}/{bo} ", "tabsel" if i == sel else
                     ("ok" if st == LIVE else "warn")))
    p = view.product
    state = ins.state if ins else "waiting"
    sid = ins.security_id if ins else "-"
    learned = "definition ✓" if ins and ins.definition else "no definition yet"
    l1 = (f" {view.symbol}  {p.label}  ch {p.channel_id}  sec_id {sid}  tick "
          f"{view.tick:.10g}  {learned}   state {state.upper()}"
          f"{'' if not ins else f'  rptSeq {ins.rpt_seq}  gaps {ins.gaps}  recoveries {ins.recoveries}'}")
    if book:
        last = book.last_trade
        l2 = (f" bid {view.px(book.best_bid()):>10}  offer {view.px(book.best_offer()):>10}"
              f"  spread {view.px(book.spread())}  last "
              f"{view.px(last.price) + ' ' + last.aggressor if last else '--'}"
              f"  vol {book.volume or 0}  settle {view.px(book.settlement)}")
    else:
        l2 = " bid --  offer --"
    arb = ch.arb
    only = ch.one_feed_only()
    l3 = (f" feeds  won A {arb.won.get('A', 0)} / B {arb.won.get('B', 0)}  dup {arb.duplicates}"
          f"  covered by B {only.get('B', 0)} / by A {only.get('A', 0)}"
          f"  lost on both {arb.lost}   1-4/Tab product  v split  q quit")
    return tabs, [(l1, "hdr"), (l2, "hdr"), (l3, "dim")], state


def render_text(views, sel, width=100, height=30) -> str:
    """The single view as plain text (for --print-after and tests)."""
    tabs, lines, _ = header_lines(views[sel], views, sel)
    out = ["".join(t for t, _ in tabs)] + [t for t, _ in lines]
    for segs in ladder_lines(views[sel], width, height - len(out)):
        out.append("".join(t for t, _ in segs).rstrip())
    return "\n".join(out)


COLORS = {"bid": CP_BID, "offer": CP_OFFER, "price": CP_PRICE, "bestbid": CP_BEST_BID,
          "bestoff": CP_BEST_OFF, "last": CP_LAST, "hdr": CP_HDR, "dim": CP_DIM,
          "ok": CP_OK, "warn": CP_WARN, "tabsel": CP_TAB, "": 0}


def _attr(kind):
    a = curses.color_pair(COLORS.get(kind, 0)) if kind else 0
    if kind in ("bestbid", "bestoff", "tabsel"):
        a |= curses.A_BOLD
    if kind == "last":
        a |= curses.A_BOLD | curses.A_REVERSE
    return a


def _put(stdscr, y, x, s, attr=0):
    h, w = stdscr.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    try:
        stdscr.addnstr(y, x, s, max(0, w - x - 1), attr)
    except curses.error:
        pass


def draw(stdscr, views, sel, split):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    tabs, lines, _ = header_lines(views[sel], views, sel)
    x = 0
    for text, kind in tabs:
        _put(stdscr, 0, x, text, _attr(kind))
        x += len(text) + 1
    for i, (text, kind) in enumerate(lines, start=1):
        _put(stdscr, i, 0, text.ljust(w - 1), _attr(kind))
    top = len(lines) + 1
    avail = h - top - 1
    if avail < 2:
        stdscr.noutrefresh()
        return
    cols = views if split else [views[sel]]
    col_w = max(20, w // len(cols))
    for c, view in enumerate(cols):
        x0 = c * col_w
        if split:
            _put(stdscr, top, x0, f" {view.symbol} ".center(col_w - 1, "─"),
                 _attr("tabsel" if views.index(view) == sel else "dim"))
        y0 = top + (1 if split else 0)
        for i, segs in enumerate(ladder_lines(view, col_w - 1, avail - (1 if split else 0))):
            x = x0
            for text, kind in segs:
                _put(stdscr, y0 + i, x, text, _attr(kind))
                x += len(text)
    stdscr.noutrefresh()


def run(stdscr, args, views, feeds):
    curses.curs_set(0)
    curses.use_default_colors()
    for cp, fg in ((CP_BID, curses.COLOR_GREEN), (CP_OFFER, curses.COLOR_RED),
                   (CP_PRICE, curses.COLOR_WHITE), (CP_BEST_BID, curses.COLOR_GREEN),
                   (CP_BEST_OFF, curses.COLOR_RED), (CP_LAST, curses.COLOR_YELLOW),
                   (CP_HDR, curses.COLOR_CYAN), (CP_DIM, curses.COLOR_WHITE),
                   (CP_OK, curses.COLOR_GREEN), (CP_WARN, curses.COLOR_YELLOW),
                   (CP_TAB, curses.COLOR_CYAN)):
        curses.init_pair(cp, fg, -1)
    stdscr.nodelay(True)
    sel, split = 0, args.split
    frame_dt = 1.0 / max(1, args.fps)
    while True:
        feeds.poll(frame_dt)
        draw(stdscr, views, sel, split)
        curses.doupdate()
        try:
            ch = stdscr.getch()
        except curses.error:
            ch = -1
        if ch in (ord("q"), ord("Q")):
            break
        if ord("1") <= ch <= ord(str(len(views))):
            sel = ch - ord("1")
        elif ch in (9, curses.KEY_RIGHT):
            sel = (sel + 1) % len(views)
        elif ch == curses.KEY_LEFT:
            sel = (sel - 1) % len(views)
        elif ch in (ord("v"), ord("V")):
            split = not split


def main() -> int:
    ap = argparse.ArgumentParser(description="Live MDP 3.0 futures ladder (terminal)")
    ap.add_argument("--product", "--products", dest="product", action="append",
                    help="product code(s), comma-separated or repeated, up to 4 (default ES)")
    ap.add_argument("--feeds", default="AB", choices=("AB", "A", "B"),
                    help="feed letters to join (default both: arbitration)")
    ap.add_argument("--iface", default="0.0.0.0", help="local interface IP to receive on")
    ap.add_argument("--fps", type=float, default=10.0, help="max redraw rate")
    ap.add_argument("--split", action="store_true", help="start in split view")
    add_schema_argument(ap, receive=True)
    ap.add_argument("--print-after", type=float, default=None, metavar="SECONDS",
                    help="listen this long, print one text frame per product, exit")
    args = ap.parse_args()
    try:
        codes = parse_product_list(args.product)
    except ValueError as e:
        ap.error(str(e))

    products = [get_product(c) for c in codes]
    handler = FeedHandler(open_wire(args.schema))
    handler.add_routes_for(products, KINDS, args.feeds)
    feeds = FeedSet(handler, args.iface)
    views = [View(p, handler) for p in products]
    try:
        if args.print_after is not None:
            end = time.monotonic() + args.print_after
            while time.monotonic() < end:
                feeds.poll(0.05)
            for i in range(len(views)):
                print(render_text(views, i))
                print()
            return 0
        curses.wrapper(run, args, views, feeds)
    except KeyboardInterrupt:
        pass
    finally:
        feeds.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
