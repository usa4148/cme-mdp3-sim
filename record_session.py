"""
Record a dashboard session to JSON.

Runs up to four products through the real publisher, loss model and feed
handler (session.py), so everything recorded was decoded from SBE bytes —
books, trades, recoveries, definitions — then writes the session JSON the
dashboard renders.

    python record_session.py --steps 400 --out session.json
    python record_session.py --product ES,NQ,CL --loss-a 0.05 --loss-b 0.05
    python record_session.py --schema subset --pcap session.pcap

`build_session(...)` returns the same dict, for callers that want it in memory.
"""
from __future__ import annotations

import argparse
import json
import os

from session import SessionParams, simulate, to_pcap
from wire import add_schema_argument
from pyver import require_python

require_python()


def build_session(product="ES", steps=400, depth=10, volatility=0.6, reversion=0.004,
                  jump_prob=0.0, jump_size=8.0, seed=20260820, start=None, low=None,
                  high=None, band=None, offline=False, refresh=False, security_id=None,
                  schema=None, loss_a=0.0, loss_b=0.0, snapshot_every=10,
                  secdef_every=50, feeds="AB") -> dict:
    """A session dict. `product` may be several codes ('ES,NQ'); price settings
    take a single value (one product) or {code: value} / 'ES=…,NQ=…'."""
    p = SessionParams(products=product, steps=steps, depth=depth, volatility=volatility,
                      reversion=reversion, jump_prob=jump_prob, jump_size=jump_size,
                      seed=seed, start=start, low=low, high=high, band=band,
                      loss_a=loss_a, loss_b=loss_b, snapshot_every=snapshot_every,
                      secdef_every=secdef_every, feeds=feeds, offline=offline,
                      refresh=refresh, security_id=security_id)
    if schema:
        p.schema = schema
    return simulate(p.validate())


def add_session_arguments(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--product", "--products", dest="product", default="ES",
                    help="up to 4 product codes, comma-separated (default ES)")
    add_schema_argument(ap)
    ap.add_argument("--start", default=None,
                    help="opening price; CODE=value,… with several products")
    ap.add_argument("--low", default=None, help="band low; CODE=value,… with several")
    ap.add_argument("--high", default=None, help="band high; CODE=value,… with several")
    ap.add_argument("--band", default=None, help="band width around --start")
    ap.add_argument("--offline", action="store_true", help="never fetch prior closes")
    ap.add_argument("--steps", type=int, default=400)
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--volatility", type=float, default=0.6)
    ap.add_argument("--reversion", type=float, default=0.004)
    ap.add_argument("--jump-prob", type=float, default=0.0)
    ap.add_argument("--jump-size", type=float, default=8.0)
    ap.add_argument("--loss-a", type=float, default=0.0)
    ap.add_argument("--loss-b", type=float, default=0.0)
    ap.add_argument("--snapshot-every", type=int, default=10,
                    help="steps between snapshot loops (0 = off)")
    ap.add_argument("--secdef-every", type=int, default=50,
                    help="steps between definition loops (0 = off)")
    ap.add_argument("--feeds", default="AB", choices=("AB", "A", "B"))
    ap.add_argument("--seed", type=int, default=20260820)


def params_from_args(args) -> SessionParams:
    return SessionParams(
        products=args.product, schema=args.schema, steps=args.steps, depth=args.depth,
        volatility=args.volatility, reversion=args.reversion, jump_prob=args.jump_prob,
        jump_size=args.jump_size, seed=args.seed, start=args.start or {},
        low=args.low or {}, high=args.high or {}, band=args.band or {},
        loss_a=args.loss_a, loss_b=args.loss_b, snapshot_every=args.snapshot_every,
        secdef_every=args.secdef_every, feeds=args.feeds, offline=args.offline).validate()


def main():
    ap = argparse.ArgumentParser(description="Record a dashboard session to JSON")
    add_session_arguments(ap)
    ap.add_argument("--out", default="session.json")
    ap.add_argument("--pcap", default=None, help="also write the session's datagrams here")
    args = ap.parse_args()
    try:
        params = params_from_args(args)
    except ValueError as e:
        ap.error(str(e))
    session, datagrams = simulate(params, want_datagrams=True)
    with open(args.out, "w") as f:
        json.dump(session, f, separators=(",", ":"))
    if args.pcap:
        with open(args.pcap, "wb") as f:
            f.write(to_pcap(datagrams))
    syms = ", ".join(i["symbol"] for i in session["instruments"])
    print(f"Wrote {args.out}: {syms} · {len(session['frames'])} frames · "
          f"{session['totals']['datagrams']} datagrams ({session['totals']['dropped']} dropped) · "
          f"{session['schema']['label']} · {os.path.getsize(args.out):,} bytes JSON"
          + (f" · pcap {args.pcap}" if args.pcap else ""))


if __name__ == "__main__":
    main()
