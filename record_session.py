"""
Record a simulated session to JSON for visualization.

Runs the engine, encodes each step to real MDP 3.0 SBE bytes, then DECODES the
bytes back (so everything the visualization shows was reconstructed from the
wire, not from engine internals). Emits a compact JSON timeline.

    python record_session.py --steps 400 --out session.json

Defaults to the ES front month; --product picks any futures product in CME's
config.xml. The walk starts at that product's prior close unless you pass
--start, and the band is derived around it unless you pass --low/--high.

`build_session(...)` returns the same structure as a dict, for the Flask app.
"""
from __future__ import annotations

import argparse
import json
import os

from contracts import DEFAULT_PRODUCT
from engine import MarketEngine
from packet import build_packet, parse_packet
from products import get_product, product_catalog
from sbe import Schema
from settlement import resolve_start_band
from pyver import require_python

require_python()

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema", "mdp3.xml")


def build_session(low=None, high=None, steps=400, depth=10, volatility=0.6,
                  reversion=0.004, jump_prob=0.0, jump_size=8.0,
                  security_id=None, seed=20260820, start=None,
                  band=None, offline=False, refresh=False,
                  product=DEFAULT_PRODUCT) -> dict:
    """Simulate a session, encode+decode via real MDP 3.0 SBE, return a timeline dict.

    `product` is a CME product code from config.xml (default ES) and decides the
    channel, multicast feed, tick size and tick value. With no `start` the walk
    opens at that product's prior close; with no `low`/`high` the band is derived
    around that opening price.
    """
    schema = Schema(SCHEMA_PATH)
    prod = get_product(product)
    spec, contract, feed = prod.spec, prod.front_month(), prod.incremental
    security_id = spec.security_id if security_id is None else security_id
    origin = resolve_start_band(start, low, high, band, product=prod.code,
                                offline=offline, refresh=refresh)
    low, high, start_px = origin["low"], origin["high"], origin["start"]
    eng = MarketEngine(low, high, depth=depth, volatility=volatility,
                       reversion=reversion, jump_prob=jump_prob,
                       jump_size=jump_size, seed=seed, start=start_px,
                       tick=spec.tick, tick_value=spec.tick_value)

    frames = []
    seq = 0
    ts = 1_787_000_000_000_000_000  # fixed base ns for reproducibility
    sample_hex = None
    sample_decoded = []
    total_bytes = 0

    for i in range(steps):
        ts += 100_000_000  # 100ms between packets
        incs, trades = eng.step()

        messages = []
        book_entries = [{
            "mdEntryPx": inc.price, "mdEntrySize": inc.size, "securityId": security_id,
            "rptSeq": eng.next_rpt_seq(), "numberOfOrders": inc.orders,
            "mdPriceLevel": inc.level, "mdUpdateAction": inc.action, "mdEntryType": inc.side,
        } for inc in incs]
        if book_entries:
            messages.append(schema.encode_message(
                "MDIncrementalRefreshBook",
                {"transactTime": ts, "matchEventIndicator": ["LastQuoteMsg", "EndOfEvent"]},
                book_entries))
        trade_entries = [{
            "mdEntryPx": t.price, "mdEntrySize": t.size, "securityId": security_id,
            "rptSeq": eng.next_rpt_seq(), "numberOfOrders": 0,
            "aggressorSide": t.aggressor, "mdUpdateAction": "New", "mdEntryType": "Trade",
        } for t in trades]
        if trade_entries:
            messages.append(schema.encode_message(
                "MDIncrementalRefreshTradeSummary",
                {"transactTime": ts, "matchEventIndicator": ["LastTradeMsg", "EndOfEvent"]},
                trade_entries))
        if not messages:
            continue

        seq += 1
        pkt = build_packet(seq, ts, messages)
        total_bytes += len(pkt)

        # DECODE from the wire — everything below comes from the bytes, not the engine
        r_seq, r_ts, r_frames = parse_packet(pkt)
        decoded = [schema.decode_message(f)[0] for f in r_frames]

        if i == 5 or sample_hex is None:  # capture a sample packet for the wire view
            sample_hex = pkt.hex()
            sample_decoded = decoded

        bids = sorted(eng.bids.values(), key=lambda l: -l.price)[:depth]
        offers = sorted(eng.offers.values(), key=lambda l: l.price)[:depth]
        frames.append({
            "seq": r_seq, "ts": r_ts, "mid": eng.mid,
            "bestBid": eng.best_bid(), "bestOffer": eng.best_offer(),
            "bytes": len(pkt),
            "bids": [[l.price, l.size, l.orders] for l in bids],
            "offers": [[l.price, l.size, l.orders] for l in offers],
            "trades": [[t.price, t.size, t.aggressor] for t in trades],
            "nMsgs": len(r_frames),
        })

    return {
        "meta": {
            "channel": prod.channel_id, "channelLabel": prod.channel_label,
            "product": prod.code, "productLabel": prod.label,
            "group": prod.group, "exchange": prod.exchange,
            "symbol": contract.symbol, "contract": contract.label,
            "expiry": contract.expiry.isoformat(),
            "securityId": security_id,
            "low": low, "high": high, "center": eng.center,
            "start": start_px, "startSource": origin["source"],
            "closeDate": origin["closeDate"], "closeSymbol": origin["symbol"],
            "startStale": origin["stale"], "band": round(high - low, 10),
            "tick": spec.tick, "tickValue": spec.tick_value,
            "decimals": spec.decimals, "depth": depth,
            "schemaId": schema.schema_id, "schemaVersion": schema.version,
            "steps": len(frames), "totalBytes": total_bytes,
            "multicast": f"{feed.ip}:{feed.port}",
            "products": product_catalog(),
        },
        "sampleHex": sample_hex,
        "sampleDecoded": sample_decoded,
        "frames": frames,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--product", default=DEFAULT_PRODUCT,
                    help="CME product code from config.xml (default ES)")
    ap.add_argument("--low", type=float, default=None,
                    help="lower price bound (default: derived from --start)")
    ap.add_argument("--high", type=float, default=None,
                    help="upper price bound (default: derived from --start)")
    ap.add_argument("--start", type=float, default=None,
                    help="opening price (default: prior session's ES close)")
    ap.add_argument("--band", type=float, default=None,
                    help="derived band width around --start (default: 100 ticks)")
    ap.add_argument("--offline", action="store_true",
                    help="never fetch the prior close; use the cache only")
    ap.add_argument("--refresh", action="store_true",
                    help="re-fetch the prior close even if cached")
    ap.add_argument("--steps", type=int, default=400)
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--volatility", type=float, default=0.6)
    ap.add_argument("--reversion", type=float, default=0.004)
    ap.add_argument("--jump-prob", type=float, default=0.0)
    ap.add_argument("--jump-size", type=float, default=8.0)
    ap.add_argument("--security-id", type=int, default=None)
    ap.add_argument("--seed", type=int, default=20260820)
    ap.add_argument("--out", default="session.json")
    args = ap.parse_args()

    out = build_session(
        low=args.low, high=args.high, steps=args.steps, depth=args.depth,
        volatility=args.volatility, reversion=args.reversion,
        jump_prob=args.jump_prob, jump_size=args.jump_size,
        security_id=args.security_id, seed=args.seed, start=args.start,
        band=args.band, offline=args.offline, refresh=args.refresh,
        product=args.product)
    with open(args.out, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    m = out["meta"]
    origin = f"start {m['start']} ({m['startSource']}"
    origin += f" {m['closeDate']}" if m["closeDate"] else ""
    origin += ", stale)" if m["startStale"] else ")"
    print(f"Wrote {args.out}: {m['product']} {m['symbol']} ch{m['channel']}, "
          f"{m['steps']} frames, {origin}, band [{m['low']}, {m['high']}], "
          f"{m['totalBytes']} bytes on the wire, {os.path.getsize(args.out)} bytes JSON")


if __name__ == "__main__":
    main()
