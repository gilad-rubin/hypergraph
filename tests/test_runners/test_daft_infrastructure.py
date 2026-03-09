"""Tests for DaftRunner infrastructure: RunnerCapabilities, is_gate, has_gates, recursive validation."""

from __future__ import annotations

import pytest

from hypergraph import Graph, node
from hypergraph.exceptions import IncompatibleRunnerError
from hypergraph.nodes.gate import ifelse, route
from hypergraph.runners._shared.types import RunnerCapabilities
from hypergraph.runners._shared.validation import validate_runner_compatibility

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@node(output_name="y")
def add_one(x: int) -> int:
    return x + 1


@node(output_name="z")
def add_two(y: int) -> int:
    return y + 2


@node(output_name="out")
def passthrough(x: int) -> int:
    return x


@route(targets=["a_path", "b_path"])
def my_route(x: int) -> str:
    return "a_path" if x > 0 else "b_path"


@ifelse(when_true="yes_path", when_false="no_path")
def my_ifelse(x: int) -> bool:
    return x > 0


@node(output_name="a_out")
def a_path(x: int) -> int:
    return x


@node(output_name="b_out")
def b_path(x: int) -> int:
    return -x


@node(output_name="yes_out")
def yes_path(x: int) -> int:
    return x


@node(output_name="no_out")
def no_path(x: int) -> int:
    return -x


# ---------------------------------------------------------------------------
# RunnerCapabilities new fields
# ---------------------------------------------------------------------------


class TestRunnerCapabilitiesNewFields:
    def test_supports_gates_defaults_to_true(self):
        cap = RunnerCapabilities()
        assert cap.supports_gates is True

    def test_supports_events_defaults_to_true(self):
        cap = RunnerCapabilities()
        assert cap.supports_events is True

    def test_supports_distributed_defaults_to_false(self):
        cap = RunnerCapabilities()
        assert cap.supports_distributed is False

    def test_existing_fields_unchanged(self):
        cap = RunnerCapabilities()
        assert cap.supports_cycles is True
        assert cap.supports_async_nodes is False
        assert cap.supports_streaming is False
        assert cap.returns_coroutine is False
        assert cap.supports_interrupts is False
        assert cap.supports_checkpointing is False

    def test_all_fields_overridable(self):
        cap = RunnerCapabilities(
            supports_cycles=False,
            supports_gates=False,
            supports_events=False,
            supports_distributed=True,
        )
        assert cap.supports_cycles is False
        assert cap.supports_gates is False
        assert cap.supports_events is False
        assert cap.supports_distributed is True


# ---------------------------------------------------------------------------
# is_gate property
# ---------------------------------------------------------------------------


class TestIsGateProperty:
    def test_is_gate_true_for_route_node(self):
        assert my_route.is_gate is True

    def test_is_gate_true_for_ifelse_node(self):
        assert my_ifelse.is_gate is True

    def test_is_gate_false_for_function_node(self):
        assert add_one.is_gate is False

    def test_is_gate_default_false_on_hypernode(self):
        """HyperNode.is_gate should default to False (checked via FunctionNode)."""
        assert hasattr(add_one, "is_gate")
        assert add_one.is_gate is False


# ---------------------------------------------------------------------------
# Graph.has_gates
# ---------------------------------------------------------------------------


class TestGraphHasGates:
    def test_has_gates_true_for_route(self):
        graph = Graph([my_route, a_path, b_path])
        assert graph.has_gates is True

    def test_has_gates_true_for_ifelse(self):
        graph = Graph([my_ifelse, yes_path, no_path])
        assert graph.has_gates is True

    def test_has_gates_false_for_plain_dag(self):
        graph = Graph([add_one, add_two])
        assert graph.has_gates is False


# ---------------------------------------------------------------------------
# validate_runner_compatibility: gate check
# ---------------------------------------------------------------------------


class TestValidateGates:
    def test_validate_rejects_gates_when_unsupported(self):
        graph = Graph([my_route, a_path, b_path])
        cap = RunnerCapabilities(supports_gates=False)

        with pytest.raises(IncompatibleRunnerError, match="gates"):
            validate_runner_compatibility(graph, cap)

    def test_validate_accepts_gates_when_supported(self):
        graph = Graph([my_route, a_path, b_path])
        cap = RunnerCapabilities(supports_gates=True)
        validate_runner_compatibility(graph, cap)  # should not raise

    def test_validate_no_gates_passes_with_unsupported(self):
        graph = Graph([add_one, add_two])
        cap = RunnerCapabilities(supports_gates=False)
        validate_runner_compatibility(graph, cap)  # should not raise


# ---------------------------------------------------------------------------
# Recursive validation for nested GraphNodes
# ---------------------------------------------------------------------------


class TestRecursiveValidation:
    def test_recursive_catches_nested_gates(self):
        """A nested graph with gates should fail if parent capabilities reject gates."""
        inner = Graph([my_route, a_path, b_path], name="inner_with_gate")
        outer = Graph([inner.as_node(name="nested")], name="outer")

        cap = RunnerCapabilities(supports_gates=False)
        with pytest.raises(IncompatibleRunnerError, match="gates"):
            validate_runner_compatibility(outer, cap)

    def test_recursive_catches_nested_cycles(self):
        """A nested graph with cycles should fail if parent capabilities reject cycles."""

        @node(output_name="count")
        def increment(count: int) -> int:
            return count + 1

        @route(targets=["increment"], fallback="increment")
        def always_loop(count: int) -> str:
            return "increment"

        inner = Graph(
            [increment, always_loop],
            name="cyclic_inner",
            entrypoint="increment",
        )
        outer = Graph([inner.as_node(name="nested").with_inputs(count="x")], name="outer")

        cap = RunnerCapabilities(supports_cycles=False, supports_gates=False)
        # Should fail on either cycles or gates in the nested graph
        with pytest.raises(IncompatibleRunnerError):
            validate_runner_compatibility(outer, cap)

    def test_recursive_passes_for_clean_nested_dag(self):
        """A nested DAG with no gates/cycles should pass with restrictive capabilities."""
        inner = Graph([add_one, add_two], name="clean_inner")
        outer = Graph([inner.as_node(name="nested")], name="outer")

        cap = RunnerCapabilities(supports_cycles=False, supports_gates=False)
        validate_runner_compatibility(outer, cap)  # should not raise


# ---------------------------------------------------------------------------
# Existing runners unaffected
# ---------------------------------------------------------------------------


class TestExistingRunnersUnaffected:
    def test_sync_runner_capabilities_unchanged(self):
        from hypergraph.runners.sync.runner import SyncRunner

        runner = SyncRunner()
        cap = runner.capabilities
        assert cap.supports_cycles is True
        assert cap.supports_gates is True
        assert cap.supports_async_nodes is False

    def test_async_runner_capabilities_unchanged(self):
        from hypergraph.runners.async_.runner import AsyncRunner

        runner = AsyncRunner()
        cap = runner.capabilities
        assert cap.supports_cycles is True
        assert cap.supports_gates is True
        assert cap.supports_async_nodes is True
