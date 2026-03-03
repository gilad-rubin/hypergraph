"""Phase 1 Design Contract tests — RunLog (in-memory, always-on).

These encode every design decision from plan-v5.md. Written before
implementation (TDD). A phase is complete only when all tests pass.
"""

import json

import pytest

from hypergraph import AsyncRunner, Graph, InMemoryCache, SyncRunner, node

from .conftest import (
    account_support,
    general_support,
    mock_classifier,
    mock_embed,
    mock_failing_llm,
    mock_format,
    mock_llm,
    mock_slow_llm,
)

# ---------------------------------------------------------------------------
# Scenario 1: Basic timing inspection (UC1)
# ---------------------------------------------------------------------------


class TestBasicTiming:
    def test_runlog_exists_after_run(self):
        """UC1: result.log is always available after run()."""
        graph = Graph([mock_embed, mock_llm, mock_format])
        result = SyncRunner().run(graph, {"text": "hello", "prompt": "test"})

        assert result.log is not None

    def test_runlog_has_steps(self):
        """UC1: result.log.steps is a tuple of NodeRecords."""
        graph = Graph([mock_embed, mock_llm, mock_format])
        result = SyncRunner().run(graph, {"text": "hello", "prompt": "test"})

        assert isinstance(result.log.steps, tuple)
        assert len(result.log.steps) == 3
        node_names = [r.node_name for r in result.log.steps]
        assert "mock_embed" in node_names
        assert "mock_llm" in node_names
        assert "mock_format" in node_names

    def test_runlog_shows_per_node_timing(self):
        """UC1: result.log.timing shows total ms per node."""
        graph = Graph([mock_embed, mock_slow_llm, mock_format])
        result = SyncRunner().run(graph, {"text": "hello", "prompt": "test"})

        timing = result.log.timing
        assert "mock_slow_llm" in timing
        assert "mock_format" in timing
        assert timing["mock_slow_llm"] > timing["mock_format"]

    def test_runlog_node_stats_aggregation(self):
        """UC1: result.log.node_stats has correct counts and averages."""
        graph = Graph([mock_embed, mock_llm])
        results = SyncRunner().map(
            graph,
            {"text": ["a", "b", "c"], "prompt": "test"},
            map_over="text",
        )

        # Each item has its own RunLog
        for r in results:
            assert r.log is not None
            stats = r.log.node_stats["mock_embed"]
            assert stats.count == 1
            assert stats.avg_ms > 0

    def test_runlog_summary_one_liner(self):
        """Progressive Disclosure: result.log.summary() returns concise string."""
        graph = Graph([mock_embed, mock_llm])
        result = SyncRunner().run(graph, {"text": "hello", "prompt": "test"})

        summary = result.log.summary()
        assert isinstance(summary, str)
        assert len(summary) < 200  # one-liner


# ---------------------------------------------------------------------------
# Scenario 2: Error inspection (UC2)
# ---------------------------------------------------------------------------


class TestErrorInspection:
    def test_runlog_errors_returns_only_failures(self):
        """UC2: result.log.errors filters to failed nodes only."""
        graph = Graph([mock_embed, mock_failing_llm])
        result = SyncRunner().run(
            graph,
            {"text": "hello", "prompt": "test"},
            error_handling="continue",
        )

        assert len(result.log.errors) >= 1
        failed = result.log.errors[0]
        assert failed.node_name == "mock_failing_llm"
        assert failed.status == "failed"
        assert "504 Gateway Timeout" in failed.error

    def test_runlog_each_map_item_has_own_log(self):
        """UC2: Each map item gets its own RunLog."""
        graph = Graph([mock_embed, mock_llm])
        results = SyncRunner().map(
            graph,
            {"text": ["a", "b", "c"], "prompt": "test"},
            map_over="text",
        )

        for r in results:
            assert r.log is not None
            assert len(r.log.steps) >= 1


# ---------------------------------------------------------------------------
# Scenario 3: Routing decisions (UC3)
# ---------------------------------------------------------------------------


