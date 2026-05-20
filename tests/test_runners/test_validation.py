"""Tests for runner validation functions."""

import pytest

from hypergraph import Graph, interrupt, node
from hypergraph.exceptions import IncompatibleRunnerError, MissingInputError
from hypergraph.graph.validation import GraphConfigError
from hypergraph.runners import RunnerCapabilities
from hypergraph.runners._shared.input_normalization import normalize_inputs
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

    def test_entrypoint_required_for_cycles(self):
        """Cycles require graph-level entrypoint configuration."""
        with pytest.raises(GraphConfigError, match="Cyclic graphs require an explicit entrypoint"):
            Graph([counter])

    def test_entrypoint_input_provided_passes(self):
        """Cycle validation passes when configured with an entrypoint."""
        graph = Graph([counter], entrypoint="counter")
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
        with pytest.warns(UserWarning), pytest.raises(MissingInputError) as exc_info:
            validate_inputs(graph, {"xx": 1})  # typo: xx instead of x
        assert "x" in exc_info.value.missing
        # Should suggest 'xx' as similar to missing 'x'
        assert "xx" in str(exc_info.value)

    def test_multiple_missing_inputs(self):
        """Error includes all missing inputs."""
        graph = Graph([add])
        with pytest.raises(MissingInputError) as exc_info:
            validate_inputs(graph, {})
        assert len(exc_info.value.missing) == 2
        assert set(exc_info.value.missing) == {"a", "b"}


class TestInternalOverrideValidation:
    """Tests for internal override policy and conflict checks."""

    @staticmethod
    def _make_split_graph() -> Graph:
        @node(output_name=("left", "right"))
        def split(x: int) -> tuple[int, int]:
            return x, x + 1

        @node(output_name="double_left")
        def use_left(left: int) -> int:
            return left * 2

        @node(output_name="double_right")
        def use_right(right: int) -> int:
            return right * 2

        return Graph([split, use_left, use_right])

    def test_mixed_compute_and_inject_for_same_node_errors(self):
        """Providing producer inputs + produced outputs is a hard conflict."""
        graph = self._make_split_graph()
        with pytest.raises(ValueError, match="conflict.*you provided"):
            validate_inputs(graph, {"left": 100, "x": 5})

    def test_full_internal_injection_is_rejected(self):
        """Internal output injection is always rejected."""
        graph = self._make_split_graph()
        with pytest.raises(ValueError, match="internal parameters"):
            validate_inputs(graph, {"left": 100, "right": 200})

    def test_internal_override_warning_includes_producer_mapping(self):
        """Error text should identify which node produces each internal key."""
        graph = self._make_split_graph()
        with pytest.raises(ValueError, match="left <- split"):
            validate_inputs(graph, {"left": 100, "right": 200})

    def test_removed_internal_override_argument_is_rejected(self):
        """validate_inputs no longer accepts on_internal_override."""
        graph = Graph([double])
        with pytest.raises(TypeError, match="on_internal_override"):
            validate_inputs(graph, {"x": 1}, on_internal_override="warn")  # type: ignore[call-arg]

    def test_cycle_entrypoint_param_not_flagged_as_internal_override(self):
        """Cycle entry point params are excluded from compute+inject conflict checks.

        'count' is both produced by an edge (self-loop) and needed as a seed
        value to bootstrap the cycle. Providing it should NOT be treated as an
        internal override conflict.
        """
        graph = Graph([counter], entrypoint="counter")
        # 'count' is a cycle entrypoint param — should pass cleanly
        validate_inputs(graph, {"count": 0})

    def test_entrypoint_scope_treats_excluded_branch_output_as_root_input(self):
        """Active-scope required roots are not treated as internal overrides."""

        @node(output_name="x")
        def a(seed: int) -> int:
            return seed + 1

        @node(output_name="b_out")
        def b(x: int) -> int:
            return x * 2

        @node(output_name="c_out")
        def c(x: int) -> int:
            return x * 3

        @node(output_name="e")
        def e(b_out: int, c_out: int) -> int:
            return b_out + c_out

        graph = Graph([a, b, c, e]).with_entrypoint("b").select("e")
        validate_inputs(graph, {"x": 10, "c_out": 99})


