"""
Schema-driven SBE (Simple Binary Encoding) codec for CME MDP 3.0.

Parses an SBE messageSchema XML and encodes/decodes messages generically,
so the exact wire layout lives in the schema (schema/mdp3.xml) rather than in
code. Replace the schema with CME's official templates_FixBinary.xml for
bit-for-bit production parity.

Supports: composites (decimal and plain, with member offsets), enums, sets,
fixed-length char arrays, constant fields, optional/null values, Decimal price
scaling, and any number of repeating groups per message (CME 3-byte dimension
or any other dimension composite the schema names).

The parser is namespace-agnostic: the hand-written subset uses the `sbe:`
prefix, CME's official file uses `ns2:` with a different URI, and both load.
"""
from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field as dc_field
from typing import Any

# primitiveType -> (struct format char, size in bytes)
_PRIM = {
    "int8": ("b", 1), "uint8": ("B", 1),
    "int16": ("h", 2), "uint16": ("H", 2),
    "int32": ("i", 4), "uint32": ("I", 4),
    "int64": ("q", 8), "uint64": ("Q", 8),
    "char": ("c", 1),
}

# SBE's default null sentinels, used when an optional type names none
_DEFAULT_NULL = {
    "int8": -128, "uint8": 0xFF, "int16": -0x8000, "uint16": 0xFFFF,
    "int32": -0x80000000, "uint32": 0xFFFFFFFF,
    "int64": -0x8000000000000000, "uint64": 0xFFFFFFFFFFFFFFFF, "char": 0,
}


def _local(tag: str) -> str:
    """Element name without its namespace: '{uri}message' -> 'message'."""
    return tag.rsplit("}", 1)[-1]


@dataclass
class Primitive:
    name: str
    prim: str
    size: int                    # wire size; 0 for a constant
    presence: str = "required"   # required | optional | constant
    const_value: Any = None
    null_value: Any = None
    length: int = 1              # > 1 for a fixed-length char array
    offset: int | None = None    # member offset inside a composite

    @property
    def is_string(self) -> bool:
        return self.prim == "char" and self.length > 1

    @property
    def null(self):
        if self.null_value is not None:
            return int(self.null_value)
        return _DEFAULT_NULL[self.prim]


@dataclass
class Composite:
    name: str
    members: list  # list[Primitive]; constants occupy no wire space

    @property
    def wire_size(self) -> int:
        end = 0
        for off, m in self.layout():
            end = max(end, off + m.size)
        return end

    def layout(self):
        """(offset, member) for every on-wire member."""
        out, running = [], 0
        for m in self.members:
            if m.presence == "constant":
                continue
            off = m.offset if m.offset is not None else running
            out.append((off, m))
            running = off + m.size
        return out

    @property
    def is_decimal(self) -> bool:
        names = {m.name for m in self.members}
        return "mantissa" in names and "exponent" in names

    def exponent(self) -> int:
        for m in self.members:
            if m.name == "exponent":
                return int(m.const_value)
        return 0


@dataclass
class Enum:
    name: str
    encoding: str            # resolved primitiveType
    size: int
    is_char: bool
    by_name: dict            # name -> raw value (str or int)
    by_value: dict           # raw value -> name
    null: Any = None


@dataclass
class SetType:
    name: str
    encoding: str            # resolved primitiveType
    size: int
    choices: dict            # name -> bit position


@dataclass
class Field:
    name: str
    type_name: str
    offset: int
    kind: str                # 'primitive' | 'composite' | 'enum' | 'set'
    size: int
    tag: int | None = None   # FIX tag (the schema's field id): stable across schemas


@dataclass
class Group:
    name: str
    id: int
    block_length: int
    dimension: str = "groupSize"
    fields: list = dc_field(default_factory=list)


@dataclass
class Message:
    name: str
    id: int
    block_length: int
    fields: list = dc_field(default_factory=list)
    groups: list = dc_field(default_factory=list)


