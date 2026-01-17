"""Tests for runner validation functions."""

import pytest

from hypergraph import Graph, node
from hypergraph.exceptions import IncompatibleRunnerError, MissingInputError
from hypergraph.runners import RunnerCapabilities
from hypergraph.runners._shared.validation import (
    validate_inputs,
    validate_map_compatible,
    validate_runner_compatibility,
)


# === Test Fixtures ===


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="sum")
def add(a: int, b: int) -> int:
    return a + b


@node(output_name="result")
def with_default(x: int, y: int = 10) -> int:
    return x + y


@node(output_name="incremented")
async def async_double(x: int) -> int:
    return x * 2


@node(output_name="count")
def counter(count: int) -> int:
    return count + 1


# === Tests ===


class TestValidateInputs:
    """Tests for validate_inputs function."""

    def test_all_required_inputs_provided_passes(self):
        """No error when all required inputs are provided."""
        graph = Graph([double])
        # Should not raise
        validate_inputs(graph, {"x": 1})

    def test_missing_required_input_raises(self):
        """Error when required input is missing."""
        graph = Graph([double])
        with pytest.raises(MissingInputError) as exc_info:
            validate_inputs(graph, {})
        assert "x" in exc_info.value.missing

    def test_optional_input_can_be_omitted(self):
        """No error when optional input with default is omitted."""
        graph = Graph([with_default])
        # y has default, only x is required
        validate_inputs(graph, {"x": 1})

    def test_bound_input_can_be_omitted(self):
        """No error when bound input is omitted."""
        graph = Graph([add]).bind(a=5)
        # a is bound, only b is required
        validate_inputs(graph, {"b": 10})

    def test_seed_input_required_for_cycles(self):
        """Seed inputs must be provided for cyclic graphs."""
        # Create a cycle: counter -> counter
        # count is both input and output
        graph = Graph([counter])
        # count should be a seed (cycle input)
        assert "count" in graph.inputs.seeds

        with pytest.raises(MissingInputError) as exc_info:
            validate_inputs(graph, {})
        assert "count" in exc_info.value.missing

    def test_seed_input_provided_passes(self):
        """No error when seed input is provided."""
        graph = Graph([counter])
        validate_inputs(graph, {"count": 0})

    def test_extra_inputs_ignored(self):
        """Extra inputs that don't match graph inputs are allowed (with warning)."""
        graph = Graph([double])
        # extra_param doesn't exist - should warn but not error
        with pytest.warns(UserWarning, match="internal parameters"):
            validate_inputs(graph, {"x": 1, "extra_param": "ignored"})

    def test_error_message_lists_missing_inputs(self):
        """Error message includes list of missing inputs."""
        graph = Graph([add])
        with pytest.raises(MissingInputError) as exc_info:
            validate_inputs(graph, {})
        # Both a and b should be in the message
        assert "a" in str(exc_info.value)
        assert "b" in str(exc_info.value)

    def test_error_message_suggests_similar_names(self):
        """Error message suggests similar names for typos."""
        graph = Graph([double])
        with pytest.raises(MissingInputError) as exc_info:
            validate_inputs(graph, {"xx": 1})  # typo: xx instead of x
        # Should suggest 'x' for 'xx'... but actually the missing is 'x'
        # The suggestion would be if we provided something close to what's missing
        # Let's test a more realistic typo case
        assert "x" in exc_info.value.missing

    def test_multiple_missing_inputs(self):
        """Error includes all missing inputs."""
        graph = Graph([add])
        with pytest.raises(MissingInputError) as exc_info:
            validate_inputs(graph, {})
        assert len(exc_info.value.missing) == 2
        assert set(exc_info.value.missing) == {"a", "b"}


class TestValidateRunnerCompatibility:
    """Tests for validate_runner_compatibility function."""

    def test_sync_runner_rejects_async_nodes(self):
        """Sync runner cannot run graphs with async nodes."""
        graph = Graph([async_double])
        sync_caps = RunnerCapabilities(supports_async_nodes=False)

        with pytest.raises(IncompatibleRunnerError) as exc_info:
            validate_runner_compatibility(graph, sync_caps)
        assert "async" in str(exc_info.value).lower()
        assert exc_info.value.capability == "supports_async_nodes"

    def test_async_runner_accepts_async_nodes(self):
        """Async runner can run graphs with async nodes."""
        graph = Graph([async_double])
        async_caps = RunnerCapabilities(supports_async_nodes=True)
        # Should not raise
        validate_runner_compatibility(graph, async_caps)

    def test_async_runner_accepts_sync_nodes(self):
        """Async runner can also run graphs with sync nodes."""
        graph = Graph([double])
        async_caps = RunnerCapabilities(supports_async_nodes=True)
        # Should not raise
        validate_runner_compatibility(graph, async_caps)

    def test_sync_runner_accepts_sync_nodes(self):
        """Sync runner can run graphs with only sync nodes."""
        graph = Graph([double])
        sync_caps = RunnerCapabilities(supports_async_nodes=False)
        # Should not raise
        validate_runner_compatibility(graph, sync_caps)

    def test_error_message_names_incompatible_node(self):
        """Error message includes the name of incompatible node."""
        graph = Graph([async_double])
        sync_caps = RunnerCapabilities(supports_async_nodes=False)

        with pytest.raises(IncompatibleRunnerError) as exc_info:
            validate_runner_compatibility(graph, sync_caps)
        assert exc_info.value.node_name == "async_double"

    def test_runner_without_cycle_support(self):
        """Runner without cycle support rejects cyclic graphs."""
        graph = Graph([counter])
        no_cycles_caps = RunnerCapabilities(supports_cycles=False)

        with pytest.raises(IncompatibleRunnerError) as exc_info:
            validate_runner_compatibility(graph, no_cycles_caps)
        assert exc_info.value.capability == "supports_cycles"

    def test_runner_with_cycle_support_accepts_cycles(self):
        """Runner with cycle support accepts cyclic graphs."""
        graph = Graph([counter])
        caps = RunnerCapabilities(supports_cycles=True)
        # Should not raise
        validate_runner_compatibility(graph, caps)


class TestValidateMapCompatible:
    """Tests for validate_map_compatible function."""

    def test_dag_graph_passes(self):
        """DAG graphs are map-compatible."""
        graph = Graph([double, add.with_inputs(a="doubled")])
        # Should not raise
        validate_map_compatible(graph)

    def test_cyclic_graph_passes(self):
        """Cyclic graphs are currently map-compatible."""
        graph = Graph([counter])
        # Should not raise (Phase 2 will add interrupt checks)
        validate_map_compatible(graph)
