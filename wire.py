"""
Schema-neutral MDP 3.0 messages: one vocabulary, either schema on the wire.

The toolkit has two SBE schemas:

  subset  schema/mdp3.xml              the hand-written working subset
  cme     schema/templates_FixBinary.xml   CME's official file, unchanged

They agree on template ids and FIX tags, but not on names (`mdEntryPx` vs
`MDEntryPx`, `MDEntries` vs `NoMDEntries`), not on every enum name
(`SessionHighTrade`… vs `TradingSessionHighPrice`), and not on layout — CME's
46/48 price at exponent -9 and carry a second (order id) group the subset does
not. Code that builds or reads messages should not care, so everything above
the codec speaks one *canonical* vocabulary — the subset's names — and a `Wire`
translates:

* fields and groups are matched by **FIX tag** (the schema's field/group id),
  falling back to a case-insensitive name match;
* enum and set values are matched by **wire value**: the canonical name is
  turned into its raw value, which the target schema encodes under whatever name
  it uses, and the reverse on decode;
* fields only the target has (CME's `TradeableSize`, `MDTradeEntryID`, …) are
  left null on encode and passed through, under their own names, on decode.

Both files declare schema id 1 version 13, so a packet cannot say which layout
it is. What gives it away is length: every MDP message is size-prefixed, and
decoding with the wrong schema leaves bytes over or runs off the end. `decode()`
checks that and raises `SchemaMismatch`; `AutoWire` uses the same check to
pick the schema a feed is actually using.

    from wire import open_wire
    w = open_wire("cme")                     # or "subset", "auto", a path
    msg = w.encode("MDIncrementalRefreshBook", root, entries)
    d = w.decode(msg)                        # canonical names either way
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from functools import lru_cache

from sbe import Schema

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_DIR = os.path.join(HERE, "schema")
SCHEMA_FILES = {"cme": "templates_FixBinary.xml", "subset": "mdp3.xml"}
CANONICAL_PATH = os.path.join(SCHEMA_DIR, SCHEMA_FILES["subset"])
LABELS = {"cme": "CME templates_FixBinary.xml", "subset": "working subset (mdp3.xml)"}


class SchemaMismatch(ValueError):
    """The frame does not decode cleanly with this schema."""


def available() -> dict[str, str]:
    """Schema names present on disk -> path, CME's first when it is there."""
    return {n: os.path.join(SCHEMA_DIR, f) for n, f in SCHEMA_FILES.items()
            if os.path.exists(os.path.join(SCHEMA_DIR, f))}


def default_name() -> str:
    """$MDP3_SCHEMA, else CME's file when present, else the subset."""
    env = os.environ.get("MDP3_SCHEMA")
    if env:
        return env
    return "cme" if "cme" in available() else "subset"


def schema_path(name: str) -> str:
    if name in SCHEMA_FILES:
        path = os.path.join(SCHEMA_DIR, SCHEMA_FILES[name])
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"schema {name!r} needs {path}: run ./mdp3 fetch-schema to get CME's "
                f"templates_FixBinary.xml, or use --schema subset.")
        return path
    if os.path.exists(name):
        return name
    raise ValueError(f"unknown schema {name!r}: use {', '.join(SCHEMA_FILES)}, auto, "
                     f"or a path to an SBE XML file")


def _norm(name: str) -> str:
    return name.replace("_", "").lower()


def _group_norm(name: str) -> str:
    n = _norm(name)
    return n[2:] if n.startswith("no") and len(n) > 2 else n


@dataclass
class _FieldMap:
    target: str                    # field name in the target schema
    to_target: object = None       # value converter canonical -> target (or None)
    to_canon: object = None        # value converter target -> canonical (or None)


@dataclass
class _BlockMap:
    fields: dict = field(default_factory=dict)       # canonical name -> _FieldMap
    back: dict = field(default_factory=dict)          # target name -> (canonical, conv)


@dataclass
class _MsgMap:
    target_name: str
    canon_name: str
    root: _BlockMap
    groups: list                    # [(canonical group name | None, target name, _BlockMap)]


