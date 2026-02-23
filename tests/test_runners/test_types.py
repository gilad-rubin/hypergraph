"""Tests for runner types: RunStatus, RunResult, RunnerCapabilities, GraphState."""


from hypergraph.runners import (
    GraphState,
    NodeExecution,
    PauseInfo,
    RunnerCapabilities,
    RunResult,
    RunStatus,
)


class TestRunStatus:
    """Tests for RunStatus enum."""

    def test_completed_status_exists(self):
        assert RunStatus.COMPLETED is not None

    def test_failed_status_exists(self):
        assert RunStatus.FAILED is not None

    def test_status_values_are_strings(self):
        assert RunStatus.COMPLETED.value == "completed"
        assert RunStatus.FAILED.value == "failed"


class TestRunResult:
    """Tests for RunResult dataclass."""

    def test_create_with_values_and_status(self):
        result = RunResult(
            values={"x": 1, "y": 2},
            status=RunStatus.COMPLETED,
        )
        assert result.values == {"x": 1, "y": 2}
        assert result.status == RunStatus.COMPLETED

    def test_run_id_is_auto_generated(self):
        result = RunResult(values={}, status=RunStatus.COMPLETED)
        assert result.run_id is not None
        assert result.run_id.startswith("run-")
        assert len(result.run_id) == 16  # "run-" + 12 hex chars

    def test_run_id_is_unique(self):
        result1 = RunResult(values={}, status=RunStatus.COMPLETED)
        result2 = RunResult(values={}, status=RunStatus.COMPLETED)
        assert result1.run_id != result2.run_id

    def test_run_id_can_be_explicit(self):
        result = RunResult(
            values={},
            status=RunStatus.COMPLETED,
            run_id="custom-run-id",
        )
        assert result.run_id == "custom-run-id"

    def test_workflow_id_is_optional(self):
        result = RunResult(values={}, status=RunStatus.COMPLETED)
        assert result.workflow_id is None

    def test_workflow_id_can_be_set(self):
        result = RunResult(
            values={},
            status=RunStatus.COMPLETED,
            workflow_id="workflow-123",
        )
        assert result.workflow_id == "workflow-123"

    def test_dict_like_access(self):
        result = RunResult(
            values={"x": 42, "y": "hello"},
            status=RunStatus.COMPLETED,
        )
        assert result["x"] == 42
        assert result["y"] == "hello"

    def test_dict_like_contains(self):
        result = RunResult(values={"x": 1}, status=RunStatus.COMPLETED)
        assert "x" in result
        assert "y" not in result

    def test_get_with_default(self):
        result = RunResult(values={"x": 1}, status=RunStatus.COMPLETED)
        assert result.get("x") == 1
        assert result.get("y") is None
        assert result.get("y", "default") == "default"

    def test_error_is_none_by_default(self):
        result = RunResult(values={}, status=RunStatus.COMPLETED)
        assert result.error is None

    def test_error_can_be_set(self):
        error = ValueError("test error")
        result = RunResult(
            values={},
            status=RunStatus.FAILED,
            error=error,
        )
        assert result.error is error

    def test_repr_is_compact_for_large_embedding(self):
        result = RunResult(
            values={"embedding": [0.1] * 5000, "label": "ok"},
            status=RunStatus.COMPLETED,
            run_id="run-fixed-id",
        )
        text = repr(result)

        assert "embedding" in text
        assert "len=5000" in text
        assert "status=completed" in text
        assert "run_id='run-fixed-id'" in text
        assert len(text) <= 4000

    def test_repr_keeps_small_payloads_readable(self):
        result = RunResult(
            values={"x": 1, "y": "hello"},
            status=RunStatus.COMPLETED,
        )
        text = repr(result)

        assert "values={'x': 1, 'y': 'hello'}" in text
        assert "status=completed" in text

    def test_repr_preserves_short_container_types(self):
        result = RunResult(
            values={
                "tuple_value": (1, 2),
                "single_tuple": (1,),
                "set_value": {1, 2},
                "frozenset_value": frozenset({1, 2}),
            },
            status=RunStatus.COMPLETED,
        )
        text = repr(result)

        assert "'tuple_value': (1, 2)" in text
        assert "'single_tuple': (1,)" in text
        assert "'set_value': {" in text
        assert "'frozenset_value': frozenset({" in text
        assert "'tuple_value': [1, 2]" not in text
        assert "'set_value': [1, 2]" not in text

    def test_repr_compacts_failed_and_paused_runs(self):
        error = ValueError("x" * 500)
        pause = PauseInfo(
            node_name="review/interrupt",
            output_param="decision",
            value=[1] * 100,
            output_params=("decision", "notes"),
            values={"decision": "approve", "notes": "x" * 300},
        )
        result = RunResult(
            values={"result": "partial"},
            status=RunStatus.PAUSED,
            error=error,
            pause=pause,
        )
        text = repr(result)

        assert "ValueError" in text
        assert "PauseInfo" in text
        assert "review/interrupt" in text
        assert "len=100" in text
        assert len(text) <= 4000

    def test_repr_pretty_uses_compact_repr(self):
        result = RunResult(values={"embedding": [0.1] * 5000}, status=RunStatus.COMPLETED)

        class _PrettyPrinter:
            def __init__(self):
                self.output = ""

            def text(self, value):
                self.output += value

        pretty = _PrettyPrinter()
        result._repr_pretty_(pretty, cycle=False)
        assert pretty.output == repr(result)

        cycle_pretty = _PrettyPrinter()
        result._repr_pretty_(cycle_pretty, cycle=True)
        assert cycle_pretty.output == "RunResult(...)"


