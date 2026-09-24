"""
Product chooser: reads CME's channel configuration and joins it to contract specs.

CME's `config.xml` says which products live on which channel and which multicast
feeds carry them. `contracts.py` says what a tick is worth and when the front
month expires. This module joins the two into a single `Product` the simulator
can be pointed at:

    python3 products.py                  # list everything simulatable
    python3 products.py --group Energy   # just one group
    python3 products.py --show CL        # one product in detail

Config file resolution, in order: an explicit path, then `$CME_CONFIG_XML`, then
`schema/config.xml` (drop CME's real download there), then the bundled extract
`schema/config.sample.xml`.

A product code can appear on several channels — ES is listed on the E-mini
futures channel and on two options channels. Since this simulator publishes
futures, channels whose label says Options are skipped and Futures channels win.
"""
from __future__ import annotations

import argparse
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache

from contracts import (DEFAULT_PRODUCT, GROUP_ORDER, SPECS, ContractSpec,
                       front_month, get_spec)
from pyver import require_python

require_python()

_HERE = os.path.dirname(os.path.abspath(__file__))
FULL_CONFIG = os.path.join(_HERE, "schema", "config.xml")
SAMPLE_CONFIG = os.path.join(_HERE, "schema", "config.sample.xml")


def default_config_path() -> str:
    """CME's real config.xml when it has been downloaded, else the extract."""
    env = os.environ.get("CME_CONFIG_XML")
    if env:
        return env
    return FULL_CONFIG if os.path.exists(FULL_CONFIG) else SAMPLE_CONFIG


CONFIG_PATH = default_config_path()


@dataclass(frozen=True)
class Feed:
    """One multicast connection on a channel."""
    conn_id: str
    type: str                     # Incremental | Snapshot | InstrumentReplay…
    feed_id: str                  # A | B
    ip: str
    port: int
    protocol: str = "UDP/IP"

    def __str__(self) -> str:
        return f"{self.ip}:{self.port} ({self.type} feed {self.feed_id})"


@dataclass(frozen=True)
class Product:
    """A tradable product: where it is on the wire, and what its ticks are worth."""
    code: str
    group_code: str               # CME product group code from config.xml
    exchange: str                 # CME | CBOT | NYMEX | COMEX
    channel_id: int
    channel_label: str
    feeds: tuple

    @property
    def label(self) -> str:
        """Human name. CME's config.xml carries no product names, so use ours."""
        return self.spec.name

    @property
    def group(self) -> str:
        return self.spec.group

    @property
    def spec(self) -> ContractSpec:
        return get_spec(self.code)

    @property
    def incremental(self) -> Feed | None:
        """The incremental feed A — what the simulator publishes on."""
        a = [f for f in self.feeds if f.type == "Incremental" and f.feed_id == "A"]
        return a[0] if a else next((f for f in self.feeds if f.type == "Incremental"), None)

    def front_month(self, today=None):
        return front_month(self.code, today)


# --------------------------------------------------------------- parsing ----
def _field(el, name: str, default: str = "") -> str:
    """Read a field from an attribute or a same-named child element."""
    if el.get(name) is not None:
        return (el.get(name) or "").strip()
    child = el.find(name)
    if child is not None and child.text:
        return child.text.strip()
    return default


def _product_code(el) -> str:
    return (_field(el, "code") or _field(el, "id")).upper()


def _product_group_code(el) -> str:
    """CME nests the group as <group code="ES"/>; tolerate a plain attribute too."""
    g = el.find("group")
    if g is not None and g.get("code"):
        return g.get("code").strip()
    return _field(el, "group")


def _parse_feeds(channel_el) -> tuple:
    """Multicast connections on a channel.

    CME writes the type as <type feed-type="I">Incremental</type> and the feed
    letter as <feed>A</feed>. TCP entries (historical replay) carry a port but no
    multicast <ip>, so they are skipped.
    """
    feeds = []
    for c in channel_el.findall("connections/connection"):
        port, ip = _field(c, "port"), _field(c, "ip")
        if not port.isdigit() or not ip:
            continue
        type_el = c.find("type")
        feeds.append(Feed(
            conn_id=_field(c, "id") or c.get("id", ""),
            type=(type_el.text or "").strip() if type_el is not None else _field(c, "type"),
            feed_id=_field(c, "feed") or _field(c, "feed-id"),
            ip=ip,
            port=int(port),
            protocol=_field(c, "protocol", "UDP/IP"),
        ))
    return tuple(feeds)


def _exchange_of(channel_label: str) -> str:
    """CME's channel labels start with the listing exchange."""
    first = (channel_label or "").split(" ", 1)[0].upper()
    return first if first in ("CME", "CBOT", "NYMEX", "COMEX") else ""


