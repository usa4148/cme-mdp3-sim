"""
Local Flask host for the MDP 3.0 dashboard.

Three ways to look at a feed, one page:

  Simulated  up to four products through the real publisher, loss model and
             feed handler (session.py); the in-page engine previews tuning
             instantly and the server re-runs it authoritatively.
  Capture    a .pcap / .pcapng — uploaded, or picked from the capture directory
             (./pcaps) or the S3 bucket in config.env — decoded the same way.
  Live       the real multicast feed (the simulator, a replay, a real line),
             through the feed handler, streamed with server-sent events.

Every view works on CME's official templates_FixBinary.xml or the working
subset (?schema=cme|subset; captures and live default to auto-detect).

Run:
    .venv/bin/python app.py                  # http://127.0.0.1:8000
    .venv/bin/python app.py --port 5000 --captures /data/pcaps

Query strings set the initial state, e.g.
    /?products=ES,NQ,CL,ZN&loss_a=0.05&loss_b=0.05
    /?products=CL&schema=subset&volatility=1.2&jump_prob=0.04
    /?mode=live&products=ES,NQ
    /?start=ES=7772.50&band=ES=40

API (JSON unless noted):
    GET  /api/session          session for the query's parameters
    GET  /api/session.pcap     the same session's datagrams (application/vnd.tcpdump.pcap)
    GET  /api/captures         captures available locally and in S3
    GET  /api/capture?src=…    a capture as a session        POST /api/capture  (upload)
    GET  /api/live?products=…  text/event-stream of live snapshots
    GET  /api/products  /api/schemas  /api/prior-close?product=  /healthz
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone

from flask import Flask, Response, abort, request, send_from_directory, stream_with_context

import s3store
from contracts import DEFAULT_PRODUCT, get_spec
from live import LiveRegistry
from pcap import PcapError, capture_files
from products import product_catalog
from session import (ParamError, SessionParams, datagrams_from_pcap, from_capture,
                     simulate, to_pcap)
from settlement import prior_close
from wire import LABELS, available, default_name
from pyver import require_python, runtime_banner

require_python()

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
MAX_UPLOAD = 128 << 20                        # 128 MB
SESSION_PLACEHOLDER = "__SESSION_JSON__"


def json_for_html(obj) -> str:
    """JSON safe to embed in <script type="application/json">: no '</script>' escape."""
    return (json.dumps(obj, separators=(",", ":"))
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def render_index(session: dict | None, inline_assets: bool = False) -> str:
    """index.html with the initial session embedded (and, for a static build,
    the stylesheet and scripts inlined so the file works from file://)."""
    html = open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    html = html.replace(SESSION_PLACEHOLDER, json_for_html(session) if session else "null")
    if inline_assets:
        for name in ("app.css",):
            css = open(os.path.join(WEB, name), encoding="utf-8").read()
            html = html.replace(f'<link rel="stylesheet" href="static/{name}">',
                                f"<style>\n{css}\n</style>")
        for name in ("engine.js", "app.js"):
            js = open(os.path.join(WEB, name), encoding="utf-8").read()
            html = html.replace(f'<script src="static/{name}"></script>',
                                "<script>\n" + js.replace("</script", "<\\/script") + "\n</script>")
    return html


def _error(status: int, message: str):
    return Response(json.dumps({"error": message}), status=status,
                    mimetype="application/json")


def create_app(capture_dir: str | None = None, live: LiveRegistry | None = None) -> Flask:
    app = Flask(__name__, static_folder=WEB, static_url_path="/static")
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD
    app.config["CAPTURE_DIR"] = os.path.abspath(capture_dir or os.path.join(HERE, "pcaps"))
    registry = live or LiveRegistry()
    app.extensions["live"] = registry
    s3store.load_env_file()

    def params():
        try:
            return SessionParams.from_query(request.args)
        except ParamError as e:
            abort(_error(400, str(e)))

    # ---- pages ----
    @app.get("/")
    def index():
        mode = request.args.get("mode", "simulated")
        session = None
        if mode == "simulated":
            try:
                session = simulate(SessionParams.from_query(request.args))
            except ParamError as e:
                return Response(f"bad parameters: {e}", status=400, mimetype="text/plain")
        resp = Response(render_index(session), mimetype="text/html")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        if resp.mimetype == "text/html":
            resp.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; script-src 'self'; "
                "connect-src 'self'; frame-ancestors 'none'")
        return resp

    # ---- simulated sessions ----
    @app.get("/api/session")
    def api_session():
        return Response(json.dumps(simulate(params()), separators=(",", ":")),
                        mimetype="application/json")

    @app.get("/api/session.pcap")
    def api_session_pcap():
        p = params()
        _session, datagrams = simulate(p, want_datagrams=True)
        name = f"mdp3-{'-'.join(p.products)}-{p.schema}-seed{p.seed}.pcap"
        return Response(to_pcap(datagrams), mimetype="application/vnd.tcpdump.pcap",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # ---- captures ----
    def local_captures() -> list[dict]:
        root = app.config["CAPTURE_DIR"]
        if not os.path.isdir(root):
            return []
        out = []
        for path in capture_files(root):
            st = os.stat(path)
            rel = os.path.relpath(path, root)
            out.append({"src": f"local:{rel}", "name": rel, "size": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                        "where": "local"})
        return sorted(out, key=lambda c: c["mtime"], reverse=True)[:500]

    def s3_captures(limit: int = 100) -> list[dict]:
        if not s3store.configured():
            return []
        s3 = s3store.client()
        objs = s3store.list_keys(s3, s3store.bucket(), s3store.prefix() + "/")
        return [{"src": f"s3:{o['Key']}", "name": o["Key"], "size": o.get("Size", 0),
                 "mtime": str(o.get("LastModified", "")), "where": "s3"}
                for o in reversed(objs[-limit:])]

    @app.get("/api/captures")
    def api_captures():
        out = {"dir": app.config["CAPTURE_DIR"], "local": local_captures(),
               "s3": [], "s3Configured": s3store.configured()}
        try:
            out["s3"] = s3_captures()
        except Exception as e:                     # S3 down must not break local
            out["s3Error"] = str(e)
        return out

    def load_capture(src: str) -> tuple[bytes, str]:
        if src.startswith("local:"):
            root = app.config["CAPTURE_DIR"]
            path = os.path.realpath(os.path.join(root, src[6:]))
            if os.path.commonpath([path, os.path.realpath(root)]) != os.path.realpath(root) \
                    or not os.path.isfile(path):
                abort(_error(404, "no such capture"))
            return open(path, "rb").read(), os.path.basename(path)
        if src.startswith("s3:"):
            if not s3store.configured():
                abort(_error(404, "S3 is not configured"))
            key = src[3:]
            if not key.startswith(s3store.prefix() + "/") or ".." in key:
                abort(_error(404, "no such capture"))
            return s3store.get(s3store.client(), s3store.bucket(), key), key.rsplit("/", 1)[-1]
        abort(_error(400, "src must be local:<path> or s3:<key>"))

    def capture_session(blob: bytes, name: str):
        schema = request.args.get("schema") or "auto"
        if schema not in ("auto", *available()):
            return _error(400, f"schema must be auto or one of {', '.join(available())}")
        picks = request.args.get("instruments")
        try:
            keys = [int(x) for x in picks.split(",")][:4] if picks else None
        except ValueError:
            return _error(400, "instruments must be security ids")
        try:
            datagrams, stats = datagrams_from_pcap(blob)
        except PcapError as e:
            return _error(400, f"not a capture: {e}")
        if not datagrams:
            return _error(400, "the capture holds no IPv4 UDP datagrams")
        s = from_capture(datagrams, name, schema, keys)
        s["source"]["read"] = {"frames": stats.frames, "udp": stats.udp,
                               "skipped": stats.skipped, "fragments": stats.fragments}
        return Response(json.dumps(s, separators=(",", ":")), mimetype="application/json")

    @app.get("/api/capture")
    def api_capture_get():
        blob, name = load_capture(request.args.get("src", ""))
        return capture_session(blob, name)

    @app.post("/api/capture")
    def api_capture_post():
        f = request.files.get("file")
        if f is None:
            return _error(400, "upload a .pcap or .pcapng as form field 'file'")
        return capture_session(f.read(), os.path.basename(f.filename or "upload.pcap"))

    # ---- live ----
    @app.get("/api/live")
    def api_live():
        q = request.args
        try:
            key = registry.key(q.get("products") or DEFAULT_PRODUCT, q.get("schema") or "auto",
                               q.get("iface") or "127.0.0.1", (q.get("feeds") or "AB").upper())
        except ValueError as e:
            return _error(400, str(e))
        try:
            mon = registry.acquire(key)
        except OSError as e:
            return _error(503, f"cannot join the feeds: {e}")

        @stream_with_context
        def stream():
            version, seen = 0, 0
            try:
                yield "retry: 2000\n\n"
                while True:
                    version = mon.wait(version, timeout=15)
                    if mon.error:
                        yield f"event: error\ndata: {json.dumps({'error': mon.error})}\n\n"
                        return
                    snap = mon.latest
                    if snap is None:
                        yield ": waiting\n\n"
                        continue
                    events = mon.events_since(seen)
                    if events:
                        seen = events[-1]["n"]
                    yield f"data: {json.dumps(dict(snap, events=events), separators=(',', ':'))}\n\n"
            finally:
                registry.release(key)

        return Response(stream(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- catalog ----
    @app.get("/api/products")
    def api_products():
        return {"default": DEFAULT_PRODUCT, "products": product_catalog()}

    @app.get("/api/schemas")
    def api_schemas():
        return {"default": default_name(),
                "schemas": [{"name": n, "label": LABELS.get(n, n)} for n in available()]}

    @app.get("/api/prior-close")
    def api_prior_close():
        refresh = request.args.get("refresh") in ("1", "true", "yes")
        try:
            spec = get_spec(request.args.get("product") or DEFAULT_PRODUCT)
        except ValueError as e:
            return _error(400, str(e))
        pc = prior_close(spec.yahoo, refresh=refresh)
        if pc is None:
            return _error(503, f"no prior close for {spec.yahoo}")
        return {"price": pc.price, "date": pc.date, "symbol": pc.symbol,
                "source": pc.source, "stale": pc.stale}

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "time": time.time()}

    @app.errorhandler(413)
    def too_large(_e):
        return _error(413, f"upload is larger than {MAX_UPLOAD >> 20} MB")

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(WEB, "favicon.svg", mimetype="image/svg+xml")

    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="MDP 3.0 dashboard host")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; 0.0.0.0 exposes it on the LAN (default: localhost only)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--captures", default=None,
                    help="directory of .pcap/.pcapng files to offer (default ./pcaps)")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    if args.host == "0.0.0.0":
        print("WARNING: binding to 0.0.0.0 — the dashboard is reachable by anyone on "
              "your network (no auth). Use only on a trusted LAN.")
    shown = "127.0.0.1" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print(f"MDP 3.0 dashboard → http://{shown}:{args.port}")
    print(f"  {runtime_banner()}  ·  schema default {default_name()}")
    # Session builds and live streams are long-running: serve requests on threads.
    create_app(args.captures).run(host=args.host, port=args.port, debug=args.debug,
                                  threaded=True)