class TestRoutingDecisions:
    def test_runlog_captures_routing_decision(self):
        """UC3: NodeRecord.decision shows the gate's routing choice."""
        graph = Graph([mock_classifier, account_support, general_support])
        result = SyncRunner().run(graph, {"query": "How do I reset my password?"})

        classifier_record = next(r for r in result.log.steps if r.node_name == "mock_classifier")
        assert classifier_record.decision == "account_support"

    def test_runlog_no_decision_for_regular_nodes(self):
        """Regular (non-gate) nodes have decision=None."""
        graph = Graph([mock_embed, mock_llm])
        result = SyncRunner().run(graph, {"text": "hello", "prompt": "test"})

        for record in result.log.steps:
            assert record.decision is None


# ---------------------------------------------------------------------------
# Scenario 4: Cache hits (UC1 variant)
# ---------------------------------------------------------------------------


class TestCacheHits:
    def test_runlog_cached_nodes_marked(self):
        """Cached nodes have cached=True and status='completed'."""
        from hypergraph import node as node_decorator

        @node_decorator(output_name="result", cache=True)
        def cacheable_add(x: int) -> int:
            return x + 1

        @node_decorator(output_name="doubled", cache=True)
        def cacheable_double(result: int) -> int:
            return result * 2

        graph = Graph([cacheable_add, cacheable_double])
        runner = SyncRunner(cache=InMemoryCache())

        # First run — no cache
        r1 = runner.run(graph, {"x": 5})
        assert all(not r.cached for r in r1.log.steps)

        # Second run — cache hit
        r2 = runner.run(graph, {"x": 5})
        assert any(r.cached for r in r2.log.steps)
        # Cached nodes have status "completed", not "cached"
        cached_records = [r for r in r2.log.steps if r.cached]
        for r in cached_records:
            assert r.status == "completed"


# ---------------------------------------------------------------------------
# Scenario 5: Display formats (UC7 — AI agent)
# ---------------------------------------------------------------------------


class TestDisplayFormats:
    def test_runlog_str_is_formatted_table(self):
        """UC7: str(result.log) produces human-readable table."""
        graph = Graph([mock_embed, mock_llm, mock_format])
        result = SyncRunner().run(graph, {"text": "hello", "prompt": "test"})

        output = str(result.log)
        assert "RunLog:" in output
        assert "mock_embed" in output
        assert "mock_llm" in output
        assert "completed" in output

    def test_runlog_to_dict_is_json_serializable(self):
        """UC7: result.log.to_dict() can be JSON-serialized."""
        graph = Graph([mock_embed, mock_llm])
        result = SyncRunner().run(graph, {"text": "hello", "prompt": "test"})

        d = result.log.to_dict()
        json_str = json.dumps(d)  # must not raise
        parsed = json.loads(json_str)
        assert "steps" in parsed

    def test_runlog_to_dict_primitives_only(self):
        """to_dict() returns only primitive types (str, int, float, bool, None, list, dict)."""
        graph = Graph([mock_embed, mock_llm])
        result = SyncRunner().run(graph, {"text": "hello", "prompt": "test"})

        d = result.log.to_dict()

        def assert_primitive(obj, path=""):
            if obj is None:
                return
            if isinstance(obj, (str, int, float, bool)):
                return
            if isinstance(obj, dict):
                for k, v in obj.items():
                    assert isinstance(k, str), f"Non-string key at {path}.{k}"
                    assert_primitive(v, f"{path}.{k}")
                return
            if isinstance(obj, list):
                for i, v in enumerate(obj):
                    assert_primitive(v, f"{path}[{i}]")
                return
            pytest.fail(f"Non-primitive type {type(obj).__name__} at {path}")

        assert_primitive(d)

    def test_runlog_repr_is_concise(self):
        """RunLog repr is concise for REPL/debugger."""
        graph = Graph([mock_embed, mock_llm])
        result = SyncRunner().run(graph, {"text": "hello", "prompt": "test"})

        r = repr(result.log)
        assert "RunLog" in r
        assert len(r) < 200

    def test_runlog_repr_pretty_shows_table(self):
        """_repr_pretty_ outputs the full table (same as str)."""
        graph = Graph([mock_embed, mock_llm])
        result = SyncRunner().run(graph, {"text": "hello", "prompt": "test"})

        class FakePP:
            def __init__(self):
                self.result = ""

            def text(self, s):
                self.result = s

        pp = FakePP()
        result.log._repr_pretty_(pp, cycle=False)
        assert pp.result == str(result.log)

        pp2 = FakePP()
        result.log._repr_pretty_(pp2, cycle=True)
        assert pp2.result == "RunLog(...)"


