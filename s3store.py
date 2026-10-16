"""
S3 (VAST) storage for packet captures.

Config comes from the environment, optionally loaded from a `config.env` next
to this file (copy config.env.example; chmod 600). Secrets never go on a
command line.

    AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY   S3 credentials
    S3_ENDPOINT_URL                             VAST S3 endpoint (e.g. https://vip:443)
    S3_BUCKET                                   target bucket
    S3_PREFIX                                   key prefix (default "mdp3-pcap")
    S3_REGION                                   region (default us-east-1)
    S3_VERIFY_SSL                               "false" to skip TLS verify (self-signed)

boto3 is imported only when S3 is actually used, so local-only capture and
replay need nothing beyond the standard library.

    python3 s3store.py ls                       # newest captures
    python3 s3store.py get --out ./pcaps        # download (keeps host/date layout)
    python3 s3store.py verify                   # decode the newest capture end to end
"""
from __future__ import annotations

import argparse
import os
import warnings

from pyver import require_python

require_python()

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENV = os.path.join(HERE, "config.env")


def load_env_file(path: str = DEFAULT_ENV) -> None:
    """Populate os.environ from a KEY=VALUE file (never overrides what is set)."""
    if not path or not os.path.isfile(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def configured() -> bool:
    b = os.environ.get("S3_BUCKET", "")
    return bool(b) and b != "REPLACE_ME"


def bucket() -> str:
    return os.environ["S3_BUCKET"]


def prefix() -> str:
    return os.environ.get("S3_PREFIX", "mdp3-pcap").strip("/")


def client():
    import boto3                               # lazily: local mode needs no boto3
    from botocore.config import Config
    verify = os.environ.get("S3_VERIFY_SSL", "true").lower() != "false"
    if not verify:
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT_URL"),
        region_name=os.environ.get("S3_REGION", "us-east-1"),
        verify=verify,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def parse_url(url: str) -> tuple[str, str]:
    """'s3://bucket/key/prefix' -> (bucket, prefix); 's3:prefix' uses S3_BUCKET."""
    if url.startswith("s3://"):
        b, _, p = url[5:].partition("/")
        return b, p
    if url.startswith("s3:"):
        return bucket(), url[3:].lstrip("/")
    raise ValueError(f"not an s3 url: {url}")


def list_keys(s3, bkt: str, pfx: str) -> list[dict]:
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bkt, Prefix=pfx):
        out += page.get("Contents", [])
    return sorted(out, key=lambda o: o["Key"])


def get(s3, bkt: str, key: str) -> bytes:
    return s3.get_object(Bucket=bkt, Key=key)["Body"].read()


def put(s3, bkt: str, key: str, body: bytes) -> None:
    s3.put_object(Bucket=bkt, Key=key, Body=body, ContentType="application/vnd.tcpdump.pcap")


def main():
    ap = argparse.ArgumentParser(description="List, fetch and verify captures in S3")
    ap.add_argument("--env-file", default=DEFAULT_ENV)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_ls = sub.add_parser("ls", help="list captures")
    p_ls.add_argument("prefix", nargs="?", help="key prefix (default S3_PREFIX)")
    p_ls.add_argument("-n", type=int, default=20, help="show the newest N (0 = all)")
    p_get = sub.add_parser("get", help="download captures")
    p_get.add_argument("prefix", nargs="?")
    p_get.add_argument("--out", default="./pcaps")
    p_ver = sub.add_parser("verify", help="decode the newest capture through the feed handler")
    p_ver.add_argument("prefix", nargs="?")
    args = ap.parse_args()

    load_env_file(args.env_file)
    if not configured():
        raise SystemExit(f"S3 is not configured: set S3_BUCKET etc. in {args.env_file}")
    s3, bkt = client(), bucket()
    pfx = (args.prefix or prefix()).strip("/") + "/"
    objs = list_keys(s3, bkt, pfx)

    if args.cmd == "ls":
        shown = objs if not args.n else objs[-args.n:]
        for o in shown:
            print(f"{o['LastModified']:%Y-%m-%d %H:%M:%S}  {o['Size']:>10,}  {o['Key']}")
        total = sum(o["Size"] for o in objs)
        print(f"{len(objs)} object(s), {total:,} B under s3://{bkt}/{pfx}")
    elif args.cmd == "get":
        for o in objs:
            dest = os.path.join(args.out, o["Key"][len(pfx):])
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            s3.download_file(bkt, o["Key"], dest)
        print(f"downloaded {len(objs)} capture(s) to {args.out}")
    elif args.cmd == "verify":
        if not objs:
            raise SystemExit(f"nothing under s3://{bkt}/{pfx}")
        from replay import summarize
        newest = objs[-1]["Key"]
        body = get(s3, bkt, newest)
        print(f"s3://{bkt}/{newest}  {len(body):,} B")
        summarize([body])


if __name__ == "__main__":
    main()
