"""
Schema-driven SBE (Simple Binary Encoding) codec for CME MDP 3.0.

Parses an SBE messageSchema XML and encodes/decodes messages generically,
so the exact wire layout lives in the schema (schema/mdp3.xml) rather than in
code. Replace the schema with CME's official templates_FixBinary.xml for
bit-for-bit production parity.

Supports: composites, enums, sets, repeating groups (CME 3-byte dimension),
constant fields, optional/null values, and Decimal price scaling.
"""
from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field as dc_field
from typing import Any

SBE_NS = "{http://fixprotocol.io/2016/sbe}"

# primitiveType -> (struct format char, size in bytes, signed)
_PRIM = {
    "int8": ("b", 1), "uint8": ("B", 1),
    "int16": ("h", 2), "uint16": ("H", 2),
    "int32": ("i", 4), "uint32": ("I", 4),
    "int64": ("q", 8), "uint64": ("Q", 8),
    "char": ("c", 1),
}


@dataclass
class Primitive:
    name: str
    prim: str
    size: int
    presence: str = "required"   # required | optional | constant
    const_value: Any = None
    null_value: Any = None


@dataclass
class Composite:
    name: str
    members: list  # list[Primitive] (constants excluded from wire)

    @property
    def wire_size(self) -> int:
        return sum(m.size for m in self.members if m.presence != "constant")

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
    encoding: str            # primitiveType of encodingType
    size: int
    is_char: bool
    by_name: dict            # name -> raw value (str or int)
    by_value: dict           # raw value -> name


@dataclass
class SetType:
    name: str
    encoding: str
    size: int
    choices: dict            # name -> bit position


@dataclass
class Field:
    name: str
    type_name: str
    offset: int
    kind: str                # 'primitive' | 'composite' | 'enum' | 'set'
    size: int


