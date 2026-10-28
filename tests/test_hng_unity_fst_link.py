from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import hng_unity_fst_link  # noqa: E402
import hng_unity_fst_compile  # noqa: E402
import fst_inspect  # noqa: E402


PROJECT = Path(r"D:\hng-map\Project")
ASSETS = PROJECT / "Assets"
SCENE = ASSETS / "Scene" / "T01M08_ColmarHamlet_173288-QQ.unity"
FST = Path(
    r"D:\HnG-test\_packed\Environments\Scenes\Gamemaps\T01M08_Mill.fst.bak_pre_clone"
)


def test_farm_plot_additions_select_the_scale_specific_complete_root() -> None:
    manifest_path = Path(r"D:\HnG-test\work\T01M08_ColmarHamlet.fst-link.json")
    if not manifest_path.is_file():
        pytest.skip("local Unity/FST identity manifest is unavailable")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    templates = [
        item for item in manifest["links"]
        if item["prefab_path"].endswith("FarmPlotA_GroupA.prefab")
        and item["addition_supported"]
    ]
    assert len(templates) == 48
    for scale, expected_name in ((0.849, "Cabbage1A1"), (1.0, "Cabbage1B1")):
        selected = hng_unity_fst_compile.select_addition_template(
            SimpleNamespace(
                hng_scale=(scale, scale, scale),
                hng_quaternion=(0.0, 0.0, 0.0, 1.0),
            ),
            templates,
        )
        roots = [
            candidate for candidate in selected["candidates"]
            if candidate.get("addition_root_template")
        ]
        assert len(roots) == 1
        assert roots[0]["fst_name"].startswith(expected_name)


