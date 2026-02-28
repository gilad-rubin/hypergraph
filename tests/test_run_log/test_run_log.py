"""Phase 1 Design Contract tests — RunLog (in-memory, always-on).

These encode every design decision from plan-v5.md. Written before
implementation (TDD). A phase is complete only when all tests pass.
"""

import json

import pytest

from hypergraph import AsyncRunner, Graph, InMemoryCache, SyncRunner

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