def is_futures_channel(label: str) -> bool:
    """Futures channels only — a code like ES is also listed on options channels."""
    low = (label or "").lower()
    return "futures" in low and "options" not in low


@lru_cache(maxsize=8)
def load_products(config_path: str = None) -> tuple:
    """Every futures product in the config file, deduped. Cached per path."""
    config_path = config_path or default_config_path()
    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"CME channel config not found at {config_path}. Download config.xml "
            f"from https://www.cmegroup.com/ftp/SBEFix/Production/Configuration/ "
            f"(CME login required) to schema/config.xml, or pass another path.")
    root = ET.parse(config_path).getroot()
    out, seen = [], set()
    for ch in root.iter("channel"):
        ch_id, label = _field(ch, "id"), _field(ch, "label")
        if not ch_id.isdigit() or not is_futures_channel(label):
            continue
        feeds = _parse_feeds(ch)
        for p in ch.findall("products/product"):
            code = _product_code(p)
            if not code or code in seen:       # first futures channel wins
                continue
            seen.add(code)
            out.append(Product(code=code, group_code=_product_group_code(p),
                               exchange=_exchange_of(label), channel_id=int(ch_id),
                               channel_label=label, feeds=feeds))
    return tuple(out)


def simulatable(config_path: str = None) -> tuple:
    """Products we can actually simulate — those with contract specs and a feed.

    CME's real config.xml lists far more than this module has economics for;
    anything without a `contracts.py` spec is filtered out of the chooser.
    """
    return tuple(p for p in load_products(config_path)
                 if p.code in SPECS and p.incremental is not None)


def get_product(code: str | None = None, config_path: str = None) -> Product:
    """Look up one product by code. Defaults to ES."""
    code = (code or DEFAULT_PRODUCT).upper()
    for p in load_products(config_path):
        if p.code == code:
            if code not in SPECS:
                raise ValueError(
                    f"{code} is in {os.path.basename(config_path)} but contracts.py "
                    f"has no spec for it (tick size, tick value, expiry cycle). "
                    f"Add one to SPECS to simulate it.")
            return p
    known = ", ".join(p.code for p in simulatable(config_path))
    raise ValueError(f"unknown product {code!r}; available: {known}")


def product_catalog(config_path: str = None) -> list:
    """Compact list for the dashboard's chooser."""
    return [{"code": p.code, "label": p.label, "group": p.group,
             "exchange": p.exchange, "channel": p.channel_id,
             "symbol": p.front_month().symbol}
            for p in sorted(simulatable(config_path),
                            key=lambda x: (GROUP_ORDER.index(x.group), x.code))]


def main():
    ap = argparse.ArgumentParser(description="List CME products available to simulate")
    ap.add_argument("--config", default=None, help="path to CME config.xml")
    ap.add_argument("--group", help="filter by product group (Equity, Energy, …)")
    ap.add_argument("--show", help="show one product in detail")
    args = ap.parse_args()

    if args.show:
        p = get_product(args.show, args.config)
        fm, s = p.front_month(), get_spec(args.show)
        print(f"{p.code}  {p.label}")
        print(f"  group        {p.group} ({p.exchange}, CME group code {p.group_code})")
        print(f"  channel      {p.channel_id} — {p.channel_label}")
        for f in p.feeds:
            print(f"  feed         {f}")
        print(f"  front month  {fm.label}, last trade {fm.expiry.isoformat()}")
        print(f"  tick         {s.tick:.10g}  (${s.tick_value:.4g}/tick, "
              f"multiplier {s.multiplier:.0f})")
        print(f"  securityID   {s.security_id} (synthetic)")
        print(f"  prior close  {s.yahoo}")
        return

    rows = simulatable(args.config)
    if args.group:
        rows = [p for p in rows if p.group.lower() == args.group.lower()]
    rows = sorted(rows, key=lambda x: (GROUP_ORDER.index(x.group), x.code))
    print(f"{'code':<5} {'product':<24} {'group':<8} {'exch':<6} {'ch':<5} "
          f"{'front':<8} {'feed':<22}")
    for p in rows:
        print(f"{p.code:<5} {p.label[:24]:<24} {p.group:<8} {p.exchange:<6} "
              f"{p.channel_id:<5} {p.front_month().symbol:<8} "
              f"{p.incremental.ip + ':' + str(p.incremental.port):<22}")
    src = os.path.basename(args.config or default_config_path())
    print(f"\n{len(rows)} products from {src}  ·  default {DEFAULT_PRODUCT}")


if __name__ == "__main__":
    main()
