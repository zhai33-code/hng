#!/usr/bin/env python3
"""Plan safe Unity-to-FST changes using a frozen identity link manifest."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from fst_inspect import (
    bind_scene_nodes_to_transforms,
    locate_kinematics,
    map_kinematics_property_sets,
    parse_fst,
    parse_reference_graph,
    patch_kinematics_transform,
)
from hng_unity_scene import parse_scene


def quaternion_distance(a, b):
    return min(math.dist(a, b), math.dist(a, tuple(-value for value in b)))


def plan_changes(scene: Path, assets: Path, link_path: Path) -> dict[str, object]:
    manifest = json.loads(link_path.read_text(encoding="utf-8"))
    baseline = {item["instance_file_id"]: item for item in manifest["links"]}
    current_list = parse_scene(scene, assets)
    current = {item.instance_file_id: item for item in current_list}
    if len(current) != len(current_list):
        raise ValueError("Unity scene contains duplicate PrefabInstance file IDs")

    updates = []
    unsupported_updates = []
    deleted = []
    for instance_id, old in baseline.items():
        item = current.get(instance_id)
        if item is None:
            if old["status"] == "matched":
                deleted.append(instance_id)
            continue
        changed = (
            math.dist(item.hng_position, old["fst_position"]) > 1e-5
            or quaternion_distance(item.hng_quaternion, old["fst_quaternion"]) > 1e-6
            or math.dist(item.hng_scale, old["fst_scale"]) > 1e-6
            or item.parent_file_id != old["parent_file_id"]
        )
        if not changed:
            continue
        change = {
            "instance_file_id": instance_id,
            "prefab_guid": item.prefab_guid,
            "parent_name": item.parent_name,
            "old_position": old["fst_position"],
            "new_position": item.hng_position,
            "old_quaternion": old["fst_quaternion"],
            "new_quaternion": item.hng_quaternion,
            "old_scale": old["fst_scale"],
            "new_scale": item.hng_scale,
            "record_indices": [value["record_index"] for value in old["candidates"]],
        }
        if (
            old["status"] == "matched"
            and item.parent_file_id == old["parent_file_id"]
            and old["parent_name"] == "O1"
            and item.parent_name == "O1"
        ):
            updates.append(change)
        else:
            change["reason"] = (
                "reparented" if item.parent_file_id != old["parent_file_id"]
                else f"baseline status/parent is {old['status']}/{old['parent_name']}"
            )
            unsupported_updates.append(change)

    known_guids = {
        item["prefab_guid"]
        for item in manifest["links"]
        if item["status"] == "matched" and item["parent_name"] == "O1"
    }
    added = []
    unsupported_added = []
    for instance_id in sorted(set(current) - set(baseline)):
        item = current[instance_id]
        value = {
            "instance_file_id": instance_id,
            "prefab_guid": item.prefab_guid,
            "prefab_path": item.prefab_path,
            "parent_name": item.parent_name,
            "position": item.hng_position,
            "quaternion": item.hng_quaternion,
            "scale": item.hng_scale,
        }
        if item.parent_name == "O1" and item.prefab_guid in known_guids:
            added.append(value)
        else:
            value["reason"] = "no verified O1 template for prefab GUID"
            unsupported_added.append(value)

    return {
        "format": "hng-unity-fst-plan-v1",
        "source_scene": str(scene.resolve()),
        "identity_manifest": str(link_path.resolve()),
        "summary": {
            "updates": len(updates),
            "added": len(added),
            "deleted": len(deleted),
            "unsupported_updates": len(unsupported_updates),
            "unsupported_added": len(unsupported_added),
        },
        "updates": updates,
        "added": added,
        "deleted_instance_file_ids": deleted,
        "unsupported_updates": unsupported_updates,
        "unsupported_added": unsupported_added,
    }


def _load_bindings(path: Path):
    stream = parse_fst(path)
    transforms = locate_kinematics(stream)
    graph = parse_reference_graph(stream, transforms)
    properties = map_kinematics_property_sets(stream, transforms)
    bindings = bind_scene_nodes_to_transforms(stream, graph, properties)
    return stream, graph, {item.record_index: item for item in bindings}


def apply_update_plan(base_fst: Path, plan: dict[str, object], output: Path) -> None:
    """Apply only audited O1 transform updates; additions/deletions stay blocked."""
    summary = plan["summary"]
    blocked = (
        summary["added"]
        + summary["deleted"]
        + summary["unsupported_updates"]
        + summary["unsupported_added"]
    )
    if blocked:
        raise ValueError(f"plan contains {blocked} non-update operations")
    encoded = base_fst.read_bytes()
    output.write_bytes(encoded)
    for update in plan["updates"]:
        record_indices = update["record_indices"]
        stream, graph, bindings = _load_bindings(output)
        originals = [bindings[index].transform for index in record_indices]
        anchor = originals[0]
        for index, original in zip(record_indices, originals):
            # Context twins deliberately differ by a few float ULPs. Preserve
            # their component deltas so schema-731 digest_B values stay unique.
            position = tuple(
                update["new_position"][axis]
                + original.position[axis]
                - anchor.position[axis]
                for axis in range(3)
            )
            quaternion = tuple(
                update["new_quaternion"][axis]
                + original.quaternion[axis]
                - anchor.quaternion[axis]
                for axis in range(4)
            )
            scale = tuple(
                update["new_scale"][axis]
                + original.scale[axis]
                - anchor.scale[axis]
                for axis in range(3)
            )
            stream, graph, bindings = _load_bindings(output)
            encoded = patch_kinematics_transform(
                stream,
                graph,
                bindings[index],
                position=position,
                quaternion=quaternion,
                scale=scale,
            )
            output.write_bytes(encoded)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", type=Path)
    parser.add_argument("link_manifest", type=Path)
    parser.add_argument("--assets", type=Path, default=Path(r"D:\hng-map\Project\Assets"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-fst", type=Path)
    parser.add_argument("--fst-output", type=Path)
    ns = parser.parse_args()
    result = plan_changes(ns.scene, ns.assets, ns.link_manifest)
    ns.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if ns.base_fst or ns.fst_output:
        if not ns.base_fst or not ns.fst_output:
            parser.error("--base-fst and --fst-output must be used together")
        apply_update_plan(ns.base_fst, result, ns.fst_output)
        print(f"wrote {ns.fst_output}")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
