"""
Parametrized tests using the capability matrix.

This file demonstrates how to use the capability matrix for systematic testing.
"""

import pytest

from hypergraph.runners import SyncRunner, AsyncRunner, RunStatus

from .matrix import (
    Capability,
    Runner,
    Topology,
    MapMode,
    NestingDepth,
    Renaming,
    Binding,
    all_valid_combinations,
    combinations_for,
    count_combinations,
)
from .builders import build_graph_for_capability, get_test_inputs


# =============================================================================
# Matrix sanity tests
# =============================================================================


class TestMatrixSanity:
    """Tests that the matrix itself is well-formed."""

    def test_has_valid_combinations(self):
        """Matrix generates at least some valid combinations."""
        combos = list(all_valid_combinations())
        assert len(combos) > 0

    def test_all_combinations_are_valid(self):
        """Every generated combination passes is_valid()."""
        for cap in all_valid_combinations():
            assert cap.is_valid(), f"Invalid combination generated: {cap}"

    def test_sync_combinations_have_no_async_nodes(self):
        """Sync runner combinations don't have async nodes."""
        for cap in combinations_for(runner=Runner.SYNC):
            assert not cap.has_async_nodes, f"Sync combo has async nodes: {cap}"

    def test_nested_combinations_have_graph_node(self):
        """Nested combinations include GraphNode type."""
        from .matrix import NodeType

        for cap in all_valid_combinations():
            if cap.nesting != NestingDepth.FLAT:
                assert NodeType.GRAPH_NODE in cap.node_types, f"Nested but no GraphNode: {cap}"

    def test_combination_string_is_unique(self):
        """Each combination has a unique string representation."""
        combos = list(all_valid_combinations())
        strings = [str(c) for c in combos]
        assert len(strings) == len(set(strings)), "Duplicate string representations"

    def test_count_combinations_reports_correctly(self):
        """count_combinations matches actual count."""
        counts = count_combinations()
        actual = len(list(all_valid_combinations()))
        assert counts["total_valid"] == actual


# =============================================================================
# Subset tests (faster, focused)
# =============================================================================


class TestSyncRunnerSubset:
    """Test a subset of sync runner combinations."""

    @pytest.mark.parametrize(
        "cap",
        list(combinations_for(runner=Runner.SYNC, nesting=NestingDepth.FLAT))[:10],
        ids=str,
    )
    def test_sync_flat_combinations(self, cap: Capability):
        """Sync runner with flat graphs should execute."""
        graph = build_graph_for_capability(cap)
        inputs = get_test_inputs(cap)

        runner = SyncRunner()

        # Cyclic graphs need max_iterations
        kwargs = {}
        if cap.topology == Topology.CYCLIC:
            kwargs["max_iterations"] = 5

        result = runner.run(graph, inputs, **kwargs)
        assert result.status == RunStatus.COMPLETED


class TestAsyncRunnerSubset:
    """Test a subset of async runner combinations."""

    @pytest.mark.parametrize(
        "cap",
        list(combinations_for(runner=Runner.ASYNC, nesting=NestingDepth.FLAT))[:10],
        ids=str,
    )
    async def test_async_flat_combinations(self, cap: Capability):
        """Async runner with flat graphs should execute."""
        graph = build_graph_for_capability(cap)
        inputs = get_test_inputs(cap)

        runner = AsyncRunner()

        kwargs = {}
        if cap.topology == Topology.CYCLIC:
            kwargs["max_iterations"] = 5

        result = await runner.run(graph, inputs, **kwargs)
        assert result.status == RunStatus.COMPLETED


class TestNestedSubset:
    """Test nested graph combinations."""

    @pytest.mark.parametrize(
        "cap",
        list(combinations_for(runner=Runner.SYNC, nesting=NestingDepth.ONE_LEVEL))[:5],
        ids=str,
    )
    def test_sync_one_level_nesting(self, cap: Capability):
        """Sync runner with one level of nesting."""
        graph = build_graph_for_capability(cap)
        inputs = get_test_inputs(cap)

        runner = SyncRunner()

        kwargs = {}
        if cap.topology == Topology.CYCLIC:
            kwargs["max_iterations"] = 5

        result = runner.run(graph, inputs, **kwargs)
        assert result.status == RunStatus.COMPLETED


# =============================================================================
# Full matrix test (slow, comprehensive)
# =============================================================================


@pytest.mark.slow
class TestFullMatrix:
    """
    Full matrix tests - run all valid combinations.

    Mark with @pytest.mark.slow so they can be skipped in quick runs.
    Run with: pytest -m slow
    """

    @pytest.mark.parametrize("cap", list(combinations_for(runner=Runner.SYNC)), ids=str)
    def test_all_sync_combinations(self, cap: Capability):
        """Every valid sync combination should execute."""
        graph = build_graph_for_capability(cap)
        inputs = get_test_inputs(cap)

        runner = SyncRunner()

        kwargs = {}
        if cap.topology == Topology.CYCLIC:
            kwargs["max_iterations"] = 5

        result = runner.run(graph, inputs, **kwargs)
        assert result.status == RunStatus.COMPLETED

    @pytest.mark.parametrize("cap", list(combinations_for(runner=Runner.ASYNC)), ids=str)
    async def test_all_async_combinations(self, cap: Capability):
        """Every valid async combination should execute."""
        graph = build_graph_for_capability(cap)
        inputs = get_test_inputs(cap)

        runner = AsyncRunner()

        kwargs = {}
        if cap.topology == Topology.CYCLIC:
            kwargs["max_iterations"] = 5

        result = await runner.run(graph, inputs, **kwargs)
        assert result.status == RunStatus.COMPLETED


