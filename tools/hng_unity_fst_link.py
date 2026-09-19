#!/usr/bin/env python3
"""Link baseline Unity PrefabInstances to FST model SceneNodes by exact TRS.

The generated manifest freezes Unity's stable instance_file_id to one or more
contextual FST record indices.  Only unambiguous geometric matches are linked;
unmatched or ambiguous instances remain explicit for later investigation.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path

from fst_inspect import (
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

    grid: dict[tuple[int, int, int], list[object]] = {}
    for binding in models:
        key = cell(worlds[binding.record_index].position, position_tolerance)
        grid.setdefault(key, []).append(binding)

    links = []
    status_counts: Counter[str] = Counter()
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
                        if (
                            position_error <= position_tolerance
                            and quaternion_error <= quaternion_tolerance
                        ):
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
        status_counts[status] += 1
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
                "candidates": [
                    {
                        "record_index": binding.record_index,
                        "fst_name": binding.node["name"],
                        "position_error": position_error,
                        "quaternion_error": quaternion_error,
                    }
                    for binding, position_error, quaternion_error in sorted(
                        candidates, key=lambda value: value[0].record_index
                    )
                ],
            }
        )

    return {
        "format": "hng-unity-fst-link-v1",
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
