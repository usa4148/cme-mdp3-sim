"""
Record a simulated session to JSON for visualization.

Runs the engine, encodes each step to real MDP 3.0 SBE bytes, then DECODES the
bytes back (so everything the visualization shows was reconstructed from the
wire, not from engine internals). Emits a compact JSON timeline.

    python record_session.py --low 5000 --high 5025 --steps 400 --out session.json

`build_session(...)` returns the same structure as a dict, for the Flask app.
"""
from __future__ import annotations

import argparse
import json
import os

from engine import MarketEngine, TICK, TICK_VALUE
from packet import build_packet, parse_packet
from sbe import Schema

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema", "mdp3.xml")


def build_session(low=5000.0, high=5025.0, steps=400, depth=10, volatility=0.6,
                  reversion=0.004, jump_prob=0.0, jump_size=8.0,
                  security_id=42003, seed=20260820) -> dict:
    """Simulate a session, encode+decode via real MDP 3.0 SBE, return a timeline dict."""
    schema = Schema(SCHEMA_PATH)
    eng = MarketEngine(low, high, depth=depth, volatility=volatility,
                       reversion=reversion, jump_prob=jump_prob,
                       jump_size=jump_size, seed=seed)

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
            "channel": 310, "product": "ES", "securityId": security_id,
            "low": low, "high": high, "center": eng.center,
            "tick": TICK, "tickValue": TICK_VALUE, "depth": depth,
            "schemaId": schema.schema_id, "schemaVersion": schema.version,
            "steps": len(frames), "totalBytes": total_bytes,
            "multicast": "224.0.31.1:14310",
        },
        "sampleHex": sample_hex,
        "sampleDecoded": sample_decoded,
        "frames": frames,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--low", type=float, default=5000)
    ap.add_argument("--high", type=float, default=5025)
    ap.add_argument("--steps", type=int, default=400)
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--volatility", type=float, default=0.6)
    ap.add_argument("--reversion", type=float, default=0.004)
    ap.add_argument("--jump-prob", type=float, default=0.0)
    ap.add_argument("--jump-size", type=float, default=8.0)
    ap.add_argument("--security-id", type=int, default=42003)
    ap.add_argument("--seed", type=int, default=20260820)
    ap.add_argument("--out", default="session.json")
    args = ap.parse_args()

    out = build_session(
        low=args.low, high=args.high, steps=args.steps, depth=args.depth,
        volatility=args.volatility, reversion=args.reversion,
        jump_prob=args.jump_prob, jump_size=args.jump_size,
        security_id=args.security_id, seed=args.seed)
    with open(args.out, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Wrote {args.out}: {out['meta']['steps']} frames, "
          f"{out['meta']['totalBytes']} bytes on the wire, {os.path.getsize(args.out)} bytes JSON")


if __name__ == "__main__":
    main()
