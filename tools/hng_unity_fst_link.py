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

    cloneable_record_indices = {
        binding.record_index for binding in models if cloneable(binding)
    }

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
    verified_offset_rules = {
        # BarbedWireEntanglementA_Big is exported in two opposite root
        # orientations (13 + 9).  Restrict learning to its complete root.
        "83b91b8ac72972247a1f6466d3ca6a19": {
            "support_ratio": 0.40,
            "name_fragment": "BarbedWireEntanglementA_3m_A1",
            "max_distance": 20.0,
        },
        # CrateA_Small: its four shipped O1 instances split into two 90-degree
        # rotations (two instances each).  A 0.50 support ratio lowers the
        # learned-offset threshold to two instances per rotation so the fixed
        # prefab-local crate offset is recovered for all four.
        "06fecb1c7cd170e4d8e8a3e527708caa": {"support_ratio": 0.50},
        # Brick fence roots sit six FST units below their Unity pivots and are
        # split between two symmetric orientations.  Root-name filtering keeps
        # adjacent wall/LOD records from becoming false votes.
        "be5b41e719d02924ba37579af8d13afb": {
            "minimum_support": 6,
            "name_fragment": "FenceE_4m1",
            "max_distance": 10.0,
        },
        "05d49efd79133c74986c67f197c09dd5": {
            "minimum_support": 4,
            "name_fragment": "FenceE_2m_End1",
            "max_distance": 10.0,
        },
        # FarmPlotA_GroupA has two exported root variants selected entirely by
        # Unity root scale: 0.849 -> Cabbage1A1 (18 instances), 1.0 ->
        # Cabbage1B1 (30 instances).  Both roots own their LOD/collision tree.
        "8f2596c2a620a6c4a9abc7b557df19a8": {
            "minimum_support": 18,
            "name_fragments": ("Cabbage1A1", "Cabbage1B1"),
            "max_distance": 2.0,
        },
    }
    # Prefabs whose learned-offset matches are known to be geometrically
    # coincidental but semantically wrong (e.g. an open shed accidentally
    # lining up with a camouflage shed). Their offset learning is disabled to
    # avoid polluting the compiled FST with incorrect additions.
    offset_learning_excluded = {
        "f596d2c2e82dc04498f1c2a86d135da0",  # ShedOpenA -> Shed_Camouflage_A_01
    }
    for guid, group in unmatched_by_guid.items():
        if guid in offset_learning_excluded:
            continue
        rule = verified_offset_rules.get(guid, {})
        name_fragments = tuple(rule.get("name_fragments", ()))
        if rule.get("name_fragment"):
            name_fragments = (str(rule["name_fragment"]),)
        max_distance = float(rule.get("max_distance", 100.0))
        relations = defaultdict(lambda: defaultdict(list))
        for link in group:
            instance = instance_by_id[link["instance_file_id"]]
            for binding in models:
                if binding.record_index in unavailable:
                    continue
                if binding.record_index not in cloneable_record_indices:
                    # Non-cloneable targets (e.g. generic LOD0/1/2/3 nodes)
                    # cannot serve as addition templates and only pollute the
                    # learned-offset signature votes with semantically wrong
                    # matches, so they are excluded from learning.
                    continue
                if name_fragments and not any(
                    fragment in str(binding.node["name"])
                    for fragment in name_fragments
                ):
                    continue
                world = worlds[binding.record_index]
                position_error = math.dist(instance.hng_position, world.position)
                if position_error > max_distance:
                    continue
                relative = relative_transform(
                    instance, world, position_tolerance, quaternion_tolerance
                )
                relations[transform_signature(relative)][link["instance_file_id"]].append(
                    (binding, position_error, quaternion_distance(
                        instance.hng_quaternion, world.quaternion
                    ), relative)
                )
        support_ratio = float(rule.get("support_ratio", 0.75))
        minimum_support = int(
            rule.get("minimum_support", max(2, math.ceil(len(group) * support_ratio)))
        )
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

    # A few remaining O1 instances are singletons that the majority-vote
    # learner above cannot recover: offset learning needs at least two
    # supporting instances per signature, but these prefabs ship a single
    # instance.  For a hand-verified set where a nearby FST model has the same
    # semantic identity and an agreeing orientation, mark the instance as a
    # learned offset.  Geometry plus quaternion alone is not sufficient (see
    # FarmPlotA vs Cabbage1A1), so these are vetted against the official
    # baseline and enumerated explicitly by prefab GUID.
    verified_singleton_offsets = {
        # prefab_guid: (fst_name_fragment, max_dist, max_qdist)
        "b6c15cc0456f77546b41f5bdf6e143f5": ("House1C", 1.0, 0.05),    # HouseA_B_Inaccessible
        "03395f02df643d04e9d7ef2a9a7cf81e": ("CratePile_B1", 5.0, 0.05),  # CrateA_GroupA
        "0db8963ec695210448ec0c43d99f11df": ("House5B1", 8.0, 0.05),   # ShedA_GroupB
        "8d02d6b8ef397954ead79c3524c15c2a": ("CratePile_A1", 5.0, 0.30),  # CrateA_GroupB
        "f6a8635aec9d7fc4495355d54831ed40": ("RadioSet_A1", 5.0, 0.30),  # RadioA_GroupA
        "89a27d4eef268324ab5f0c3589c49aad": ("CableSpool1A1", 1.0, 2.0),  # CableDrumA
        "4354b33d9cc7dfe4e88b0d1e4260c68e": ("House1A_Addon1", 3.0, 2.0),  # AddonHouseA_GroupA
        "ebb4dcc8be8f58a4792810cf64e30750": ("FenceE_ColumnA1", 7.0, 2.0),  # FenceBrickA_Pole
        "a7748a10c38baa34882cbe12d60cdf6f": ("FenceE_RuinsA1", 13.0, 0.10),  # FenceBrickA_2m_Destroyed
        "2dcee1dacbb930e4283fbbe6d91fdbd9": ("Crates3x2x21", 23.0, 2.0),  # CrateA_GroupC
        "be5b41e719d02924ba37579af8d13afb": ("FenceE_4m1", 7.0, 2.0),  # one irregular 4m brick fence
        # Three O1 2m walls use the same complete FenceE root as the majority,
        # but their authored pivots are shifted one unit horizontally, so they
        # form one/two-instance signatures below the vote threshold.
        "05d49efd79133c74986c67f197c09dd5": ("FenceE_2m_End1", 7.0, 2.0),
        # The Unity grouping pivots for these buildings differ from the game
        # roots by large but exact round offsets.  Their semantic FST roots are
        # unique and own complete cloneable destruction/LOD subtrees.
        "8ef7a77fec9749a4d8c0174edffd1e45": ("House5C1", 31.0, 2.0),  # ShedB_GroupA
        "2c31a23ccbf089f4988e7d60f3dd1cbc": (
            "House4C_PoliceStation_main1", 81.0, 2.0
        ),
        "cfe61cbbead459f4eaaeaa80bb0c24e1": ("House3B_Barn1", 70.0, 2.0),
    }
    for guid, (name_fragment, max_dist, max_qdist) in verified_singleton_offsets.items():
        for link in unmatched_by_guid.get(guid, ()):
            if link["status"] != "unmatched":
                continue
            instance = instance_by_id[link["instance_file_id"]]
            candidates = []
            for binding in models:
                if binding.record_index in unavailable:
                    continue
                if name_fragment not in str(binding.node["name"]):
                    continue
                world = worlds[binding.record_index]
                position_error = math.dist(instance.hng_position, world.position)
                if position_error > max_dist:
                    continue
                quaternion_error = quaternion_distance(
                    instance.hng_quaternion, world.quaternion
                )
                if quaternion_error > max_qdist:
                    continue
                relative = relative_transform(
                    instance, world, position_tolerance, quaternion_tolerance
                )
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
            if not candidates:
                continue
            link["status"] = "matched_offset"
            link["link_kind"] = "verified_singleton_offset"
            link["candidates"] = candidates
            unavailable.update(value["record_index"] for value in candidates)

    # A small set of objective/misc-layer objects also has authoritative game
    # roots.  Keep these rules parent-specific: most A1/Misc entries are Unity
    # dressing absent from the shipped FST, so applying O1 rules globally
    # would manufacture identities for objects that do not exist in the map.
    verified_non_o1_offsets = {
        # (parent_name, prefab_guid): (FST root fragment, distance, qdistance)
        # All three A1 damaged 4 m walls coincide one-to-one with the three
        # support-beam roots at a fixed six-unit vertical pivot offset.
        ("A1", "3b640eb626bf58f49a56d4bb7eaee2cf"): (
            "FenceE_4m_SupportBeams1", 7.0, 2.0
        ),
        ("B1", "6dfeaf3a1eebedf4e937778de59cfa40"): ("TentMilitary1B1", 2.0, 2.0),
        ("B1", "c3f8fee8e0c369d4197917fff450069d"): ("Firepit1A1", 2.0, 2.0),
        ("B1", "2dcee1dacbb930e4283fbbe6d91fdbd9"): ("Crates3x2x22", 23.0, 2.0),
        ("B1", "0698a613551e681429a8dac692f79096"): ("Crates3x3x21", 23.0, 2.0),
        ("Misc", "9a67420990ef5ac4b995891ddf1c1b7e"): ("House5B2", 6.0, 2.0),
        ("Misc", "57e1ad3ff5b408043891f1bab7cb1076"): ("Shed_Bicycle", 1.0, 2.0),
        ("Misc", "b1e23d68f7fa6a04d8ce3de6ad06c81c"): ("FenceA_8m", 6.0, 2.0),
        ("Misc", "c17df02a89ae40247ab8ebb5c1c2bdc3"): ("FenceT01_1A_4m1", 21.0, 2.0),
    }
    for (parent_name, guid), rule in verified_non_o1_offsets.items():
        name_fragment, max_dist, max_qdist = rule
        target_links = [
            item for item in links
            if item["parent_name"] == parent_name
            and item["prefab_guid"] == guid
            and item["status"] == "unmatched"
        ]
        for link in target_links:
            instance = instance_by_id[link["instance_file_id"]]
            candidates = []
            for binding in models:
                if binding.record_index in unavailable:
                    continue
                if name_fragment not in str(binding.node["name"]):
                    continue
                world = worlds[binding.record_index]
                position_error = math.dist(instance.hng_position, world.position)
                if position_error > max_dist:
                    continue
                quaternion_error = quaternion_distance(
                    instance.hng_quaternion, world.quaternion
                )
                if quaternion_error > max_qdist:
                    continue
                relative = relative_transform(
                    instance, world, position_tolerance, quaternion_tolerance
                )
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
            if not candidates:
                continue
            link["status"] = "matched_offset"
            link["link_kind"] = "verified_parent_offset"
            link["candidates"] = candidates
            unavailable.update(value["record_index"] for value in candidates)

    # Three shipped iron-fence objects are authored in Unity as two adjacent
    # 4 m pieces plus their middle pole, while the FST stores each trio as one
    # FenceB_8m root.  Preserve that many-to-one identity explicitly.  The
    # compiler only permits an update when all three Unity members still
    # project to the same root transform, so moving one piece independently is
    # reported as unsupported instead of dragging the whole 8 m model.
    iron_segment_suffix = "FenceIronA_GroupB_4m.prefab"
    iron_pole_suffix = "FenceIronA_GroupB_Pole.prefab"
    iron_roots_by_transform = defaultdict(list)
    for binding in models:
        if str(binding.node["name"]).startswith("FenceB_8m"):
            iron_roots_by_transform[binding.transform.digest_b].append(binding)
    claimed_iron_members = set()
    for transform_digest, root_bindings in iron_roots_by_transform.items():
        root = min(root_bindings, key=lambda value: value.record_index)
        world = worlds[root.record_index]
        segment_members = []
        pole_members = []
        for link in links:
            if (
                link["status"] != "unmatched"
                or link["parent_name"] != "O1"
                or link["instance_file_id"] in claimed_iron_members
            ):
                continue
            instance = instance_by_id[link["instance_file_id"]]
            distance = math.dist(instance.hng_position, world.position)
            relative = relative_transform(
                instance, world, position_tolerance, quaternion_tolerance
            )
            if link["prefab_path"].endswith(iron_segment_suffix) and 19.0 <= distance <= 31.0:
                segment_members.append((link, instance, relative))
            elif link["prefab_path"].endswith(iron_pole_suffix) and 4.0 <= distance <= 6.0:
                pole_members.append((link, instance, relative))
        if len(segment_members) != 2 or len(pole_members) != 1:
            continue
        members = segment_members + pole_members
        reference_quaternion = members[0][2][1]
        if any(
            quaternion_distance(relative[1], reference_quaternion) > 0.01
            for _, _, relative in members[1:]
        ):
            continue
        member_ids = sorted(link["instance_file_id"] for link, _, _ in members)
        group_id = f"iron8m:{transform_digest.hex()}"
        for link, instance, relative in members:
            candidates = []
            for binding in sorted(root_bindings, key=lambda value: value.record_index):
                candidates.append({
                    "record_index": binding.record_index,
                    "node_digest_b": binding.node_digest_b.hex(),
                    "transform_digest_b": binding.transform.digest_b.hex(),
                    "cloneable": cloneable(binding),
                    "fst_name": binding.node["name"],
                    "position_error": math.dist(instance.hng_position, world.position),
                    "quaternion_error": quaternion_distance(
                        instance.hng_quaternion, world.quaternion
                    ),
                    "relative_position": relative[0],
                    "relative_quaternion": relative[1],
                    "relative_scale": relative[2],
                })
            link["status"] = "matched_group"
            link["link_kind"] = "rigid_composite_member"
            link["composite_group_id"] = group_id
            link["composite_member_ids"] = member_ids
            link["candidates"] = candidates
            claimed_iron_members.add(link["instance_file_id"])
        unavailable.update(binding.record_index for binding in root_bindings)

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
        transform_digests = set(candidates_by_transform)
        candidate_positions = {}
        for value in item["candidates"]:
            record = graph.flat_records[value["record_index"] - 1][1]
            position = reference_positions.get(record.digest_b)
            if position is not None:
                candidate_positions[value["record_index"]] = position
        for value in item["candidates"]:
            record = graph.flat_records[value["record_index"] - 1][1]
            start = candidate_positions.get(value["record_index"])
            if start is None:
                continue
            end = start + record.value
            covered = {
                other["transform_digest_b"]
                for other in item["candidates"]
                if start <= candidate_positions.get(other["record_index"], -1) < end
            }
            value["addition_covered_transform_count"] = len(covered)
            value["addition_root_template"] = bool(
                value.get("clone_template_node_digest_b")
                and covered == transform_digests
            )
        has_complete_root = any(
            value.get("addition_root_template") for value in item["candidates"]
        )
        complete_root_digests = {
            value["transform_digest_b"]
            for value in item["candidates"]
            if value.get("addition_root_template")
        }
        # Several exported prefabs put their root model and shared LOD/body
        # models at the same world position.  Name diversity makes the raw
        # positional match look ambiguous, but a unique cloneable root whose
        # subtree contains every candidate transform is authoritative.  Bind
        # only that root: patching the shared identity transform used by the
        # LOD records would move unrelated objects globally.
        source_transform_count = transform_count
        if item["status"] == "ambiguous" and len(complete_root_digests) == 1:
            root_digest = next(iter(complete_root_digests))
            item["bundle_members"] = item["candidates"]
            item["candidates"] = [
                value for value in item["candidates"]
                if value["transform_digest_b"] == root_digest
            ]
            item["status"] = "matched_bundle"
            item["link_kind"] = "complete_subtree_root"
            candidates_by_transform = defaultdict(list)
            for value in item["candidates"]:
                candidates_by_transform[value["transform_digest_b"]].append(value)
            transform_count = len(candidates_by_transform)
            has_complete_root = True
        item["addition_supported"] = (
            item["status"] in {"matched", "matched_offset", "matched_bundle"}
            and transform_count > 0
            and (
                has_complete_root
                or all(
                    any(value.get("clone_template_node_digest_b") for value in values)
                    for values in candidates_by_transform.values()
                )
            )
        )
        item["addition_component_count"] = transform_count
        item["source_component_count"] = source_transform_count
        item["addition_uses_complete_root"] = has_complete_root

    # Some prefab types are absent as individual instances from the official
    # Unity scene, so no coordinate identity can exist for updates, while the
    # FST still contains an exact semantic, self-contained root usable for new
    # instances.  Keep these templates separate from links: they authorize
    # additions only and can never turn an unmatched shipped object into an
    # update target.
    verified_addition_only_roots = {
        # Unity prefab GUID: (allowed parents, exact FST root name, root offset)
        "eb329672ee9f37f48ae4b497ba30e70e": (
            ("A1",), "Fences_Wood_Plank_A_01", (0.0, 0.0, 0.0)
        ),  # FencePlankA_2m
        "269394d6f5b2c5a409342be43ff7bcd3": (
            ("Bush",), "DeadBushA1", (0.0, 0.0, 0.0)
        ),  # BushA_Dead
    }
    addition_templates = []
    for guid, (allowed_parents, root_name, relative_position) in (
        verified_addition_only_roots.items()
    ):
        roots = [
            binding for binding in models
            if str(binding.node["name"]) == root_name and cloneable(binding)
        ]
        prefab_link = next(
            (item for item in links if item["prefab_guid"] == guid), None
        )
        if len(roots) != 1 or prefab_link is None:
            continue
        root = roots[0]
        addition_templates.append({
            "instance_file_id": -root.record_index,
            "prefab_guid": guid,
            "prefab_path": prefab_link["prefab_path"],
            "parent_name": allowed_parents[0],
            "allowed_parent_names": list(allowed_parents),
            "fst_position": (0.0, 0.0, 0.0),
            "fst_quaternion": (0.0, 0.0, 0.0, 1.0),
            "fst_scale": (1.0, 1.0, 1.0),
            "status": "addition_template",
            "addition_supported": True,
            "link_kind": "verified_addition_only_root",
            "candidates": [{
                "record_index": root.record_index,
                "node_digest_b": root.node_digest_b.hex(),
                "transform_digest_b": root.transform.digest_b.hex(),
                "cloneable": True,
                "clone_template_node_digest_b": root.node_digest_b.hex(),
                "fst_name": root.node["name"],
                "relative_position": relative_position,
                "relative_quaternion": (0.0, 0.0, 0.0, 1.0),
                "relative_scale": (1.0, 1.0, 1.0),
                "addition_root_template": True,
            }],
        })
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
        "addition_templates": addition_templates,
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
