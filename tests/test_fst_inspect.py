from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "fst_inspect.py"
SPEC = importlib.util.spec_from_file_location("fst_inspect", MODULE_PATH)
assert SPEC and SPEC.loader
fst_inspect = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fst_inspect
SPEC.loader.exec_module(fst_inspect)

ASSET_ROOT = Path(r"D:\HnG-test\_packed")
MAP_ROOT = ASSET_ROOT / "Environments" / "Scenes" / "Gamemaps"
SCHEMA = ASSET_ROOT / "_schemas" / "000417b244e3b07175a81ba446daace8.bin"
# These roundtrip/cache tests assert exact structural counts of the official
# original map.  The live T01M08_Mill.fst is the deployed (post-clone-fix) build,
# so pin them to the pristine pre-clone backup to keep the baseline stable.
OFFICIAL_MILL = MAP_ROOT / "T01M08_Mill.fst.bak_pre_clone"


def require_assets() -> None:
    if not MAP_ROOT.is_dir() or not SCHEMA.is_file():
        pytest.skip("local HnG extracted assets are unavailable")


def test_all_official_fst_wrappers_and_group_tables() -> None:
    require_assets()
    maps = sorted(MAP_ROOT.glob("*.fst"))
    assert len(maps) == 15
    for path in maps:
        stream = fst_inspect.parse_fst(path)
        assert hashlib.md5(stream.payload).digest() == stream.decompressed[:16]
        assert stream.schema_digest.hex() == SCHEMA.stem
        assert stream.groups
        assert stream.group_table_end < stream.inline_scan_end


def test_rs03_schema_and_kinematics_layout() -> None:
    require_assets()
    schema = fst_inspect.parse_rs03(SCHEMA)
    assert len(schema.classes) == 751
    assert schema.by_name("ZSceneNode").schema_id == 434
    assert schema.by_name("ZPropertySet").schema_id == 438

    local = schema.by_name("ZLocalKinematics")
    assert local.schema_id == 730
    assert [schema.names[field.name_index] for field in local.fields] == [
        "posx", "posy", "posz",
        "quatx", "quaty", "quatz", "quatw",
        "sclx", "scly", "sclz",
        "nposx", "nposy", "nposz",
        "nrotx", "nroty", "nrotz",
        ":BaseClass",
    ]
    assert all(field.type_id == -18 for field in local.fields[:-1])
    assert local.fields[-1].type_id == 0
    assert schema.by_name("ZKinematics").schema_id == 731


def test_official_fst_lossless_roundtrip() -> None:
    require_assets()
    schema = fst_inspect.parse_rs03(SCHEMA)
    for path in sorted(MAP_ROOT.glob("*.fst")):
        stream = fst_inspect.parse_fst(path)
        entries, reference_offset = fst_inspect.parse_cache_entries(stream, schema)
        assert entries
        assert reference_offset < len(stream.decompressed)
        rebuilt = fst_inspect.rebuild_decompressed(
            stream, schema, entries, reference_offset
        )
        assert rebuilt == stream.decompressed, path.name


