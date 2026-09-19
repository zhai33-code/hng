#!/usr/bin/env python3
"""Read HnG's Unity reconstruction scenes into a compiler-neutral manifest.

The project stores most placed objects as Unity PrefabInstance YAML records.
This frontend deliberately extracts only data that is explicit in the project;
it does not invent missing HnG object/property metadata.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


BLOCK_RE = re.compile(r"(?=^--- !u!1001 &)", re.MULTILINE)
OBJECT_BLOCK_RE = re.compile(r"(?=^--- !u!)", re.MULTILINE)
OBJECT_HEADER_RE = re.compile(r"^--- !u!(\d+) &(-?\d+)", re.MULTILINE)
HEADER_RE = re.compile(r"^--- !u!1001 &(-?\d+)", re.MULTILINE)
SOURCE_RE = re.compile(r"m_SourcePrefab: \{fileID: [^,]+, guid: ([0-9a-f]{32}), type: 3\}")
PARENT_RE = re.compile(r"m_TransformParent: \{fileID: (-?\d+)\}")
MOD_RE = re.compile(
    r"- target: \{fileID: (-?\d+), guid: ([0-9a-f]{32}), type: 3\}\s+"
    r"propertyPath: ([^\r\n]+)\s+value:([^\r\n]*)\s+"
    r"objectReference: \{[^\r\n]*\}",
    re.MULTILINE,
)
GUID_RE = re.compile(r"^guid: ([0-9a-f]{32})$", re.MULTILINE)


class UnitySceneError(ValueError):
    pass


@dataclass(frozen=True)
class SceneInstance:
    instance_file_id: int
    parent_file_id: int
    parent_name: str
    prefab_guid: str
    prefab_path: str
    name: str
    unity_position: tuple[float, float, float]
    unity_quaternion: tuple[float, float, float, float]
    unity_scale: tuple[float, float, float]
    hng_position: tuple[float, float, float]
    hng_quaternion: tuple[float, float, float, float]
    hng_scale: tuple[float, float, float]


def build_guid_index(assets: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for meta in assets.rglob("*.meta"):
        try:
            text = meta.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        match = GUID_RE.search(text)
        if match:
            asset = meta.with_suffix("")
            result[match.group(1)] = asset.relative_to(assets.parent).as_posix()
    return result


def _float(values: dict[str, str], key: str, default: float) -> float:
    raw = values.get(key)
    if raw is None or not raw.strip():
        return default
    return float(raw.strip())


def _select_transform(modifications: dict[int, dict[str, str]]) -> dict[str, str] | None:
    candidates = []
    for fields in modifications.values():
        axes = sum(f"m_LocalPosition.{axis}" in fields for axis in "xyz")
        rotation = sum(f"m_LocalRotation.{axis}" in fields for axis in "xyzw")
        if axes == 3:
            candidates.append((rotation, len(fields), fields))
    return max(candidates, default=(0, 0, None), key=lambda item: item[:2])[2]


def parse_scene(scene: Path | str, assets: Path | str) -> list[SceneInstance]:
    scene = Path(scene)
    assets = Path(assets)
    guid_index = build_guid_index(assets)
    text = scene.read_text(encoding="utf-8")
    game_object_names: dict[int, str] = {}
    transform_game_objects: dict[int, int] = {}
    for object_block in OBJECT_BLOCK_RE.split(text):
        object_header = OBJECT_HEADER_RE.search(object_block)
        if not object_header:
            continue
        class_id, object_id = map(int, object_header.groups())
        if class_id == 1:
            name = re.search(r"^  m_Name: (.*)$", object_block, re.MULTILINE)
            if name:
                game_object_names[object_id] = name.group(1).strip()
        elif class_id == 4:
            game_object = re.search(
                r"^  m_GameObject: \{fileID: (-?\d+)\}$",
                object_block,
                re.MULTILINE,
            )
            if game_object:
                transform_game_objects[object_id] = int(game_object.group(1))
    result: list[SceneInstance] = []
    for block in BLOCK_RE.split(text):
        header = HEADER_RE.search(block)
        source = SOURCE_RE.search(block)
        if not header or not source:
            continue
        guid = source.group(1)
        grouped: dict[int, dict[str, str]] = {}
        names: list[str] = []
        for file_id, target_guid, field, value in MOD_RE.findall(block):
            if target_guid != guid:
                continue
            field = field.strip().strip("'")
            value = value.strip()
            grouped.setdefault(int(file_id), {})[field] = value
            if field == "m_Name" and value:
                names.append(value)
        transform = _select_transform(grouped)
        if transform is None:
            continue
        prefab_path = guid_index.get(guid, "")
        name = names[0] if names else (Path(prefab_path).stem if prefab_path else guid)
        position = tuple(_float(transform, f"m_LocalPosition.{axis}", 0.0) for axis in "xyz")
        quaternion = tuple(_float(transform, f"m_LocalRotation.{axis}", 0.0) for axis in "xyzw")
        scale = tuple(_float(transform, f"m_LocalScale.{axis}", 1.0) for axis in "xyz")
        parent = PARENT_RE.search(block)
        # The serialized reconstruction scenes use metres and a reflected Z
        # axis.  This mapping is validated against all twelve Mill instances
        # of FenceSandbagA_BLowStraight_4m: each maps uniquely to the matching
        # FST SceneNode within 0.008 game units and 2.2e-5 quaternion distance.
        # PrefabSpawn.cs is an older one-off runtime helper and does not
        # describe the coordinates stored in these .unity scene files.
        hng_position = (
            position[0] * 10.0,
            position[1] * 10.0,
            -position[2] * 10.0,
        )
        hng_quaternion = (
            -quaternion[0],
            -quaternion[1],
            quaternion[2],
            quaternion[3],
        )
        hng_scale = scale
        parent_file_id = int(parent.group(1)) if parent else 0
        parent_name = game_object_names.get(
            transform_game_objects.get(parent_file_id, 0), ""
        )
        result.append(
            SceneInstance(
                instance_file_id=int(header.group(1)),
                parent_file_id=parent_file_id,
                parent_name=parent_name,
                prefab_guid=guid,
                prefab_path=prefab_path,
                name=name,
                unity_position=position,
                unity_quaternion=quaternion,
                unity_scale=scale,
                hng_position=hng_position,
                hng_quaternion=hng_quaternion,
                hng_scale=hng_scale,
            )
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", type=Path)
    parser.add_argument("--assets", type=Path, default=Path(r"D:\hng-map\Project\Assets"))
    parser.add_argument("--output", type=Path)
    ns = parser.parse_args()
    instances = parse_scene(ns.scene, ns.assets)
    manifest = {
        "format": "hng-unity-scene-v1",
        "source_scene": str(ns.scene.resolve()),
        "instance_count": len(instances),
        "instances": [asdict(item) for item in instances],
    }
    encoded = json.dumps(manifest, indent=2, ensure_ascii=False)
    if ns.output:
        ns.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