class Schema:
    def __init__(self, path: str):
        tree = ET.parse(path)
        root = tree.getroot()
        self.path = path
        self.schema_id = int(root.get("id"))
        self.version = int(root.get("version"))
        self.byte_order = "<" if "little" in (root.get("byteOrder") or "little") else ">"

        self.primitives: dict[str, Primitive] = {}
        self.composites: dict[str, Composite] = {}
        self.enums: dict[str, Enum] = {}
        self.sets: dict[str, SetType] = {}
        self.messages: dict[int, Message] = {}
        self.messages_by_name: dict[str, Message] = {}

        type_blocks = [el for el in root if _local(el.tag) == "types"]
        # Two passes: an enum's encodingType may name a type declared after it
        # (CME's file declares `uInt8NULL` and then enums encoded as it).
        for types_el in type_blocks:
            self._parse_types(types_el)
        for types_el in type_blocks:
            self._parse_enums_and_sets(types_el)
        for msg_el in root:
            if _local(msg_el.tag) == "message":
                self._parse_message(msg_el)

        self.header = self.composites["messageHeader"]
        self.group_dim = self.composites["groupSize"]

    # ---- schema parsing ----
    def _parse_types(self, types_el):
        for el in list(types_el):
            tag, name = _local(el.tag), el.get("name")
            if tag == "type":
                self.primitives[name] = self._parse_primitive(el)
            elif tag == "composite":
                members = [self._parse_primitive(c) for c in el if _local(c.tag) == "type"]
                self.composites[name] = Composite(name, members)

    def _parse_enums_and_sets(self, types_el):
        for el in list(types_el):
            tag = _local(el.tag)
            if tag == "enum":
                self._parse_enum(el)
            elif tag == "set":
                self._parse_set(el)

    def _parse_primitive(self, el) -> Primitive:
        prim = el.get("primitiveType")
        length = int(el.get("length") or 1)
        presence = el.get("presence", "required")
        const_value = el.text.strip() if presence == "constant" and el.text else None
        size = 0 if presence == "constant" else _PRIM[prim][1] * length
        offset = el.get("offset")
        return Primitive(el.get("name"), prim, size, presence, const_value,
                         el.get("nullValue"), length,
                         int(offset) if offset is not None else None)

    def _resolve_encoding(self, enc: str) -> tuple[str, Any]:
        """encodingType -> (primitiveType, null). CME names types, not primitives."""
        if enc in _PRIM:
            return enc, None
        p = self.primitives[enc]
        return p.prim, (p.null if p.presence == "optional" else None)

    def _parse_enum(self, el):
        prim, null = self._resolve_encoding(el.get("encodingType"))
        is_char = prim == "char"
        by_name, by_value = {}, {}
        for vv in el.findall("validValue"):
            raw = vv.text.strip()
            val = raw if is_char else int(raw)
            by_name[vv.get("name")] = val
            by_value[val] = vv.get("name")
        self.enums[el.get("name")] = Enum(el.get("name"), prim, _PRIM[prim][1],
                                          is_char, by_name, by_value, null)

    def _parse_set(self, el):
        prim, _ = self._resolve_encoding(el.get("encodingType"))
        choices = {c.get("name"): int(c.text.strip()) for c in el.findall("choice")}
        self.sets[el.get("name")] = SetType(el.get("name"), prim, _PRIM[prim][1], choices)

    def _field_kind(self, type_name):
        if type_name in self.enums:
            return "enum", self.enums[type_name].size
        if type_name in self.sets:
            return "set", self.sets[type_name].size
        if type_name in self.composites:
            return "composite", self.composites[type_name].wire_size
        if type_name in self.primitives:
            return "primitive", self.primitives[type_name].size
        raise KeyError(f"Unknown type {type_name}")

    def _parse_fields(self, container):
        fields, running = [], 0
        for f in container:
            if _local(f.tag) != "field":
                continue
            type_name = f.get("type")
            kind, size = self._field_kind(type_name)
            offset = f.get("offset")
            offset = int(offset) if offset is not None else running
            tag = f.get("id")
            fields.append(Field(f.get("name"), type_name, offset, kind, size,
                                int(tag) if tag and tag.isdigit() else None))
            running = offset + size
        return fields

    def _parse_message(self, el):
        msg = Message(el.get("name"), int(el.get("id")), int(el.get("blockLength")))
        msg.fields = self._parse_fields(el)
        for g in el:
            if _local(g.tag) != "group":
                continue
            grp = Group(g.get("name"), int(g.get("id")), int(g.get("blockLength")),
                        g.get("dimensionType") or "groupSize")
            grp.fields = self._parse_fields(g)
            msg.groups.append(grp)
        self.messages[msg.id] = msg
        self.messages_by_name[msg.name] = msg

    # ---- value <-> bytes for a single primitive ----
    def _fmt(self, prim: str) -> str:
        return self.byte_order + _PRIM[prim][0]

    def _pack_primitive(self, prim: Primitive, value) -> bytes:
        if prim.prim == "char":
            if value is None:
                return bytes(prim.length)
            b = value.encode() if isinstance(value, str) else bytes(value)
            # SBE char arrays are fixed width, padded with NULs
            return b[:prim.length].ljust(prim.length, b"\x00")
        if value is None:
            value = prim.null
        return struct.pack(self._fmt(prim.prim), int(value))

    def _unpack_primitive(self, prim: Primitive, buf, off):
        if prim.presence == "constant":
            return prim.const_value
        if prim.prim == "char":
            raw = bytes(buf[off:off + prim.length])
            return raw.split(b"\x00", 1)[0].decode(errors="replace")
        (v,) = struct.unpack_from(self._fmt(prim.prim), buf, off)
        if prim.presence == "optional" and v == prim.null:
            return None
        return v

    # ---- value <-> bytes for a field ----
    def _encode_field(self, fld: Field, value, out: bytearray, base: int):
        pos = base + fld.offset
        if fld.kind == "primitive":
            prim = self.primitives[fld.type_name]
            if prim.presence == "constant":
                return
            out[pos:pos + fld.size] = self._pack_primitive(prim, value)
        elif fld.kind == "enum":
            en = self.enums[fld.type_name]
            if value is None:
                raw = en.null if en.null is not None else 0
            else:
                raw = en.by_name.get(value, value)  # accept symbolic name or raw
            if en.is_char:
                ch = raw if isinstance(raw, str) else chr(raw)
                out[pos:pos + 1] = ch.encode()[:1]
            else:
                out[pos:pos + en.size] = struct.pack(self._fmt(en.encoding), int(raw))
        elif fld.kind == "set":
            st = self.sets[fld.type_name]
            bits = 0
            if isinstance(value, (list, tuple, set)):
                for name in value:
                    bits |= (1 << st.choices[name])
            else:
                bits = int(value)
            out[pos:pos + st.size] = struct.pack(self._fmt(st.encoding), bits)
        elif fld.kind == "composite":
            comp = self.composites[fld.type_name]
            if comp.is_decimal:
                mant = next(m for m in comp.members if m.name == "mantissa")
                if value is None:
                    raw = mant.null
                else:
                    raw = round(float(value) * (10 ** (-comp.exponent())))
                out[pos:pos + mant.size] = struct.pack(self._fmt(mant.prim), raw)
            else:
                value = value or {}
                for off, m in comp.layout():
                    if m.name not in value and m.presence == "optional":
                        out[pos + off:pos + off + m.size] = self._pack_primitive(m, None)
                        continue
                    out[pos + off:pos + off + m.size] = self._pack_primitive(m, value.get(m.name))

    def _decode_field(self, fld: Field, buf, base: int):
        pos = base + fld.offset
        if fld.kind == "primitive":
            return self._unpack_primitive(self.primitives[fld.type_name], buf, pos)
        if fld.kind == "enum":
            en = self.enums[fld.type_name]
            if en.is_char:
                raw = buf[pos:pos + 1].decode(errors="replace")
            else:
                (raw,) = struct.unpack_from(self._fmt(en.encoding), buf, pos)
            if raw not in en.by_value and en.null is not None and raw == en.null:
                return None
            return en.by_value.get(raw, raw)
        if fld.kind == "set":
            st = self.sets[fld.type_name]
            (bits,) = struct.unpack_from(self._fmt(st.encoding), buf, pos)
            return [name for name, b in st.choices.items() if bits & (1 << b)]
        if fld.kind == "composite":
            comp = self.composites[fld.type_name]
            if comp.is_decimal:
                mant = next(m for m in comp.members if m.name == "mantissa")
                (raw,) = struct.unpack_from(self._fmt(mant.prim), buf, pos)
                if mant.presence == "optional" and raw == mant.null:
                    return None
                # round() clears float fuzz: 15.625 at exponent -9 must stay 15.625
                exp = comp.exponent()
                return round(raw * (10 ** exp), -exp) if exp < 0 else raw * (10 ** exp)
            return {m.name: self._unpack_primitive(m, buf, pos + off)
                    for off, m in comp.layout()}
        raise ValueError(f"Cannot decode field {fld.name}")

    def is_optional(self, fld: Field) -> bool:
        """Has a null value: an absent value is encoded as null, not zero."""
        if fld.kind == "primitive":
            return self.primitives[fld.type_name].presence == "optional"
        if fld.kind == "enum":
            return self.enums[fld.type_name].null is not None
        if fld.kind == "composite":
            comp = self.composites[fld.type_name]
            return any(m.presence == "optional" for m in comp.members)
        return False

    def _encode_block(self, fields, values: dict, out: bytearray, base: int) -> None:
        for fld in fields:
            if fld.name in values:
                self._encode_field(fld, values[fld.name], out, base)
            elif self.is_optional(fld):
                self._encode_field(fld, None, out, base)

    # ---- group dimension ----
    def _pack_dim(self, dim_name: str, block_length: int, count: int) -> bytes:
        comp = self.composites[dim_name]
        out = bytearray(comp.wire_size)
        for off, m in comp.layout():
            if m.name == "blockLength":
                out[off:off + m.size] = struct.pack(self._fmt(m.prim), block_length)
            elif m.name == "numInGroup":
                out[off:off + m.size] = struct.pack(self._fmt(m.prim), count)
        return bytes(out)

    def _unpack_dim(self, dim_name: str, buf, off) -> tuple[int, int, int]:
        comp = self.composites[dim_name]
        vals = {m.name: struct.unpack_from(self._fmt(m.prim), buf, off + o)[0]
                for o, m in comp.layout()}
        return vals["blockLength"], vals["numInGroup"], comp.wire_size

    # ---- message encode/decode (SBE header + body, no packet framing) ----
    def encode_message(self, name: str, root: dict, entries: list[dict] | None = None,
                       groups: dict[str, list[dict]] | None = None) -> bytes:
        """Encode one message.

        `entries` fills the message's first repeating group (the only one the
        incremental and snapshot messages have); `groups` fills any group by
        name, for messages like the instrument definition that carry several.
        """
        msg = self.messages_by_name[name]
        out = bytearray()
        out += struct.pack(self.byte_order + "HHHH",
                           msg.block_length, msg.id, self.schema_id, self.version)
        block_start = len(out)
        out += bytes(msg.block_length)
        self._encode_block(msg.fields, root, out, block_start)
        for i, grp in enumerate(msg.groups):
            if groups is not None:
                items = groups.get(grp.name, [])
            else:
                items = (entries or []) if i == 0 else []
            out += self._pack_dim(grp.dimension, grp.block_length, len(items))
            for entry in items:
                estart = len(out)
                out += bytes(grp.block_length)
                self._encode_block(grp.fields, entry, out, estart)
        return bytes(out)

    def type_label(self, fld: Field) -> str:
        """Human-readable wire type of a field, e.g. 'PRICENULL9 (int64 e-9)'."""
        if fld.kind == "composite":
            comp = self.composites[fld.type_name]
            if comp.is_decimal:
                mant = next(m for m in comp.members if m.name == "mantissa")
                return f"{fld.type_name} ({mant.prim} e{comp.exponent()})"
            return f"{fld.type_name} (composite)"
        if fld.kind == "enum":
            return f"{fld.type_name} (enum {self.enums[fld.type_name].encoding})"
        if fld.kind == "set":
            return f"{fld.type_name} (set {self.sets[fld.type_name].encoding})"
        p = self.primitives[fld.type_name]
        if p.presence == "constant":
            return f"{fld.type_name} (constant)"
        return f"{fld.type_name} ({p.prim}{f'[{p.length}]' if p.length > 1 else ''})"

    def explain(self, buf, off: int = 0) -> list[dict]:
        """Every byte of one message, attributed: the template inspector's model.

        Returns contiguous segments {off, len, path, field, type, value, kind}
        covering [off, end of message) exactly once — header, root fields, padding,
        each group dimension and each entry's fields. `kind` is one of header,
        field, padding, dimension. Constant fields are listed with len 0.
        """
        segs: list[dict] = []
        start = off

        def seg(o, n, path, kind, field=None, type_=None, value=None):
            segs.append({"off": o - start, "len": n, "path": path, "kind": kind,
                         "field": field, "type": type_, "value": value})

        def block(fields, base, length, prefix):
            pos = base
            for fld in sorted(fields, key=lambda f: (f.offset, f.size)):
                if fld.size == 0:
                    seg(base + fld.offset, 0, prefix + fld.name, "field", fld.name,
                        self.type_label(fld), self._decode_field(fld, buf, base))
                    continue
                if base + fld.offset > pos:
                    seg(pos, base + fld.offset - pos, prefix + "(padding)", "padding")
                seg(base + fld.offset, fld.size, prefix + fld.name, "field", fld.name,
                    self.type_label(fld), self._decode_field(fld, buf, base))
                pos = base + fld.offset + fld.size
            if base + length > pos:
                seg(pos, base + length - pos, prefix + "(padding)", "padding")

        bl, tid, sid, ver = struct.unpack_from(self.byte_order + "HHHH", buf, off)
        for i, (name, v) in enumerate((("blockLength", bl), ("templateId", tid),
                                       ("schemaId", sid), ("version", ver))):
            seg(off + 2 * i, 2, f"header.{name}", "header", name, "uint16", v)
        off += 8
        msg = self.messages[tid]
        block(msg.fields, off, bl, "")
        off += bl
        for grp in msg.groups:
            gbl, num, dim = self._unpack_dim(grp.dimension, buf, off)
            seg(off, dim, f"{grp.name}.dimension", "dimension", grp.name,
                grp.dimension, {"blockLength": gbl, "numInGroup": num})
            off += dim
            for i in range(num):
                block(grp.fields, off, gbl, f"{grp.name}[{i}].")
                off += gbl
        return segs

    def decode_message(self, buf, off=0):
        block_length, template_id, schema_id, version = struct.unpack_from(
            self.byte_order + "HHHH", buf, off)
        off += 8
        msg = self.messages[template_id]
        root = {fld.name: self._decode_field(fld, buf, off) for fld in msg.fields}
        off += block_length
        groups = {}
        for grp in msg.groups:
            gbl, num, dim_size = self._unpack_dim(grp.dimension, buf, off)
            off += dim_size
            items = []
            for _ in range(num):
                items.append({fld.name: self._decode_field(fld, buf, off) for fld in grp.fields})
                off += gbl
            groups[grp.name] = items
        return {"template": msg.name, "id": template_id, "root": root, "groups": groups}, off