def test_all_twelve_mill_sandbags_link_uniquely_to_contextual_record_pairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not SCENE.is_file() or not FST.is_file():
        pytest.skip("local Unity and HnG assets are unavailable")
    result = hng_unity_fst_link.link_scene(SCENE, ASSETS, FST)
    sandbags = [
        item
        for item in result["links"]
        if item["prefab_path"].endswith("FenceSandbagA_BLowStraight_4m.prefab")
    ]
    assert len(sandbags) == 12
    assert all(item["parent_name"] == "O1" for item in sandbags)
    assert all(item["status"] == "matched" for item in sandbags)
    assert all(len(item["candidates"]) == 2 for item in sandbags)
    assert max(
        candidate["position_error"]
        for item in sandbags
        for candidate in item["candidates"]
    ) < 0.008
    assert max(
        candidate["quaternion_error"]
        for item in sandbags
        for candidate in item["candidates"]
    ) < 2.2e-5

    link_path = tmp_path / "mill-link.json"
    link_path.write_text(json.dumps(result), encoding="utf-8")
    plan = hng_unity_fst_compile.plan_changes(SCENE, ASSETS, link_path)
    assert plan["summary"] == {
        "updates": 0,
        "added": 0,
        "deleted": 0,
        "unsupported_updates": 0,
        "unsupported_added": 0,
    }

    current_instances = hng_unity_fst_compile.parse_scene(SCENE, ASSETS)
    tent_instance = next(
        item for item in current_instances
        if item.parent_name == "B1" and item.prefab_path.endswith("TentA_GroupB.prefab")
    )
    a1_brick = next(
        item for item in current_instances
        if item.parent_name == "A1" and item.prefab_path.endswith("FenceBrickA_2m.prefab")
    )
    bush = next(
        item for item in current_instances
        if item.parent_name == "Bush" and item.prefab_path.endswith("BushB.prefab")
    )
    tree = next(
        item for item in current_instances
        if item.parent_name == "Tree"
        and item.prefab_path.endswith("TreeBeechA_MatureD_AutumnYellow.prefab")
    )
    plank = next(
        item for item in current_instances
        if item.parent_name == "A1" and item.prefab_path.endswith("FencePlankA_2m.prefab")
    )
    dead_bush = next(
        item for item in current_instances
        if item.parent_name == "Bush" and item.prefab_path.endswith("BushA_Dead.prefab")
    )
    additions = [
        replace(
            tent_instance,
            instance_file_id=9_100_000_001,
            hng_position=(tent_instance.hng_position[0] + 500.0, *tent_instance.hng_position[1:]),
        ),
        replace(
            a1_brick,
            instance_file_id=9_100_000_002,
            hng_position=(a1_brick.hng_position[0] + 500.0, *a1_brick.hng_position[1:]),
        ),
        replace(
            bush,
            instance_file_id=9_100_000_003,
            hng_position=(bush.hng_position[0] + 500.0, *bush.hng_position[1:]),
        ),
        replace(
            tree,
            instance_file_id=9_100_000_004,
            hng_position=(tree.hng_position[0] + 500.0, *tree.hng_position[1:]),
        ),
        replace(
            plank,
            instance_file_id=9_100_000_005,
            hng_position=(plank.hng_position[0] + 500.0, *plank.hng_position[1:]),
        ),
        replace(
            dead_bush,
            instance_file_id=9_100_000_006,
            hng_position=(dead_bush.hng_position[0] + 500.0, *dead_bush.hng_position[1:]),
        ),
    ]
    original_parse_scene = hng_unity_fst_compile.parse_scene
    monkeypatch.setattr(
        hng_unity_fst_compile,
        "parse_scene",
        lambda scene, assets: current_instances + additions,
    )
    multi_parent_add = hng_unity_fst_compile.plan_changes(SCENE, ASSETS, link_path)
    monkeypatch.setattr(hng_unity_fst_compile, "parse_scene", original_parse_scene)
    assert multi_parent_add["summary"]["added"] == 6
    assert multi_parent_add["summary"]["unsupported_added"] == 0
    assert {
        item["parent_name"] for item in multi_parent_add["added"]
    } == {"A1", "B1", "Bush", "Tree"}
    assert {
        Path(item["prefab_path"]).name for item in multi_parent_add["added"]
    } >= {"FencePlankA_2m.prefab", "BushA_Dead.prefab"}

    deletable = next(
        item for item in result["links"]
        if any(
            candidate["record_index"] == 19347
            for candidate in item.get("candidates", ())
        )
    )
    second_deletable = next(
        item for item in sandbags
        if item["instance_file_id"] != deletable["instance_file_id"]
    )
    deleted_ids = {
        deletable["instance_file_id"], second_deletable["instance_file_id"]
    }
    without_deletable = [
        item for item in current_instances
        if item.instance_file_id not in deleted_ids
    ]
    monkeypatch.setattr(
        hng_unity_fst_compile,
        "parse_scene",
        lambda scene, assets: without_deletable,
    )
    deletion_plan = hng_unity_fst_compile.plan_changes(SCENE, ASSETS, link_path)
    monkeypatch.setattr(hng_unity_fst_compile, "parse_scene", original_parse_scene)
    assert deletion_plan["summary"]["deleted"] == 2
    assert deletion_plan["summary"]["unsupported_updates"] == 0
    assert set(deletion_plan["deleted_instance_file_ids"]) == deleted_ids
    deletion_path = tmp_path / "mill-deleted-official-sandbag.fst"
    hng_unity_fst_compile.apply_update_plan(FST, deletion_plan, deletion_path)
    deletion_stream = fst_inspect.parse_fst(deletion_path)
    deletion_transforms = fst_inspect.locate_kinematics(deletion_stream)
    deletion_graph = fst_inspect.parse_reference_graph(
        deletion_stream, deletion_transforms
    )
    deletion_properties = fst_inspect.map_kinematics_property_sets(
        deletion_stream, deletion_transforms
    )
    deletion_bindings = fst_inspect.bind_scene_nodes_to_transforms(
        deletion_stream, deletion_graph, deletion_properties
    )
    deleted_root_digest = bytes.fromhex(
        next(
            candidate["node_digest_b"]
            for deletion in deletion_plan["deleted"]
            if deletion["instance_file_id"] == deletable["instance_file_id"]
            for candidate in deletion["targets"]
            if candidate["record_index"] == 19347
        )
    )
    assert all(
        item.node_digest_b != deleted_root_digest for item in deletion_bindings
    )

    # The shipped 8 m iron fences are each represented by two Unity 4 m
    # segments and one pole.  A rigid move of all three members is one safe FST
    # root update; moving only one member must remain blocked.
    iron_groups = {}
    for link in result["links"]:
        if link.get("composite_group_id"):
            iron_groups.setdefault(link["composite_group_id"], []).append(link)
    assert len(iron_groups) == 3
    assert sorted(len(members) for members in iron_groups.values()) == [3, 3, 3]
    group = next(iter(iron_groups.values()))
    group_ids = {item["instance_file_id"] for item in group}
    rigid_instances = [
        replace(
            item,
            hng_position=(item.hng_position[0] + 100.0, *item.hng_position[1:]),
        ) if item.instance_file_id in group_ids else item
        for item in current_instances
    ]
    monkeypatch.setattr(
        hng_unity_fst_compile, "parse_scene", lambda scene, assets: rigid_instances
    )
    rigid_plan = hng_unity_fst_compile.plan_changes(SCENE, ASSETS, link_path)
    assert rigid_plan["summary"]["updates"] == 1
    assert rigid_plan["summary"]["unsupported_updates"] == 0
    assert rigid_plan["updates"][0]["composite_member_ids"] == sorted(group_ids)
    rigid_path = tmp_path / "mill-rigid-iron-group.fst"
    hng_unity_fst_compile.apply_update_plan(FST, rigid_plan, rigid_path)
    rigid_stream = fst_inspect.parse_fst(rigid_path)
    rigid_transforms = fst_inspect.locate_kinematics(rigid_stream)
    rigid_graph = fst_inspect.parse_reference_graph(rigid_stream, rigid_transforms)
    rigid_properties = fst_inspect.map_kinematics_property_sets(
        rigid_stream, rigid_transforms
    )
    rigid_bindings = {
        item.record_index: item
        for item in fst_inspect.bind_scene_nodes_to_transforms(
            rigid_stream, rigid_graph, rigid_properties
        )
    }
    rigid_update = rigid_plan["updates"][0]
    rigid_target = rigid_update["targets"][0]
    expected_root = hng_unity_fst_compile.compose_transform(
        tuple(rigid_update["new_position"]),
        tuple(rigid_update["new_quaternion"]),
        tuple(rigid_update["new_scale"]),
        rigid_target,
    )
    assert rigid_bindings[rigid_target["record_index"]].transform.position == pytest.approx(
        expected_root[0]
    )

    moved_member_id = min(group_ids)
    broken_instances = [
        replace(
            item,
            hng_position=(item.hng_position[0] + 100.0, *item.hng_position[1:]),
        ) if item.instance_file_id == moved_member_id else item
        for item in current_instances
    ]
    monkeypatch.setattr(
        hng_unity_fst_compile, "parse_scene", lambda scene, assets: broken_instances
    )
    broken_plan = hng_unity_fst_compile.plan_changes(SCENE, ASSETS, link_path)
    monkeypatch.setattr(hng_unity_fst_compile, "parse_scene", original_parse_scene)
    assert broken_plan["summary"]["updates"] == 0
    assert broken_plan["summary"]["unsupported_updates"] == 1
    assert "no longer form one rigid 8 m fence root" in (
        broken_plan["unsupported_updates"][0]["reason"]
    )

    without_group = [
        item for item in current_instances if item.instance_file_id not in group_ids
    ]
    monkeypatch.setattr(
        hng_unity_fst_compile, "parse_scene", lambda scene, assets: without_group
    )
    group_delete_plan = hng_unity_fst_compile.plan_changes(
        SCENE, ASSETS, link_path
    )
    monkeypatch.setattr(hng_unity_fst_compile, "parse_scene", original_parse_scene)
    assert group_delete_plan["summary"]["deleted"] == 1
    assert group_delete_plan["summary"]["unsupported_updates"] == 0
    assert group_delete_plan["deleted"][0]["composite_member_ids"] == sorted(
        group_ids
    )
    group_delete_path = tmp_path / "mill-deleted-iron-group.fst"
    hng_unity_fst_compile.apply_update_plan(
        FST, group_delete_plan, group_delete_path
    )
    group_delete_stream = fst_inspect.parse_fst(group_delete_path)
    group_delete_transforms = fst_inspect.locate_kinematics(group_delete_stream)
    group_delete_graph = fst_inspect.parse_reference_graph(
        group_delete_stream, group_delete_transforms
    )
    group_delete_properties = fst_inspect.map_kinematics_property_sets(
        group_delete_stream, group_delete_transforms
    )
    group_delete_bindings = fst_inspect.bind_scene_nodes_to_transforms(
        group_delete_stream, group_delete_graph, group_delete_properties
    )
    group_root_digests = {
        bytes.fromhex(target["node_digest_b"])
        for target in group_delete_plan["deleted"][0]["targets"]
    }
    assert not group_root_digests.intersection(
        item.node_digest_b for item in group_delete_bindings
    )

    # Perturb the frozen baseline, which is equivalent to moving this stable
    # Unity file ID in the current scene, and verify it becomes one safe update.
    moved = next(item for item in result["links"] if item["instance_file_id"] == sandbags[0]["instance_file_id"])
    moved["fst_position"] = (moved["fst_position"][0] + 10.0, *moved["fst_position"][1:])
    link_path.write_text(json.dumps(result), encoding="utf-8")
    changed = hng_unity_fst_compile.plan_changes(SCENE, ASSETS, link_path)
    assert changed["summary"]["updates"] == 1
    assert changed["updates"][0]["instance_file_id"] == moved["instance_file_id"]
    assert len(changed["updates"][0]["record_indices"]) == 2

    # Verified non-O1 layers use the same audited transform update path.
    moved["fst_position"] = (moved["fst_position"][0] - 10.0, *moved["fst_position"][1:])
    tent = next(
        item for item in result["links"]
        if item["parent_name"] == "B1"
        and item["prefab_path"].endswith("TentA_GroupB.prefab")
    )
    tent["fst_position"] = (tent["fst_position"][0] + 10.0, *tent["fst_position"][1:])
    link_path.write_text(json.dumps(result), encoding="utf-8")
    non_o1_changed = hng_unity_fst_compile.plan_changes(SCENE, ASSETS, link_path)
    assert non_o1_changed["summary"]["updates"] == 1
    assert non_o1_changed["updates"][0]["instance_file_id"] == tent["instance_file_id"]

    compiled_path = tmp_path / "mill-updated.fst"
    hng_unity_fst_compile.apply_update_plan(FST, changed, compiled_path)
    compiled_stream = fst_inspect.parse_fst(compiled_path)
    compiled_transforms = fst_inspect.locate_kinematics(compiled_stream)
    compiled_graph = fst_inspect.parse_reference_graph(
        compiled_stream, compiled_transforms
    )
    compiled_properties = fst_inspect.map_kinematics_property_sets(
        compiled_stream, compiled_transforms
    )
    compiled_bindings = {
        item.record_index: item
        for item in fst_inspect.bind_scene_nodes_to_transforms(
            compiled_stream, compiled_graph, compiled_properties
        )
    }
    first, second = changed["updates"][0]["record_indices"]
    target = changed["updates"][0]["new_position"]
    assert compiled_bindings[first].transform.position == pytest.approx(target)
    assert compiled_bindings[second].transform.position == pytest.approx(target)
    assert compiled_bindings[first].transform.digest_b == compiled_bindings[second].transform.digest_b

    addition_id = 9000000001
    addition_name = f"Unity_{addition_id}_FenceSandbagA_BLowStraight_4m"
    add_plan = {
        "summary": {
            "updates": 0, "added": 1, "deleted": 0,
            "unsupported_updates": 0, "unsupported_added": 0,
        },
        "updates": [],
        "added": [{
            "instance_file_id": addition_id,
            "prefab_path": sandbags[0]["prefab_path"],
            "template_node_digest_b": sandbags[0]["candidates"][0]["node_digest_b"],
            "position": (sandbags[0]["fst_position"][0] + 20.0, *sandbags[0]["fst_position"][1:]),
            "quaternion": sandbags[0]["fst_quaternion"],
            "scale": sandbags[0]["fst_scale"],
        }],
        "deleted_instance_file_ids": [],
        "unsupported_updates": [],
        "unsupported_added": [],
    }
    added_path = tmp_path / "mill-added.fst"
    hng_unity_fst_compile.apply_update_plan(FST, add_plan, added_path)
    added_stream = fst_inspect.parse_fst(added_path)
    added_transforms = fst_inspect.locate_kinematics(added_stream)
    added_graph = fst_inspect.parse_reference_graph(added_stream, added_transforms)
    added_properties = fst_inspect.map_kinematics_property_sets(added_stream, added_transforms)
    added_bindings = fst_inspect.bind_scene_nodes_to_transforms(
        added_stream, added_graph, added_properties
    )
    new_binding = next(item for item in added_bindings if item.node["name"] == addition_name)
    assert new_binding.transform.position == pytest.approx(add_plan["added"][0]["position"])

    # Compiler-created additions are now reversible at the object-graph level.
    # Removing the complete Unity_* root must restore the original record and
    # DFS-stream sizes without leaving a bindable SceneNode behind.
    removed_bytes = fst_inspect.remove_cloned_scene_tree(
        added_stream, added_graph, new_binding
    )
    removed_path = tmp_path / "mill-added-then-removed.fst"
    removed_path.write_bytes(removed_bytes)
    removed_stream = fst_inspect.parse_fst(removed_path)
    removed_transforms = fst_inspect.locate_kinematics(removed_stream)
    removed_graph = fst_inspect.parse_reference_graph(
        removed_stream, removed_transforms
    )
    removed_properties = fst_inspect.map_kinematics_property_sets(
        removed_stream, removed_transforms
    )
    removed_bindings = fst_inspect.bind_scene_nodes_to_transforms(
        removed_stream, removed_graph, removed_properties
    )
    original_stream = fst_inspect.parse_fst(FST)
    original_transforms = fst_inspect.locate_kinematics(original_stream)
    original_graph = fst_inspect.parse_reference_graph(
        original_stream, original_transforms
    )
    original_properties = fst_inspect.map_kinematics_property_sets(
        original_stream, original_transforms
    )
    original_bindings = fst_inspect.bind_scene_nodes_to_transforms(
        original_stream, original_graph, original_properties
    )
    assert all(item.node["name"] != addition_name for item in removed_bindings)
    assert len(removed_graph.flat_records) == len(original_graph.flat_records)
    assert len(removed_graph.relation_values) == len(original_graph.relation_values)

    # A shipped root may use the same deletion path only when the conservative
    # first-occurrence audit proves it is safe.  This complete outer sandbag
    # root needs one shared expansion relocated to a surviving back-reference,
    # exercising the path required by most official logical instances.
    safe_shipped = next(
        item for item in original_bindings
        if item.record_index == 19347 and item.node["name"] == "SandbagWallA_3m_highB"
    )
    shipped_removed_bytes = fst_inspect.remove_cloned_scene_tree(
        original_stream,
        original_graph,
        safe_shipped,
        allow_shipped=True,
        relocate_shared=True,
    )
    shipped_removed_path = tmp_path / "mill-shipped-sandbag-removed.fst"
    shipped_removed_path.write_bytes(shipped_removed_bytes)
    shipped_stream = fst_inspect.parse_fst(shipped_removed_path)
    shipped_transforms = fst_inspect.locate_kinematics(shipped_stream)
    shipped_graph = fst_inspect.parse_reference_graph(
        shipped_stream, shipped_transforms
    )
    shipped_properties = fst_inspect.map_kinematics_property_sets(
        shipped_stream, shipped_transforms
    )
    shipped_bindings = fst_inspect.bind_scene_nodes_to_transforms(
        shipped_stream, shipped_graph, shipped_properties
    )
    assert len(shipped_graph.flat_records) < len(original_graph.flat_records)
    assert all(
        item.node_digest_b != safe_shipped.node_digest_b
        for item in shipped_bindings
    )
    assert sum(
        item.node["name"] == "SandbagWallA_3m_highB"
        for item in shipped_bindings
    ) < sum(
        item.node["name"] == "SandbagWallA_3m_highB"
        for item in original_bindings
    )


