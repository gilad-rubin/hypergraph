"""Static documentation contract checks for public API drift."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import get_args, get_type_hints

from hypergraph import (
    AsyncHandle,
    AsyncRunner,
    DaftRunner,
    FailureEvidence,
    Graph,
    MapResult,
    RunResult,
    SyncHandle,
    SyncRunner,
    WorkflowStoppedError,
)
from hypergraph.checkpointers import Checkpointer, MemoryCheckpointer, SqliteCheckpointer, SqliteRunInspector
from hypergraph.events import RunEndEvent
from hypergraph.runners._shared.results import NodeRecord

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


def _documented_function_defs(section: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    code = section.split("```python\n", maxsplit=1)[1].split("\n```", maxsplit=1)[0]
    tree = ast.parse(code)
    return {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _parameter_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    arguments = function.args
    names = [
        *(argument.arg for argument in arguments.posonlyargs),
        *(argument.arg for argument in arguments.args),
        *(argument.arg for argument in arguments.kwonlyargs),
    ]
    if arguments.vararg is not None:
        names.append(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.append(arguments.kwarg.arg)
    return tuple(names)


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


def test_background_handle_docs_pin_public_contract() -> None:
    runners = _read("docs/06-api-reference/runners.md")
    guide = _read("docs/05-how-to/control-background-execution.md")
    summary = _read("docs/SUMMARY.md")
    docs_readme = _read("docs/README.md")
    events = _read("docs/06-api-reference/events.md")

    runner_sections = {
        SyncRunner: _scoped_section(_scoped_section(runners, "## SyncRunner"), "### start_run() and start_map()"),
        AsyncRunner: _scoped_section(_scoped_section(runners, "## AsyncRunner"), "### start_run() and start_map()"),
    }
    expected_returns = {
        (SyncRunner, "start_run"): "SyncHandle[RunResult]",
        (SyncRunner, "start_map"): "SyncHandle[MapResult]",
        (AsyncRunner, "start_run"): "AsyncHandle[RunResult]",
        (AsyncRunner, "start_map"): "AsyncHandle[MapResult]",
    }
    for runner_type, section in runner_sections.items():
        documented = _documented_function_defs(section)
        assert set(documented) == {"start_run", "start_map"}
        for method_name, function in documented.items():
            runtime_method = getattr(runner_type, method_name)
            assert _parameter_names(function) == tuple(inspect.signature(runtime_method).parameters)
            assert ast.unparse(function.returns) == expected_returns[(runner_type, method_name)]
            assert "error_handling" not in inspect.signature(runtime_method).parameters

    assert not hasattr(DaftRunner, "start_run")
    assert not hasattr(DaftRunner, "start_map")
    assert "does not implement `start_run()` or `start_map()`" in _scoped_section(runners, "## DaftRunner")

    handle_section = _scoped_section(runners, "## SyncHandle and AsyncHandle")
    for handle_type in (SyncHandle, AsyncHandle):
        public_members = {name for name in handle_type.__dict__ if not name.startswith("_")}
        assert public_members == {"done", "stop", "result"}
        assert isinstance(inspect.getattr_static(handle_type, "done"), property)
        assert tuple(inspect.signature(handle_type.stop).parameters) == ("self", "info")
        assert tuple(inspect.signature(handle_type.result).parameters) == ("self", "raise_on_failure")
    assert not inspect.iscoroutinefunction(SyncHandle.stop)
    assert not inspect.iscoroutinefunction(AsyncHandle.stop)
    assert not inspect.iscoroutinefunction(SyncHandle.result)
    assert inspect.iscoroutinefunction(AsyncHandle.result)
    for excluded_surface in (
        "`status`",
        "`wait()`",
        "`failure`",
        "`failures`",
        "`failed_item_indexes`",
        "`view`",
        "`inspect`",
        "`cancel()`",
        "`cancelled()`",
        "`exception()`",
        "`add_done_callback()`",
        "`running()`",
        "`__await__`",
    ):
        assert excluded_surface in handle_section

    assert MapResult.__dataclass_fields__["unstarted_item_indexes"].default == ()
    assert isinstance(inspect.getattr_static(MapResult, "requested_count"), property)
    map_result = _scoped_section(runners, "## MapResult")
    assert "unstarted_item_indexes: tuple[int, ...] = ()" in map_result
    assert "results.requested_count == len(results) + len(results.unstarted_item_indexes)" in map_result
    assert "does not fabricate" in map_result

    assert "Control Work After It Starts](05-how-to/control-background-execution.md)" in summary
    assert "Control Work After It Starts](05-how-to/control-background-execution.md)" in docs_readme
    assert "AsyncRunner.start_*()` are ordinary methods" in guide
    assert "Use a Checkpointer for Recovery, Not Handle Reconnection" in guide
    assert "instead of tunneling into `run()`" in guide
    assert "put it inside `values={...}`" in guide
    assert "complete stopped batch" not in guide
    assert "settled stopped batch" in guide
    assert "Parent `RunEndEvent` and OpenTelemetry batch counts" in guide
    assert "All existing batch counts describe real settled child outcomes" in events
    assert "no new event count field is added" in events
    assert "requested_count" not in RunEndEvent.__dataclass_fields__
    assert "unstarted_item_indexes" not in RunEndEvent.__dataclass_fields__


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
    assert "node_failures: tuple[FailureEvidence, ...]" in run_attributes
    assert "best-effort" in run_result
    assert "get_failure_evidence" in run_result
    assert "worker/parser/parse_order" in run_result
    assert "large values or secrets" in run_result
    assert "until the `RunResult` or raised exception" in run_result
    assert tuple(FailureEvidence.__dataclass_fields__) == (
        "node_name",
        "error",
        "inputs",
        "superstep",
        "duration_ms",
        "graph_name",
        "workflow_id",
        "item_index",
    )
    assert "node_failures" in RunResult.__dataclass_fields__
    assert isinstance(inspect.getattr_static(RunResult, "failure"), property)
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


def test_checkpointer_semantics_docs_mirror_high_drift_surfaces() -> None:
    checkpointers = _read("docs/06-api-reference/checkpointers.md")
    runners = _read("docs/06-api-reference/runners.md")
    events = _read("docs/06-api-reference/events.md")
    batch = _read("docs/05-how-to/batch-processing.md")

    for get_steps in (
        Checkpointer.get_steps,
        MemoryCheckpointer.get_steps,
        SqliteCheckpointer.get_steps,
        SqliteCheckpointer.steps,
        SqliteRunInspector.steps,
    ):
        assert inspect.signature(get_steps).parameters["show_internal"].default is False
    assert "show_internal=True" in checkpointers
    assert "retention carriers" in checkpointers

    list_runs = inspect.signature(Checkpointer.list_runs).parameters
    count_runs = inspect.signature(Checkpointer.count_runs).parameters
    assert {"graph_name", "since", "parent_run_id"} <= set(list_runs)
    assert list_runs["parent_run_id"].default is not None
    assert count_runs["parent_run_id"].default is not None
    assert "Omit `parent_run_id` for all runs" in checkpointers
    assert "explicit-`None`/top-level" in checkpointers

    assert RunResult.__dataclass_fields__["restored"].default is False
    assert isinstance(inspect.getattr_static(MapResult, "restored_count"), property)
    assert "restored" in get_args(get_type_hints(NodeRecord)["status"])
    assert "restored: bool" in _scoped_section(runners, "## RunResult")
    assert "restored_count" in _scoped_section(runners, "## MapResult")
    assert "fully restored map omits the average" in runners

    assert "batch_restored_items" in RunEndEvent.__dataclass_fields__
    assert "batch_restored_items: int | None" in events
    run_end_event = _scoped_section(events, "### RunEndEvent")
    assert run_end_event.index("batch_outcome: str | None") < run_end_event.index("batch_restored_items: int | None")
    assert "hypergraph.batch.restored_items" in events
    assert "Restored children" in events

    assert issubclass(WorkflowStoppedError, Exception)
    assert "WorkflowStoppedError" in runners
    assert "non-empty runtime mapping" in batch
    assert "before a new run event or persistence write" in batch

    assert "{source}-fork-{hex}" in runners
    assert "generic `run-...`" in runners
    assert "job-1-fork-a1b2c3" in checkpointers
    assert "nested source" in checkpointers


def test_inspect_mode_docs_mirror_public_contract() -> None:
    readme = _read("README.md")
    docs_readme = _read("docs/README.md")
    summary = _read("docs/SUMMARY.md")
    debug = _read("docs/05-how-to/debug-workflows.md")
    observe = _read("docs/05-how-to/observe-execution.md")
    control = _read("docs/05-how-to/control-background-execution.md")
    runners = _read("docs/06-api-reference/runners.md")
    checkpointers = _read("docs/06-api-reference/checkpointers.md")
    hypertable = _read("docs/08-hypertable/getting-started.md")
    pyproject = _read("pyproject.toml")

    assert "RunView" not in readme
    assert "RunView" not in observe
    assert "Debug Workflows](05-how-to/debug-workflows.md)" in summary
    assert "Debug Workflows](05-how-to/debug-workflows.md)" in docs_readme

    sync_section = _scoped_section(runners, "## SyncRunner")
    async_section = _scoped_section(runners, "## AsyncRunner")
    sync_start_section = _scoped_section(sync_section, "### start_run() and start_map()")
    async_start_section = _scoped_section(async_section, "### start_run() and start_map()")
    runner_sections = {
        (SyncRunner, "run"): _scoped_section(sync_section, "### run()"),
        (SyncRunner, "map"): _scoped_section(sync_section, "### map()"),
        (SyncRunner, "start_run"): sync_start_section,
        (SyncRunner, "start_map"): sync_start_section,
        (AsyncRunner, "run"): _scoped_section(async_section, "### run()"),
        (AsyncRunner, "map"): _scoped_section(async_section, "### map()"),
        (AsyncRunner, "start_run"): async_start_section,
        (AsyncRunner, "start_map"): async_start_section,
    }
    for (runner_type, method_name), section in runner_sections.items():
        documented = _documented_function_defs(section)[method_name]
        runtime_method = getattr(runner_type, method_name)
        public_runtime_parameters = tuple(name for name in inspect.signature(runtime_method).parameters if name == "self" or not name.startswith("_"))
        assert _parameter_names(documented) == public_runtime_parameters
        inspect_parameter = next(argument for argument in documented.args.kwonlyargs if argument.arg == "inspect")
        inspect_default = documented.args.kw_defaults[documented.args.kwonlyargs.index(inspect_parameter)]
        assert isinstance(inspect_default, ast.Constant)
        assert inspect_default.value is False

    required_debug_truth = (
        "RunResult",
        "MapResult",
        "inspect=True",
        "result.inspect()",
        "batch.inspect()",
        "HYPERGRAPH_DISPLAY=plain",
        "depth 6",
        "100 mapping",
        "200 sequence",
        "200 rows",
        "20 columns",
        "20,000 characters",
        'values={"inspect": "graph-owned"}',
        "batch.failures",
        "failure.item_index",
        "graph.visualize()",
        "not captured; rerun with inspect=True",
    )
    for truth in required_debug_truth:
        assert truth in debug

    assert "does not require a checkpointer" in debug
    assert "restored nodes" in debug
    assert "same object identities" in debug
    assert "sensitive" in debug.lower()
    normalized_debug = " ".join(debug.split())
    assert "without a kernel" in normalized_debug
    assert "executed slash-qualified paths" in normalized_debug
    assert 'inspect="graph-owned"' not in debug

    assert "inspect=True" in control
    assert "result.inspect()" in control
    assert "batch.inspect()" in control
    assert "do not `await runner.start_run(...)`" in control
    assert "does not expose" in control and "inspection" in control

    assert "current-process inspect view" in checkpointers
    assert "does not require a checkpointer" in checkpointers
    assert "restored nodes" in checkpointers
    assert "do not reconstruct successful inputs or outputs" in checkpointers

    for text in (readme, debug, observe, control, runners):
        assert "._repr_html_(" not in text
        assert ".artifact" not in text
        assert ".to_html(" not in text
        assert ".save(" not in text

    assert "../05-how-to/batch-processing.md" in hypertable
    assert "../05-how-to/visualize-graphs.md" in hypertable
    assert "../05-how-to/test-without-framework.md" in hypertable
    assert '"/docs/changelog.md"' in pyproject
    assert '"/CHANGELOG.md"' not in pyproject
