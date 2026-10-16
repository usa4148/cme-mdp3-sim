"""
Fetch CME's own reference files into schema/ — on request, never automatically.

This repository does not redistribute CME's files. Two of them make the
toolkit production-accurate, and CME publishes both on its anonymous FTP
server, the channel it provides for feed handlers to retrieve them:

  templates_FixBinary.xml   the MDP 3.0 SBE schema (--schema cme)
  config.xml                the full channel configuration (42 channels,
                            ~5,000 products; products.py prefers it)

    python3 cme_files.py                 # both
    python3 cme_files.py --schema        # templates_FixBinary.xml only
    python3 cme_files.py --config        # config.xml only
    ./mdp3 fetch-schema | fetch-config | fetch-cme

Each download is checked before it replaces anything: the schema must parse and
define templates 46, 48, 52 and 54; the config must parse and list futures
channels. Files are written atomically. The cmegroup.com website is not used —
CME's Data Terms of Use prohibit automated access to it.

The files remain CME Group's; they are gitignored here, and your use of them is
subject to CME's terms.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
import urllib.request

from pyver import require_python

require_python()

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_DIR = os.path.join(HERE, "schema")
FTP = "ftp://ftp.cmegroup.com/SBEFix/Production"
FILES = {
    "schema": (f"{FTP}/Templates/templates_FixBinary.xml", "templates_FixBinary.xml"),
    "config": (f"{FTP}/Configuration/config.xml", "config.xml"),
}
TIMEOUT_S = 60
MAX_BYTES = 32 << 20


def _check_schema(path: str) -> str:
    from sbe import Schema
    s = Schema(path)
    missing = [t for t in (46, 48, 52, 54) if t not in s.messages]
    if missing:
        raise ValueError(f"schema lacks templates {missing}")
    return f"schema id {s.schema_id} version {s.version}, {len(s.messages)} templates"


def _check_config(path: str) -> str:
    from products import load_products
    products = load_products(path)
    if not products:
        raise ValueError("no futures channels/products found")
    channels = {p.channel_id for p in products}
    return f"{len(channels)} futures channels, {len(products)} futures products"


CHECKS = {"schema": _check_schema, "config": _check_config}


def fetch(kind: str, dest_dir: str = SCHEMA_DIR) -> str:
    url, name = FILES[kind]
    dest = os.path.join(dest_dir, name)
    with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"{url} is larger than {MAX_BYTES >> 20} MB")
    os.makedirs(dest_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=dest_dir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        summary = CHECKS[kind](tmp)                 # validate before replacing
        old = open(dest, "rb").read() if os.path.exists(dest) else None
        os.replace(tmp, dest)
    except BaseException:
        os.unlink(tmp)
        raise
    state = ("unchanged" if old == data else "updated") if old is not None else "new"
    sha = hashlib.sha256(data).hexdigest()[:12]
    return f"{name}: {len(data):,} B, {state}, sha256 {sha}… — {summary}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch CME's schema and channel config into schema/")
    ap.add_argument("--schema", action="store_true", help="templates_FixBinary.xml only")
    ap.add_argument("--config", action="store_true", help="config.xml only")
    ap.add_argument("--dir", default=SCHEMA_DIR, help="destination (default schema/)")
    args = ap.parse_args()
    kinds = [k for k in ("schema", "config") if getattr(args, k)] or ["schema", "config"]
    failed = False
    for kind in kinds:
        try:
            print(fetch(kind, args.dir))
        except Exception as e:                       # report each, keep going
            failed = True
            print(f"{FILES[kind][1]}: FAILED — {e}")
    print("These files are CME Group's; they stay out of git (see .gitignore).")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
