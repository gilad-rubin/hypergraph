#!/usr/bin/env python3
"""Generate debug HTML for hypergraph visualization and extract debug info."""
import argparse
import importlib
import json
import keyword
import sys
import tempfile
from pathlib import Path


def _validate_identifier(name: str, param_name: str) -> None:
    """Validate string is a safe Python identifier (allows dotted module paths)."""
    parts = name.split(".")
    for part in parts:
        if not part.isidentifier():
            raise ValueError(f"Invalid {param_name}: '{name}' - '{part}' is not a valid identifier")
        if keyword.iskeyword(part):
            raise ValueError(f"Invalid {param_name}: '{name}' - '{part}' is a Python keyword")


def _ensure_src_on_path() -> None:
    root = Path(__file__).resolve().parents[4]
    src_path = root / "src"
    if src_path.is_dir() and str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))


def _load_graph_object(graph_module: str, graph_var: str):
    _validate_identifier(graph_module, "graph_module")
    _validate_identifier(graph_var, "graph_var")
    _ensure_src_on_path()

    module = importlib.import_module(graph_module)
    obj = module
    for part in graph_var.split("."):
        obj = getattr(obj, part)
    return obj


def generate_debug_html(
    graph_module: str,
    graph_var: str,
    depth: int = 1,
    separate_outputs: bool = False,
) -> tuple[str, dict]:
    """Generate debug HTML and extract debug info.

    Args:
        graph_module: Module path (e.g., 'mypackage.graphs')
        graph_var: Variable name of the graph config
        depth: Expansion depth

    Returns:
        Tuple of (html_path, debug_info_dict)
    """
    from hypergraph.viz.renderer import render_graph
    from hypergraph.viz.widget import visualize

    graph_obj = _load_graph_object(graph_module, graph_var)

    if hasattr(graph_obj, "graph"):
        graph = graph_obj.graph
    elif hasattr(graph_obj, "to_flat_graph"):
        graph = graph_obj
    else:
        graph = graph_obj

    if hasattr(graph, "bind"):
        bound = graph.bind()
    else:
        bound = graph

    flat_graph = bound.to_flat_graph()

    result = render_graph(
        flat_graph,
        depth=depth,
        separate_outputs=separate_outputs,
        debug_overlays=True,
    )

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".html")
    html_path = tmp.name
    tmp.close()

    visualize(
        bound,
        depth=depth,
        separate_outputs=separate_outputs,
        filepath=html_path,
        _debug_overlays=True,
    )

    debug_info = {
        "expansion_state": {},
        "edges_to_check": [],
        "input_groups": [],
        "node_parents": {},
        "expandable_nodes": result.get("meta", {}).get("expandableNodes", []),
        "edges_by_state_keys": sorted(result.get("meta", {}).get("edgesByState", {}).keys()),
        "separate_outputs": separate_outputs,
        "node_ids": [n.get("id") for n in result.get("nodes", [])],
    }

    for n in result["nodes"]:
        if n.get("data", {}).get("nodeType") == "PIPELINE":
            debug_info["expansion_state"][n["id"]] = n.get("data", {}).get("isExpanded", False)

    for e in result["edges"]:
        debug_info["edges_to_check"].append({
            "id": e["id"],
            "source": e["source"],
            "target": e["target"],
            "valueName": e.get("data", {}).get("valueName", ""),
        })

    for n in result["nodes"]:
        nt = n.get("data", {}).get("nodeType", "")
        if nt in ("INPUT", "INPUT_GROUP"):
            debug_info["input_groups"].append({
                "id": n["id"],
                "nodeType": nt,
                "ownerContainer": n.get("data", {}).get("ownerContainer"),
                "deepestOwnerContainer": n.get("data", {}).get("deepestOwnerContainer"),
                "parentNode": n.get("parentNode"),
            })

    for n in result["nodes"]:
        if n.get("parentNode"):
            debug_info["node_parents"][n["id"]] = n["parentNode"]

    debug_info["output_to_producer"] = result.get("meta", {}).get("output_to_producer", {})
    debug_info["param_to_consumer"] = result.get("meta", {}).get("param_to_consumer", {})

    def _state_key(expandable_nodes, expansion_state, separate_outputs):
        sep_key = "sep:1" if separate_outputs else "sep:0"
        if not expandable_nodes:
            return sep_key
        parts = []
        for node_id in expandable_nodes:
            bit = "1" if expansion_state.get(node_id, False) else "0"
            parts.append(str(node_id) + ":" + bit)
        return ",".join(parts) + "|" + sep_key

    debug_info["initial_state_key"] = _state_key(
        debug_info["expandable_nodes"],
        debug_info["expansion_state"],
        separate_outputs,
    )

    return html_path, debug_info


def main():
    parser = argparse.ArgumentParser(description="Debug hypergraph visualization")
    parser.add_argument("module", help="Module path (e.g., 'myapp.graphs')")
    parser.add_argument("variable", help="Graph variable name")
    parser.add_argument("--depth", type=int, default=1, help="Expansion depth")
    parser.add_argument("--separate-outputs", action="store_true", help="Enable separate outputs mode")
    parser.add_argument("--open", action="store_true", help="Open in browser")

    args = parser.parse_args()

    html_path, debug_info = generate_debug_html(
        args.module,
        args.variable,
        args.depth,
        args.separate_outputs,
    )

    print(f"\n=== HTML Generated ===")
    print(f"Path: {html_path}")

    if args.open:
        subprocess.run(["open", html_path])

    print(f"\n=== Debug Info ===")
    print(json.dumps(debug_info, indent=2))

    # Analyze for common issues
    print(f"\n=== Issue Detection ===")

    # Check for orphan edges (source doesn't exist or is hidden)
    node_ids = set(debug_info.get("node_ids", []))
    for e in debug_info.get("edges_to_check", []):
        if e["source"].startswith("input_") or e["source"].startswith("data_"):
            continue  # Skip synthetic nodes
        if e["source"] not in node_ids and not e["source"].startswith("input_"):
            print(f"  ISSUE: Edge source '{e['source']}' not in visible nodes")
            print(f"    Edge: {e['source']} -> {e['target']}")
        if e["target"] not in node_ids:
            print(f"  ISSUE: Edge target '{e['target']}' not in visible nodes")
            print(f"    Edge: {e['source']} -> {e['target']}")

    # Check initial state key availability
    initial_key = debug_info.get("initial_state_key")
    state_keys = set(debug_info.get("edges_by_state_keys", []))
    if initial_key and initial_key not in state_keys:
        print(f"  ISSUE: initial_state_key '{initial_key}' not present in edgesByState")

    # Check INPUT positioning
    for inp in debug_info.get("input_groups", []):
        if inp["deepestOwnerContainer"] and not inp["ownerContainer"]:
            print(f"  ISSUE: INPUT '{inp['id']}' has deepest owner but no runtime owner")
            print(f"    deepestOwner: {inp['deepestOwnerContainer']}")


if __name__ == "__main__":
    main()
