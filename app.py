"""
Local Flask host for the ES Feed Simulator dashboard.

On each page load it runs the Python simulator (real MDP 3.0 SBE encode + decode),
injects the resulting session into the dashboard template, and serves it. The
in-page tuner then re-simulates client-side for instant what-if exploration.

Run:
    .venv/bin/python app.py            # http://127.0.0.1:8000
    .venv/bin/python app.py --port 5000

Initial dynamics can be set via query string, e.g.:
    http://127.0.0.1:8000/?volatility=1.2&jump_prob=0.04&jump_size=12&seed=7
"""
from __future__ import annotations

import argparse
import json
import os

from flask import Flask, Response, request

from record_session import build_session

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(HERE, "viz_template.html")

app = Flask(__name__)


def _params_from_query(q):
    def f(name, default, cast):
        try:
            return cast(q.get(name, default))
        except (TypeError, ValueError):
            return default
    return dict(
        low=f("low", 5000.0, float), high=f("high", 5025.0, float),
        steps=f("steps", 400, int), depth=f("depth", 10, int),
        volatility=f("volatility", 0.6, float), reversion=f("reversion", 0.004, float),
        jump_prob=f("jump_prob", 0.0, float), jump_size=f("jump_size", 8.0, float),
        security_id=f("security_id", 42003, int), seed=f("seed", 20260820, int),
    )


@app.route("/")
def index():
    params = _params_from_query(request.args)
    if params["high"] <= params["low"]:
        return Response("high must be greater than low", status=400)
    session = build_session(**params)
    template = open(TEMPLATE_PATH, encoding="utf-8").read()
    html = template.replace("__SESSION_JSON__", json.dumps(session, separators=(",", ":")))
    return Response(html, mimetype="text/html")


@app.route("/api/session")
def api_session():
    """Authoritative Python-generated session (real SBE) as JSON."""
    params = _params_from_query(request.args)
    if params["high"] <= params["low"]:
        return Response('{"error":"high must be greater than low"}',
                        status=400, mimetype="application/json")
    return Response(json.dumps(build_session(**params)), mimetype="application/json")


@app.route("/healthz")
def healthz():
    return {"status": "ok"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; use 0.0.0.0 to expose on the LAN (default: localhost only)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    if args.host == "0.0.0.0":
        print("WARNING: binding to 0.0.0.0 — the dashboard is reachable by anyone on "
              "your network (no auth). Use only on a trusted LAN.")
    shown = "127.0.0.1" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"ES Feed Simulator → http://{shown}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)