# ---------------------------------------------------------------------------
# Scenario 6: Async runner parity
# ---------------------------------------------------------------------------


class TestAsyncParity:
    @pytest.mark.asyncio
    async def test_async_runner_produces_runlog(self):
        """Async runner produces RunLog identical in structure to sync."""
        graph = Graph([mock_embed, mock_llm])
        result = await AsyncRunner().run(graph, {"text": "hello", "prompt": "test"})

        assert result.log is not None
        assert len(result.log.steps) == 2
        assert result.log.timing["mock_embed"] >= 0

    @pytest.mark.asyncio
    async def test_async_runner_routing_decision(self):
        """Async runner captures routing decisions same as sync."""
        graph = Graph([mock_classifier, account_support, general_support])
        result = await AsyncRunner().run(graph, {"query": "How do I reset my password?"})

        classifier_record = next(r for r in result.log.steps if r.node_name == "mock_classifier")
        assert classifier_record.decision == "account_support"


# ---------------------------------------------------------------------------
# Structural invariants
# ---------------------------------------------------------------------------


class TestStructuralInvariants:
    def test_runlog_is_frozen(self):
        """RunLog is immutable after creation."""
        graph = Graph([mock_embed, mock_llm])
        result = SyncRunner().run(graph, {"text": "hello", "prompt": "test"})

        with pytest.raises(AttributeError):
            result.log.graph_name = "hacked"

    def test_node_record_is_frozen(self):
        """NodeRecord is immutable."""
        graph = Graph([mock_embed, mock_llm])
        result = SyncRunner().run(graph, {"text": "hello", "prompt": "test"})

        with pytest.raises(AttributeError):
            result.log.steps[0].node_name = "hacked"

    def test_runlog_total_duration(self):
        """total_duration_ms is positive and roughly sane."""
        graph = Graph([mock_embed, mock_slow_llm])
        result = SyncRunner().run(graph, {"text": "hello", "prompt": "test"})

        assert result.log.total_duration_ms > 0
        # Should be at least ~50ms (mock_slow_llm sleeps 50ms)
        assert result.log.total_duration_ms >= 40


# ---------------------------------------------------------------------------
# Scenario 7: Inner RunLogs for nested graphs
# ---------------------------------------------------------------------------


@node(output_name="doubled")
def _double(x: int) -> int:
    return x * 2


@node(output_name="incremented")
def _increment(doubled: int) -> int:
    return doubled + 1


