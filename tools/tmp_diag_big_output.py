import json
from collections import Counter
from pathlib import Path

from fst_inspect import (
    _reference_positions,
    bind_scene_nodes_to_transforms,
    locate_kinematics,
    map_kinematics_property_sets,
    parse_fst,
    parse_reference_graph,
)


def load(path):
    stream = parse_fst(path)
    transforms = locate_kinematics(stream)
    graph = parse_reference_graph(stream, transforms)
    props = map_kinematics_property_sets(stream, transforms)
    bindings = bind_scene_nodes_to_transforms(stream, graph, props)
    return stream, graph, bindings


plan = json.loads(Path(r"D:\HnG-test\work\T01M08_ColmarHamlet_custom.plan.json").read_text(encoding="utf-8"))
base_stream, base_graph, base_bindings = load(Path(r"D:\HnG-test\_packed\Environments\Scenes\Gamemaps\T01M08_Mill.fst.bak_pre_clone"))
out_stream, out_graph, out_bindings = load(Path(r"D:\HnG-test\work\T01M08_Mill.custom.fst"))

print("PLAN", plan["summary"])
for addition in plan["added"]:
    print("ADD", addition["instance_file_id"], addition["position"], len(addition["template_components"]))

print("\nOUTPUT UNITY ROOT BINDINGS")
roots = [b for b in out_bindings if str(b.node["name"]).startswith("Unity_")]
for b in roots:
    print(b.node["name"], tuple(round(v, 3) for v in b.transform.position), b.node["node_type"])
print("root count", len(roots))

print("\nBASE TEMPLATE SUBTREES")
positions = _reference_positions(base_graph)
by_digest = {}
for binding in base_bindings:
    by_digest.setdefault(binding.node_digest_b.hex(), binding)
components = plan["added"][0]["template_components"]
for component in components:
    binding = by_digest[component["node_digest_b"]]
    record = base_graph.flat_records[binding.record_index - 1][1]
    start = positions[binding.node_digest_b]
    end = start + record.value
    names = []
    for other in base_bindings:
        pos = positions.get(other.node_digest_b, -1)
        if start <= pos < end:
            names.append(str(other.node["name"]))
    print(component["fst_name"], "span", record.value, "bound_models", len(names), Counter(names).most_common(8))
