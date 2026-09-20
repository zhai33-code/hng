#!/usr/bin/env python3
"""Link baseline Unity PrefabInstances to FST model SceneNodes by position.

The generated manifest freezes Unity's stable instance_file_id to one or more
contextual FST record indices.  Only unambiguous geometric matches are linked;
unmatched or ambiguous instances remain explicit for later investigation.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from fst_inspect import (
    _reference_positions,
    bind_scene_nodes_to_transforms,
    locate_kinematics,
    map_kinematics_property_sets,
    parse_fst,
    parse_reference_graph,
    scene_node_world_transforms,
)
from hng_unity_scene import parse_scene


def quaternion_distance(a, b) -> float:
    direct = math.dist(a, b)
    negated = math.dist(a, tuple(-value for value in b))
    return min(direct, negated)


def quaternion_multiply(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quaternion_conjugate(value):
    return (-value[0], -value[1], -value[2], value[3])


def quaternion_rotate(quaternion, vector):
    rotated = quaternion_multiply(
        quaternion_multiply(quaternion, (*vector, 0.0)),
        quaternion_conjugate(quaternion),
    )
    return rotated[:3]


def relative_transform(instance, world, position_tolerance, quaternion_tolerance):
    inverse = quaternion_conjugate(instance.hng_quaternion)
    delta = tuple(world.position[index] - instance.hng_position[index] for index in range(3))
    position = tuple(
        value / instance.hng_scale[index]
        for index, value in enumerate(quaternion_rotate(inverse, delta))
    )
    quaternion = quaternion_multiply(inverse, world.quaternion)
    scale = tuple(
        world.scale[index] / instance.hng_scale[index] for index in range(3)
    )
    # Serializer noise around an identity relationship must not become a real
    # offset when the user later moves or clones the prefab.
    if math.dist(position, (0.0, 0.0, 0.0)) <= position_tolerance:
        position = (0.0, 0.0, 0.0)
    if quaternion_distance(quaternion, (0.0, 0.0, 0.0, 1.0)) <= quaternion_tolerance:
        quaternion = (0.0, 0.0, 0.0, 1.0)
    if math.dist(scale, (1.0, 1.0, 1.0)) <= position_tolerance:
        scale = (1.0, 1.0, 1.0)
    return position, quaternion, scale


def canonical_quaternion(value):
    pivot = max(range(4), key=lambda index: abs(value[index]))
    return tuple(-item for item in value) if value[pivot] < 0 else tuple(value)


def transform_signature(relative):
    position, quaternion, scale = relative
    quaternion = canonical_quaternion(quaternion)
    return (
        tuple(round(value, 1) for value in position),
        tuple(round(value, 2) for value in quaternion),
        tuple(round(value, 2) for value in scale),
    )


def cell(position, size):
    return tuple(math.floor(value / size) for value in position)


def link_scene(
    scene: Path,
    assets: Path,
    fst_path: Path,
    *,
    position_tolerance: float = 0.05,
    quaternion_tolerance: float = 0.001,
) -> dict[str, object]:
    instances = parse_scene(scene, assets)
    stream = parse_fst(fst_path)
    transforms = locate_kinematics(stream)
    graph = parse_reference_graph(stream, transforms)
    properties = map_kinematics_property_sets(stream, transforms)
    bindings = bind_scene_nodes_to_transforms(stream, graph, properties)
    worlds = scene_node_world_transforms(graph, bindings)
    models = [item for item in bindings if item.node["node_type"] == "#model"]
    inline_payloads = {item.digest for item in stream.inline_objects}
    reference_positions = _reference_positions(graph)

    def cloneable(binding) -> bool:
        template_record = graph.flat_records[binding.record_index - 1][1]
        template_position = reference_positions.get(binding.node_digest_b)
        if template_position is None:
            return False
        template_end = template_position + template_record.value
        transform_index = next(
            index
            for index, (schema_id, record) in enumerate(graph.flat_records, 1)
            if schema_id == 731 and record.digest_b == binding.transform.digest_b
        )
        subtree_indices = set(graph.relation_values[template_position:template_end])
        if binding.property_record_index not in subtree_indices:
            return False
        if transform_index not in subtree_indices:
            return False
        direct = graph.children_by_digest_b.get(binding.node_digest_b, ())
        for parent_index in direct[: int(binding.node["first"])]:
            if parent_index == 0:
                continue
            parent_schema, parent_record = graph.flat_records[parent_index - 1]
            if parent_schema != 434 or parent_record.digest_a not in inline_payloads:
                return False
        return True

    grid: dict[tuple[int, int, int], list[object]] = {}
    for binding in models:
        key = cell(worlds[binding.record_index].position, position_tolerance)
        grid.setdefault(key, []).append(binding)

    links = []
    parent_counts = Counter(item.parent_file_id for item in instances)
    parent_name_counts = Counter(item.parent_name or "<unknown>" for item in instances)
    for instance in instances:
        base = cell(instance.hng_position, position_tolerance)
        candidates = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for binding in grid.get(
                        (base[0] + dx, base[1] + dy, base[2] + dz), ()
                    ):
                        world = worlds[binding.record_index]
                        position_error = math.dist(instance.hng_position, world.position)
                        quaternion_error = quaternion_distance(
                            instance.hng_quaternion, world.quaternion
                        )
                        if position_error <= position_tolerance:
                            candidates.append((binding, position_error, quaternion_error))

        # Official FST commonly contains two contextual records for the same
        # logical model.  They are one valid link when their names and TRS are
        # equivalent, and both record indices are retained for later writes.
        names = {str(item[0].node["name"]) for item in candidates}
        if not candidates:
            status = "unmatched"
        elif len(names) == 1:
            status = "matched"
        else:
            status = "ambiguous"
        candidate_values = []
        for binding, position_error, quaternion_error in sorted(
            candidates, key=lambda value: value[0].record_index
        ):
            world = worlds[binding.record_index]
            relative_position, relative_quaternion, relative_scale = relative_transform(
                instance, world, position_tolerance, quaternion_tolerance
            )
            candidate_values.append(
                {
                    "record_index": binding.record_index,
                    "node_digest_b": binding.node_digest_b.hex(),
                    "transform_digest_b": binding.transform.digest_b.hex(),
                    "cloneable": cloneable(binding),
                    "fst_name": binding.node["name"],
                    "position_error": position_error,
                    "quaternion_error": quaternion_error,
                    "relative_position": relative_position,
                    "relative_quaternion": relative_quaternion,
                    "relative_scale": relative_scale,
                }
            )
        links.append(
            {
                "instance_file_id": instance.instance_file_id,
                "parent_file_id": instance.parent_file_id,
                "parent_name": instance.parent_name,
                "prefab_guid": instance.prefab_guid,
                "prefab_path": instance.prefab_path,
                "unity_name": instance.name,
                "fst_position": instance.hng_position,
                "fst_quaternion": instance.hng_quaternion,
                "fst_scale": instance.hng_scale,
                "status": status,
                "link_kind": "root_position" if status == "matched" else None,
                "candidates": candidate_values,
            }
        )

    # Some Unity prefab roots intentionally differ from their exported FST
    # model pivots. Learn only strongly repeated prefab-local transforms from
    # the official baseline: at least three instances and 75% of every still
    # unmatched O1 instance of that GUID must exhibit the same relationship.
    # This keeps one-off coincidences and sparse regular layouts unsupported.
    instance_by_id = {item.instance_file_id: item for item in instances}
    unavailable = {
        candidate["record_index"]
        for item in links
        if item["status"] != "unmatched"
        for candidate in item["candidates"]
    }
    unmatched_by_guid = defaultdict(list)
    for item in links:
        if item["status"] == "unmatched" and item["parent_name"] == "O1":
            unmatched_by_guid[item["prefab_guid"]].append(item)
    verified_majority_offsets = {
        # BarbedWireEntanglementA_Big: 13 official O1 instances share the
        # complete component layout; the remaining nine Unity roots are not
        # represented in the shipped Mill FST.
        "83b91b8ac72972247a1f6466d3ca6a19": 0.50,
    }
    for guid, group in unmatched_by_guid.items():
        relations = defaultdict(lambda: defaultdict(list))
        for link in group:
            instance = instance_by_id[link["instance_file_id"]]
            for binding in models:
                if binding.record_index in unavailable:
                    continue
                world = worlds[binding.record_index]
                position_error = math.dist(instance.hng_position, world.position)
                if position_error > 100.0:
                    continue
                relative = relative_transform(
                    instance, world, position_tolerance, quaternion_tolerance
                )
                relations[transform_signature(relative)][link["instance_file_id"]].append(
                    (binding, position_error, quaternion_distance(
                        instance.hng_quaternion, world.quaternion
                    ), relative)
                )
        support_ratio = verified_majority_offsets.get(guid, 0.75)
        minimum_support = max(3, math.ceil(len(group) * support_ratio))
        accepted = {
            signature: per_instance
            for signature, per_instance in relations.items()
            if len(per_instance) >= minimum_support
        }
        proposed = defaultdict(list)
        for per_instance in accepted.values():
            for instance_id, values in per_instance.items():
                proposed[instance_id].extend(values)

        # Reject any learned assignment that tries to claim the same FST node
        # for two Unity roots. Such conflicts are characteristic of repeated
        # fence/grid coincidences rather than a real prefab-local transform.
        owners = defaultdict(set)
        for instance_id, values in proposed.items():
            for binding, *_ in values:
                owners[binding.record_index].add(instance_id)
        conflicted = {index for index, values in owners.items() if len(values) > 1}
        for link in group:
            instance_id = link["instance_file_id"]
            values = [
                value for value in proposed.get(instance_id, ())
                if value[0].record_index not in conflicted
            ]
            if not values:
                continue
            candidates = []
            for binding, position_error, quaternion_error, relative in sorted(
                values, key=lambda value: value[0].record_index
            ):
                candidates.append({
                    "record_index": binding.record_index,
                    "node_digest_b": binding.node_digest_b.hex(),
                    "transform_digest_b": binding.transform.digest_b.hex(),
                    "cloneable": cloneable(binding),
                    "fst_name": binding.node["name"],
                    "position_error": position_error,
                    "quaternion_error": quaternion_error,
                    "relative_position": relative[0],
                    "relative_quaternion": relative[1],
                    "relative_scale": relative[2],
                })
            link["status"] = "matched_offset"
            link["link_kind"] = "learned_prefab_offset"
            link["candidates"] = candidates
            unavailable.update(value["record_index"] for value in candidates)

    # A matched component can live below a shared prototype and therefore be
    # unsafe to clone as a complete instance.  The shipped map frequently has
    # another occurrence of the exact same model name with a self-contained
    # subtree.  Retain the matched component's relative transform, but record
    # that equivalent subtree as its addition-only clone template.
    clone_templates_by_name = defaultdict(list)
    for binding in models:
        if cloneable(binding):
            clone_templates_by_name[str(binding.node["name"])].append(binding)
    for item in links:
        for value in item["candidates"]:
            if value["cloneable"]:
                value["clone_template_node_digest_b"] = value["node_digest_b"]
                continue
            alternatives = clone_templates_by_name.get(str(value["fst_name"]), ())
            if alternatives:
                value["clone_template_node_digest_b"] = alternatives[0].node_digest_b.hex()

    for item in links:
        candidates_by_transform = defaultdict(list)
        for value in item["candidates"]:
            candidates_by_transform[value["transform_digest_b"]].append(value)
        transform_count = len(candidates_by_transform)
        item["addition_supported"] = (
            item["status"] in {"matched", "matched_offset"}
            and transform_count > 0
            and all(
                any(value.get("clone_template_node_digest_b") for value in values)
                for values in candidates_by_transform.values()
            )
        )
        item["addition_component_count"] = transform_count
    status_counts = Counter(item["status"] for item in links)

    return {
        "format": "hng-unity-fst-link-v3",
        "source_scene": str(scene.resolve()),
        "source_fst": str(fst_path.resolve()),
        "coordinate_mapping": {
            "position": "(10*x, 10*y, -10*z)",
            "quaternion": "(-x, -y, z, w)",
            "scale": "(x, y, z)",
        },
        "position_tolerance": position_tolerance,
        "quaternion_tolerance": quaternion_tolerance,
        "unity_instance_count": len(instances),
        "fst_model_binding_count": len(models),
        "status_counts": dict(sorted(status_counts.items())),
        "parent_file_id_counts": {
            str(key): value for key, value in parent_counts.most_common()
        },
        "parent_name_counts": dict(parent_name_counts.most_common()),
        "status_by_parent_name": {
            parent_name: dict(Counter(
                item["status"] for item in links if item["parent_name"] == parent_name
            ))
            for parent_name in parent_name_counts
        },
        "o1_prefab_status": {
            guid: {
                "prefab_path": next(
                    item["prefab_path"] for item in links if item["prefab_guid"] == guid
                ),
                "counts": dict(Counter(
                    item["status"]
                    for item in links
                    if item["parent_name"] == "O1" and item["prefab_guid"] == guid
                )),
            }
            for guid in sorted({
                item["prefab_guid"] for item in links if item["parent_name"] == "O1"
            })
        },
        "links": links,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", type=Path)
    parser.add_argument("fst", type=Path)
    parser.add_argument(
        "--assets", type=Path, default=Path(r"D:\hng-map\Project\Assets")
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--position-tolerance", type=float, default=0.05)
    parser.add_argument("--quaternion-tolerance", type=float, default=0.001)
    ns = parser.parse_args()
    result = link_scene(
        ns.scene,
        ns.assets,
        ns.fst,
        position_tolerance=ns.position_tolerance,
        quaternion_tolerance=ns.quaternion_tolerance,
    )
    ns.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in (
        "unity_instance_count", "fst_model_binding_count", "status_counts"
    )}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
