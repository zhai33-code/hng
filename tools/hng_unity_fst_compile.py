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
    normalize_digest_a_blocks,
    parse_fst,
    parse_reference_graph,
    patch_kinematics_transform,
    insert_cloned_instance,
)
from hng_unity_scene import parse_scene


def quaternion_distance(a, b):
    return min(math.dist(a, b), math.dist(a, tuple(-value for value in b)))


def quaternion_multiply(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quaternion_rotate(quaternion, vector):
    conjugate = (-quaternion[0], -quaternion[1], -quaternion[2], quaternion[3])
    rotated = quaternion_multiply(
        quaternion_multiply(quaternion, (*vector, 0.0)), conjugate
    )
    return rotated[:3]


def compose_transform(position, quaternion, scale, relative):
    local_position = tuple(
        relative["relative_position"][index] * scale[index] for index in range(3)
    )
    rotated = quaternion_rotate(quaternion, local_position)
    return (
        tuple(position[index] + rotated[index] for index in range(3)),
        quaternion_multiply(quaternion, relative["relative_quaternion"]),
        tuple(scale[index] * relative["relative_scale"][index] for index in range(3)),
    )


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
            if old["status"] in {"matched", "matched_offset"}:
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
            "targets": old["candidates"],
            "record_indices": [value["record_index"] for value in old["candidates"]],
        }
        if (
            old["status"] in {"matched", "matched_offset"}
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

    known_templates = {
        item["prefab_guid"]: item
        for item in manifest["links"]
        if item["status"] in {"matched", "matched_offset"}
        and item["parent_name"] == "O1"
        and item.get("addition_supported", True)
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
        if item.parent_name == "O1" and item.prefab_guid in known_templates:
            template = known_templates[item.prefab_guid]
            representatives = {}
            for candidate in template["candidates"]:
                digest = candidate.get(
                    "transform_digest_b", candidate["node_digest_b"]
                )
                previous = representatives.get(digest)
                if previous is None or (
                    candidate.get("clone_template_node_digest_b")
                    and not previous.get("clone_template_node_digest_b")
                ):
                    representatives[digest] = candidate
            value["template_components"] = [
                {
                    "node_digest_b": candidate.get(
                        "clone_template_node_digest_b", candidate["node_digest_b"]
                    ),
                    "fst_name": candidate["fst_name"],
                    **{
                        key: candidate[key]
                        for key in (
                            "relative_position", "relative_quaternion", "relative_scale"
                        )
                    },
                }
                for candidate in representatives.values()
            ]
            # Retain the v1 single-template fields for old consumers.
            first = value["template_components"][0]
            value["template_node_digest_b"] = first["node_digest_b"]
            value["template_relative_transform"] = {
                key: first[key]
                for key in ("relative_position", "relative_quaternion", "relative_scale")
            }
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
    """Apply audited O1 updates and cloneable single-component additions."""
    summary = plan["summary"]
    blocked = (
        summary["deleted"]
        + summary["unsupported_updates"]
        + summary["unsupported_added"]
    )
    if blocked:
        raise ValueError(f"plan contains {blocked} non-update operations")
    encoded = base_fst.read_bytes()
    output.write_bytes(encoded)
    for update in plan["updates"]:
        targets = update.get("targets")
        if targets is None:
            targets = [
                {
                    "record_index": index,
                    "relative_position": (0.0, 0.0, 0.0),
                    "relative_quaternion": (0.0, 0.0, 0.0, 1.0),
                    "relative_scale": (1.0, 1.0, 1.0),
                }
                for index in update["record_indices"]
            ]
        stream, graph, bindings = _load_bindings(output)
        # Multiple contextual SceneNode records commonly share one schema-731
        # transform. Patch each distinct transform digest once; every referring
        # SceneNode then observes the update through its existing PropertySet.
        representatives = {}
        for target in targets:
            index = target["record_index"]
            representatives.setdefault(bindings[index].transform.digest_b, target)
        for target in representatives.values():
            stream, graph, bindings = _load_bindings(output)
            position, quaternion, scale = compose_transform(
                tuple(update["new_position"]),
                tuple(update["new_quaternion"]),
                tuple(update["new_scale"]),
                target,
            )
            encoded = patch_kinematics_transform(
                stream,
                graph,
                bindings[target["record_index"]],
                position=position,
                quaternion=quaternion,
                scale=scale,
            )
            output.write_bytes(encoded)
    for addition in plan["added"]:
        components = addition.get("template_components")
        if components is None:
            components = [{
                "node_digest_b": addition["template_node_digest_b"],
                "fst_name": Path(addition["prefab_path"]).stem,
                **addition.get("template_relative_transform", {
                    "relative_position": (0.0, 0.0, 0.0),
                    "relative_quaternion": (0.0, 0.0, 0.0, 1.0),
                    "relative_scale": (1.0, 1.0, 1.0),
                }),
            }]
        base_name = f"Unity_{addition['instance_file_id']}_{Path(addition['prefab_path']).stem}"
        for component_index, component in enumerate(components, 1):
            stream, graph, bindings = _load_bindings(output)
            template_digest = bytes.fromhex(component["node_digest_b"])
            template = next(
                (item for item in bindings.values() if item.node_digest_b == template_digest),
                None,
            )
            if template is None:
                raise ValueError("addition template SceneNode digest is absent")
            name = base_name
            if len(components) > 1:
                name = f"{base_name}__{component_index}_{component['fst_name']}"
            position, quaternion, scale = compose_transform(
                tuple(addition["position"]),
                tuple(addition["quaternion"]),
                tuple(addition["scale"]),
                component,
            )
            encoded = insert_cloned_instance(
                stream,
                None,
                graph,
                template,
                name=name,
                position=position,
                quaternion=quaternion,
                scale=scale,
            )
            output.write_bytes(encoded)
            if len(components) > 1:
                normalized_stream = parse_fst(output)
                output.write_bytes(normalize_digest_a_blocks(normalized_stream))


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