class TestInnerLogs:
    def test_simple_nested_step_returns_run_log(self):
        """A nested GraphNode step.log returns a single RunLog."""
        inner = Graph([_double], name="inner")
        outer = Graph([inner.as_node()])
        result = SyncRunner().run(outer, {"x": 5})

        assert result["doubled"] == 10
        assert result.log is not None
        assert len(result.log.steps) == 1

        step = result.log.steps[0]
        assert step.node_name == "inner"

        from hypergraph.runners._shared.types import RunLog

        assert isinstance(step.log, RunLog)
        assert step.log.graph_name == "inner"
        assert len(step.log.steps) == 1
        assert step.log.steps[0].node_name == "_double"

    def test_map_over_step_returns_map_log(self):
        """A map_over GraphNode step.log returns a MapLog when N inner."""
        inner = Graph([_double], name="inner")
        outer = Graph([inner.as_node().map_over("x")])
        result = SyncRunner().run(outer, {"x": [1, 2, 3]})

        step = result.log.steps[0]

        from hypergraph.runners._shared.types import MapLog

        assert isinstance(step.log, MapLog)
        assert len(step.log) == 3

        for item_log in step.log:
            assert item_log.graph_name == "inner"
            assert len(item_log.steps) == 1
            assert item_log.steps[0].node_name == "_double"

    def test_regular_step_log_is_none(self):
        """Regular (non-GraphNode) steps have log=None."""
        graph = Graph([_double])
        result = SyncRunner().run(graph, {"x": 5})

        for step in result.log.steps:
            assert step.log is None

    def test_deeply_nested_inner_logs(self):
        """Inner logs propagate through multiple nesting levels via .log."""
        innermost = Graph([_double], name="innermost")
        middle = Graph(
            [innermost.as_node(), _increment.with_inputs(doubled="doubled")],
            name="middle",
        )
        outer = Graph([middle.as_node()])
        result = SyncRunner().run(outer, {"x": 5})

        assert result["incremented"] == 11
        # Outer step → middle RunLog
        middle_log = result.log.steps[0].log
        assert len(middle_log.steps) == 2

        # Middle step → innermost RunLog
        innermost_step = next(s for s in middle_log.steps if s.node_name == "innermost")
        assert innermost_step.log.graph_name == "innermost"

    def test_to_dict_includes_inner_log(self):
        """to_dict() serializes inner_log via .log accessor."""
        inner = Graph([_double], name="inner")
        outer = Graph([inner.as_node()])
        result = SyncRunner().run(outer, {"x": 5})

        d = result.log.to_dict()
        step_dict = d["steps"][0]
        assert "inner_log" in step_dict
        assert step_dict["inner_log"] is not None

        inner_log_dict = step_dict["inner_log"]
        assert inner_log_dict["graph_name"] == "inner"
        assert len(inner_log_dict["steps"]) == 1

        # Ensure JSON-serializable
        json.dumps(d)

    def test_str_shows_inner_annotation(self):
        """__str__() shows '(N inner)' for steps with inner logs."""
        inner = Graph([_double], name="inner")
        outer = Graph([inner.as_node()])
        result = SyncRunner().run(outer, {"x": 5})

        output = str(result.log)
        assert "(1 inner)" in output

    def test_str_shows_footer_hint(self):
        """__str__() footer has .steps[i].log drill-down hint."""
        inner = Graph([_double], name="inner")
        outer = Graph([inner.as_node()])
        result = SyncRunner().run(outer, {"x": 5})

        output = str(result.log)
        assert ".steps[0].log" in output

    @pytest.mark.asyncio
    async def test_async_nested_graph_has_inner_log(self):
        """Async runner also surfaces inner RunLogs via .log."""
        inner = Graph([_double], name="inner")
        outer = Graph([inner.as_node()])
        result = await AsyncRunner().run(outer, {"x": 5})

        assert result["doubled"] == 10
        step = result.log.steps[0]

        from hypergraph.runners._shared.types import RunLog

        assert isinstance(step.log, RunLog)
        assert step.log.graph_name == "inner"

    @pytest.mark.asyncio
    async def test_async_map_over_step_returns_map_log(self):
        """Async runner step.log returns MapLog for map_over."""
        inner = Graph([_double], name="inner")
        outer = Graph([inner.as_node().map_over("x")])
        result = await AsyncRunner().run(outer, {"x": [1, 2, 3]})

        step = result.log.steps[0]

        from hypergraph.runners._shared.types import MapLog

        assert isinstance(step.log, MapLog)
        assert len(step.log) == 3


# ---------------------------------------------------------------------------
# Scenario 8: Duration rounding
# ---------------------------------------------------------------------------


class TestDurationRounding:
    def test_node_record_duration_rounded(self):
        """NodeRecord.duration_ms is rounded to DURATION_PRECISION decimals."""
        graph = Graph([mock_embed, mock_llm])
        result = SyncRunner().run(graph, {"text": "hello", "prompt": "test"})

        for step in result.log.steps:
            # Check that duration has at most 3 decimal places
            text = f"{step.duration_ms:.10f}"
            integer, decimal = text.split(".")
            significant = decimal.rstrip("0")
            assert len(significant) <= 3, f"Duration {step.duration_ms} has more than 3 decimal places"

    def test_runlog_total_duration_rounded(self):
        """RunLog.total_duration_ms is rounded to DURATION_PRECISION decimals."""
        graph = Graph([mock_embed, mock_llm])
        result = SyncRunner().run(graph, {"text": "hello", "prompt": "test"})

        text = f"{result.log.total_duration_ms:.10f}"
        integer, decimal = text.split(".")
        significant = decimal.rstrip("0")
        assert len(significant) <= 3, f"Total duration {result.log.total_duration_ms} has more than 3 decimal places"