class TestNormalizeInputs:
    """Tests for values + kwargs input normalization."""

    def test_values_only(self):
        """values dict is returned unchanged when kwargs are empty."""
        assert normalize_inputs({"x": 1}, {}) == {"x": 1}

    def test_kwargs_only(self):
        """kwargs shorthand works without values dict."""
        assert normalize_inputs(None, {"x": 1}) == {"x": 1}

    def test_values_and_kwargs_merge(self):
        """values and kwargs merge when keys do not overlap."""
        assert normalize_inputs({"x": 1}, {"y": 2}) == {"x": 1, "y": 2}

    def test_values_and_kwargs_overlap_raises(self):
        """Duplicate keys across values and kwargs raise ValueError."""
        with pytest.raises(ValueError, match="both values and kwargs"):
            normalize_inputs({"x": 1}, {"x": 2})

    def test_reserved_option_names_raise(self):
        """Reserved option names in kwargs raise ValueError."""
        with pytest.raises(ValueError, match="reserved runner options"):
            normalize_inputs(
                {"x": 1},
                {"select": "bad"},
                reserved_option_names=frozenset({"select"}),
            )


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
        graph = Graph([counter], entrypoint="counter")
        no_cycles_caps = RunnerCapabilities(supports_cycles=False)

        with pytest.raises(IncompatibleRunnerError) as exc_info:
            validate_runner_compatibility(graph, no_cycles_caps)
        assert exc_info.value.capability == "supports_cycles"

    def test_runner_with_cycle_support_accepts_cycles(self):
        """Runner with cycle support accepts cyclic graphs."""
        graph = Graph([counter], entrypoint="counter")
        caps = RunnerCapabilities(supports_cycles=True)
        # Should not raise
        validate_runner_compatibility(graph, caps)


class TestValidateMapCompatible:
    """Tests for validate_map_compatible function."""

    def test_dag_graph_passes(self):
        """DAG graphs are map-compatible."""
        graph = Graph([double, add.rename_inputs(a="doubled")])
        # Should not raise
        validate_map_compatible(graph)

    def test_cyclic_graph_passes(self):
        """Cyclic graphs are currently map-compatible."""
        graph = Graph([counter], entrypoint="counter")
        validate_map_compatible(graph)

    def test_graphnode_map_over_rejects_interrupts(self):
        @interrupt(output_name="decision")
        def approval(draft: str) -> str: ...

        inner = Graph([approval], name="inner")

        with pytest.raises(GraphConfigError, match="InterruptNode\\(s\\).*incompatible with map execution"):
            Graph([inner.as_node().map_over("draft")])


class TestValidateDelegatedRunners:
    """Tests for validate_delegated_runners function."""

    def test_no_delegation_passes(self):
        """Graph without runner_override passes validation."""
        from hypergraph.runners._shared.validation import validate_delegated_runners

        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node()])
        parent_caps = RunnerCapabilities(supports_async_nodes=False)
        # Should not raise
        validate_delegated_runners(outer, parent_caps)

    def test_compatible_delegation_passes(self):
        """Delegating to a runner that supports the subgraph features passes."""
        from hypergraph.runners._shared.validation import validate_delegated_runners

        inner = Graph([double], name="inner")

        # Fake runner with compatible capabilities
        class FakeRunner:
            @property
            def capabilities(self):
                return RunnerCapabilities(supports_async_nodes=False, returns_coroutine=False)

        gn = inner.as_node(runner=FakeRunner())
        outer = Graph([gn])
        parent_caps = RunnerCapabilities(supports_async_nodes=False, returns_coroutine=False)
        # Should not raise
        validate_delegated_runners(outer, parent_caps)

    def test_sync_parent_rejects_async_child(self):
        """Sync parent cannot delegate to an async-returning runner."""
        from hypergraph.runners._shared.validation import validate_delegated_runners

        inner = Graph([double], name="inner")

        class AsyncReturningRunner:
            @property
            def capabilities(self):
                return RunnerCapabilities(returns_coroutine=True)

        gn = inner.as_node(runner=AsyncReturningRunner())
        outer = Graph([gn])
        parent_caps = RunnerCapabilities(returns_coroutine=False)

        with pytest.raises(IncompatibleRunnerError) as exc_info:
            validate_delegated_runners(outer, parent_caps)
        assert "inner" in str(exc_info.value)
        assert exc_info.value.capability == "returns_coroutine"

    def test_child_runner_rejects_incompatible_subgraph(self):
        """Delegated runner must support the subgraph's features."""
        from hypergraph.runners._shared.validation import validate_delegated_runners

        inner = Graph([async_double], name="inner")

        class SyncOnlyRunner:
            @property
            def capabilities(self):
                return RunnerCapabilities(supports_async_nodes=False)

        gn = inner.as_node(runner=SyncOnlyRunner())
        outer = Graph([gn])
        parent_caps = RunnerCapabilities(supports_async_nodes=True, returns_coroutine=True)

        with pytest.raises(IncompatibleRunnerError) as exc_info:
            validate_delegated_runners(outer, parent_caps)
        assert exc_info.value.capability == "supports_async_nodes"


class TestRuntimeOverrideRejection:
    """Runtime entrypoint/select overrides are rejected with clear messages."""

    def test_runtime_entrypoint_rejected(self):
        """Runtime entrypoint= raises ValueError pointing to graph config."""
        from hypergraph.runners._shared.validation import precompute_input_validation

        graph = Graph([double])
        with pytest.raises(ValueError, match="Runtime entrypoint overrides are no longer supported"):
            precompute_input_validation(graph, entrypoint="double")

    def test_runtime_select_rejected(self):
        """Runtime select= override raises ValueError pointing to graph.select()."""
        from hypergraph.runners._shared.validation import precompute_input_validation

        graph = Graph([double])
        with pytest.raises(ValueError, match="Runtime select overrides are no longer supported"):
            precompute_input_validation(graph, selected=("doubled",))
