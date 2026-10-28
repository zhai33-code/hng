from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "hng_unity_scene.py"
SPEC = importlib.util.spec_from_file_location("hng_unity_scene", MODULE_PATH)
assert SPEC and SPEC.loader
hng_unity_scene = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = hng_unity_scene
SPEC.loader.exec_module(hng_unity_scene)

PROJECT = Path(r"D:\hng-map\Project")
ASSETS = PROJECT / "Assets"
MILL_SCENE = ASSETS / "Scene" / "T01M08_ColmarHamlet_173288-QQ.unity"


def require_project() -> None:
    if not MILL_SCENE.is_file():
        pytest.skip("Unity reconstruction project is unavailable")


def test_mill_scene_prefab_instances_and_coordinate_conversion() -> None:
    require_project()
    instances = hng_unity_scene.parse_scene(MILL_SCENE, ASSETS)
    assert len(instances) > 6000
    sandbag = next(
        item
        for item in instances
        if item.prefab_path.endswith("FenceSandbagA_BLowStraight_4m.prefab")
        and abs(item.unity_position[0] - -15.096108) < 1e-6
    )
    assert sandbag.name == "FenceSandbagA_BLowStraight_4m"
    assert sandbag.parent_name == "O1"
    assert sandbag.hng_position == pytest.approx(
        (-150.96108, 57.996297, 336.89682)
    )
    assert sandbag.hng_quaternion == pytest.approx(
        (-0.0000076198858, 0.25882012, -0.0000036253784, 0.9659255)
    )
