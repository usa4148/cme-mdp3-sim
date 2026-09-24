"""
Local Flask host for the ES Feed Simulator dashboard.

On each page load it runs the Python simulator (real MDP 3.0 SBE encode + decode),
injects the resulting session into the dashboard template, and serves it. The
in-page tuner then re-simulates client-side for instant what-if exploration.

Run:
    .venv/bin/python app.py            # http://127.0.0.1:8000
    .venv/bin/python app.py --port 5000

The dashboard defaults to the ES front month; the product chooser (or ?product=)
switches to any futures product in CME's config.xml. The walk opens at that
product's previous close (looked up once and cached by settlement.py) unless a
start price is supplied.

Initial state can be set via query string, e.g.:
    http://127.0.0.1:8000/?volatility=1.2&jump_prob=0.04&jump_size=12&seed=7
    http://127.0.0.1:8000/?product=CL
    http://127.0.0.1:8000/?start=7772.50&band=40
"""
from __future__ import annotations

import argparse
import json
import os

from flask import Flask, Response, request

from contracts import DEFAULT_PRODUCT, get_spec
from products import product_catalog
from record_session import build_session
from settlement import prior_close
from pyver import require_python, runtime_banner

require_python()

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(HERE, "viz_template.html")

app = Flask(__name__)


def _params_from_query(q):
    def f(name, default, cast):
        try:
            return cast(q.get(name, default))
        except (TypeError, ValueError):
            return default

    def opt(name, cast):
        """None when the param is absent or blank — the value is then derived."""
        raw = q.get(name)
        if raw is None or raw == "":
            return None
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return None

    return dict(
        product=(q.get("product") or DEFAULT_PRODUCT).upper(),
        low=opt("low", float), high=opt("high", float),
        start=opt("start", float), band=opt("band", float),
        offline=q.get("offline") in ("1", "true", "yes"),
        steps=f("steps", 400, int), depth=f("depth", 10, int),
        volatility=f("volatility", 0.6, float), reversion=f("reversion", 0.004, float),
        jump_prob=f("jump_prob", 0.0, float), jump_size=f("jump_size", 8.0, float),
        security_id=opt("security_id", int), seed=f("seed", 20260820, int),
    )


def _bad_band(params):
    """True when both bounds were given and they don't describe a band."""
    return (params["low"] is not None and params["high"] is not None
            and params["high"] <= params["low"])


@app.route("/")
def index():
    params = _params_from_query(request.args)
    if _bad_band(params):
        return Response("high must be greater than low", status=400)
    try:
        session = build_session(**params)
    except ValueError as e:
        return Response(str(e), status=400)
    template = open(TEMPLATE_PATH, encoding="utf-8").read()
    html = template.replace("__SESSION_JSON__", json.dumps(session, separators=(",", ":")))
    return Response(html, mimetype="text/html")


@app.route("/api/session")
def api_session():
    """Authoritative Python-generated session (real SBE) as JSON."""
    params = _params_from_query(request.args)
    if _bad_band(params):
        return Response('{"error":"high must be greater than low"}',
                        status=400, mimetype="application/json")
    try:
        session = build_session(**params)
    except ValueError as e:
        return Response(json.dumps({"error": str(e)}), status=400,
                        mimetype="application/json")
    return Response(json.dumps(session), mimetype="application/json")


@app.route("/api/products")
def api_products():
    """Every futures product the chooser can offer, from CME's config.xml."""
    return {"default": DEFAULT_PRODUCT, "products": product_catalog()}


@app.route("/api/prior-close")
def api_prior_close():
    """The previous session's ES close the simulator starts from."""
    refresh = request.args.get("refresh") in ("1", "true", "yes")
    try:
        spec = get_spec(request.args.get("product") or DEFAULT_PRODUCT)
    except ValueError as e:
        return Response(json.dumps({"error": str(e)}), status=400,
                        mimetype="application/json")
    pc = prior_close(spec.yahoo, refresh=refresh)
    if pc is None:
        return Response(json.dumps({"error": f"no prior close for {spec.yahoo}"}),
                        status=503, mimetype="application/json")
    return {"price": pc.price, "date": pc.date, "symbol": pc.symbol,
            "source": pc.source, "stale": pc.stale}


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
    print(f"MDP 3.0 Feed Simulator → http://{shown}:{args.port}")
    print(f"  {runtime_banner()}")
    # Each page load runs a full simulation, so serve requests on threads:
    # otherwise one slow session build blocks every other request behind it.
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
