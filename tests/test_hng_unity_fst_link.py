from __future__ import annotations

import json
import sys
from pathlib import Path

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


def test_all_twelve_mill_sandbags_link_uniquely_to_contextual_record_pairs(
    tmp_path: Path,
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

    # Perturb the frozen baseline, which is equivalent to moving this stable
    # Unity file ID in the current scene, and verify it becomes one safe update.
    moved = next(item for item in result["links"] if item["instance_file_id"] == sandbags[0]["instance_file_id"])
    moved["fst_position"] = (moved["fst_position"][0] + 10.0, *moved["fst_position"][1:])
    link_path.write_text(json.dumps(result), encoding="utf-8")
    changed = hng_unity_fst_compile.plan_changes(SCENE, ASSETS, link_path)
    assert changed["summary"]["updates"] == 1
    assert changed["updates"][0]["instance_file_id"] == moved["instance_file_id"]
    assert len(changed["updates"][0]["record_indices"]) == 2

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
    for candidate in compound["candidates"]:
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
    prefix = f"Unity_{compound_id}_{Path(compound['prefab_path']).stem}__"
    names = {str(item.node["name"]) for item in bindings if str(item.node["name"]).startswith(prefix)}
    assert len(names) == len(components)
