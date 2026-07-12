"""Contract tests for the repo-local visualization debugging workflow."""

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import pytest

from hypergraph import Graph, node
from hypergraph.viz.debug import VizDebugger
from hypergraph.viz.renderer import render_graph
from hypergraph.viz.renderer.ir_builder import build_graph_ir
from hypergraph.viz.scene_builder import build_initial_scene

ROOT = Path(__file__).resolve().parents[2]
STREAMING_DOC = ROOT / "docs/03-patterns/06-streaming.md"
DEBUG_SKILL = ROOT / ".claude/skills/debug-viz/SKILL.md"
DEBUG_SCRIPT = ROOT / ".claude/skills/debug-viz/scripts/debug_viz.py"
OLD_INSPECTOR = ROOT / ".claude/skills/debug-viz/scripts/inspect_edges_by_state.py"
SCENE_INSPECTOR = ROOT / ".claude/skills/debug-viz/scripts/inspect_scene.py"


def _markdown_section(text: str, heading: str) -> str:
    """Return one level-two Markdown section without later sections."""
    _, section = text.split(heading, maxsplit=1)
    return section.split("\n## ", maxsplit=1)[0]


def _load_script(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_nested_graph() -> Graph:
    @node(output_name="doubled")
    def double(x: int) -> int:
        return x * 2

    @node(output_name="result")
    def add_one(doubled: int) -> int:
        return doubled + 1

    inner = Graph(nodes=[double], name="inner")
    return Graph(nodes=[inner.as_node(), add_one])


@pytest.mark.parametrize(
    ("form", "marker"),
    [
        pytest.param("async", "async for", id="async"),
        pytest.param("sync", "for index", id="sync"),
    ],
)
def test_map_iter_examples_use_the_demonstrated_graph_output(form: str, marker: str) -> None:
    text = STREAMING_DOC.read_text()
    rag_section = _markdown_section(text, "## Streaming in RAG Pipelines")
    map_iter_section = _markdown_section(text, "## Streaming Batch Results with map_iter()")

    output_match = re.search(r'@node\(output_name="([^"]+)"\)\s+async def generate\(', rag_section)
    assert output_match is not None, "the demonstrated RAG graph must declare generate's output"
    demonstrated_output = output_match.group(1)

    blocks = re.findall(r"```python\n(.*?)```", map_iter_section, flags=re.DOTALL)
    map_iter_blocks = [candidate for candidate in blocks if "map_iter(" in candidate]
    assert len(map_iter_blocks) == 2, "the section must contain one async and one sync map_iter example"
    block = next(
        (candidate for candidate in map_iter_blocks if marker in candidate and ("async for" in candidate) == (form == "async")),
        None,
    )
    assert block is not None, f"missing {form} map_iter example"

    result_key = re.search(r"result\[['\"]([^'\"]+)['\"]\]", block)
    assert result_key is not None, f"the {form} map_iter example must read its RunResult"
    assert result_key.group(1) == demonstrated_output, (
        f"the {form} map_iter example reads {result_key.group(1)!r}, but the demonstrated graph produces {demonstrated_output!r}"
    )


def test_viz_debugger_prints_current_browser_debug_guidance(capsys: pytest.CaptureFixture[str]) -> None:
    VizDebugger(_make_nested_graph()).visualize()
    output = capsys.readouterr().out
    docstring = VizDebugger.visualize.__doc__ or ""

    missing = {guidance for guidance in ("window.__hypergraphVizDebug", "window.__hypergraph_debug_viz") if guidance not in output}
    assert not missing and "BOUNDS | WIDTHS | TEXTS" not in output, f"missing current browser guidance: {sorted(missing)}; output was:\n{output}"
    assert "metadata-only" in docstring and "exposed for every visualization" in docstring
    assert "BOUNDS/WIDTHS/TEXTS" not in docstring


def test_debug_skill_teaches_only_existing_ir_scene_builder_surfaces() -> None:
    skill = DEBUG_SKILL.read_text()
    current_references = (
        "GraphIR",
        "renderer/ir_builder.py",
        "scene_builder.py",
        "assets/scene_builder.js",
        "assets/viz_layout.js",
        "assets/viz_debug.js",
        "window.__hypergraphVizDebug",
        "window.__hypergraph_debug_viz",
        "tests/viz/test_scene_builder.py",
        "tests/viz/test_viz_modules_js.py",
    )
    removed_references = (
        "edgesByState",
        "src/hypergraph/viz/renderer.py",
        "assets/layout.js",
        "assets/state_utils.js",
        "assets/constants.js",
        "html_generator.py",
        "test_edges_by_state_contract.py",
        "BOUNDS | WIDTHS | TEXTS",
    )
    documented_paths = (
        "src/hypergraph/viz/renderer/ir_builder.py",
        "src/hypergraph/viz/scene_builder.py",
        "src/hypergraph/viz/assets/scene_builder.js",
        "src/hypergraph/viz/assets/viz_layout.js",
        "src/hypergraph/viz/assets/viz_debug.js",
        "tests/viz/test_scene_builder.py",
        "tests/viz/test_viz_modules_js.py",
    )

    missing = [reference for reference in current_references if reference not in skill]
    stale = [reference for reference in removed_references if reference in skill]
    nonexistent = [path for path in documented_paths if not (ROOT / path).is_file()]
    assert not missing and not stale and not nonexistent, (
        f"missing current references: {missing}; stale references: {stale}; nonexistent paths: {nonexistent}"
    )


def test_debug_script_summary_describes_current_ir_scene_and_routing() -> None:
    module = _load_script(DEBUG_SCRIPT, "debug_viz_script")
    graph = _make_nested_graph()
    payload = render_graph(graph.to_flat_graph(), depth=1)

    summary = module.build_debug_summary(payload)

    assert summary["ir"] == {
        "schema_version": payload["meta"]["ir"]["schema_version"],
        "node_count": len(payload["meta"]["ir"]["nodes"]),
        "edge_count": len(payload["meta"]["ir"]["edges"]),
        "expandable_nodes": payload["meta"]["ir"]["expandable_nodes"],
        "external_input_count": len(payload["meta"]["ir"]["external_inputs"]),
    }
    assert summary["initial_expansion"] == payload["meta"]["initial_expansion"]
    assert summary["initial_scene"]["node_ids"] == [node["id"] for node in payload["nodes"]]
    assert summary["initial_scene"]["nodes"] == [
        {
            "id": node["id"],
            "node_type": node.get("data", {}).get("nodeType"),
            "parent": node.get("parentNode"),
            "hidden": bool(node.get("hidden")),
            "owner_container": node.get("data", {}).get("ownerContainer"),
            "deepest_owner_container": node.get("data", {}).get("deepestOwnerContainer"),
        }
        for node in payload["nodes"]
    ]
    assert summary["initial_scene"]["edges"] == [
        {
            "id": edge["id"],
            "source": edge["source"],
            "target": edge["target"],
            "value_name": edge.get("data", {}).get("valueName"),
            "edge_type": edge.get("data", {}).get("edgeType"),
        }
        for edge in payload["edges"]
    ]
    assert summary["routing_maps"] == {
        "output_to_producer": payload["meta"]["output_to_producer"],
        "param_to_consumer": payload["meta"]["param_to_consumer"],
        "node_to_parent": payload["meta"]["node_to_parent"],
    }
    assert summary["browser_debug"] == {
        "api": "window.__hypergraphVizDebug",
        "dev_controls": "Set window.__hypergraph_debug_viz = true before rendering.",
    }
    json.dumps(summary)


def test_debug_script_has_no_removed_state_table_machinery() -> None:
    stale = {
        path.name: [reference for reference in ("edgesByState", "edges_by_state", "initial_state_key", "_state_key") if reference in path.read_text()]
        for path in (DEBUG_SCRIPT, SCENE_INSPECTOR)
    }
    stale = {name: references for name, references in stale.items() if references}
    assert not stale, f"debug scripts still reference removed state-table machinery: {stale}"


def test_obsolete_state_table_inspector_is_replaced() -> None:
    assert not OLD_INSPECTOR.exists() and SCENE_INSPECTOR.is_file(), (
        f"obsolete inspector exists={OLD_INSPECTOR.exists()}; scene inspector exists={SCENE_INSPECTOR.is_file()}"
    )


@pytest.mark.parametrize("expanded", [False, True], ids=["collapsed", "expanded"])
def test_scene_inspector_reports_selected_state_in_scene_order(expanded: bool) -> None:
    assert SCENE_INSPECTOR.is_file(), "inspect_scene.py must replace the obsolete state-table inspector"
    module = _load_script(SCENE_INSPECTOR, f"inspect_scene_{expanded}")
    graph = _make_nested_graph()
    ir = build_graph_ir(graph.to_flat_graph())
    expansion_state = {node_id: expanded for node_id in ir.expandable_nodes}
    expected_scene = build_initial_scene(ir, expansion_state=expansion_state)

    report = module.inspect_scene(graph, expanded=expanded)

    assert report["schema_version"] == ir.schema_version
    assert report["expansion_state"] == expansion_state
    assert report["visible_nodes"] == [node for node in expected_scene["nodes"] if not node.get("hidden", False)]
    assert report["visible_edges"] == [edge for edge in expected_scene["edges"] if not edge.get("hidden", False)]
    json.dumps(report)
