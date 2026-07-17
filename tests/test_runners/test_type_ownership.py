"""Structural contract for canonical runner result and state types."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import pickle
from collections.abc import Sequence
from pathlib import Path

import hypergraph
import hypergraph.runners as runners

ROOT_EXPORTS = (
    "node",
    "ifelse",
    "route",
    "interrupt",
    "FunctionNode",
    "GraphNode",
    "GraphNodeMapExecutionConfig",
    "GateNode",
    "IfElseNode",
    "RouteNode",
    "InterruptNode",
    "HyperNode",
    "END",
    "RetryPolicy",
    "RetryAfterError",
    "Graph",
    "InputSpec",
    "SyncHandle",
    "AsyncHandle",
    "SyncRunner",
    "AsyncRunner",
    "DaftRunner",
    "BaseRunner",
    "ErrorHandling",
    "FailureEvidence",
    "InspectionDisplay",
    "PauseInfo",
    "RunResult",
    "MapResult",
    "RunStatus",
    "RenameError",
    "GraphConfigError",
    "AttemptTimeoutError",
    "RetryWindowExpiredError",
    "CompactedRetentionError",
    "MissingInputError",
    "InfiniteLoopError",
    "IncompatibleRunnerError",
    "ExecutionError",
    "get_failure_evidence",
    "WorkflowAlreadyCompletedError",
    "GraphChangedError",
    "WorkflowForkError",
    "InputOverrideRequiresForkError",
    "WorkflowAlreadyRunningError",
    "WorkflowStoppedError",
    "RunLog",
    "MapLog",
    "NodeRecord",
    "NodeStats",
    "BaseEvent",
    "SuperstepStartEvent",
    "Event",
    "EventDispatcher",
    "EventProcessor",
    "AsyncEventProcessor",
    "TypedEventProcessor",
    "InterruptEvent",
    "NodeEndEvent",
    "NodeErrorEvent",
    "NodeStartEvent",
    "RouteDecisionEvent",
    "RunEndEvent",
    "RunStartEvent",
    "StopRequestedEvent",
    "CacheHitEvent",
    "InnerCacheEvent",
    "StreamingChunkEvent",
    "RichProgressProcessor",
    "NodeContext",
    "NodeSpanRef",
    "current_node_span",
    "CacheBackend",
    "InMemoryCache",
    "DiskCache",
    "Checkpointer",
    "CheckpointPolicy",
    "SqliteCheckpointer",
    "set_display_mode",
    "get_display_mode",
)

RUNNER_EXPORTS = (
    "ErrorHandling",
    "FailureEvidence",
    "RunStatus",
    "PauseExecution",
    "PauseInfo",
    "RunResult",
    "MapResult",
    "RunLog",
    "MapLog",
    "NodeRecord",
    "NodeStats",
    "RunnerCapabilities",
    "GraphState",
    "NodeExecution",
    "InspectionDisplay",
    "SyncHandle",
    "AsyncHandle",
    "BaseRunner",
    "SyncRunner",
    "AsyncRunner",
    "DaftRunner",
)

RESULT_NAMES = (
    "ErrorHandling",
    "FailureEvidence",
    "RunStatus",
    "aggregate_run_status",
    "RunResult",
    "build_terminal_run_result",
    "build_paused_run_result",
    "build_failed_run_result",
    "build_restored_run_result",
    "build_pre_run_failed_result",
    "MapResult",
    "PauseInfo",
    "NodeRecord",
    "NodeStats",
    "RunLog",
    "MapLog",
    "DURATION_PRECISION",
)

STATE_NAMES = (
    "CheckpointErrorSink",
    "PauseExecution",
    "RunnerCapabilities",
    "ExecutionContext",
    "NodeExecution",
    "GraphState",
)


def _canonical_modules():
    results_spec = importlib.util.find_spec("hypergraph.runners._shared.results")
    state_spec = importlib.util.find_spec("hypergraph.runners._shared.state")
    assert results_spec is not None, "canonical results.py is missing"
    assert state_spec is not None, "canonical state.py is missing"
    return (
        importlib.import_module("hypergraph.runners._shared.results"),
        importlib.import_module("hypergraph.runners._shared.state"),
    )


def test_canonical_owner_modules_exist() -> None:
    _canonical_modules()


def test_legacy_types_module_is_import_only() -> None:
    import hypergraph.runners._shared.types as legacy_types

    tree = ast.parse(Path(legacy_types.__file__).read_text())
    definitions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    assert definitions == []
    assert all(isinstance(node, (ast.Expr, ast.Import, ast.ImportFrom)) for node in tree.body)


def test_legacy_public_and_canonical_identities() -> None:
    import hypergraph.runners._shared.types as legacy_types

    canonical_results, canonical_state = _canonical_modules()
    for name in RESULT_NAMES:
        assert getattr(legacy_types, name) is getattr(canonical_results, name)
    for name in STATE_NAMES:
        assert getattr(legacy_types, name) is getattr(canonical_state, name)
    assert legacy_types._generate_run_id is canonical_results.generate_run_id

    for name in RUNNER_EXPORTS[:14]:
        owner = canonical_state if name in STATE_NAMES else canonical_results
        assert getattr(runners, name) is getattr(owner, name)

    for name in (
        "ErrorHandling",
        "FailureEvidence",
        "PauseInfo",
        "RunResult",
        "MapResult",
        "RunStatus",
        "RunLog",
        "MapLog",
        "NodeRecord",
        "NodeStats",
    ):
        assert getattr(hypergraph, name) is getattr(canonical_results, name)


def test_public_export_order_is_unchanged() -> None:
    assert tuple(hypergraph.__all__) == ROOT_EXPORTS
    assert tuple(runners.__all__) == RUNNER_EXPORTS


def test_canonical_modules_and_legacy_pickle_lookups() -> None:
    canonical_results, canonical_state = _canonical_modules()
    result_classes = (
        "RunStatus",
        "FailureEvidence",
        "RunResult",
        "MapResult",
        "PauseInfo",
        "NodeRecord",
        "NodeStats",
        "RunLog",
        "MapLog",
    )
    state_classes = (
        "PauseExecution",
        "RunnerCapabilities",
        "ExecutionContext",
        "NodeExecution",
        "GraphState",
    )

    for name in result_classes:
        canonical = getattr(canonical_results, name)
        assert canonical.__module__ == "hypergraph.runners._shared.results"
        payload = f"chypergraph.runners._shared.types\n{name}\n.".encode()
        assert pickle.loads(payload) is canonical
    for name in state_classes:
        canonical = getattr(canonical_state, name)
        assert canonical.__module__ == "hypergraph.runners._shared.state"
        payload = f"chypergraph.runners._shared.types\n{name}\n.".encode()
        assert pickle.loads(payload) is canonical

    empty_map = canonical_results.MapResult(
        results=(),
        run_id=None,
        total_duration_ms=0.0,
        map_over=("x",),
        map_mode="zip",
        graph_name="graph",
    )
    assert isinstance(empty_map, Sequence)


def test_results_do_not_import_state_at_runtime() -> None:
    canonical_results, _ = _canonical_modules()
    tree = ast.parse(Path(canonical_results.__file__).read_text())
    imported_modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None}
    assert "hypergraph.runners._shared.state" not in imported_modules


def test_production_does_not_import_legacy_types() -> None:
    package_root = Path(hypergraph.__file__).parent
    offenders = []
    for source in package_root.rglob("*.py"):
        if source.name == "types.py" and source.parent.name == "_shared":
            continue
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "hypergraph.runners._shared.types":
                offenders.append(f"{source.relative_to(package_root)}:{node.lineno}")
    assert offenders == []


def test_canonical_owners_contain_no_inline_html_or_css() -> None:
    canonical_results, canonical_state = _canonical_modules()
    forbidden_fragments = ("<div", "<span", "<table", "<details", "<script", "style=", "border-radius")
    for module in (canonical_results, canonical_state):
        source = Path(module.__file__).read_text()
        assert not any(fragment in source for fragment in forbidden_fragments)
