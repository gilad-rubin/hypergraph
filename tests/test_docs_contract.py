"""Static documentation contract checks for public API drift."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from hypergraph import Graph, MapResult, RunResult

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    next_heading = text.find("\n##", start + len(heading))
    if next_heading == -1:
        return text[start:]
    return text[start:next_heading]


def _scoped_section(text: str, heading: str) -> str:
    """Return one Markdown heading through the next heading at the same level."""
    start = text.index(heading)
    level = heading.split(maxsplit=1)[0]
    next_heading = text.find(f"\n{level} ", start + len(heading))
    if next_heading == -1:
        return text[start:]
    return text[start:next_heading]


def _function_def(path: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(_read(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"Could not find function {name!r} in {path}")


def test_public_docs_track_current_api_contracts() -> None:
    runners = _read("docs/06-api-reference/runners.md")
    graph_api = _read("docs/06-api-reference/graph.md")
    events = _read("docs/06-api-reference/events.md")
    visualize = _read("docs/05-how-to/visualize-graphs.md")

    assert inspect.signature(Graph.visualize).parameters["show_types"].default is True
    graph_visualize = _section(graph_api, "### `visualize(")
    assert "show_types=True" in graph_visualize
    assert "Default: True" in graph_visualize
    assert "show_types=True" in visualize
    assert "Dagre" in visualize
    assert "Kiwi" not in visualize
    assert "Dagre" in inspect.getdoc(Graph.visualize)
    assert "Kiwi" not in inspect.getdoc(Graph.visualize)

    assert tuple(inspect.signature(Graph.as_node).parameters) == (
        "self",
        "name",
        "namespaced",
        "runner",
        "complete_on_stop",
    )
    graph_as_node = _section(graph_api, "### `as_node(")
    assert "runner=None" in graph_as_node
    assert "complete_on_stop=False" in graph_as_node
    assert "with_runner" in runners
    assert "inherits the parent runner" in runners
    assert "DaftRunner" in runners and "does not support runner overrides" in runners

    map_dataframe = _section(runners, "### map_dataframe()")
    map_dataframe_def = _function_def("src/hypergraph/runners/daft/runner.py", "map_dataframe")
    map_dataframe_args = {arg.arg for arg in (map_dataframe_def.args.args + map_dataframe_def.args.kwonlyargs)}
    assert {"select", "on_missing", "error_handling"} - map_dataframe_args == {
        "select",
        "on_missing",
        "error_handling",
    }
    assert ") -> DataFrame" in map_dataframe
    # No runtime select parameter — output scope comes from graph.select(...),
    # which map_dataframe honors by projecting output columns (D15 / #143).
    assert "select=" not in map_dataframe
    assert "graph.select" in map_dataframe
    assert "on_missing" not in map_dataframe
    assert "error_handling" not in map_dataframe
    assert "MapResult" not in map_dataframe

    assert 'PARTIAL = "partial"' in runners
    assert 'STOPPED = "stopped"' in runners
    assert "PARTIAL` when some items completed and some failed" in runners
    assert "FAILED` when at least one item failed and none completed" in runners
    assert "STOPPED` if any item stopped" in runners

    value_resolution = _section(runners, "### Value Resolution Order")
    assert "Strict resume" in value_resolution
    assert "rejects fresh runtime inputs" in value_resolution
    assert "Interrupt response payloads" in value_resolution
    assert "`fork_from`" in value_resolution and "`retry_from`" in value_resolution

    assert "### SuperstepStartEvent" in events
    assert "SuperstepStartEvent" in _section(events, "### Event (Union Type)")
    assert "on_superstep_start" in events

    run_result_docs = inspect.getdoc(RunResult)
    assert run_result_docs is not None
    assert "STOPPED" in run_result_docs
    assert "PARTIAL" not in run_result_docs


def test_cyclic_docs_examples_configure_entrypoints() -> None:
    readme = _read("README.md")
    hierarchical = _read("docs/03-patterns/04-hierarchical.md")

    agentic_loops = _section(readme, "### Agentic Loops")
    assert 'entrypoint="retrieve"' in agentic_loops

    assert "conversation = Graph(" in hierarchical
    assert 'entrypoint="rag"' in hierarchical
    assert '@route(targets=["generate_prompt_variants", END])' in hierarchical
    assert "variant_tester = Graph(" in hierarchical
    assert 'entrypoint="generate_prompt_variants"' in hierarchical
    assert "optimization_loop = Graph(" in hierarchical
    assert 'entrypoint="variant_tester"' in hierarchical


def test_result_docs_mirror_checkpoint_evidence() -> None:
    runners = _read("docs/06-api-reference/runners.md")

    run_result = _scoped_section(runners, "## RunResult")
    run_attributes = _scoped_section(run_result, "### Attributes")
    assert "log: RunLog | None" in run_attributes
    assert "checkpoint_ok: bool" in run_attributes
    assert "checkpoint_errors: tuple[str, ...]" in run_attributes
    assert "best-effort" in run_result
    run_progressive_disclosure = _scoped_section(run_result, "### Progressive Disclosure")
    assert '"checkpoint_ok": True' in run_progressive_disclosure
    assert '"checkpoint_errors": []' in run_progressive_disclosure

    map_result = _scoped_section(runners, "## MapResult")
    assert "results.checkpoint_ok" in map_result
    assert "results.checkpoint_errors" in map_result
    assert "derived" in map_result.lower()
    map_attributes = _scoped_section(map_result, "### Attributes")
    assert "checkpoint_ok" not in map_attributes
    assert "checkpoint_errors" not in map_attributes
    assert "checkpoint_ok" not in MapResult.__dataclass_fields__
    assert "checkpoint_errors" not in MapResult.__dataclass_fields__
    assert isinstance(inspect.getattr_static(MapResult, "checkpoint_ok"), property)
    assert isinstance(inspect.getattr_static(MapResult, "checkpoint_errors"), property)


def test_streaming_chunk_event_docs_mirror_correlation_fields() -> None:
    events = _read("docs/06-api-reference/events.md")

    streaming = _scoped_section(events, "### StreamingChunkEvent")
    assert "chunk: object" in streaming
    assert "node_name: str" in streaming
    assert "graph_name: str" in streaming
    assert "`run_id`" in streaming
    assert "`workflow_id`" in streaming
    assert "`item_index`" in streaming
    assert "`parent_span_id`" in streaming
    assert "span of the emitting node" in streaming