# =============================================================================
# Specific capability tests
# =============================================================================


class TestMapOverCombinations:
    """Test map_over specific combinations."""

    @pytest.mark.parametrize(
        "cap",
        list(combinations_for(map_mode=MapMode.ZIP, runner=Runner.SYNC))[:5],
        ids=str,
    )
    def test_zip_mode_produces_equal_length_outputs(self, cap: Capability):
        """Zip mode should produce outputs matching input length."""
        if not cap.has_nesting:
            pytest.skip("Map mode without nesting uses runner.map()")

        graph = build_graph_for_capability(cap)
        inputs = get_test_inputs(cap)

        runner = SyncRunner()
        result = runner.run(graph, inputs)

        assert result.status == RunStatus.COMPLETED
        # Outputs should be lists when mapped
        for key, value in result.values.items():
            if isinstance(value, list):
                # Check length matches input
                input_lengths = [len(v) for v in inputs.values() if isinstance(v, list)]
                if input_lengths:
                    assert len(value) == input_lengths[0]


class TestCyclicCombinations:
    """Test cyclic graph combinations."""

    @pytest.mark.parametrize(
        "cap",
        list(combinations_for(topology=Topology.CYCLIC, runner=Runner.SYNC, nesting=NestingDepth.FLAT)),
        ids=str,
    )
    def test_cyclic_requires_max_iterations(self, cap: Capability):
        """Cyclic graphs need max_iterations to terminate."""
        graph = build_graph_for_capability(cap)
        inputs = get_test_inputs(cap)

        runner = SyncRunner()
        result = runner.run(graph, inputs, max_iterations=10)

        assert result.status == RunStatus.COMPLETED


class TestRenamingCombinations:
    """Test renaming capability combinations."""

    @pytest.mark.parametrize(
        "cap",
        list(combinations_for(renaming=Renaming.INPUTS, runner=Runner.SYNC, nesting=NestingDepth.FLAT))[:5],
        ids=str,
    )
    def test_input_renaming_works(self, cap: Capability):
        """Graphs with renamed inputs should execute correctly."""
        graph = build_graph_for_capability(cap)
        inputs = get_test_inputs(cap)

        runner = SyncRunner()

        kwargs = {}
        if cap.topology == Topology.CYCLIC:
            kwargs["max_iterations"] = 5

        result = runner.run(graph, inputs, **kwargs)
        assert result.status == RunStatus.COMPLETED

    @pytest.mark.parametrize(
        "cap",
        list(combinations_for(renaming=Renaming.OUTPUTS, runner=Runner.SYNC, nesting=NestingDepth.FLAT))[:5],
        ids=str,
    )
    def test_output_renaming_works(self, cap: Capability):
        """Graphs with renamed outputs should execute correctly."""
        graph = build_graph_for_capability(cap)
        inputs = get_test_inputs(cap)

        runner = SyncRunner()

        kwargs = {}
        if cap.topology == Topology.CYCLIC:
            kwargs["max_iterations"] = 5

        result = runner.run(graph, inputs, **kwargs)
        assert result.status == RunStatus.COMPLETED

    @pytest.mark.parametrize(
        "cap",
        list(combinations_for(renaming=Renaming.NODE_NAME, runner=Runner.SYNC, nesting=NestingDepth.FLAT))[:5],
        ids=str,
    )
    def test_node_name_renaming_works(self, cap: Capability):
        """Graphs with renamed node names should execute correctly."""
        graph = build_graph_for_capability(cap)
        inputs = get_test_inputs(cap)

        runner = SyncRunner()

        kwargs = {}
        if cap.topology == Topology.CYCLIC:
            kwargs["max_iterations"] = 5

        result = runner.run(graph, inputs, **kwargs)
        assert result.status == RunStatus.COMPLETED


class TestBindingCombinations:
    """Test binding capability combinations."""

    @pytest.mark.parametrize(
        "cap",
        list(combinations_for(binding=Binding.BOUND, runner=Runner.SYNC, nesting=NestingDepth.FLAT))[:5],
        ids=str,
    )
    def test_bound_graphs_execute(self, cap: Capability):
        """Graphs with bound values should execute correctly."""
        graph = build_graph_for_capability(cap)
        inputs = get_test_inputs(cap)

        runner = SyncRunner()

        kwargs = {}
        if cap.topology == Topology.CYCLIC:
            kwargs["max_iterations"] = 5

        result = runner.run(graph, inputs, **kwargs)
        assert result.status == RunStatus.COMPLETED

    @pytest.mark.parametrize(
        "cap",
        list(combinations_for(binding=Binding.BOUND, topology=Topology.CYCLIC, runner=Runner.SYNC))[:3],
        ids=str,
    )
    def test_bound_cyclic_graphs(self, cap: Capability):
        """Cyclic graphs with bound limit should execute."""
        graph = build_graph_for_capability(cap)
        inputs = get_test_inputs(cap)

        # Bound limit means we don't need to provide it
        assert "limit" not in inputs

        runner = SyncRunner()
        result = runner.run(graph, inputs, max_iterations=15)

        assert result.status == RunStatus.COMPLETED
