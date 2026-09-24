"""
Build a standalone dashboard.html from viz_template.html + a recorded session.

The Flask app (app.py) serves the dashboard dynamically and is the normal way to
run it. This script produces a *static, self-contained* dashboard.html that can be
opened directly in a browser (file://) with no server — handy for sharing a
snapshot or archiving a specific run.

    python build_dashboard.py                       # default dynamics
    python build_dashboard.py --volatility 1.2 --jump-prob 0.04 --jump-size 12
    python build_dashboard.py --start 7772.50 --band 40
    python build_dashboard.py --product GC
"""
from __future__ import annotations

import argparse
import json
import os

from contracts import DEFAULT_PRODUCT
from record_session import build_session
from pyver import require_python

require_python()

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "viz_template.html")
PLACEHOLDER = "__SESSION_JSON__"


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
    ap.add_argument("--steps", type=int, default=400)
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--volatility", type=float, default=0.6)
    ap.add_argument("--reversion", type=float, default=0.004)
    ap.add_argument("--jump-prob", type=float, default=0.0)
    ap.add_argument("--jump-size", type=float, default=8.0)
    ap.add_argument("--seed", type=int, default=20260820)
    ap.add_argument("--session-out", default="session.json",
                    help="also write the raw session JSON here (blank to skip)")
    ap.add_argument("--out", default="dashboard.html")
    args = ap.parse_args()

    session = build_session(
        low=args.low, high=args.high, steps=args.steps, depth=args.depth,
        volatility=args.volatility, reversion=args.reversion,
        jump_prob=args.jump_prob, jump_size=args.jump_size, seed=args.seed,
        start=args.start, band=args.band, offline=args.offline,
        product=args.product)
    session_json = json.dumps(session, separators=(",", ":"))

    if args.session_out:
        with open(os.path.join(HERE, args.session_out), "w") as f:
            f.write(session_json)

    template = open(TEMPLATE, encoding="utf-8").read()
    if PLACEHOLDER not in template:
        raise SystemExit(f"{PLACEHOLDER} not found in {TEMPLATE}")
    html = template.replace(PLACEHOLDER, session_json)
    with open(os.path.join(HERE, args.out), "w", encoding="utf-8") as f:
        f.write(html)
    m = session["meta"]
    print(f"Wrote {args.out} ({len(html):,} bytes): {m['product']} {m['symbol']}, "
          f"{m['steps']} frames, start {m['start']} ({m['startSource']}), "
          f"band [{m['low']}, {m['high']}].")


if __name__ == "__main__":
    main()