class Wire:
    """Encode/decode canonical messages with one concrete schema."""

    def __init__(self, target: Schema, name: str, canonical: Schema | None = None):
        self.schema = target
        self.name = name
        self.canonical = canonical or target
        self.identity = canonical is None or canonical is target
        self._maps: dict[int, _MsgMap] = {}
        self._by_canon: dict[str, int] = {}
        for tid, cm in self.canonical.messages.items():
            self._by_canon[cm.name] = tid
        if not self.identity:
            for tid, tm in target.messages.items():
                cm = self.canonical.messages.get(tid)
                self._maps[tid] = self._build_map(cm, tm)

    # ---- description ----
    @property
    def schema_id(self) -> int:
        return self.schema.schema_id

    @property
    def version(self) -> int:
        return self.schema.version

    def describe(self) -> dict:
        return {"name": self.name, "label": LABELS.get(self.name, self.name),
                "file": os.path.basename(self.schema.path), "id": self.schema_id,
                "version": self.version}

    def template_name(self, tid: int) -> str:
        cm = self.canonical.messages.get(tid)
        return cm.name if cm else self.schema.messages[tid].name

    # ---- mapping construction ----
    def _converters(self, cf, tf):
        """Value converters between a canonical and a target field."""
        cs, ts = self.canonical, self.schema
        if cf.kind == "enum" and tf.kind == "enum":
            ce, te = cs.enums[cf.type_name], ts.enums[tf.type_name]

            def to_t(v, ce=ce):
                return ce.by_name.get(v, v)               # raw value; codec takes raw

            def to_c(v, ce=ce, te=te):
                if v is None:
                    return None
                raw = te.by_name.get(v, v)
                return ce.by_value.get(raw, v)
            return to_t, to_c
        if cf.kind == "set" and tf.kind == "set":
            cst, tst = cs.sets[cf.type_name], ts.sets[tf.type_name]

            def to_t(v, cst=cst):
                if isinstance(v, (list, tuple, set)):
                    return sum(1 << cst.choices[n] for n in v if n in cst.choices)
                return v

            def to_c(v, cst=cst, tst=tst):
                by_bit = {b: n for n, b in cst.choices.items()}
                return [by_bit.get(tst.choices[n], n) for n in v]
            return to_t, to_c
        if cf.kind == "enum" and tf.kind == "primitive":     # e.g. a constant type
            ce = cs.enums[cf.type_name]
            return (lambda v, ce=ce: ce.by_name.get(v, v)), \
                   (lambda v, ce=ce: ce.by_value.get(v, v))
        return None, None

    def _block_map(self, cfields, tfields) -> _BlockMap:
        bm = _BlockMap()
        by_tag = {f.tag: f for f in tfields if f.tag is not None}
        by_name = {_norm(f.name): f for f in tfields}
        used = set()
        for cf in cfields:
            tf = by_tag.get(cf.tag) if cf.tag is not None else None
            tf = tf or by_name.get(_norm(cf.name))
            if tf is None:
                continue
            to_t, to_c = self._converters(cf, tf)
            bm.fields[cf.name] = _FieldMap(tf.name, to_t, to_c)
            bm.back[tf.name] = (cf.name, to_c)
            used.add(tf.name)
        for tf in tfields:                                   # target-only: pass through
            if tf.name not in used:
                bm.back[tf.name] = (tf.name, None)
        return bm

    def _build_map(self, cm, tm) -> _MsgMap:
        if cm is None:
            return _MsgMap(tm.name, tm.name, self._block_map([], tm.fields),
                           [(None, g.name, self._block_map([], g.fields)) for g in tm.groups])
        groups = []
        for tg in tm.groups:
            cg = next((g for g in cm.groups if g.id == tg.id), None) or \
                 next((g for g in cm.groups if _group_norm(g.name) == _group_norm(tg.name)), None)
            groups.append((cg.name if cg else None, tg.name,
                           self._block_map(cg.fields if cg else [], tg.fields)))
        return _MsgMap(tm.name, cm.name, self._block_map(cm.fields, tm.fields), groups)

    # ---- encode ----
    def tid(self, name: str) -> int:
        try:
            return self._by_canon[name]
        except KeyError:
            raise KeyError(f"no message {name!r} in the canonical schema") from None

    def encode(self, name: str, root: dict, entries: list[dict] | None = None,
               groups: dict[str, list[dict]] | None = None) -> bytes:
        if self.identity:
            return self.schema.encode_message(name, root, entries, groups)
        tid = self.tid(name)
        mm = self._maps[tid]
        cmsg = self.canonical.messages[tid]
        if groups is None:
            groups = {cmsg.groups[0].name: entries or []} if cmsg.groups else {}
        t_root = self._to_target(mm.root, root)
        t_groups = {}
        for cname, tname, bm in mm.groups:
            items = groups.get(cname, []) if cname else []
            t_groups[tname] = [self._to_target(bm, e) for e in items]
        return self.schema.encode_message(mm.target_name, t_root, groups=t_groups)

    @staticmethod
    def _to_target(bm: _BlockMap, values: dict) -> dict:
        out = {}
        for k, v in values.items():
            fm = bm.fields.get(k)
            if fm is None:
                continue
            out[fm.target] = fm.to_target(v) if (fm.to_target and v is not None) else v
        return out

    # ---- decode ----
    def decode(self, frame: bytes) -> dict:
        """One size-framed message -> {template, id, root, groups}, canonical names."""
        try:
            d, end = self.schema.decode_message(frame)
        except (struct.error, KeyError, IndexError) as e:
            raise SchemaMismatch(f"{self.name}: cannot decode frame ({e})") from None
        if end != len(frame):
            raise SchemaMismatch(f"{self.name}: template {d['id']} decoded {end} of "
                                 f"{len(frame)} bytes")
        if self.identity:
            return d
        mm = self._maps[d["id"]]
        root = self._to_canon(mm.root, d["root"])
        groups = {}
        for cname, tname, bm in mm.groups:
            groups[cname or tname] = [self._to_canon(bm, e) for e in d["groups"].get(tname, [])]
        return {"template": mm.canon_name, "id": d["id"], "root": root, "groups": groups}

    @staticmethod
    def _to_canon(bm: _BlockMap, values: dict) -> dict:
        out = {}
        for k, v in values.items():
            cname, conv = bm.back.get(k, (k, None))
            out[cname] = conv(v) if (conv and v is not None) else v
        return out

    def explain(self, frame: bytes) -> list[dict]:
        """Byte-by-byte layout (target schema names) plus each field's canonical name."""
        segs = self.schema.explain(frame)
        if not self.identity:
            tid = struct.unpack_from("<H", frame, 2)[0]
            mm = self._maps.get(tid)
            if mm:
                for s in segs:
                    if s["kind"] == "field" and s["field"]:
                        bm = mm.root
                        if "[" in s["path"]:
                            gname = s["path"].split("[", 1)[0]
                            bm = next((b for _c, t, b in mm.groups if t == gname), bm)
                        s["canonical"] = bm.back.get(s["field"], (s["field"], None))[0]
        return segs

    # ---- sizes (the dashboard's byte-count model) ----
    def layout(self) -> dict:
        """Per template: header + root block + each group's dimension/entry size."""
        out = {}
        for tid in (46, 48, 52, 54):
            m = self.schema.messages.get(tid)
            if m is None:
                continue
            out[str(tid)] = {
                "name": self.template_name(tid), "block": m.block_length,
                "groups": [{"name": g.name, "dim": self.schema.composites[g.dimension].wire_size,
                            "entry": g.block_length} for g in m.groups]}
        return out


