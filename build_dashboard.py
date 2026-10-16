"""
Build a standalone dashboard.html: the dashboard plus one recorded session,
with its stylesheet and scripts inlined, so it opens straight from disk
(file://) with no server.

The Flask app (app.py) is the normal way to run the dashboard — it adds the
capture and live modes and re-runs sessions on the server. The static page keeps
the simulated view: playback, the in-page engine (tuning previews exactly what
the simulator would do), the feed timeline, inspector and definitions of the
baked session.

    python build_dashboard.py                               # ES, defaults
    python build_dashboard.py --product ES,NQ,CL,ZN --loss-a 0.05 --loss-b 0.05
    python build_dashboard.py --schema subset --pcap dashboard.pcap
"""
from __future__ import annotations

import argparse
import json
import os

from app import render_index
from record_session import add_session_arguments, params_from_args
from session import simulate, to_pcap
from pyver import require_python

require_python()

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(description="Bake a static, serverless dashboard.html")
    add_session_arguments(ap)
    ap.add_argument("--session-out", default="session.json",
                    help="also write the raw session JSON here (blank to skip)")
    ap.add_argument("--pcap", default=None, help="also write the session's datagrams here")
    ap.add_argument("--out", default="dashboard.html")
    args = ap.parse_args()
    try:
        params = params_from_args(args)
    except ValueError as e:
        ap.error(str(e))
    session, datagrams = simulate(params, want_datagrams=True)
    if args.session_out:
        with open(os.path.join(HERE, args.session_out), "w") as f:
            json.dump(session, f, separators=(",", ":"))
    if args.pcap:
        with open(args.pcap, "wb") as f:
            f.write(to_pcap(datagrams))
    html = render_index(session, inline_assets=True)
    out = os.path.join(HERE, args.out) if not os.path.isabs(args.out) else args.out
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    syms = ", ".join(i["symbol"] for i in session["instruments"])
    print(f"Wrote {args.out} ({len(html):,} bytes): {syms} · {len(session['frames'])} frames · "
          f"{session['schema']['label']}" + (f" · pcap {args.pcap}" if args.pcap else ""))


if __name__ == "__main__":
    main()
