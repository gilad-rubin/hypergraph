"""
Parametrized tests using the capability matrix.

Test Strategy:
- Default: Use pairwise combinations (~100 tests) for fast local development
- CI: Use full matrix (~8000 tests) with `pytest -m full_matrix`

All tests run in parallel via pytest-xdist.
"""

import pytest

from hypergraph import InMemoryCache
from hypergraph.runners import SyncRunner, AsyncRunner, RunStatus

from .matrix import (
    Caching,
    Capability,
    Runner,
    Topology,
    MapMode,
    NestingDepth,
    Renaming,
    Binding,
    NodeType,
    all_valid_combinations,
    pairwise_combinations,
    combinations_for,
    count_combinations,
)
from .builders import build_graph_for_capability, get_test_inputs


# =============================================================================
# Test helpers
# =============================================================================


def _make_cache(cap: Capability):
    """Return a cache backend if the capability requires one."""
    if cap.caching == Caching.IN_MEMORY:
        return InMemoryCache()
    return None


def run_capability_sync(cap: Capability) -> None:
    """Run a capability test with SyncRunner."""
    graph = build_graph_for_capability(cap)
    inputs = get_test_inputs(cap)

    runner = SyncRunner(cache=_make_cache(cap))
    kwargs = {"max_iterations": 10} if cap.topology == Topology.CYCLIC else {}

    result = runner.run(graph, inputs, **kwargs)
    assert result.status == RunStatus.COMPLETED, f"Failed: {cap}"


async def run_capability_async(cap: Capability) -> None:
    """Run a capability test with AsyncRunner."""
    graph = build_graph_for_capability(cap)
    inputs = get_test_inputs(cap)

    runner = AsyncRunner(cache=_make_cache(cap))
    kwargs = {"max_iterations": 10} if cap.topology == Topology.CYCLIC else {}

    result = await runner.run(graph, inputs, **kwargs)
    assert result.status == RunStatus.COMPLETED, f"Failed: {cap}"


# Cache pairwise combinations (computed once at import time)
_sync_pairwise = [c for c in pairwise_combinations() if c.runner == Runner.SYNC]
_async_pairwise = [c for c in pairwise_combinations() if c.runner == Runner.ASYNC]


# =============================================================================
# Matrix sanity tests
# =============================================================================


class TestMatrixSanity:
    """Tests that the matrix itself is well-formed."""

    def test_has_valid_combinations(self):
        """Matrix generates at least some valid combinations."""
        combos = list(all_valid_combinations())
        assert len(combos) > 0

    def test_pairwise_is_smaller_than_full(self):
        """Pairwise should be much smaller than full matrix."""
        pairwise = list(pairwise_combinations())
        full = list(all_valid_combinations())
        assert len(pairwise) < len(full) / 10, "Pairwise should be <10% of full"
        assert len(pairwise) >= 20, "Pairwise should have meaningful coverage"

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
# Pairwise tests (default - fast local development)
# =============================================================================


class TestPairwiseSync:
    """Pairwise sync runner tests - covers all dimension pairs efficiently."""

    @pytest.mark.parametrize("cap", _sync_pairwise, ids=str)
    def test_sync_pairwise(self, cap: Capability):
        """Execute sync capability from pairwise set."""
        run_capability_sync(cap)


class TestPairwiseAsync:
    """Pairwise async runner tests - covers all dimension pairs efficiently."""

    @pytest.mark.parametrize("cap", _async_pairwise, ids=str)
    async def test_async_pairwise(self, cap: Capability):
        """Execute async capability from pairwise set."""
        await run_capability_async(cap)


# =============================================================================
# Full matrix tests (CI only - comprehensive)
# =============================================================================


@pytest.mark.full_matrix
class TestFullMatrixSync:
    """
    Full matrix sync tests - run ALL valid sync combinations.

    Run with: pytest -m full_matrix
    """

    @pytest.mark.parametrize(
        "cap", list(combinations_for(runner=Runner.SYNC)), ids=str
    )
    def test_all_sync_combinations(self, cap: Capability):
        """Every valid sync combination should execute."""
        run_capability_sync(cap)


@pytest.mark.full_matrix
class TestFullMatrixAsync:
    """
    Full matrix async tests - run ALL valid async combinations.

    Run with: pytest -m full_matrix
    """

    @pytest.mark.parametrize(
        "cap", list(combinations_for(runner=Runner.ASYNC)), ids=str
    )
    async def test_all_async_combinations(self, cap: Capability):
        """Every valid async combination should execute."""
        await run_capability_async(cap)


# =============================================================================
# Focused capability tests (verify specific behaviors beyond just "it runs")
# =============================================================================


class TestMapOverBehavior:
    """Test map_over specific behavior."""

    @pytest.mark.parametrize(
        "cap",
        [c for c in _sync_pairwise if c.map_mode == MapMode.ZIP and c.has_nesting],
        ids=str,
    )
    def test_zip_mode_produces_equal_length_outputs(self, cap: Capability):
        """Zip mode should produce outputs matching input length."""
        graph = build_graph_for_capability(cap)
        inputs = get_test_inputs(cap)

        runner = SyncRunner()
        result = runner.run(graph, inputs)

        assert result.status == RunStatus.COMPLETED
        for value in result.values.values():
            if isinstance(value, list):
                input_lengths = [len(v) for v in inputs.values() if isinstance(v, list)]
                if input_lengths:
                    assert len(value) == input_lengths[0]


class TestCyclicBehavior:
    """Test cyclic graph specific behavior."""

    @pytest.mark.parametrize(
        "cap",
        [c for c in _sync_pairwise if c.topology == Topology.CYCLIC],
        ids=str,
    )
    def test_cyclic_stabilizes(self, cap: Capability):
        """Cyclic graphs should stabilize within max_iterations."""
        run_capability_sync(cap)


class TestRenamingBehavior:
    """Test renaming specific behavior."""

    @pytest.mark.parametrize(
        "cap",
        [c for c in _sync_pairwise if c.renaming != Renaming.NONE],
        ids=str,
    )
    def test_renamed_graphs_execute(self, cap: Capability):
        """Graphs with renamed nodes/inputs/outputs should execute."""
        run_capability_sync(cap)


class TestBindingBehavior:
    """Test binding specific behavior."""

    @pytest.mark.parametrize(
        "cap",
        [c for c in _sync_pairwise if c.binding == Binding.BOUND],
        ids=str,
    )
    def test_bound_graphs_execute(self, cap: Capability):
        """Graphs with bound values should execute."""
        run_capability_sync(cap)

    @pytest.mark.parametrize(
        "cap",
        [c for c in _sync_pairwise if c.binding == Binding.BOUND and c.topology == Topology.CYCLIC],
        ids=str,
    )
    def test_binding_removes_input_requirement(self, cap: Capability):
        """Bound values should not require explicit input."""
        inputs = get_test_inputs(cap)
        assert "limit" not in inputs