class AutoWire:
    """Receive-side Wire that works out which schema a feed is using.

    It decodes with the current candidate; when a frame does not fit (length
    mismatch), it tries the others, and switches to the first that fits. 52 and
    54 are identical in both schemas, so it is the incremental 46/48 frames that
    settle it. `detected` names the schema once one has fitted a 46/48.
    """

    def __init__(self, wires: list[Wire]):
        self.wires = wires
        self.current = wires[0]
        self.detected: str | None = None
        self.switches = 0
        self.on_detect = None

    @property
    def name(self) -> str:
        return self.detected or f"auto ({'/'.join(w.name for w in self.wires)})"

    @property
    def schema(self):
        return self.current.schema

    schema_id = property(lambda self: self.current.schema_id)
    version = property(lambda self: self.current.version)

    def describe(self) -> dict:
        d = self.current.describe()
        d["auto"] = True
        d["detected"] = self.detected
        return d

    def template_name(self, tid):
        return self.current.template_name(tid)

    def encode(self, *a, **k):
        raise TypeError("AutoWire is receive-only: pick a schema to send with")

    def decode(self, frame: bytes) -> dict:
        try:
            d = self.current.decode(frame)
            self._settle(d)
            return d
        except SchemaMismatch:
            pass
        for w in self.wires:
            if w is self.current:
                continue
            try:
                d = w.decode(frame)
            except SchemaMismatch:
                continue
            self.current = w
            self.switches += 1
            self._settle(d)
            return d
        raise SchemaMismatch("frame fits none of: " + ", ".join(w.name for w in self.wires))

    def _settle(self, d):
        if self.detected is None and d["id"] in (46, 48):
            self.detected = self.current.name
            if self.on_detect:
                self.on_detect(self.detected)

    def explain(self, frame):
        return self.current.explain(frame)

    def layout(self):
        return self.current.layout()


@lru_cache(maxsize=None)
def _schema(path: str) -> Schema:
    return Schema(path)


@lru_cache(maxsize=None)
def _wire(name: str) -> Wire:
    path = schema_path(name)
    target = _schema(path)
    canonical = _schema(CANONICAL_PATH)
    return Wire(target, name if name in SCHEMA_FILES else os.path.basename(path),
                None if target is canonical else canonical)


def open_wire(name: str | None = None):
    """'cme' | 'subset' | 'auto' | a path | None (the default)."""
    name = name or default_name()
    if name == "auto":
        names = list(available())
        return AutoWire([_wire(n) for n in names])
    return _wire(name)


def add_schema_argument(ap, receive: bool = False) -> None:
    """The --schema flag every tool shares."""
    choices = "cme, subset" + (", auto" if receive else "")
    default = "auto" if receive else default_name()
    ap.add_argument("--schema", default=default,
                    help=f"SBE schema on the wire: {choices}, or a path (default {default}"
                         f"{'' if receive else '; $MDP3_SCHEMA overrides'})")