def test_mill_inline_cache_is_digest_verified_and_finds_sandbags(tmp_path: Path) -> None:
    require_assets()
    stream = fst_inspect.parse_fst(OFFICIAL_MILL)
    assert len(stream.groups) == 87
    assert stream.feature_value == 33
    assert len(stream.inline_objects) == 9238
    assert stream.inline_sentinels == 293
    assert all(hashlib.md5(item.payload).digest() == item.digest for item in stream.inline_objects)
    sandbags = []
    for item in fst_inspect.iter_schema_objects(stream, 434):
        node = fst_inspect.decode_scene_node(item.payload)
        if "sandbag" in node["name"].lower():
            sandbags.append(node)
    assert sandbags

    transforms = fst_inspect.locate_kinematics(stream)
    assert len(transforms) == 2365
    assert transforms[0].offset == 0x899594
    assert all(
        hashlib.md5(stream.decompressed[item.offset:item.offset + 64]).digest()
        == item.digest_a
        for item in transforms
    )
    assert all(
        hashlib.md5((731).to_bytes(4, "little") + item.digest_a).digest()
        == item.digest_b
        for item in transforms
    )
    property_map = fst_inspect.map_kinematics_property_sets(stream, transforms)
    assert len(property_map) == 2365
    graph = fst_inspect.parse_reference_graph(stream, transforms)
    assert graph.relation_offset == 0x8BEE10
    assert len(graph.relation_values) == 172099
    assert len(graph.roots) == 35
    assert len(graph.children_by_digest_b) == 51255
    bindings = fst_inspect.bind_scene_nodes_to_transforms(stream, graph, property_map)
    sandbag_models = [
        item for item in bindings
        if item.node["node_type"] == "#model"
        and "sandbag" in item.node["name"].lower()
    ]
    assert sandbag_models
    assert all(item.transform.digest_a for item in sandbag_models)

    selected = sandbag_models[0]
    moved_position = (
        selected.transform.position[0] + 0.125,
        selected.transform.position[1],
        selected.transform.position[2],
    )
    patched_bytes = fst_inspect.patch_kinematics_transform(
        stream, graph, selected, position=moved_position
    )
    patched_path = tmp_path / "T01M08_Mill-patched.fst"
    patched_path.write_bytes(patched_bytes)
    patched = fst_inspect.parse_fst(patched_path)
    patched_transforms = fst_inspect.locate_kinematics(patched)
    patched_graph = fst_inspect.parse_reference_graph(patched, patched_transforms)
    patched_properties = fst_inspect.map_kinematics_property_sets(
        patched, patched_transforms
    )
    patched_bindings = fst_inspect.bind_scene_nodes_to_transforms(
        patched, patched_graph, patched_properties
    )
    rebound = next(
        item for item in patched_bindings
        if item.node_digest_b == selected.node_digest_b
    )
    assert rebound.transform.position == pytest.approx(moved_position)
    assert rebound.transform.quaternion == pytest.approx(selected.transform.quaternion)
    assert rebound.transform.scale == pytest.approx(selected.transform.scale)


def test_insert_cloned_instance_roundtrip(tmp_path: Path) -> None:
    require_assets()
    stream = fst_inspect.parse_fst(OFFICIAL_MILL)
    schema = fst_inspect.parse_rs03(SCHEMA)
    transforms = fst_inspect.locate_kinematics(stream)
    property_map = fst_inspect.map_kinematics_property_sets(stream, transforms)
    graph = fst_inspect.parse_reference_graph(stream, transforms)
    bindings = fst_inspect.bind_scene_nodes_to_transforms(stream, graph, property_map)
    template = next(
        item for item in bindings
        if item.node["node_type"] == "#model"
        and "sandbag" in item.node["name"].lower()
    )

    clone_name = "sandbag_clone_test"
    clone_position = (
        template.transform.position[0] + 50.0,
        template.transform.position[1] + 1.25,
        template.transform.position[2] - 7.5,
    )
    clone_bytes = fst_inspect.insert_cloned_instance(
        stream,
        schema,
        graph,
        template,
        name=clone_name,
        position=clone_position,
        quaternion=template.transform.quaternion,
    )
    clone_path = tmp_path / "T01M08_Mill-clone.fst"
    clone_path.write_bytes(clone_bytes)

    clone_stream = fst_inspect.parse_fst(clone_path)
    clone_transforms = fst_inspect.locate_kinematics(clone_stream)
    assert len(clone_transforms) == 2366
    clone_graph = fst_inspect.parse_reference_graph(clone_stream, clone_transforms)
    clone_properties = fst_inspect.map_kinematics_property_sets(
        clone_stream, clone_transforms
    )
    clone_bindings = fst_inspect.bind_scene_nodes_to_transforms(
        clone_stream, clone_graph, clone_properties
    )
    # The clone splices the template's complete owned SceneNode skeleton
    # (root + collision child + mesh + LODs), each node carrying its own
    # Kinematics property/transform, so it adds 7 contextual bindings rather
    # than just the root.
    assert len(clone_bindings) == len(bindings) + 7

    clone_binding = next(
        item
        for item in clone_bindings
        if item.node["name"] == clone_name
        and item.node["node_type"] == template.node["node_type"]
    )
    assert clone_binding.transform.position == pytest.approx(clone_position)
    assert clone_binding.transform.quaternion == pytest.approx(
        template.transform.quaternion
    )
    assert clone_binding.transform.scale == pytest.approx(template.transform.scale)

    template_binding = next(
        item for item in clone_bindings
        if item.node_digest_b == template.node_digest_b
    )
    assert template_binding.transform.position == pytest.approx(
        template.transform.position
    )
    assert template_binding.transform.quaternion == pytest.approx(
        template.transform.quaternion
    )
