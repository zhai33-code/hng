#!/usr/bin/env python3
"""Plan safe Unity-to-FST changes using a frozen identity link manifest."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from fst_inspect import (
    _reference_positions,
    bind_scene_nodes_to_transforms,
    locate_kinematics,
    map_kinematics_property_sets,
    normalize_digest_a_blocks,
    parse_fst,
    parse_reference_graph,
    patch_kinematics_transform,
    insert_cloned_instance,
    remove_scene_trees,
    scene_node_world_transforms,
)
from hng_unity_scene import parse_scene


UPDATE_PARENT_NAMES = frozenset({"O1", "A1", "B1", "Misc", "Bush", "Tree"})
ADDITION_PARENT_NAMES = frozenset({"O1", "A1", "B1", "Misc", "Bush", "Tree"})
TEMPLATE_PARENT_NAMES = frozenset({"O1", "A1", "B1", "Misc", "Bush", "Tree"})


def quaternion_distance(a, b):
    return min(math.dist(a, b), math.dist(a, tuple(-value for value in b)))


def select_addition_template(item, templates):
    """Choose the official FST variant that best matches a Unity instance."""
    return min(
        templates,
        key=lambda value: (
            math.dist(item.hng_scale, value["fst_scale"]),
            quaternion_distance(item.hng_quaternion, value["fst_quaternion"]),
            value["instance_file_id"],
        ),
    )


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


def quaternion_inverse(quaternion):
    """Conjugate (inverse for unit quaternions) of a quaternion."""
    return (-quaternion[0], -quaternion[1], -quaternion[2], quaternion[3])


def world_to_parent_local(world_trs, parent):
    """Convert a desired world TRS into a local TRS relative to the parent.

    `parent` is the parent's *world* transform ``(position, quaternion, scale)``
    or ``None`` when the clone has no serialized parent (world == local).

    The parent world TRS comes from ``scene_node_world_transforms``, which can
    carry sub-millimeter float noise (e.g. a true identity parent reported as
    scale ``1.0000004`` / quaternion ``(0,0,0,0.9999995)``).  A near-identity
    parent is returned unchanged so that tiny noise is never amplified into a
    visible offset; only parents with a real, significant offset are inverted.
    """
    world_position, world_quaternion, world_scale = world_trs
    if parent is None:
        return world_position, world_quaternion, world_scale
    parent_position, parent_quaternion, parent_scale = parent
    parent_position_error = math.dist(parent_position, (0.0, 0.0, 0.0))
    parent_scale_error = math.dist(parent_scale, (1.0, 1.0, 1.0))
    if (
        parent_position_error < 1e-3
        and parent_scale_error < 1e-3
        and abs(parent_quaternion[3]) > 1.0 - 1e-4
    ):
        return world_position, world_quaternion, world_scale
    local_scale = tuple(
        world_scale[index] / parent_scale[index] for index in range(3)
    )
    local_quaternion = quaternion_multiply(
        quaternion_inverse(parent_quaternion), world_quaternion
    )
    delta = tuple(
        world_position[index] - parent_position[index] for index in range(3)
    )
    local_position = quaternion_rotate(quaternion_inverse(parent_quaternion), delta)
    local_position = tuple(
        local_position[index] / parent_scale[index] for index in range(3)
    )
    return local_position, local_quaternion, local_scale


def serialized_parent(bindings, graph, binding):
    """Return the bound serialized parent SceneNode of ``binding``, or None."""
    direct = graph.children_by_digest_b.get(binding.node_digest_b, ())
    for child in direct[: int(binding.node["first"])]:
        if child == 0:
            continue
        if graph.flat_records[child - 1][0] == 434 and child in bindings:
            return bindings[child]
    return None


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
    composite_groups = {}
    for old in manifest["links"]:
        group_id = old.get("composite_group_id")
        if group_id:
            composite_groups.setdefault(group_id, []).append(old)
    composite_member_ids = {
        old["instance_file_id"]
        for members in composite_groups.values()
        for old in members
    }
    for group_id, members in composite_groups.items():
        member_ids = sorted(old["instance_file_id"] for old in members)
        current_members = {
            instance_id: current.get(instance_id) for instance_id in member_ids
        }
        missing = [
            instance_id for instance_id, item in current_members.items() if item is None
        ]
        changed = [
            old for old in members
            if current_members[old["instance_file_id"]] is None
            or math.dist(
                current_members[old["instance_file_id"]].hng_position,
                old["fst_position"],
            ) > 1e-5
            or quaternion_distance(
                current_members[old["instance_file_id"]].hng_quaternion,
                old["fst_quaternion"],
            ) > 1e-6
            or math.dist(
                current_members[old["instance_file_id"]].hng_scale,
                old["fst_scale"],
            ) > 1e-6
            or current_members[old["instance_file_id"]].parent_file_id
            != old["parent_file_id"]
        ]
        if not changed:
            continue
        group_change = {
            "instance_file_id": member_ids[0],
            "composite_group_id": group_id,
            "composite_member_ids": member_ids,
            "prefab_guid": members[0]["prefab_guid"],
            "parent_name": members[0]["parent_name"],
        }
        if missing:
            if len(missing) == len(member_ids):
                root_targets = members[0].get("candidates", ())
                if root_targets:
                    deleted.append({
                        **group_change,
                        "prefab_path": members[0]["prefab_path"],
                        "targets": root_targets,
                    })
                    continue
            group_change["reason"] = (
                "incomplete rigid composite; missing Unity members "
                + ", ".join(str(value) for value in missing)
            )
            unsupported_updates.append(group_change)
            continue
        if any(
            current_members[old["instance_file_id"]].parent_file_id
            != old["parent_file_id"]
            or current_members[old["instance_file_id"]].parent_name
            not in UPDATE_PARENT_NAMES
            for old in members
        ):
            group_change["reason"] = "rigid composite member was reparented"
            unsupported_updates.append(group_change)
            continue
        projected = []
        for old in members:
            item = current_members[old["instance_file_id"]]
            target = old["candidates"][0]
            projected.append((old, item, compose_transform(
                item.hng_position,
                item.hng_quaternion,
                item.hng_scale,
                target,
            )))
        root_position, root_quaternion, root_scale = projected[0][2]
        coherent = all(
            math.dist(root[0], root_position) <= 0.05
            and quaternion_distance(root[1], root_quaternion) <= 0.001
            and math.dist(root[2], root_scale) <= 0.001
            for _, _, root in projected[1:]
        )
        if not coherent:
            group_change["reason"] = (
                "composite members no longer form one rigid 8 m fence root"
            )
            unsupported_updates.append(group_change)
            continue
        anchor_old, anchor_item, _ = min(
            projected, key=lambda value: value[0]["instance_file_id"]
        )
        group_change.update({
            "instance_file_id": anchor_old["instance_file_id"],
            "prefab_guid": anchor_item.prefab_guid,
            "old_position": anchor_old["fst_position"],
            "new_position": anchor_item.hng_position,
            "old_quaternion": anchor_old["fst_quaternion"],
            "new_quaternion": anchor_item.hng_quaternion,
            "old_scale": anchor_old["fst_scale"],
            "new_scale": anchor_item.hng_scale,
            "targets": anchor_old["candidates"],
            "record_indices": [
                value["record_index"] for value in anchor_old["candidates"]
            ],
        })
        updates.append(group_change)

    for instance_id, old in baseline.items():
        if instance_id in composite_member_ids:
            continue
        item = current.get(instance_id)
        if item is None:
            if old["status"] in {"matched", "matched_offset", "matched_bundle"}:
                root_targets = [
                    candidate for candidate in old.get("candidates", ())
                    if candidate.get("addition_root_template")
                ]
                if root_targets:
                    deleted.append({
                        "instance_file_id": instance_id,
                        "prefab_guid": old["prefab_guid"],
                        "prefab_path": old["prefab_path"],
                        "parent_name": old["parent_name"],
                        "targets": root_targets,
                    })
                else:
                    unsupported_updates.append({
                        "instance_file_id": instance_id,
                        "prefab_guid": old["prefab_guid"],
                        "parent_name": old["parent_name"],
                        "reason": "deleted instance has no verified complete FST root",
                    })
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
            old["status"] in {"matched", "matched_offset", "matched_bundle"}
            and item.parent_file_id == old["parent_file_id"]
            and old["parent_name"] in UPDATE_PARENT_NAMES
            and item.parent_name in UPDATE_PARENT_NAMES
        ):
            updates.append(change)
        else:
            change["reason"] = (
                "reparented" if item.parent_file_id != old["parent_file_id"]
                else f"baseline status/parent is {old['status']}/{old['parent_name']}"
            )
            unsupported_updates.append(change)

    known_templates = {}
    for template in manifest["links"]:
        if (
            template["status"] in {"matched", "matched_offset", "matched_bundle"}
            and template["parent_name"] in TEMPLATE_PARENT_NAMES
            and template.get("addition_supported", True)
        ):
            known_templates.setdefault(template["prefab_guid"], []).append(template)
    for template in manifest.get("addition_templates", ()):
        if template.get("addition_supported"):
            known_templates.setdefault(template["prefab_guid"], []).append(template)
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
        if (
            item.parent_name in ADDITION_PARENT_NAMES
            and item.prefab_guid in known_templates
        ):
            allowed_templates = [
                template for template in known_templates[item.prefab_guid]
                if item.parent_name in template.get(
                    "allowed_parent_names", (item.parent_name,)
                )
            ]
            if not allowed_templates:
                value["reason"] = "no verified addition template for this parent layer"
                unsupported_added.append(value)
                continue
            # Some Unity GUIDs export to multiple complete FST roots.  Select
            # the official template whose Unity root scale is closest first,
            # then prefer the closest orientation.  FarmPlotA uses this to
            # distinguish its 0.849 Cabbage1A and 1.0 Cabbage1B variants.
            template = select_addition_template(
                item, allowed_templates
            )
            root_candidates = [
                candidate for candidate in template["candidates"]
                if candidate.get("addition_root_template")
            ]
            candidate_pool = root_candidates or template["candidates"]
            representatives = {}
            for candidate in candidate_pool:
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
            value["reason"] = (
                "parent is outside the verified addition layers"
                if item.parent_name not in ADDITION_PARENT_NAMES
                else "no verified addition template for prefab GUID"
            )
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
        "deleted": deleted,
        "deleted_instance_file_ids": [
            item["instance_file_id"] for item in deleted
        ],
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
    blocked = summary["unsupported_updates"] + summary["unsupported_added"]
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
    deletion_roots = []
    if plan.get("deleted"):
        stream, graph, bindings = _load_bindings(output)
        positions = _reference_positions(graph)
        for deletion in plan["deleted"]:
            candidate_digests = {
                bytes.fromhex(target["node_digest_b"])
                for target in deletion["targets"]
            }
            candidates = [
                item for item in bindings.values()
                if item.node_digest_b in candidate_digests
            ]
            if not candidates:
                raise ValueError("deletion target SceneNode digest is absent")
            for candidate in candidates:
                position = positions[candidate.node_digest_b]
                deletion_roots.append((position, candidate.node_digest_b))
    if deletion_roots:
        stream, graph, bindings = _load_bindings(output)
        target_bindings = []
        for _position, digest in sorted(set(deletion_roots)):
            target = next(
                (item for item in bindings.values() if item.node_digest_b == digest),
                None,
            )
            if target is None:
                raise ValueError("deletion root disappeared while applying plan")
            target_bindings.append(target)
        encoded = remove_scene_trees(
            stream,
            graph,
            target_bindings,
            allow_shipped=True,
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
            # compose_transform yields the component's desired *world* TRS.  The
            # clone is spliced under the template's existing serialized parent,
            # whose own transform is not necessarily identity (e.g. the shared
            # BarbedWire container at an offset).  Convert to the parent-relative
            # local TRS so the engine's world composition lands on the target.
            position, quaternion, scale = compose_transform(
                tuple(addition["position"]),
                tuple(addition["quaternion"]),
                tuple(addition["scale"]),
                component,
            )
            worlds = scene_node_world_transforms(graph, bindings.values())
            parent = serialized_parent(bindings, graph, template)
            parent_world = worlds[parent.record_index] if parent is not None else None
            parent_trs = (
                parent_world.position,
                parent_world.quaternion,
                parent_world.scale,
            ) if parent_world is not None else None
            position, quaternion, scale = world_to_parent_local(
                (position, quaternion, scale), parent_trs
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
