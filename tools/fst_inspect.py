#!/usr/bin/env python3
"""Inspect HnG FST object streams without relying on guessed offsets.

The implemented layout is backed by player.dll's ObjectStreamV2 read/write
paths and is deliberately strict: every size and digest that can be checked is
checked.  The final one-based DFS object-reference stream is also decoded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class FstFormatError(ValueError):
    pass


def read_uleb32(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 35, 7):
        if offset >= len(data):
            raise FstFormatError("truncated ULEB128")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            if value > 0xFFFFFFFF:
                raise FstFormatError("ULEB128 exceeds 32 bits")
            return value, offset
    raise FstFormatError("ULEB128 exceeds five bytes")


def as_i32(value: int) -> int:
    return value - 0x100000000 if value & 0x80000000 else value


def as_u32(value: int) -> int:
    return value & 0xFFFFFFFF


def encode_uleb32(value: int) -> bytes:
    if not 0 <= value <= 0xFFFFFFFF:
        raise FstFormatError(f"ULEB128 value {value} out of u32 range")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def read_cstring(data: bytes, offset: int) -> tuple[str, int]:
    end = data.find(b"\0", offset)
    if end < 0:
        raise FstFormatError("unterminated CString")
    return data[offset:end].decode("utf-8", "surrogateescape"), end + 1


def read_sized_cstring(data: bytes, offset: int) -> tuple[str, int]:
    """Read the one-byte-sized CString used by cached scene-node payloads."""
    if offset >= len(data):
        raise FstFormatError("missing sized CString length")
    size = data[offset]
    end = offset + 1 + size
    if size == 0 or end > len(data) or data[end - 1] != 0:
        raise FstFormatError("invalid sized CString")
    return data[offset + 1 : end - 1].decode("utf-8", "surrogateescape"), end


@dataclass(frozen=True)
class ObjectRecord:
    digest_a: bytes
    value: int
    digest_b: bytes
    digest_a_offset: int
    digest_b_offset: int


@dataclass(frozen=True)
class ObjectGroup:
    schema_id: int
    records: tuple[ObjectRecord, ...]


@dataclass(frozen=True)
class InlineObject:
    offset: int
    payload: bytes
    digest: bytes
    schema_ids: tuple[int, ...]


@dataclass(frozen=True)
class FstStream:
    path: Path
    decompressed: bytes
    payload: bytes
    schema_digest: bytes
    feature_value: int
    groups: tuple[ObjectGroup, ...]
    group_table_end: int
    inline_objects: tuple[InlineObject, ...]
    inline_sentinels: int
    inline_scan_end: int


def parse_fst(path: Path | str) -> FstStream:
    path = Path(path)
    raw = path.read_bytes()
    if raw[:4] != b"FST\0":
        raise FstFormatError(f"{path}: bad FST magic")
    try:
        decompressed = zlib.decompress(raw[4:])
    except zlib.error as exc:
        raise FstFormatError(f"{path}: invalid zlib stream: {exc}") from exc

    if len(decompressed) < 21:
        raise FstFormatError(f"{path}: object stream is too short")
    expected_digest = decompressed[:16]
    payload_size, payload_at = read_uleb32(decompressed, 16)
    payload = decompressed[payload_at:]
    if payload_size != len(payload):
        raise FstFormatError(
            f"{path}: wrapper length {payload_size} != actual {len(payload)}"
        )
    actual_digest = hashlib.md5(payload).digest()
    if actual_digest != expected_digest:
        raise FstFormatError(
            f"{path}: wrapper MD5 {expected_digest.hex()} != {actual_digest.hex()}"
        )

    if len(payload) < 17:
        raise FstFormatError(f"{path}: missing schema digest/header")
    schema_digest = payload[:16]
    offset = 16
    encoded_count, offset = read_uleb32(payload, offset)
    signed_count = as_i32(encoded_count)
    if signed_count >= 0:
        raise FstFormatError(
            f"{path}: unsupported non-negative group header {signed_count}"
        )
    group_count = -signed_count
    feature_value, offset = read_uleb32(payload, offset)

    groups: list[ObjectGroup] = []
    digest_schemas: dict[bytes, set[int]] = {}
    for _ in range(group_count):
        schema_id, offset = read_uleb32(payload, offset)
        record_count, offset = read_uleb32(payload, offset)
        records: list[ObjectRecord] = []
        for _ in range(record_count):
            if offset + 32 > len(payload):
                raise FstFormatError(f"{path}: truncated object record")
            digest_a_offset = payload_at + offset
            digest_a = payload[offset : offset + 16]
            offset += 16
            value, offset = read_uleb32(payload, offset)
            digest_b_offset = payload_at + offset
            digest_b = payload[offset : offset + 16]
            offset += 16
            records.append(
                ObjectRecord(
                    digest_a, value, digest_b, digest_a_offset, digest_b_offset
                )
            )
            digest_schemas.setdefault(digest_a, set()).add(schema_id)
        groups.append(ObjectGroup(schema_id, tuple(records)))
    group_table_end = offset

    inline_objects: list[InlineObject] = []
    sentinels = 0
    while offset < len(payload):
        entry_at = offset
        try:
            size, data_at = read_uleb32(payload, offset)
        except FstFormatError:
            break
        if size == 0xFFFFFFFF:
            sentinels += 1
            offset = data_at
            continue
        end = data_at + size
        if end > len(payload):
            break
        value = payload[data_at:end]
        digest = hashlib.md5(value).digest()
        schema_ids = digest_schemas.get(digest)
        if not schema_ids:
            break
        inline_objects.append(
            InlineObject(entry_at, value, digest, tuple(sorted(schema_ids)))
        )
        offset = end

    return FstStream(
        path=path,
        decompressed=decompressed,
        payload=payload,
        schema_digest=schema_digest,
        feature_value=feature_value,
        groups=tuple(groups),
        group_table_end=payload_at + group_table_end,
        inline_objects=tuple(inline_objects),
        inline_sentinels=sentinels,
        inline_scan_end=payload_at + offset,
    )


@dataclass(frozen=True)
class SchemaField:
    type_id: int
    flags: int
    name_index: int
    count: int


@dataclass(frozen=True)
class SchemaClass:
    schema_id: int
    name: str
    display_name: str
    flags: int
    custom_value: int | None
    fields: tuple[SchemaField, ...]


@dataclass(frozen=True)
class SchemaFile:
    names: tuple[str, ...]
    classes: tuple[SchemaClass, ...]

    def by_name(self, name: str) -> SchemaClass:
        return next(item for item in self.classes if item.name == name)


@dataclass(frozen=True)
class KinematicsTransform:
    entry_offset: int
    offset: int
    digest_a: bytes
    digest_b: bytes
    position: tuple[float, float, float]
    quaternion: tuple[float, float, float, float]
    scale: tuple[float, float, float]
    nposition: tuple[float, float, float]
    nrotation: tuple[float, float, float]
    end_offset: int


@dataclass(frozen=True)
class ReferenceGraph:
    relation_offset: int
    relation_values: tuple[int, ...]
    roots: tuple[int, ...]
    flat_records: tuple[tuple[int, ObjectRecord], ...]
    children_by_digest_b: dict[bytes, tuple[int, ...]]


@dataclass(frozen=True)
class NodeTransformBinding:
    record_index: int
    node_digest_b: bytes
    property_record_index: int
    node: dict[str, object]
    transform: KinematicsTransform


@dataclass(frozen=True)
class WorldTransform:
    position: tuple[float, float, float]
    quaternion: tuple[float, float, float, float]
    scale: tuple[float, float, float]


def parse_rs03(path: Path | str) -> SchemaFile:
    path = Path(path)
    data = path.read_bytes()
    magic, offset = read_cstring(data, 0)
    if magic != "RS03":
        raise FstFormatError(f"{path}: unsupported schema magic {magic!r}")
    if offset + 4 > len(data):
        raise FstFormatError(f"{path}: truncated schema name count")
    name_count = struct.unpack_from("<I", data, offset)[0]
    offset += 4
    names: list[str] = []
    for _ in range(name_count):
        value, offset = read_cstring(data, offset)
        names.append(value)

    classes: list[SchemaClass] = []
    while True:
        name, offset = read_cstring(data, offset)
        if not name:
            break
        display_name, offset = read_cstring(data, offset)
        if offset >= len(data):
            raise FstFormatError(f"{path}: missing class flags")
        flags = data[offset]
        offset += 1
        custom_value = None
        fields: list[SchemaField] = []
        if flags & 1:
            if offset + 4 > len(data):
                raise FstFormatError(f"{path}: truncated custom class value")
            custom_value = struct.unpack_from("<I", data, offset)[0]
            offset += 4
        else:
            while True:
                if offset + 4 > len(data):
                    raise FstFormatError(f"{path}: truncated field type")
                type_id = struct.unpack_from("<i", data, offset)[0]
                offset += 4
                if type_id == -32:
                    break
                if offset + 9 > len(data):
                    raise FstFormatError(f"{path}: truncated field")
                field_flags = data[offset]
                name_index, count = struct.unpack_from("<ii", data, offset + 1)
                offset += 9
                if not 0 <= name_index < len(names):
                    raise FstFormatError(
                        f"{path}: field name index {name_index} out of range"
                    )
                fields.append(SchemaField(type_id, field_flags, name_index, count))
        classes.append(
            SchemaClass(
                len(classes), name, display_name, flags, custom_value, tuple(fields)
            )
        )
    if offset != len(data):
        raise FstFormatError(f"{path}: {len(data) - offset} trailing schema bytes")
    return SchemaFile(tuple(names), tuple(classes))


def decode_scene_node(value: bytes) -> dict[str, object]:
    node_type, offset = read_sized_cstring(value, 0)
    name, offset = read_sized_cstring(value, offset)
    if offset + 8 != len(value):
        raise FstFormatError("scene-node payload does not end with two u32 values")
    first, second = struct.unpack_from("<II", value, offset)
    return {"node_type": node_type, "name": name, "first": first, "second": second}


def decode_property_set(value: bytes) -> dict[str, str]:
    name, offset = read_sized_cstring(value, 0)
    property_type, offset = read_sized_cstring(value, offset)
    if offset != len(value):
        raise FstFormatError("property-set payload has trailing bytes")
    return {"name": name, "property_type": property_type}


def locate_kinematics(stream: FstStream) -> tuple[KinematicsTransform, ...]:
    """Locate schema 731's 16-float bodies and verify both record digests.

    player.dll serializes ZLocalKinematics as sixteen raw float32 values.  The
    Digest A covers only the 64-byte body; digest B is
    MD5(LE32(schema_id) + digest A).  The byte before each body is its ULEB128
    length prefix, not an object reference.
    """
    try:
        group = next(item for item in stream.groups if item.schema_id == 731)
    except StopIteration as exc:
        raise FstFormatError(f"{stream.path}: schema 731 group is absent") from exc
    by_digest = {record.digest_a: record for record in group.records}
    found: dict[bytes, KinematicsTransform] = {}
    data = stream.decompressed
    for offset in range(stream.inline_scan_end, len(data) - 64):
        values = struct.unpack_from("<16f", data, offset)
        if not all(math.isfinite(value) and abs(value) < 1.0e8 for value in values):
            continue
        quaternion_norm = sum(value * value for value in values[3:7])
        if not 0.80 < quaternion_norm < 1.20:
            continue
        digest_a = hashlib.md5(data[offset : offset + 64]).digest()
        record = by_digest.get(digest_a)
        if record is None:
            continue
        expected_b = hashlib.md5(struct.pack("<I", 731) + digest_a).digest()
        if record.digest_b != expected_b:
            raise FstFormatError(
                f"{stream.path}: schema 731 derived digest mismatch at 0x{offset:X}"
            )
        if offset == 0 or data[offset - 1] != 64:
            continue
        found[digest_a] = KinematicsTransform(
            entry_offset=offset - 1,
            offset=offset,
            digest_a=digest_a,
            digest_b=record.digest_b,
            position=values[0:3],
            quaternion=values[3:7],
            scale=values[7:10],
            nposition=values[10:13],
            nrotation=values[13:16],
            end_offset=offset + 64,
        )
    if len(found) != len(by_digest):
        raise FstFormatError(
            f"{stream.path}: located {len(found)}/{len(by_digest)} schema 731 bodies"
        )
    return tuple(found[record.digest_a] for record in group.records)


def parse_reference_graph(
    stream: FstStream, transforms: Iterable[KinematicsTransform]
) -> ReferenceGraph:
    """Parse the final DFS object-reference stream into direct graph edges.

    The middle value in each header record is the expanded DFS size for its
    first occurrence.  Repeated contextual digest-B values occupy one entry.
    This mirrors ObjStreamV2's writer-side dependency counting and reader-side
    one-based record lookup.
    """
    transforms = tuple(transforms)
    if not transforms:
        raise FstFormatError(f"{stream.path}: no transformations locate relation tail")
    relation_offset = max(item.end_offset for item in transforms)
    values: list[int] = []
    offset = relation_offset
    while offset < len(stream.decompressed):
        value, offset = read_uleb32(stream.decompressed, offset)
        values.append(value)

    flat = tuple((group.schema_id, record) for group in stream.groups for record in group.records)
    if any(value > len(flat) for value in values):
        raise FstFormatError(f"{stream.path}: reference index exceeds record table")
    seen: set[bytes] = set()
    children: dict[bytes, tuple[int, ...]] = {}

    def visit(position: int) -> int:
        index = values[position]
        if index == 0:
            return position + 1
        _schema_id, record = flat[index - 1]
        if record.digest_b in seen:
            return position + 1
        seen.add(record.digest_b)
        end = position + record.value
        if end > len(values):
            raise FstFormatError(
                f"{stream.path}: reference subtree overruns stream at item {position}"
            )
        direct: list[int] = []
        cursor = position + 1
        while cursor < end:
            direct.append(values[cursor])
            cursor = visit(cursor)
        if cursor != end:
            raise FstFormatError(
                f"{stream.path}: reference subtree misaligns at item {position}"
            )
        children[record.digest_b] = tuple(direct)
        return cursor

    roots: list[int] = []
    position = 0
    while position < len(values):
        roots.append(values[position])
        position = visit(position)
    if len(seen) != len(flat):
        raise FstFormatError(
            f"{stream.path}: reference graph covers {len(seen)}/{len(flat)} records"
        )
    return ReferenceGraph(
        relation_offset,
        tuple(values),
        tuple(roots),
        flat,
        children,
    )


def map_kinematics_property_sets(
    stream: FstStream, transforms: Iterable[KinematicsTransform]
) -> dict[bytes, KinematicsTransform]:
    """Map each contextual Kinematics property-set digest to its transform."""
    property_objects = []
    for item in iter_schema_objects(stream, 438):
        try:
            decoded = decode_property_set(item.payload)
        except FstFormatError:
            continue
        if decoded == {"name": "Kinematics", "property_type": "kine"}:
            property_objects.append(item)
    property_digests = {item.digest for item in property_objects}
    if len(property_digests) != 1:
        raise FstFormatError(
            f"{stream.path}: expected one Kinematics property payload digest"
        )
    property_digest = next(iter(property_digests))
    group = next(item for item in stream.groups if item.schema_id == 438)
    records = [item for item in group.records if item.digest_a == property_digest]
    derived: dict[bytes, KinematicsTransform] = {}
    prefix = struct.pack("<I", 438) + property_digest
    for transform in transforms:
        contextual_digest = hashlib.md5(prefix + transform.digest_b).digest()
        derived[contextual_digest] = transform
    result = {record.digest_b: derived[record.digest_b] for record in records if record.digest_b in derived}
    if len(result) != len(records):
        raise FstFormatError(
            f"{stream.path}: mapped {len(result)}/{len(records)} Kinematics property records"
        )
    return result


def bind_scene_nodes_to_transforms(
    stream: FstStream,
    graph: ReferenceGraph,
    property_map: dict[bytes, KinematicsTransform],
) -> tuple[NodeTransformBinding, ...]:
    """Bind contextual schema-434 records to their Kinematics property/transform."""
    payloads = {item.digest: item.payload for item in stream.inline_objects}
    result: list[NodeTransformBinding] = []
    for index, (schema_id, record) in enumerate(graph.flat_records, 1):
        if schema_id != 434 or record.digest_a not in payloads:
            continue
        matches: list[tuple[int, KinematicsTransform]] = []
        for child_index in graph.children_by_digest_b.get(record.digest_b, ()):
            if child_index == 0:
                continue
            child_schema, child_record = graph.flat_records[child_index - 1]
            if child_schema == 438 and child_record.digest_b in property_map:
                matches.append((child_index, property_map[child_record.digest_b]))
        if len(matches) > 1:
            raise FstFormatError(
                f"{stream.path}: scene-node record {index} has multiple Kinematics properties"
            )
        if matches:
            result.append(
                NodeTransformBinding(
                    index,
                    record.digest_b,
                    matches[0][0],
                    decode_scene_node(payloads[record.digest_a]),
                    matches[0][1],
                )
            )
    return tuple(result)


def scene_node_world_transforms(
    graph: ReferenceGraph,
    bindings: Iterable[NodeTransformBinding],
) -> dict[int, WorldTransform]:
    """Compose bound SceneNode local transforms through serialized parents."""
    by_index = {item.record_index: item for item in bindings}
    cache: dict[int, WorldTransform] = {}

    def multiply_quaternions(a, b):
        ax, ay, az, aw = a
        bx, by, bz, bw = b
        return (
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz,
        )

    def rotate(q, value):
        x, y, z, w = q
        vx, vy, vz = value
        tx = 2.0 * (y * vz - z * vy)
        ty = 2.0 * (z * vx - x * vz)
        tz = 2.0 * (x * vy - y * vx)
        return (
            vx + w * tx + (y * tz - z * ty),
            vy + w * ty + (z * tx - x * tz),
            vz + w * tz + (x * ty - y * tx),
        )

    def resolve(index: int, stack: tuple[int, ...]) -> WorldTransform:
        if index in cache:
            return cache[index]
        if index in stack:
            raise FstFormatError(f"parent cycle at SceneNode record {index}")
        binding = by_index[index]
        local = WorldTransform(
            binding.transform.position,
            binding.transform.quaternion,
            binding.transform.scale,
        )
        direct = graph.children_by_digest_b.get(binding.node_digest_b, ())
        parent_count = int(binding.node["first"])
        parents = [
            child
            for child in direct[:parent_count]
            if child and graph.flat_records[child - 1][0] == 434
        ]
        if len(parents) > 1:
            raise FstFormatError(
                f"SceneNode record {index} has multiple serialized parents"
            )
        if not parents or parents[0] not in by_index:
            result = local
        else:
            parent = resolve(parents[0], stack + (index,))
            scaled = tuple(
                local.position[axis] * parent.scale[axis] for axis in range(3)
            )
            rotated = rotate(parent.quaternion, scaled)
            result = WorldTransform(
                tuple(parent.position[axis] + rotated[axis] for axis in range(3)),
                multiply_quaternions(parent.quaternion, local.quaternion),
                tuple(parent.scale[axis] * local.scale[axis] for axis in range(3)),
            )
        cache[index] = result
        return result

    for record_index in by_index:
        resolve(record_index, ())
    return cache


def patch_kinematics_transform(
    stream: FstStream,
    graph: ReferenceGraph,
    binding: NodeTransformBinding,
    *,
    position: tuple[float, float, float] | None = None,
    quaternion: tuple[float, float, float, float] | None = None,
    scale: tuple[float, float, float] | None = None,
) -> bytes:
    """Return a complete FST with one bound transform safely replaced.

    This is the conservative template-compiler operation: the 64-byte
    ZKinematics payload stays in place, record indices and DFS references stay
    stable, and only the two proven contextual hashes (schema 731 and its
    Kinematics schema-438 property) are updated.  ZSceneNode's contextual key
    is deliberately left alone; it is not a hash of its serialized edges.
    """
    transform = binding.transform
    position = transform.position if position is None else tuple(position)
    quaternion = transform.quaternion if quaternion is None else tuple(quaternion)
    scale = transform.scale if scale is None else tuple(scale)
    if len(position) != 3 or len(quaternion) != 4 or len(scale) != 3:
        raise ValueError("position/quaternion/scale must have 3/4/3 components")
    values = (*position, *quaternion, *scale, *transform.nposition, *transform.nrotation)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("transform contains a non-finite value")
    body = struct.pack("<16f", *values)
    digest_a = hashlib.md5(body).digest()
    digest_b = hashlib.md5(struct.pack("<I", 731) + digest_a).digest()

    transform_records = [
        record
        for schema_id, record in graph.flat_records
        if schema_id == 731 and record.digest_b == transform.digest_b
    ]
    if len(transform_records) != 1:
        raise FstFormatError(
            f"{stream.path}: expected one schema-731 record for selected transform"
        )
    property_schema, property_record = graph.flat_records[
        binding.property_record_index - 1
    ]
    if property_schema != 438:
        raise FstFormatError(f"{stream.path}: binding does not point to schema 438")
    property_digest_b = hashlib.md5(
        struct.pack("<I", 438) + property_record.digest_a + digest_b
    ).digest()

    # A collision would require removing/reindexing a record rather than doing
    # an in-place template edit, so reject it explicitly.
    for schema_id, record in graph.flat_records:
        if record is transform_records[0] or record is property_record:
            continue
        if schema_id == 731 and record.digest_b == digest_b:
            raise FstFormatError(f"{stream.path}: transform digest collision")
        if schema_id == 438 and record.digest_b == property_digest_b:
            raise FstFormatError(f"{stream.path}: property digest collision")

    decompressed = bytearray(stream.decompressed)
    decompressed[transform.offset : transform.end_offset] = body
    transform_record = transform_records[0]
    decompressed[
        transform_record.digest_a_offset : transform_record.digest_a_offset + 16
    ] = digest_a
    decompressed[
        transform_record.digest_b_offset : transform_record.digest_b_offset + 16
    ] = digest_b
    decompressed[
        property_record.digest_b_offset : property_record.digest_b_offset + 16
    ] = property_digest_b
    payload_size, payload_at = read_uleb32(decompressed, 16)
    if payload_at + payload_size != len(decompressed):
        raise FstFormatError(f"{stream.path}: wrapper changed size during patch")
    decompressed[:16] = hashlib.md5(decompressed[payload_at:]).digest()
    return b"FST\0" + zlib.compress(bytes(decompressed))


def iter_schema_objects(stream: FstStream, schema_id: int) -> Iterable[InlineObject]:
    return (item for item in stream.inline_objects if schema_id in item.schema_ids)


@dataclass(frozen=True)
class CacheEntry:
    """One inline-cache entry, consumed exactly like the reader does.

    ZObjectInputStreamV2_LoadStream walks the group table in order and, for
    every record of a class whose RS03 flags have bit 0 or 1 set, consumes one
    entry unless (schema_id, digest_A, digest_B) already has a stored payload.
    A sentinel (-1) means "no payload" and, unlike real payloads, never enters
    the dedup tree.  Value-type payloads do not need MD5(payload) == digest_A.
    """

    entry_offset: int
    size: int  # -1 sentinel, otherwise >= 0
    payload: bytes | None
    schema_id: int
    digest_a: bytes
    digest_b: bytes


def parse_cache_entries(
    stream: FstStream, schema: SchemaFile | None = None
) -> tuple[tuple[CacheEntry, ...], int]:
    """Parse the per-record inline cache; return entries and reference offset."""
    data = stream.decompressed
    pos = stream.group_table_end
    seen: set[tuple[int, bytes]] = set()
    entries: list[CacheEntry] = []
    for group in stream.groups:
        for record in group.records:
            key = (group.schema_id, record.digest_a)
            if key in seen:
                continue
            size, after = read_uleb32(data, pos)
            signed = as_i32(size)
            if signed < 0:
                if signed != -1:
                    raise FstFormatError(
                        f"{stream.path}: unexpected cache sentinel {signed}"
                    )
                entries.append(CacheEntry(pos, -1, None, group.schema_id, record.digest_a, record.digest_b))
                pos = after
                continue
            end = after + size
            if end > len(data):
                raise FstFormatError(
                    f"{stream.path}: cache payload overruns stream at 0x{pos:X}"
                )
            entries.append(CacheEntry(pos, size, data[after:end], group.schema_id, record.digest_a, record.digest_b))
            seen.add(key)
            pos = end
    return tuple(entries), pos


def rebuild_decompressed(
    stream: FstStream,
    schema: SchemaFile,
    cache_entries: tuple[CacheEntry, ...] | None = None,
    reference_offset: int | None = None,
    *,
    groups: tuple[ObjectGroup, ...] | None = None,
    reference_bytes: bytes | None = None,
) -> bytes:
    """Re-encode the whole decompressed stream from parsed pieces.

    With no overrides this is a lossless round-trip; callers may substitute
    new groups/cache entries to compile a modified scene.  The reference tail
    is copied verbatim from ``reference_offset`` (or the parsed boundary)
    unless explicit ``reference_bytes`` are given.
    """
    if cache_entries is None or reference_offset is None:
        parsed_entries, parsed_offset = parse_cache_entries(stream)
        cache_entries = parsed_entries if cache_entries is None else cache_entries
        reference_offset = (
            parsed_offset if reference_offset is None else reference_offset
        )
    groups = stream.groups if groups is None else groups

    payload = bytearray()
    payload += stream.schema_digest
    payload += encode_uleb32(as_u32(-len(groups)))
    payload += encode_uleb32(stream.feature_value)
    for group in groups:
        payload += encode_uleb32(group.schema_id)
        payload += encode_uleb32(len(group.records))
        for record in group.records:
            payload += record.digest_a
            payload += encode_uleb32(record.value)
            payload += record.digest_b
    for entry in cache_entries:
        if entry.size < 0:
            payload += encode_uleb32(0xFFFFFFFF)
        else:
            payload += encode_uleb32(entry.size)
            payload += entry.payload or b""
    if reference_bytes is None:
        payload += stream.decompressed[reference_offset:]
    else:
        payload += reference_bytes

    wrapper = bytearray(hashlib.md5(payload).digest())
    wrapper += encode_uleb32(len(payload))
    wrapper += payload
    return bytes(wrapper)


def _encode_scene_node_payload(node: dict[str, object]) -> bytes:
    node_type = str(node["node_type"]).encode("utf-8") + b"\0"
    name = str(node["name"]).encode("utf-8") + b"\0"
    if len(node_type) > 255 or len(name) > 255:
        raise FstFormatError("scene-node string exceeds sized CString limit")
    return (
        bytes([len(node_type)])
        + node_type
        + bytes([len(name)])
        + name
        + struct.pack("<II", int(node["first"]), int(node["second"]))
    )


def _reference_positions(graph: ReferenceGraph) -> dict[bytes, int]:
    """Map each first-occurrence record digest_B to its value-list position."""
    values = graph.relation_values
    seen: set[bytes] = set()
    positions: dict[bytes, int] = {}

    def visit(position: int) -> int:
        index = values[position]
        if index == 0:
            return position + 1
        _schema, record = graph.flat_records[index - 1]
        if record.digest_b in seen:
            return position + 1
        seen.add(record.digest_b)
        positions[record.digest_b] = position
        end = position + record.value
        cursor = position + 1
        while cursor < end:
            cursor = visit(cursor)
        return cursor

    position = 0
    while position < len(values):
        position = visit(position)
    return positions


def normalize_digest_a_blocks(stream: FstStream) -> bytes:
    """Stably group equal digest-A records and remap the full relation stream.

    LoadStream deep-copies adjacent records sharing one serialized payload.
    Rewriting a contextual parent during one clone can split such a block,
    which prevents a later component clone from safely extending it.
    """
    transforms = locate_kinematics(stream)
    graph = parse_reference_graph(stream, transforms)
    old_index = 1
    next_index = 1
    index_map = {0: 0}
    groups = []
    for group in stream.groups:
        buckets: dict[bytes, list[tuple[int, ObjectRecord]]] = {}
        order: list[bytes] = []
        for record in group.records:
            if record.digest_a not in buckets:
                buckets[record.digest_a] = []
                order.append(record.digest_a)
            buckets[record.digest_a].append((old_index, record))
            old_index += 1
        records = []
        for digest_a in order:
            for previous_index, record in buckets[digest_a]:
                index_map[previous_index] = next_index
                next_index += 1
                records.append(record)
        groups.append(ObjectGroup(group.schema_id, tuple(records)))
    values = [index_map[value] for value in graph.relation_values]
    entries, _reference_offset = parse_cache_entries(stream)
    reference_bytes = b"".join(encode_uleb32(value) for value in values)
    decompressed = rebuild_decompressed(
        stream,
        None,
        entries,
        0,
        groups=tuple(groups),
        reference_bytes=reference_bytes,
    )
    return b"FST\0" + zlib.compress(decompressed)


def insert_cloned_instance(
    stream: FstStream,
    schema: SchemaFile,
    graph: ReferenceGraph,
    binding: NodeTransformBinding,
    *,
    name: str,
    position: tuple[float, float, float],
    quaternion: tuple[float, float, float, float],
    scale: tuple[float, float, float] | None = None,
) -> bytes:
    """Clone one bound scene node as a new instance and return full FST bytes.

    The clone reuses the template's subtree shape: parent back-edges, the
    collision child and all LOD meshes stay shared; only the scene-node
    record, its contextual Kinematics property and the 64-byte transform are
    new.  Flat indices, per-record DFS sizes, cache entries, digests, the
    payload MD5 and the zlib wrapper are all recomputed.
    """
    return _insert_cloned_scene_tree(
        stream,
        schema,
        graph,
        binding,
        name=name,
        position=position,
        quaternion=quaternion,
        scale=scale,
    )

    # Legacy three-record implementation retained below as reverse-engineering
    # history.  It produced a structurally parseable stream, but shared child
    # SceneNodes whose parent pointers still targeted the template root.  The
    # game rejected that graph during PostLoad unused-node cleanup.
    del schema  # pragma: no cover
    transform = binding.transform
    scale = transform.scale if scale is None else tuple(scale)
    values = list(graph.relation_values)

    node_payload = _encode_scene_node_payload({**binding.node, "name": name})
    node_digest_a = hashlib.md5(node_payload).digest()

    property_schema, property_record = graph.flat_records[
        binding.property_record_index - 1
    ]
    if property_schema != 438:
        raise FstFormatError(f"{stream.path}: binding does not point to schema 438")

    body_values = (
        *position,
        *quaternion,
        *scale,
        *transform.nposition,
        *transform.nrotation,
    )
    if not all(math.isfinite(value) for value in body_values):
        raise ValueError("transform contains a non-finite value")
    body = struct.pack("<16f", *body_values)
    transform_digest_a = hashlib.md5(body).digest()
    transform_digest_b = hashlib.md5(struct.pack("<I", 731) + transform_digest_a).digest()

    kine_digest_a = property_record.digest_a
    kine_digest_b = hashlib.md5(
        struct.pack("<I", 438) + kine_digest_a + transform_digest_b
    ).digest()
    node_digest_b = hashlib.md5(
        struct.pack("<I", 434) + node_digest_a + kine_digest_b
    ).digest()

    flat_digests = {(sid, rec.digest_a, rec.digest_b) for sid, rec in graph.flat_records}
    for sid, digest_a, digest_b in (
        (434, node_digest_a, node_digest_b),
        (438, kine_digest_a, kine_digest_b),
        (731, transform_digest_a, transform_digest_b),
    ):
        if (sid, digest_a, digest_b) in flat_digests:
            raise FstFormatError(f"{stream.path}: new record collides with template")

    transform_index_old = next(
        index
        for index, (sid, rec) in enumerate(graph.flat_records, 1)
        if sid == 731 and rec.digest_b == transform.digest_b
    )
    positions = _reference_positions(graph)
    template_pos = positions[binding.node_digest_b]
    template_span = graph.flat_records[binding.record_index - 1][1].value

    # Allocate clone indices before mapping the existing table.  The reader
    # batches consecutive records with the same (schema, digest_A): it
    # deserializes the first object and deep-copies it into the remaining
    # contextual records.  A reused payload (the Kinematics PropertySet) must
    # therefore be inserted next to its digest-A block, never at group tail.
    insertion_positions: dict[int, int] = {
        434: len(next(group for group in stream.groups if group.schema_id == 434).records),
        731: len(next(group for group in stream.groups if group.schema_id == 731).records),
    }
    property_group = next(group for group in stream.groups if group.schema_id == 438)
    property_matches = [
        index
        for index, record in enumerate(property_group.records)
        if record.digest_a == kine_digest_a
    ]
    if not property_matches:
        raise FstFormatError(f"{stream.path}: Kinematics payload block is absent")
    if property_matches != list(range(property_matches[0], property_matches[-1] + 1)):
        raise FstFormatError(f"{stream.path}: Kinematics payload block is not contiguous")
    insertion_positions[438] = property_matches[-1] + 1

    index_map = {0: 0}
    appended: dict[int, int] = {}
    old_index = 1
    new_index = 1
    for group in stream.groups:
        insert_at = insertion_positions.get(group.schema_id)
        if insert_at == 0:
            appended[group.schema_id] = new_index
            new_index += 1
        for local_index, _ in enumerate(group.records, 1):
            index_map[old_index] = new_index
            old_index += 1
            new_index += 1
            if insert_at == local_index:
                appended[group.schema_id] = new_index
                new_index += 1
    node_new = appended[434]
    kine_new = appended[438]
    transform_new = appended[731]

    # Re-emit the template subtree: the three replacement slots carry fresh
    # digest-B values, so they stay first occurrences and expand in the clone;
    # every other slot collapses to a single REPEAT entry because its first
    # occurrence already precedes the splice point.
    replacements = {
        binding.record_index: node_new,
        binding.property_record_index: kine_new,
        transform_index_old: transform_new,
    }
    emit_seen = {db for db, pos in positions.items() if pos < template_pos}
    clone_spans: dict[int, int] = {}

    def emit(start: int) -> tuple[list[int], int]:
        value = values[start]
        if value == 0:
            return [0], start + 1
        _schema, record = graph.flat_records[value - 1]
        replacement = replacements.get(value)
        if record.digest_b in emit_seen:
            return [replacement if replacement is not None else index_map[value]], start + 1
        emit_seen.add(record.digest_b)
        end = start + record.value
        cursor = start + 1
        if replacement is not None:
            emitted = [replacement]
            while cursor < end:
                chunk, cursor = emit(cursor)
                emitted.extend(chunk)
            clone_spans[replacement] = len(emitted)
            return emitted, cursor
        emitted = [index_map[value]]
        while cursor < end:
            _, cursor = emit(cursor)
        return emitted, cursor

    clone, after = emit(template_pos)
    if after != template_pos + template_span:
        raise FstFormatError(f"{stream.path}: template subtree re-emission misaligns")
    for replacement in (node_new, kine_new, transform_new):
        if replacement not in clone_spans:
            raise FstFormatError(
                f"{stream.path}: template subtree lacks a first-occurrence slot to clone"
            )

    # Appending mid-table records shifts every later flat index, so remap the
    # whole stream before splicing in the clone.
    values = [index_map[value] for value in values]

    # Build the new flat table with records appended to their schema groups.
    new_records = {
        434: ObjectRecord(node_digest_a, clone_spans[node_new], node_digest_b, 0, 0),
        438: ObjectRecord(
            kine_digest_a,
            clone_spans[kine_new],
            kine_digest_b,
            0,
            0,
        ),
        731: ObjectRecord(
            transform_digest_a,
            clone_spans[transform_new],
            transform_digest_b,
            0,
            0,
        ),
    }
    new_groups: list[ObjectGroup] = []
    for group in stream.groups:
        records = list(group.records)
        if group.schema_id in new_records:
            records.insert(
                insertion_positions[group.schema_id], new_records[group.schema_id]
            )
        new_groups.append(ObjectGroup(group.schema_id, tuple(records)))

    values[template_pos + template_span : template_pos + template_span] = clone

    # Derive the new DFS sizes: first occurrences whose expansion straddles
    # the splice point absorb the clone; everything else keeps its span.
    splice = template_pos + template_span
    span_of = {
        record.digest_b: record.value for _schema, record in graph.flat_records
    }
    final_spans: dict[bytes, int] = {
        digest_b: span + len(clone) if pos < splice < pos + span else span
        for digest_b, pos in positions.items()
        for span in (span_of[digest_b],)
    }
    final_spans[node_digest_b] = len(clone)
    final_spans[kine_digest_b] = clone_spans[kine_new]
    final_spans[transform_digest_b] = clone_spans[transform_new]

    final_groups = tuple(
        ObjectGroup(
            group.schema_id,
            tuple(
                ObjectRecord(
                    record.digest_a,
                    final_spans[record.digest_b],
                    record.digest_b,
                    0,
                    0,
                )
                for record in group.records
            ),
        )
        for group in new_groups
    )

    # Structural check: the spliced stream must walk exactly with these sizes.
    flat_new = tuple(
        (group.schema_id, record) for group in final_groups for record in group.records
    )
    seen: set[bytes] = set()

    def visit(new_position: int) -> int:
        index = values[new_position]
        if index == 0:
            return new_position + 1
        _sid, record = flat_new[index - 1]
        if record.digest_b in seen:
            return new_position + 1
        seen.add(record.digest_b)
        end = new_position + record.value
        cursor = new_position + 1
        while cursor < end:
            cursor = visit(cursor)
        if cursor != end:
            raise FstFormatError(f"{stream.path}: spliced reference stream misaligns")
        return cursor

    cursor = 0
    while cursor < len(values):
        cursor = visit(cursor)
    if len(seen) != len(flat_new):
        raise FstFormatError(
            f"{stream.path}: spliced stream covers {len(seen)}/{len(flat_new)} records"
        )

    # Rebuild the cache entries in new record order, reusing original payloads.
    original_entries, _ = parse_cache_entries(stream)
    original_payloads = {
        (entry.schema_id, entry.digest_a): entry for entry in original_entries
    }
    entry_cursor = 0
    seen_keys: set[tuple[int, bytes]] = set()
    new_entries: list[CacheEntry] = []
    for group in final_groups:
        for record in group.records:
            key = (group.schema_id, record.digest_a)
            if key in seen_keys:
                continue
            if (key, record.digest_b) == (
                (434, node_digest_a),
                node_digest_b,
            ):
                new_entries.append(
                    CacheEntry(0, len(node_payload), node_payload, 434, node_digest_a, node_digest_b)
                )
                seen_keys.add(key)
                continue
            if (key, record.digest_b) == ((731, transform_digest_a), transform_digest_b):
                new_entries.append(
                    CacheEntry(0, len(body), body, 731, transform_digest_a, transform_digest_b)
                )
                seen_keys.add(key)
                continue
            original = original_entries[entry_cursor]
            entry_cursor += 1
            if (original.schema_id, original.digest_a) != key:
                raise FstFormatError(
                    f"{stream.path}: cache replay misaligns on schema {group.schema_id}"
                )
            new_entries.append(
                CacheEntry(
                    0,
                    original.size,
                    original.payload,
                    group.schema_id,
                    record.digest_a,
                    record.digest_b,
                )
            )
            if original.size >= 0:
                seen_keys.add(key)

    reference_bytes = b"".join(encode_uleb32(value) for value in values)
    decompressed = rebuild_decompressed(
        stream,
        None,
        new_entries,
        0,
        groups=final_groups,
        reference_bytes=reference_bytes,
    )
    return b"FST\0" + zlib.compress(decompressed)


def _insert_cloned_scene_tree(
    stream: FstStream,
    schema: SchemaFile,
    graph: ReferenceGraph,
    binding: NodeTransformBinding,
    *,
    name: str,
    position: tuple[float, float, float],
    quaternion: tuple[float, float, float, float],
    scale: tuple[float, float, float] | None,
) -> bytes:
    """Clone the complete owned SceneNode skeleton of one model instance.

    SceneNode children carry parent pointers and are not shareable between two
    model roots.  Property/resource records are shareable; only the root
    Kinematics property and transform change for a newly placed instance.
    """
    del schema
    old_values = list(graph.relation_values)
    positions = _reference_positions(graph)
    template_record = graph.flat_records[binding.record_index - 1][1]
    template_pos = positions[binding.node_digest_b]
    template_end = template_pos + template_record.value

    transform = binding.transform
    scale = transform.scale if scale is None else tuple(scale)
    body_values = (
        *position,
        *quaternion,
        *scale,
        *transform.nposition,
        *transform.nrotation,
    )
    if not all(math.isfinite(value) for value in body_values):
        raise ValueError("transform contains a non-finite value")
    body = struct.pack("<16f", *body_values)
    transform_digest_a = hashlib.md5(body).digest()
    transform_digest_b = hashlib.md5(
        struct.pack("<I", 731) + transform_digest_a
    ).digest()

    property_schema, property_record = graph.flat_records[
        binding.property_record_index - 1
    ]
    if property_schema != 438:
        raise FstFormatError(f"{stream.path}: binding does not point to schema 438")
    kine_digest_a = property_record.digest_a
    kine_digest_b = hashlib.md5(
        struct.pack("<I", 438) + kine_digest_a + transform_digest_b
    ).digest()

    node_payload = _encode_scene_node_payload({**binding.node, "name": name})
    root_digest_a = hashlib.md5(node_payload).digest()
    root_digest_b = hashlib.md5(
        b"HNGCLONE\0" + template_record.digest_b + root_digest_a + kine_digest_b
    ).digest()

    transform_index = next(
        index
        for index, (sid, record) in enumerate(graph.flat_records, 1)
        if sid == 731 and record.digest_b == transform.digest_b
    )
    scene_indices = [
        index
        for index, (sid, record) in enumerate(graph.flat_records, 1)
        if sid == 434
        and template_pos <= positions.get(record.digest_b, -1) < template_end
    ]
    if binding.record_index not in scene_indices:
        raise FstFormatError(f"{stream.path}: template root is not first in its subtree")

    # SceneNode serializes both sides of the hierarchy: the model points to
    # its parent, while the parent owns a counted child array.  Splicing the
    # clone into the parent's DFS subtree adds the reference edge, but the
    # cached parent payload must also advertise the extra child.  Otherwise
    # PostLoad classifies the clone as unused and tries to remove a node that
    # was never registered in the parent's SceneNode array.
    payloads = {item.digest: item.payload for item in stream.inline_objects}
    direct = graph.children_by_digest_b.get(template_record.digest_b, ())
    parent_updates: dict[int, tuple[bytes, bytes]] = {}
    for parent_index in direct[: int(binding.node["first"])]:
        if parent_index == 0:
            continue
        parent_schema, parent_record = graph.flat_records[parent_index - 1]
        if parent_schema != 434 or parent_record.digest_a not in payloads:
            raise FstFormatError(f"{stream.path}: SceneNode parent payload is absent")
        parent_node = decode_scene_node(payloads[parent_record.digest_a])
        parent_payload = _encode_scene_node_payload(
            {**parent_node, "second": int(parent_node["second"]) + 1}
        )
        parent_updates[parent_index] = (
            hashlib.md5(parent_payload).digest(),
            parent_payload,
        )

    # old flat index -> (schema, digest_A, digest_B).  Child digest-B values
    # need only be unique stable context keys; the reader does not recompute
    # them.  Their payloads stay byte-identical and are copied by the loader.
    specs: dict[int, tuple[int, bytes, bytes]] = {
        binding.record_index: (434, root_digest_a, root_digest_b),
        binding.property_record_index: (438, kine_digest_a, kine_digest_b),
        transform_index: (731, transform_digest_a, transform_digest_b),
    }
    for index in scene_indices:
        if index == binding.record_index:
            continue
        _sid, record = graph.flat_records[index - 1]
        digest_b = hashlib.md5(
            b"HNGCLONE\1" + record.digest_b + root_digest_b
        ).digest()
        specs[index] = (434, record.digest_a, digest_b)

    existing_b = {record.digest_b for _sid, record in graph.flat_records}
    new_b = [item[2] for item in specs.values()]
    if len(set(new_b)) != len(new_b) or any(value in existing_b for value in new_b):
        raise FstFormatError(f"{stream.path}: cloned contextual digest collision")

    # Insert records with reused digest_A immediately after the existing
    # contiguous block.  LoadStream batches exactly those adjacent records.
    old_flat_index: dict[tuple[int, int], int] = {}
    cursor = 1
    for group in stream.groups:
        for local_index, _record in enumerate(group.records):
            old_flat_index[(group.schema_id, local_index)] = cursor
            cursor += 1

    schedules: dict[int, dict[int, list[int]]] = {}
    for old_index, (sid, digest_a, _digest_b) in specs.items():
        group = next(item for item in stream.groups if item.schema_id == sid)
        matches = [
            local
            for local, record in enumerate(group.records)
            if record.digest_a == digest_a
        ]
        boundary = matches[-1] + 1 if matches else len(group.records)
        if matches and matches != list(range(matches[0], matches[-1] + 1)):
            raise FstFormatError(
                f"{stream.path}: schema {sid} digest-A block is not contiguous"
            )
        schedules.setdefault(sid, {}).setdefault(boundary, []).append(old_index)
    for boundaries in schedules.values():
        for items in boundaries.values():
            items.sort()

    index_map = {0: 0}
    replacement_index: dict[int, int] = {}
    next_index = 1
    for group in stream.groups:
        boundaries = schedules.get(group.schema_id, {})
        for boundary in range(len(group.records) + 1):
            for old_index in boundaries.get(boundary, ()):
                replacement_index[old_index] = next_index
                next_index += 1
            if boundary < len(group.records):
                old_index = old_flat_index[(group.schema_id, boundary)]
                index_map[old_index] = next_index
                next_index += 1
    if set(replacement_index) != set(specs):
        raise FstFormatError(f"{stream.path}: failed to allocate all clone records")

    clone_spans: dict[int, int] = {}

    def emit(start: int) -> tuple[list[int], int]:
        old_index = old_values[start]
        if old_index == 0:
            return [0], start + 1
        _sid, record = graph.flat_records[old_index - 1]
        is_first = positions.get(record.digest_b) == start
        replacement = replacement_index.get(old_index)
        if replacement is None:
            return [index_map[old_index]], start + record.value if is_first else start + 1
        if not is_first:
            return [replacement], start + 1
        end = start + record.value
        emitted = [replacement]
        at = start + 1
        while at < end:
            child, at = emit(at)
            emitted.extend(child)
        if at != end:
            raise FstFormatError(f"{stream.path}: clone subtree misaligns")
        clone_spans[replacement] = len(emitted)
        return emitted, at

    clone, after = emit(template_pos)
    if after != template_end:
        raise FstFormatError(f"{stream.path}: clone did not consume template subtree")
    if set(clone_spans) != set(replacement_index.values()):
        missing = set(replacement_index.values()) - set(clone_spans)
        raise FstFormatError(f"{stream.path}: unexpanded clone records {sorted(missing)}")

    values = [index_map[value] for value in old_values]
    splice = template_end
    values[splice:splice] = clone

    # Materialize groups in the same order used for index allocation.
    provisional_groups: list[ObjectGroup] = []
    for group in stream.groups:
        records: list[ObjectRecord] = []
        boundaries = schedules.get(group.schema_id, {})
        for boundary in range(len(group.records) + 1):
            for old_index in boundaries.get(boundary, ()):
                sid, digest_a, digest_b = specs[old_index]
                records.append(
                    ObjectRecord(
                        digest_a,
                        clone_spans[replacement_index[old_index]],
                        digest_b,
                        0,
                        0,
                    )
                )
            if boundary < len(group.records):
                old_index = old_flat_index[(group.schema_id, boundary)]
                record = group.records[boundary]
                if old_index in parent_updates:
                    parent_digest_a, _parent_payload = parent_updates[old_index]
                    record = ObjectRecord(
                        parent_digest_a,
                        record.value,
                        record.digest_b,
                        0,
                        0,
                    )
                records.append(record)
        provisional_groups.append(ObjectGroup(group.schema_id, tuple(records)))

    span_by_b = {
        record.digest_b: record.value for _sid, record in graph.flat_records
    }
    final_spans = {
        digest_b: span + len(clone) if pos < splice < pos + span else span
        for digest_b, pos in positions.items()
        for span in (span_by_b[digest_b],)
    }
    for old_index, (_sid, _digest_a, digest_b) in specs.items():
        final_spans[digest_b] = clone_spans[replacement_index[old_index]]
    final_groups = tuple(
        ObjectGroup(
            group.schema_id,
            tuple(
                ObjectRecord(
                    record.digest_a,
                    final_spans[record.digest_b],
                    record.digest_b,
                    0,
                    0,
                )
                for record in group.records
            ),
        )
        for group in provisional_groups
    )

    flat_new = tuple(
        (group.schema_id, record) for group in final_groups for record in group.records
    )
    seen: set[bytes] = set()

    def visit(at: int) -> int:
        index = values[at]
        if index == 0:
            return at + 1
        _sid, record = flat_new[index - 1]
        if record.digest_b in seen:
            return at + 1
        seen.add(record.digest_b)
        end = at + record.value
        cursor2 = at + 1
        while cursor2 < end:
            cursor2 = visit(cursor2)
        if cursor2 != end:
            raise FstFormatError(f"{stream.path}: rebuilt DFS stream misaligns")
        return cursor2

    at = 0
    while at < len(values):
        at = visit(at)
    if len(seen) != len(flat_new):
        raise FstFormatError(
            f"{stream.path}: rebuilt stream covers {len(seen)}/{len(flat_new)} records"
        )

    original_entries, _reference_offset = parse_cache_entries(stream)
    original_by_key: dict[tuple[int, bytes], list[CacheEntry]] = {}
    for entry in original_entries:
        original_by_key.setdefault((entry.schema_id, entry.digest_a), []).append(entry)
    custom_payloads: dict[tuple[int, bytes], tuple[bytes, bytes]] = {
        (434, root_digest_a): (node_payload, root_digest_b),
        (731, transform_digest_a): (body, transform_digest_b),
    }
    for old_index, (digest_a, payload) in parent_updates.items():
        _sid, old_record = graph.flat_records[old_index - 1]
        custom_payloads[(434, digest_a)] = (payload, old_record.digest_b)
    seen_keys: set[tuple[int, bytes]] = set()
    new_entries: list[CacheEntry] = []
    for group in final_groups:
        for record in group.records:
            key = (group.schema_id, record.digest_a)
            if key in seen_keys:
                continue
            if key in custom_payloads:
                payload, digest_b = custom_payloads[key]
                new_entries.append(
                    CacheEntry(
                        0,
                        len(payload),
                        payload,
                        group.schema_id,
                        record.digest_a,
                        digest_b,
                    )
                )
                seen_keys.add(key)
                continue
            originals = original_by_key.get(key)
            if originals is None:
                raise FstFormatError(
                    f"{stream.path}: cache payload is absent for schema {group.schema_id}"
                )
            for original in originals:
                new_entries.append(CacheEntry(
                    0,
                    original.size,
                    original.payload,
                    group.schema_id,
                    record.digest_a,
                    record.digest_b,
                ))
            if any(original.size >= 0 for original in originals):
                seen_keys.add(key)

    reference_bytes = b"".join(encode_uleb32(value) for value in values)
    decompressed = rebuild_decompressed(
        stream,
        None,
        tuple(new_entries),
        0,
        groups=final_groups,
        reference_bytes=reference_bytes,
    )
    return b"FST\0" + zlib.compress(decompressed)


def remove_cloned_scene_tree(
    stream: FstStream,
    graph: ReferenceGraph,
    binding: NodeTransformBinding,
    *,
    allow_shipped: bool = False,
    relocate_shared: bool = False,
) -> bytes:
    """Remove one complete SceneNode subtree and rebuild the object stream.

    By default this is limited to compiler-created clones.  Their owned
    SceneNodes/property/transform records occur only in the clone subtree,
    while every shared resource dependency was first expanded by the original
    template.  ``allow_shipped`` is for callers that deliberately opt into the
    same strict first-occurrence audit for an official root.  When
    ``relocate_shared`` is true, the outermost shared expansions that would be
    lost are moved onto surviving back-reference sites before the target is
    removed.
    """
    if (
        not allow_shipped
        and not str(binding.node["name"]).startswith("Unity_")
    ):
        raise FstFormatError("only compiler-created Unity_* roots are removable")
    positions = _reference_positions(graph)
    target_record = graph.flat_records[binding.record_index - 1][1]
    target_pos = positions.get(binding.node_digest_b)
    if target_pos is None:
        raise FstFormatError("removal target has no first reference occurrence")
    target_end = target_pos + target_record.value
    old_values = list(graph.relation_values)
    if target_end > len(old_values):
        raise FstFormatError("removal target subtree overruns relation stream")
    removed_span = target_end - target_pos
    values_without_target = old_values[:target_pos] + old_values[target_end:]
    preliminary_kept = {value for value in values_without_target if value}

    # A record first expanded inside the deleted subtree cannot survive only as
    # a later one-word back-reference.  Compiler clones never have this shape.
    blockers = []
    for index in preliminary_kept:
        _sid, record = graph.flat_records[index - 1]
        first = positions.get(record.digest_b)
        if first is not None and target_pos <= first < target_end:
            blockers.append((first, index, record.value))
    if blockers and not relocate_shared:
        raise FstFormatError(
            "deletion would remove the first occurrence of a shared record"
        )
    if binding.record_index in preliminary_kept:
        raise FstFormatError("deletion target itself is referenced outside its subtree")

    # Nested blockers travel with their outermost expanded ancestor.  Replace a
    # surviving one-word reference to each such ancestor with the original full
    # DFS expansion.  This changes only serialization order, not graph identity.
    outer_blockers = []
    for first, index, span in sorted(blockers):
        if any(
            outer_first <= first < outer_first + outer_span
            for outer_first, _outer_index, outer_span in outer_blockers
        ):
            continue
        outer_blockers.append((first, index, span))
    replacements: dict[int, tuple[int, ...]] = {}
    relocation_deltas: list[tuple[int, int]] = []
    for first, index, span in outer_blockers:
        expansion = tuple(old_values[first:first + span])
        if not expansion or expansion[0] != index:
            raise FstFormatError("shared record expansion is malformed")
        # A token that is a back-reference at the source must remain a
        # back-reference at the destination.  Prefer a surviving occurrence
        # after every dependency whose first expansion lives outside the moved
        # block; otherwise moving the block earlier would make that one-word
        # token become an under-expanded first occurrence.
        external_firsts = []
        for child_index in set(expansion):
            if child_index == 0:
                continue
            _child_schema, child_record = graph.flat_records[child_index - 1]
            child_first = positions.get(child_record.digest_b)
            if (
                child_first is not None
                and not (first <= child_first < first + span)
            ):
                external_firsts.append(child_first)
        minimum_site = max(external_firsts, default=-1)
        surviving_sites = [
            at for at, value in enumerate(old_values)
            if value == index
            and not (target_pos <= at < target_end)
            and at > minimum_site
        ]
        if not surviving_sites:
            raise FstFormatError(
                "shared record has no order-safe surviving relocation site"
            )
        site = surviving_sites[0]
        replacements[site] = expansion
        relocation_deltas.append((site, span - 1))

    values_old_indices = []
    at = 0
    while at < len(old_values):
        if at == target_pos:
            at = target_end
            continue
        replacement = replacements.get(at)
        if replacement is not None:
            values_old_indices.extend(replacement)
        else:
            values_old_indices.append(old_values[at])
        at += 1
    kept_indices = {value for value in values_old_indices if value}

    payloads = {item.digest: item.payload for item in stream.inline_objects}
    parent_updates: dict[int, tuple[bytes, bytes]] = {}
    direct = graph.children_by_digest_b.get(binding.node_digest_b, ())
    for parent_index in direct[: int(binding.node["first"])]:
        if parent_index == 0:
            continue
        parent_schema, parent_record = graph.flat_records[parent_index - 1]
        if parent_schema != 434 or parent_record.digest_a not in payloads:
            raise FstFormatError("SceneNode parent payload is absent")
        parent_node = decode_scene_node(payloads[parent_record.digest_a])
        if int(parent_node["second"]) <= 0:
            raise FstFormatError("SceneNode parent child count is already zero")
        parent_payload = _encode_scene_node_payload(
            {**parent_node, "second": int(parent_node["second"]) - 1}
        )
        parent_updates[parent_index] = (
            hashlib.md5(parent_payload).digest(), parent_payload
        )

    span_by_b = {
        record.digest_b: record.value for _sid, record in graph.flat_records
    }
    final_spans = {}
    for digest_b, pos in positions.items():
        span = span_by_b[digest_b]
        if pos < target_pos < pos + span:
            span -= removed_span
        for site, delta in relocation_deltas:
            if pos < site < pos + span_by_b[digest_b]:
                span += delta
        final_spans[digest_b] = span

    index_map = {0: 0}
    next_index = 1
    for old_index in range(1, len(graph.flat_records) + 1):
        if old_index in kept_indices:
            index_map[old_index] = next_index
            next_index += 1
    values = [index_map[value] for value in values_old_indices]

    final_groups = []
    old_index = 1
    for group in stream.groups:
        records = []
        for record in group.records:
            if old_index in kept_indices:
                digest_a = record.digest_a
                if old_index in parent_updates:
                    digest_a = parent_updates[old_index][0]
                records.append(ObjectRecord(
                    digest_a,
                    final_spans[record.digest_b],
                    record.digest_b,
                    0,
                    0,
                ))
            old_index += 1
        final_groups.append(ObjectGroup(group.schema_id, tuple(records)))
    final_groups = tuple(final_groups)

    # Validate DFS spans and record coverage before serializing anything.
    flat_new = tuple(
        (group.schema_id, record) for group in final_groups for record in group.records
    )
    seen_b = set()

    def visit(at: int) -> int:
        index = values[at]
        if index == 0:
            return at + 1
        _sid, record = flat_new[index - 1]
        if record.digest_b in seen_b:
            return at + 1
        seen_b.add(record.digest_b)
        end = at + record.value
        cursor = at + 1
        while cursor < end:
            cursor = visit(cursor)
        if cursor != end:
            raise FstFormatError(
                "deleted DFS stream misaligns at "
                f"relation {at}, record {index}, expected {end}, got {cursor}; "
                f"target={target_pos}:{target_end}, relocations={relocation_deltas}"
            )
        return cursor

    at = 0
    while at < len(values):
        at = visit(at)
    if len(seen_b) != len(flat_new):
        raise FstFormatError(
            f"deleted stream covers {len(seen_b)}/{len(flat_new)} records"
        )

    original_entries, _reference_offset = parse_cache_entries(stream)
    original_by_key: dict[tuple[int, bytes], list[CacheEntry]] = {}
    for entry in original_entries:
        original_by_key.setdefault((entry.schema_id, entry.digest_a), []).append(entry)
    custom_payloads = {
        (434, digest_a): (payload, graph.flat_records[index - 1][1].digest_b)
        for index, (digest_a, payload) in parent_updates.items()
    }
    seen_keys = set()
    new_entries = []
    for group in final_groups:
        for record in group.records:
            key = (group.schema_id, record.digest_a)
            if key in seen_keys:
                continue
            if key in custom_payloads:
                payload, digest_b = custom_payloads[key]
                new_entries.append(CacheEntry(
                    0, len(payload), payload, group.schema_id, record.digest_a, digest_b
                ))
                seen_keys.add(key)
                continue
            originals = original_by_key.get(key)
            if originals is None:
                raise FstFormatError(
                    f"cache payload is absent for schema {group.schema_id}"
                )
            for original in originals:
                new_entries.append(CacheEntry(
                    0,
                    original.size,
                    original.payload,
                    group.schema_id,
                    record.digest_a,
                    record.digest_b,
                ))
            if any(original.size >= 0 for original in originals):
                seen_keys.add(key)

    reference_bytes = b"".join(encode_uleb32(value) for value in values)
    decompressed = rebuild_decompressed(
        stream,
        None,
        tuple(new_entries),
        0,
        groups=final_groups,
        reference_bytes=reference_bytes,
    )
    return b"FST\0" + zlib.compress(decompressed)


def remove_scene_trees(
    stream: FstStream,
    graph: ReferenceGraph,
    bindings: Iterable[NodeTransformBinding],
    *,
    allow_shipped: bool = False,
) -> bytes:
    """Remove multiple SceneNode roots by reserializing the surviving graph.

    Unlike the single-root relocation prototype, this does not copy serialized
    byte ranges.  It removes the hierarchy edges and walks the surviving graph
    in DFS order again, so the first surviving reference expands naturally and
    every later occurrence is emitted as a one-word back-reference.
    """
    targets = tuple(bindings)
    if not targets:
        return b"FST\0" + zlib.compress(stream.decompressed)
    if not allow_shipped and any(
        not str(binding.node["name"]).startswith("Unity_")
        for binding in targets
    ):
        raise FstFormatError("only compiler-created Unity_* roots are removable")
    target_indices = {binding.record_index for binding in targets}
    if len(target_indices) != len(targets):
        raise FstFormatError("duplicate SceneNode removal target")

    children = {
        digest_b: list(values)
        for digest_b, values in graph.children_by_digest_b.items()
    }
    roots = list(graph.roots)
    parent_decrements: dict[int, int] = {}
    payloads = {item.digest: item.payload for item in stream.inline_objects}

    # Expand logical roots to every serialized SceneNode descendant.  LOD,
    # collision, and state nodes often have their first expansion elsewhere in
    # the stream, but their explicit parent pointer still identifies them as
    # owned by the removed root.
    changed = True
    while changed:
        changed = False
        for child_index, (schema_id, record) in enumerate(graph.flat_records, 1):
            if (
                child_index in target_indices
                or schema_id != 434
                or record.digest_a not in payloads
            ):
                continue
            node = decode_scene_node(payloads[record.digest_a])
            direct = children.get(record.digest_b, ())
            parents = {
                value for value in direct[: int(node["first"])] if value
            }
            if parents.intersection(target_indices):
                target_indices.add(child_index)
                changed = True

    for target_index in sorted(target_indices):
        removed_edges = 0
        # SceneNode stores parent links on the child and a counted child array
        # on the parent.  Contextual records may appear under more than one
        # serialized parent, so remove every occurrence in a SceneNode child
        # slice rather than trusting only the first parent pointer.
        for parent_index, (parent_schema, parent_record) in enumerate(
            graph.flat_records, 1
        ):
            if parent_schema != 434 or parent_record.digest_a not in payloads:
                continue
            parent_children = children.get(parent_record.digest_b)
            if parent_children is None:
                continue
            parent_node = decode_scene_node(payloads[parent_record.digest_a])
            child_start = int(parent_node["first"])
            child_end = child_start + int(parent_node["second"])
            positions_to_remove = [
                position for position in range(child_start, min(child_end, len(parent_children)))
                if parent_children[position] == target_index
            ]
            if not positions_to_remove:
                continue
            for position in reversed(positions_to_remove):
                del parent_children[position]
            if parent_index not in target_indices:
                parent_decrements[parent_index] = (
                    parent_decrements.get(parent_index, 0) + len(positions_to_remove)
                )
            removed_edges += len(positions_to_remove)
        while target_index in roots:
            roots.remove(target_index)
            removed_edges += 1
        # A requested contextual root may already be a descendant of another
        # requested root or may exist only through fixed reference slots.  The
        # final reachability check below is authoritative; requiring every
        # contextual target to own a counted hierarchy edge rejects valid
        # multi-instance batches.

    # References outside counted SceneNode child arrays have fixed member
    # arity (parent pointers, state links, optional object members).  Preserve
    # their serialized slot and replace only the removed object reference with
    # the format's null index.  Counted child entries above are removed rather
    # than nulled because their payload count is updated in lockstep.
    for child_values in children.values():
        for position, value in enumerate(child_values):
            if value in target_indices:
                child_values[position] = 0

    parent_updates: dict[int, tuple[bytes, bytes]] = {}
    for parent_index, decrement in parent_decrements.items():
        parent_schema, parent_record = graph.flat_records[parent_index - 1]
        if parent_schema != 434 or parent_record.digest_a not in payloads:
            raise FstFormatError("SceneNode parent payload is absent")
        parent_node = decode_scene_node(payloads[parent_record.digest_a])
        child_count = int(parent_node["second"])
        if child_count < decrement:
            raise FstFormatError("SceneNode parent child count would underflow")
        parent_payload = _encode_scene_node_payload(
            {**parent_node, "second": child_count - decrement}
        )
        parent_updates[parent_index] = (
            hashlib.md5(parent_payload).digest(), parent_payload
        )

    old_values: list[int] = []
    spans: dict[int, int] = {}
    seen_b: set[bytes] = set()

    def emit(index: int) -> None:
        start = len(old_values)
        old_values.append(index)
        if index == 0:
            return
        _schema_id, record = graph.flat_records[index - 1]
        if record.digest_b in seen_b:
            return
        seen_b.add(record.digest_b)
        for child in children.get(record.digest_b, ()):
            emit(child)
        spans[index] = len(old_values) - start

    for root in roots:
        emit(root)

    reachable = {index for index in old_values if index}
    leaked = target_indices.intersection(reachable)
    if leaked:
        raise FstFormatError(
            "removal target is still referenced outside its hierarchy edge: "
            + ", ".join(str(index) for index in sorted(leaked))
        )

    index_map = {0: 0}
    next_index = 1
    for old_index in range(1, len(graph.flat_records) + 1):
        if old_index in reachable:
            index_map[old_index] = next_index
            next_index += 1
    values = [index_map[index] for index in old_values]

    final_groups = []
    old_index = 1
    for group in stream.groups:
        records = []
        for record in group.records:
            if old_index in reachable:
                digest_a = parent_updates.get(
                    old_index, (record.digest_a, b"")
                )[0]
                records.append(ObjectRecord(
                    digest_a,
                    spans[old_index],
                    record.digest_b,
                    0,
                    0,
                ))
            old_index += 1
        final_groups.append(ObjectGroup(group.schema_id, tuple(records)))
    final_groups = tuple(final_groups)

    original_entries, _reference_offset = parse_cache_entries(stream)
    original_by_key: dict[tuple[int, bytes], list[CacheEntry]] = {}
    for entry in original_entries:
        original_by_key.setdefault((entry.schema_id, entry.digest_a), []).append(entry)
    custom_payloads = {
        (434, digest_a): (payload, graph.flat_records[index - 1][1].digest_b)
        for index, (digest_a, payload) in parent_updates.items()
    }
    seen_keys = set()
    new_entries = []
    for group in final_groups:
        for record in group.records:
            key = (group.schema_id, record.digest_a)
            if key in seen_keys:
                continue
            if key in custom_payloads:
                payload, digest_b = custom_payloads[key]
                new_entries.append(CacheEntry(
                    0, len(payload), payload, group.schema_id, record.digest_a, digest_b
                ))
                seen_keys.add(key)
                continue
            originals = original_by_key.get(key)
            if originals is None:
                raise FstFormatError(
                    f"cache payload is absent for schema {group.schema_id}"
                )
            for original in originals:
                new_entries.append(CacheEntry(
                    0,
                    original.size,
                    original.payload,
                    group.schema_id,
                    record.digest_a,
                    record.digest_b,
                ))
            if any(original.size >= 0 for original in originals):
                seen_keys.add(key)

    reference_bytes = b"".join(encode_uleb32(value) for value in values)
    decompressed = rebuild_decompressed(
        stream,
        None,
        tuple(new_entries),
        0,
        groups=final_groups,
        reference_bytes=reference_bytes,
    )
    return b"FST\0" + zlib.compress(decompressed)


def summary(stream: FstStream) -> dict[str, object]:
    result: dict[str, object] = {
        "path": str(stream.path),
        "decompressed_size": len(stream.decompressed),
        "wrapper_md5": stream.decompressed[:16].hex(),
        "payload_size": len(stream.payload),
        "schema_digest": stream.schema_digest.hex(),
        "feature_value": stream.feature_value,
        "group_count": len(stream.groups),
        "record_count": sum(len(group.records) for group in stream.groups),
        "group_table_end": stream.group_table_end,
        "inline_object_count": len(stream.inline_objects),
        "inline_sentinel_count": stream.inline_sentinels,
        "verified_inline_scan_end": stream.inline_scan_end,
    }
    scene_nodes = []
    for item in iter_schema_objects(stream, 434):
        try:
            decoded = decode_scene_node(item.payload)
        except FstFormatError:
            continue
        if "sandbag" in str(decoded["name"]).lower():
            decoded["digest"] = item.digest.hex()
            scene_nodes.append(decoded)
    result["sandbag_scene_nodes"] = scene_nodes
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fst", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--clone-record-index", type=int)
    parser.add_argument("--clone-name")
    parser.add_argument("--clone-position", type=float, nargs=3, metavar=("X", "Y", "Z"))
    parser.add_argument("--output", type=Path)
    ns = parser.parse_args()

    stream = parse_fst(ns.fst)
    schema = parse_rs03(ns.schema) if ns.schema else None
    result = summary(stream)
    if schema is not None:
        result["schema_class_count"] = len(schema.classes)
        result["schema_digest_matches_filename"] = (
            ns.schema.stem.lower() == stream.schema_digest.hex()
        )
        for class_name in ("ZLocalKinematics", "ZKinematics", "ZSceneNode", "ZPropertySet"):
            item = schema.by_name(class_name)
            result.setdefault("selected_schema_ids", {})[class_name] = item.schema_id
    transforms = locate_kinematics(stream)
    property_map = map_kinematics_property_sets(stream, transforms)
    graph = parse_reference_graph(stream, transforms)
    bindings = bind_scene_nodes_to_transforms(stream, graph, property_map)
    if ns.clone_record_index is not None:
        if schema is None:
            parser.error("--clone-record-index requires --schema")
        if ns.clone_name is None or ns.clone_position is None or ns.output is None:
            parser.error(
                "cloning requires --clone-name, --clone-position, and --output"
            )
        try:
            template = next(
                item
                for item in bindings
                if item.record_index == ns.clone_record_index
            )
        except StopIteration:
            parser.error(f"record {ns.clone_record_index} is not a bound SceneNode")
        encoded = insert_cloned_instance(
            stream,
            schema,
            graph,
            template,
            name=ns.clone_name,
            position=tuple(ns.clone_position),
            quaternion=template.transform.quaternion,
        )
        ns.output.write_bytes(encoded)
        print(f"wrote {ns.output} ({len(encoded)} bytes)")
        return 0
    result["kinematics_transform_count"] = len(transforms)
    result["kinematics_property_mapping_count"] = len(property_map)
    result["kinematics_data_start"] = transforms[0].offset
    result["kinematics_data_end"] = max(item.end_offset for item in transforms)
    result["relation_stream_offset"] = graph.relation_offset
    result["relation_count"] = len(graph.relation_values)
    result["reference_root_count"] = len(graph.roots)
    result["scene_node_transform_binding_count"] = len(bindings)
    result["sandbag_model_bindings"] = [
        {
            "record_index": item.record_index,
            "name": item.node["name"],
            "position": item.transform.position,
            "quaternion": item.transform.quaternion,
            "scale": item.transform.scale,
            "transform_offset": item.transform.offset,
            "transform_digest": item.transform.digest_a.hex(),
        }
        for item in bindings
        if item.node["node_type"] == "#model"
        and "sandbag" in str(item.node["name"]).lower()
    ]

    if ns.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for key, value in result.items():
            if key != "sandbag_scene_nodes":
                print(f"{key}: {value}")
        print(f"sandbag_scene_nodes: {len(result['sandbag_scene_nodes'])}")
        for node in result["sandbag_scene_nodes"]:
            print(f"  {node['digest']}  {node['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