@dataclass
class Group:
    name: str
    id: int
    block_length: int
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
        self.schema_id = int(root.get("id"))
        self.version = int(root.get("version"))
        self.byte_order = "<" if "little" in (root.get("byteOrder") or "little") else ">"

        self.primitives: dict[str, Primitive] = {}
        self.composites: dict[str, Composite] = {}
        self.enums: dict[str, Enum] = {}
        self.sets: dict[str, SetType] = {}
        self.messages: dict[int, Message] = {}
        self.messages_by_name: dict[str, Message] = {}

        # Only messageSchema/message carry the sbe: prefix; the type system
        # elements (types, composite, enum, set, field, group ...) are unprefixed.
        types_el = root.find("types")
        self._parse_types(types_el)
        for msg_el in root.findall(f"{SBE_NS}message"):
            self._parse_message(msg_el)

        self.header = self.composites["messageHeader"]
        self.group_dim = self.composites["groupSize"]

    # ---- schema parsing ----
    def _parse_types(self, types_el):
        for el in list(types_el):
            tag = el.tag.replace(SBE_NS, "")
            name = el.get("name")
            if tag == "type":
                self.primitives[name] = self._parse_primitive(el)
            elif tag == "composite":
                members = [self._parse_primitive(c) for c in el
                           if c.tag.replace(SBE_NS, "") == "type"]
                self.composites[name] = Composite(name, members)
            elif tag == "enum":
                self._parse_enum(el)
            elif tag == "set":
                self._parse_set(el)

    def _parse_primitive(self, el) -> Primitive:
        prim = el.get("primitiveType")
        size = _PRIM[prim][1]
        presence = el.get("presence", "required")
        const_value = el.text.strip() if presence == "constant" and el.text else None
        null_value = el.get("nullValue")
        return Primitive(el.get("name"), prim, size, presence, const_value, null_value)

    def _parse_enum(self, el):
        enc = el.get("encodingType")
        is_char = enc == "char"
        size = 1 if is_char else _PRIM[enc][1]
        by_name, by_value = {}, {}
        for vv in el.findall("validValue"):
            raw = vv.text.strip()
            val = raw if is_char else int(raw)
            by_name[vv.get("name")] = val
            by_value[val] = vv.get("name")
        self.enums[el.get("name")] = Enum(el.get("name"), enc, size, is_char, by_name, by_value)

    def _parse_set(self, el):
        enc = el.get("encodingType")
        size = _PRIM[enc][1]
        choices = {c.get("name"): int(c.text.strip()) for c in el.findall("choice")}
        self.sets[el.get("name")] = SetType(el.get("name"), enc, size, choices)

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

    def _parse_fields(self, container, offset_base=0):
        fields, running = [], 0
        for f in container.findall("field"):
            type_name = f.get("type")
            kind, size = self._field_kind(type_name)
            offset = f.get("offset")
            offset = int(offset) if offset is not None else running
            fields.append(Field(f.get("name"), type_name, offset, kind, size))
            running = offset + size
        return fields

    def _parse_message(self, el):
        msg = Message(el.get("name"), int(el.get("id")), int(el.get("blockLength")))
        msg.fields = self._parse_fields(el)
        for g in el.findall("group"):
            grp = Group(g.get("name"), int(g.get("id")), int(g.get("blockLength")))
            grp.fields = self._parse_fields(g)
            msg.groups.append(grp)
        self.messages[msg.id] = msg
        self.messages_by_name[msg.name] = msg

    # ---- value <-> bytes for a single field ----
    def _pack_primitive(self, prim: Primitive, value):
        fmt = self.byte_order + _PRIM[prim.prim][0]
        if prim.prim == "char":
            b = value.encode() if isinstance(value, str) else value
            return struct.pack(fmt, b[:1])
        return struct.pack(fmt, int(value))

    def _unpack_primitive(self, prim: Primitive, buf, off):
        fmt = self.byte_order + _PRIM[prim.prim][0]
        (v,) = struct.unpack_from(fmt, buf, off)
        if prim.prim == "char":
            return v.decode(errors="replace")
        return v

    def _encode_field(self, fld: Field, value, out: bytearray, base: int):
        pos = base + fld.offset
        if fld.kind == "primitive":
            prim = self.primitives[fld.type_name]
            out[pos:pos + fld.size] = self._pack_primitive(prim, value)
        elif fld.kind == "enum":
            en = self.enums[fld.type_name]
            raw = en.by_name.get(value, value)  # accept symbolic name or raw
            if en.is_char:
                ch = raw if isinstance(raw, str) else chr(raw)
                out[pos:pos + 1] = ch.encode()[:1]
            else:
                out[pos:pos + en.size] = struct.pack(self.byte_order + _PRIM[en.encoding][0], int(raw))
        elif fld.kind == "set":
            st = self.sets[fld.type_name]
            bits = 0
            if isinstance(value, (list, tuple, set)):
                for name in value:
                    bits |= (1 << st.choices[name])
            else:
                bits = int(value)
            out[pos:pos + st.size] = struct.pack(self.byte_order + _PRIM[st.encoding][0], bits)
        elif fld.kind == "composite":
            comp = self.composites[fld.type_name]
            if comp.is_decimal:
                mant = next(m for m in comp.members if m.name == "mantissa")
                if value is None:
                    raw = int(mant.null_value)
                else:
                    raw = round(float(value) * (10 ** (-comp.exponent())))
                out[pos:pos + mant.size] = struct.pack(self.byte_order + _PRIM[mant.prim][0], raw)
            else:
                raise ValueError(f"Composite field {fld.name} not supported as value")

    def _decode_field(self, fld: Field, buf, base: int):
        pos = base + fld.offset
        if fld.kind == "primitive":
            return self._unpack_primitive(self.primitives[fld.type_name], buf, pos)
        if fld.kind == "enum":
            en = self.enums[fld.type_name]
            if en.is_char:
                raw = buf[pos:pos + 1].decode(errors="replace")
            else:
                (raw,) = struct.unpack_from(self.byte_order + _PRIM[en.encoding][0], buf, pos)
            return en.by_value.get(raw, raw)
        if fld.kind == "set":
            st = self.sets[fld.type_name]
            (bits,) = struct.unpack_from(self.byte_order + _PRIM[st.encoding][0], buf, pos)
            return [name for name, b in st.choices.items() if bits & (1 << b)]
        if fld.kind == "composite":
            comp = self.composites[fld.type_name]
            if comp.is_decimal:
                mant = next(m for m in comp.members if m.name == "mantissa")
                (raw,) = struct.unpack_from(self.byte_order + _PRIM[mant.prim][0], buf, pos)
                if mant.null_value is not None and raw == int(mant.null_value):
                    return None
                return raw * (10 ** comp.exponent())
        raise ValueError(f"Cannot decode field {fld.name}")

    # ---- message encode/decode (SBE header + body, no packet framing) ----
    def encode_message(self, name: str, root: dict, entries: list[dict] | None = None) -> bytes:
        msg = self.messages_by_name[name]
        out = bytearray()
        # SBE message header
        out += struct.pack(self.byte_order + "HHHH",
                           msg.block_length, msg.id, self.schema_id, self.version)
        # root block
        block_start = len(out)
        out += bytes(msg.block_length)
        for fld in msg.fields:
            if fld.name in root:
                self._encode_field(fld, root[fld.name], out, block_start)
        # groups
        entries = entries or []
        for grp in msg.groups:
            out += struct.pack(self.byte_order + "HB", grp.block_length, len(entries))
            for entry in entries:
                estart = len(out)
                out += bytes(grp.block_length)
                for fld in grp.fields:
                    if fld.name in entry:
                        self._encode_field(fld, entry[fld.name], out, estart)
        return bytes(out)

    def decode_message(self, buf, off=0):
        block_length, template_id, schema_id, version = struct.unpack_from(
            self.byte_order + "HHHH", buf, off)
        off += 8
        msg = self.messages[template_id]
        root = {fld.name: self._decode_field(fld, buf, off) for fld in msg.fields}
        off += block_length
        groups = {}
        for grp in msg.groups:
            gbl, num = struct.unpack_from(self.byte_order + "HB", buf, off)
            off += 3
            items = []
            for _ in range(num):
                items.append({fld.name: self._decode_field(fld, buf, off) for fld in grp.fields})
                off += gbl
            groups[grp.name] = items
        return {"template": msg.name, "id": template_id, "root": root, "groups": groups}, off