class TestRunnerCapabilities:
    """Tests for RunnerCapabilities dataclass."""

    def test_default_capabilities(self):
        caps = RunnerCapabilities()
        assert caps.supports_cycles is True
        assert caps.supports_async_nodes is False
        assert caps.supports_streaming is False
        assert caps.returns_coroutine is False

    def test_supports_cycles_default_true(self):
        caps = RunnerCapabilities()
        assert caps.supports_cycles is True

    def test_supports_async_nodes_default_false(self):
        caps = RunnerCapabilities()
        assert caps.supports_async_nodes is False

    def test_returns_coroutine_default_false(self):
        caps = RunnerCapabilities()
        assert caps.returns_coroutine is False

    def test_custom_capabilities(self):
        caps = RunnerCapabilities(
            supports_cycles=False,
            supports_async_nodes=True,
            supports_streaming=True,
            returns_coroutine=True,
        )
        assert caps.supports_cycles is False
        assert caps.supports_async_nodes is True
        assert caps.supports_streaming is True
        assert caps.returns_coroutine is True


class TestNodeExecution:
    """Tests for NodeExecution dataclass."""

    def test_create_with_all_fields(self):
        execution = NodeExecution(
            node_name="test_node",
            input_versions={"x": 1, "y": 2},
            outputs={"result": 42},
        )
        assert execution.node_name == "test_node"
        assert execution.input_versions == {"x": 1, "y": 2}
        assert execution.outputs == {"result": 42}


class TestGraphState:
    """Tests for GraphState dataclass."""

    def test_create_empty_state(self):
        state = GraphState()
        assert state.values == {}
        assert state.versions == {}
        assert state.node_executions == {}

    def test_create_with_initial_values(self):
        state = GraphState(
            values={"x": 1, "y": 2},
            versions={"x": 1, "y": 1},
        )
        assert state.values == {"x": 1, "y": 2}
        assert state.versions == {"x": 1, "y": 1}

    def test_update_value_sets_value(self):
        state = GraphState()
        state.update_value("x", 42)
        assert state.values["x"] == 42

    def test_update_value_increments_version(self):
        state = GraphState()
        state.update_value("x", 1)
        assert state.versions["x"] == 1

        state.update_value("x", 2)
        assert state.versions["x"] == 2

        state.update_value("x", 3)
        assert state.versions["x"] == 3

    def test_initial_version_is_one(self):
        state = GraphState()
        state.update_value("x", "first")
        assert state.versions["x"] == 1

    def test_get_version_returns_zero_for_unset(self):
        state = GraphState()
        assert state.get_version("nonexistent") == 0

    def test_get_version_returns_current_version(self):
        state = GraphState()
        state.update_value("x", 1)
        state.update_value("x", 2)
        assert state.get_version("x") == 2

    def test_copy_creates_independent_state(self):
        state = GraphState()
        state.update_value("x", 1)

        copied = state.copy()
        copied.update_value("x", 2)
        copied.update_value("y", 3)

        # Original unchanged
        assert state.values["x"] == 1
        assert "y" not in state.values
        assert state.versions["x"] == 1

        # Copy has new values
        assert copied.values["x"] == 2
        assert copied.values["y"] == 3
        assert copied.versions["x"] == 2
        assert copied.versions["y"] == 1
