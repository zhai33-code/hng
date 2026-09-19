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
    assert compiled_bindings[first].transform.digest_b != compiled_bindings[second].transform.digest_b