def test_position_link_preserves_a_prefabs_intrinsic_rotation(tmp_path: Path) -> None:
    if not SCENE.is_file() or not FST.is_file():
        pytest.skip("local Unity and HnG assets are unavailable")
    result = hng_unity_fst_link.link_scene(SCENE, ASSETS, FST)
    assert result["status_by_parent_name"]["O1"]["matched_offset"] >= 70
    rock_fences = [
        item for item in result["links"]
        if item["prefab_path"].endswith("FenceRockA_8m.prefab")
        and item["parent_name"] == "O1"
    ]
    assert len(rock_fences) == 17
    assert all(item["status"] == "matched_offset" for item in rock_fences)
    brick_2m = [
        item for item in result["links"]
        if item["prefab_path"].endswith("FenceBrickA_2m.prefab")
        and item["parent_name"] == "O1"
    ]
    assert len(brick_2m) == 12
    assert all(item["status"] == "matched_offset" for item in brick_2m)
    assert all(
        str(item["candidates"][0]["fst_name"]).startswith("FenceE_2m_End1")
        for item in brick_2m
    )
    verified_buildings = {
        "ShedB_GroupA.prefab": "House5C1",
        "PoliceStationA_HouseC_GroupA.prefab": "House4C_PoliceStation_main1",
        "BarnA_A_GroupA.prefab": "House3B_Barn1",
    }
    for suffix, root_name in verified_buildings.items():
        building = next(
            item for item in result["links"]
            if item["parent_name"] == "O1"
            and item["prefab_path"].endswith(suffix)
        )
        assert building["status"] == "matched_offset"
        assert building["addition_supported"]
        assert building["candidates"][0]["fst_name"] == root_name
    assert sum(
        item["status"] == "matched_offset" and item["parent_name"] == "B1"
        for item in result["links"]
    ) == 6
    assert sum(
        item["status"] == "matched_offset" and item["parent_name"] == "Misc"
        for item in result["links"]
    ) == 7
    damaged_bricks = [
        item for item in result["links"]
        if item["parent_name"] == "A1"
        and item["prefab_path"].endswith("FenceBrickA_4m_Damaged.prefab")
    ]
    assert len(damaged_bricks) == 3
    assert all(item["status"] == "matched_offset" for item in damaged_bricks)
    assert all(item["addition_supported"] for item in damaged_bricks)
    for item in damaged_bricks:
        candidate = item["candidates"][0]
        assert candidate["fst_name"].startswith("FenceE_4m_SupportBeams1")
        assert tuple(candidate["relative_position"]) == pytest.approx(
            (0.0, -6.0, 0.0), abs=0.02
        )
    learned_records = [
        candidate["record_index"]
        for item in result["links"]
        if item["status"] == "matched_offset"
        for candidate in item["candidates"]
    ]
    assert len(learned_records) == len(set(learned_records))
    rotated = max(
        (
            item
            for item in result["links"]
            if item["status"] == "matched" and item["parent_name"] == "O1"
        ),
        key=lambda item: max(value["quaternion_error"] for value in item["candidates"]),
    )
    assert max(value["quaternion_error"] for value in rotated["candidates"]) > 1.0

    new_position = (rotated["fst_position"][0] + 25.0, *rotated["fst_position"][1:])
    plan = {
        "summary": {
            "updates": 1, "added": 0, "deleted": 0,
            "unsupported_updates": 0, "unsupported_added": 0,
        },
        "updates": [{
            "instance_file_id": rotated["instance_file_id"],
            "new_position": new_position,
            "new_quaternion": rotated["fst_quaternion"],
            "new_scale": rotated["fst_scale"],
            "targets": rotated["candidates"],
        }],
        "added": [],
        "deleted_instance_file_ids": [],
        "unsupported_updates": [],
        "unsupported_added": [],
    }
    compiled_path = tmp_path / "rotated-prefab.fst"
    hng_unity_fst_compile.apply_update_plan(FST, plan, compiled_path)
    stream = fst_inspect.parse_fst(compiled_path)
    transforms = fst_inspect.locate_kinematics(stream)
    graph = fst_inspect.parse_reference_graph(stream, transforms)
    properties = fst_inspect.map_kinematics_property_sets(stream, transforms)
    bindings = {
        item.record_index: item
        for item in fst_inspect.bind_scene_nodes_to_transforms(stream, graph, properties)
    }
    candidate = rotated["candidates"][0]
    expected_position, expected_quaternion, expected_scale = (
        hng_unity_fst_compile.compose_transform(
            new_position,
            tuple(rotated["fst_quaternion"]),
            tuple(rotated["fst_scale"]),
            candidate,
        )
    )
    actual = bindings[candidate["record_index"]].transform
    assert actual.position == pytest.approx(expected_position)
    assert actual.quaternion == pytest.approx(expected_quaternion)
    assert actual.scale == pytest.approx(expected_scale)
    assert hng_unity_fst_compile.quaternion_distance(
        actual.quaternion, tuple(rotated["fst_quaternion"])
    ) > 1.0

    rock = next(item for item in rock_fences if item["addition_supported"])
    template = next(value for value in rock["candidates"] if value["cloneable"])
    addition_id = 9000000002
    addition_position = (rock["fst_position"][0] + 30.0, *rock["fst_position"][1:])
    add_plan = {
        "summary": {
            "updates": 0, "added": 1, "deleted": 0,
            "unsupported_updates": 0, "unsupported_added": 0,
        },
        "updates": [],
        "added": [{
            "instance_file_id": addition_id,
            "prefab_path": rock["prefab_path"],
            "position": addition_position,
            "quaternion": rock["fst_quaternion"],
            "scale": rock["fst_scale"],
            "template_components": [{
                "node_digest_b": template["node_digest_b"],
                "fst_name": template["fst_name"],
                **{
                    key: template[key]
                    for key in ("relative_position", "relative_quaternion", "relative_scale")
                },
            }],
        }],
        "deleted_instance_file_ids": [],
        "unsupported_updates": [],
        "unsupported_added": [],
    }
    added_path = tmp_path / "offset-add.fst"
    hng_unity_fst_compile.apply_update_plan(FST, add_plan, added_path)
    stream = fst_inspect.parse_fst(added_path)
    transforms = fst_inspect.locate_kinematics(stream)
    graph = fst_inspect.parse_reference_graph(stream, transforms)
    properties = fst_inspect.map_kinematics_property_sets(stream, transforms)
    bindings = fst_inspect.bind_scene_nodes_to_transforms(stream, graph, properties)
    name = f"Unity_{addition_id}_{Path(rock['prefab_path']).stem}"
    added = next(item for item in bindings if item.node["name"] == name)
    expected = hng_unity_fst_compile.compose_transform(
        addition_position,
        tuple(rock["fst_quaternion"]),
        tuple(rock["fst_scale"]),
        template,
    )
    assert added.transform.position == pytest.approx(expected[0])
    assert added.transform.quaternion == pytest.approx(expected[1])

    compound = next(
        item for item in result["links"]
        if item["parent_name"] == "O1"
        and item["addition_supported"]
        and item["addition_component_count"] > 1
    )
    representatives = {}
    root_candidates = [
        candidate for candidate in compound["candidates"]
        if candidate.get("addition_root_template")
    ]
    for candidate in root_candidates or compound["candidates"]:
        digest = candidate["transform_digest_b"]
        previous = representatives.get(digest)
        if previous is None or (
            candidate.get("clone_template_node_digest_b")
            and not previous.get("clone_template_node_digest_b")
        ):
            representatives[digest] = candidate
    components = [{
        "node_digest_b": candidate.get(
            "clone_template_node_digest_b", candidate["node_digest_b"]
        ),
        "fst_name": candidate["fst_name"],
        **{
            key: candidate[key]
            for key in ("relative_position", "relative_quaternion", "relative_scale")
        },
    } for candidate in representatives.values()]
    compound_id = 9000000003
    compound_plan = {
        "summary": {
            "updates": 0, "added": 1, "deleted": 0,
            "unsupported_updates": 0, "unsupported_added": 0,
        },
        "updates": [],
        "added": [{
            "instance_file_id": compound_id,
            "prefab_path": compound["prefab_path"],
            "position": compound["fst_position"],
            "quaternion": compound["fst_quaternion"],
            "scale": compound["fst_scale"],
            "template_components": components,
        }],
        "deleted_instance_file_ids": [],
        "unsupported_updates": [],
        "unsupported_added": [],
    }
    compound_path = tmp_path / "compound-add.fst"
    hng_unity_fst_compile.apply_update_plan(FST, compound_plan, compound_path)
    stream = fst_inspect.parse_fst(compound_path)
    transforms = fst_inspect.locate_kinematics(stream)
    graph = fst_inspect.parse_reference_graph(stream, transforms)
    properties = fst_inspect.map_kinematics_property_sets(stream, transforms)
    bindings = fst_inspect.bind_scene_nodes_to_transforms(stream, graph, properties)
    prefix = f"Unity_{compound_id}_{Path(compound['prefab_path']).stem}"
    names = {str(item.node["name"]) for item in bindings if str(item.node["name"]).startswith(prefix)}
    assert len(names) == len(components)
