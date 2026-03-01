"""Tests for readable reprs and _repr_html_ on all user-facing types."""

from __future__ import annotations

from datetime import datetime, timezone

from hypergraph._utils import format_datetime, format_duration_ms
from hypergraph.checkpointers.types import (
    Checkpoint,
    Run,
    RunTable,
    StepRecord,
    StepStatus,
    StepTable,
    WorkflowStatus,
)
from hypergraph.runners._shared.types import NodeRecord, NodeStats, RunLog

# ---------------------------------------------------------------------------
# format_duration_ms
# ---------------------------------------------------------------------------


class TestFormatDurationMs:
    def test_none(self):
        assert format_duration_ms(None) == "—"

    def test_milliseconds(self):
        assert format_duration_ms(42) == "42ms"
        assert format_duration_ms(999) == "999ms"

    def test_seconds(self):
        assert format_duration_ms(1500) == "1.5s"
        assert format_duration_ms(59_999) == "60.0s"

    def test_minutes(self):
        result = format_duration_ms(125_000)
        assert "2m" in result

    def test_zero(self):
        assert format_duration_ms(0) == "0ms"


class TestFormatDatetime:
    def test_none(self):
        assert format_datetime(None) == "—"

    def test_datetime(self):
        dt = datetime(2026, 3, 1, 12, 30, tzinfo=timezone.utc)
        assert format_datetime(dt) == "2026-03-01 12:30"


# ---------------------------------------------------------------------------
# NodeStats.succeeded + avg_ms
# ---------------------------------------------------------------------------


class TestNodeStatsSemantics:
    def test_all_succeeded(self):
        stats = NodeStats(count=5, total_ms=500.0, errors=0, cached=0)
        assert stats.succeeded == 5
        assert stats.avg_ms == 100.0

    def test_all_cached(self):
        stats = NodeStats(count=5, total_ms=0.0, errors=0, cached=5)
        assert stats.succeeded == 0
        assert stats.avg_ms == 0.0

    def test_all_failed(self):
        stats = NodeStats(count=5, total_ms=100.0, errors=5, cached=0)
        assert stats.succeeded == 0
        assert stats.avg_ms == 0.0

    def test_mixed(self):
        # 10 total: 3 errors, 2 cached, 5 succeeded
        stats = NodeStats(count=10, total_ms=500.0, errors=3, cached=2)
        assert stats.succeeded == 5
        assert stats.avg_ms == 100.0

    def test_empty(self):
        stats = NodeStats()
        assert stats.succeeded == 0
        assert stats.avg_ms == 0.0


# ---------------------------------------------------------------------------
# __repr__ tests
# ---------------------------------------------------------------------------


class TestRunRepr:
    def test_basic(self):
        run = Run(id="my-run", status=WorkflowStatus.COMPLETED, duration_ms=1500.0, node_count=10, error_count=2)
        r = repr(run)
        assert "Run: my-run" in r
        assert "completed" in r
        assert "1.5s" in r
        assert "10 steps" in r
        assert "2 errors" in r

    def test_with_graph_name(self):
        run = Run(id="my-run", status=WorkflowStatus.ACTIVE, graph_name="pipeline")
        r = repr(run)
        assert "(pipeline)" in r
        assert "active" in r

    def test_no_enum_repr(self):
        """Enum should show .value, not <WorkflowStatus.COMPLETED: 'completed'>."""
        run = Run(id="x", status=WorkflowStatus.COMPLETED)
        r = repr(run)
        assert "WorkflowStatus" not in r
        assert "completed" in r


class TestStepRecordRepr:
    def test_basic(self):
        step = StepRecord(
            run_id="r",
            superstep=0,
            node_name="classify",
            index=3,
            status=StepStatus.COMPLETED,
            input_versions={"x": 1},
            duration_ms=95.0,
        )
        r = repr(step)
        assert "Step [3] classify" in r
        assert "completed" in r
        assert "95ms" in r
        assert "superstep 0" in r

    def test_cached(self):
        step = StepRecord(
            run_id="r",
            superstep=0,
            node_name="a",
            index=0,
            status=StepStatus.COMPLETED,
            input_versions={},
            cached=True,
        )
        r = repr(step)
        assert "cached" in r
        assert "completed" not in r  # cached takes precedence

    def test_error(self):
        step = StepRecord(
            run_id="r",
            superstep=0,
            node_name="a",
            index=0,
            status=StepStatus.FAILED,
            input_versions={},
            error="ValueError: boom",
        )
        r = repr(step)
        assert "error: ValueError: boom" in r


class TestCheckpointRepr:
    def test_basic(self):
        cp = Checkpoint(values={"x": 1, "y": 2}, steps=[])
        r = repr(cp)
        assert "2 values" in r
        assert "0 steps" in r


class TestNodeRecordRepr:
    def test_basic(self):
        rec = NodeRecord(
            node_name="classify",
            superstep=1,
            duration_ms=95.0,
            status="completed",
            span_id="span-1",
        )
        r = repr(rec)
        assert "NodeRecord: classify" in r
        assert "completed" in r
        assert "95ms" in r

    def test_cached(self):
        rec = NodeRecord(
            node_name="a",
            superstep=0,
            duration_ms=0.0,
            status="completed",
            span_id="s",
            cached=True,
        )
        assert "cached" in repr(rec)

    def test_decision(self):
        rec = NodeRecord(
            node_name="gate",
            superstep=0,
            duration_ms=1.0,
            status="completed",
            span_id="s",
            decision="branch_a",
        )
        assert "-> branch_a" in repr(rec)


class TestNodeStatsRepr:
    def test_succeeded_only(self):
        stats = NodeStats(count=5, total_ms=500.0, errors=0, cached=0)
        r = repr(stats)
        assert "5 succeeded" in r
        assert "avg 100ms" in r

    def test_mixed(self):
        stats = NodeStats(count=10, total_ms=500.0, errors=3, cached=2)
        r = repr(stats)
        assert "5 succeeded" in r
        assert "3 errors" in r
        assert "2 cached" in r

    def test_empty(self):
        assert "empty" in repr(NodeStats())


# ---------------------------------------------------------------------------
# Collection wrappers
# ---------------------------------------------------------------------------


class TestRunTable:
    def test_extends_list(self):
        runs = [Run(id=f"r-{i}", status=WorkflowStatus.COMPLETED) for i in range(3)]
        table = RunTable(runs)
        assert len(table) == 3
        assert table[0].id == "r-0"
        assert list(table) == runs

    def test_repr(self):
        table = RunTable([Run(id="r-1", status=WorkflowStatus.COMPLETED)])
        r = repr(table)
        assert "RunTable: 1 runs" in r
        assert "Run: r-1" in r

    def test_empty(self):
        assert "(empty)" in repr(RunTable())

    def test_repr_html(self):
        table = RunTable([Run(id="r-1", status=WorkflowStatus.COMPLETED, graph_name="test")])
        html = table._repr_html_()
        assert "<table" in html
        assert "r-1" in html
        assert "completed" in html


class TestStepTable:
    def test_extends_list(self):
        steps = [StepRecord(run_id="r", superstep=0, node_name="a", index=i, status=StepStatus.COMPLETED, input_versions={}) for i in range(3)]
        table = StepTable(steps)
        assert len(table) == 3
        assert table[0].node_name == "a"

    def test_repr(self):
        table = StepTable(
            [StepRecord(run_id="r", superstep=0, node_name="classify", index=0, status=StepStatus.COMPLETED, input_versions={}, duration_ms=95.0)]
        )
        r = repr(table)
        assert "StepTable: 1 steps" in r
        assert "classify" in r

    def test_empty(self):
        assert "(empty)" in repr(StepTable())


# ---------------------------------------------------------------------------
# _repr_html_ tests
# ---------------------------------------------------------------------------


class TestHtmlReprs:
    def test_run_html(self):
        run = Run(id="my-run", status=WorkflowStatus.COMPLETED, graph_name="pipeline", duration_ms=1500.0, node_count=10)
        html = run._repr_html_()
        assert "my-run" in html
        assert "pipeline" in html
        assert "completed" in html
        assert "<" in html  # contains HTML tags

    def test_step_record_html(self):
        step = StepRecord(
            run_id="r",
            superstep=0,
            node_name="classify",
            index=3,
            status=StepStatus.COMPLETED,
            input_versions={},
            duration_ms=95.0,
        )
        html = step._repr_html_()
        assert "classify" in html
        assert "completed" in html

    def test_checkpoint_html(self):
        cp = Checkpoint(values={"x": 1, "y": 2}, steps=[])
        html = cp._repr_html_()
        assert "2" in html
        assert "values" in html

    def test_run_log_html(self):
        log = RunLog(
            graph_name="test",
            run_id="r-1",
            total_duration_ms=100.0,
            steps=(
                NodeRecord(node_name="a", superstep=0, duration_ms=50.0, status="completed", span_id="s1"),
                NodeRecord(node_name="b", superstep=1, duration_ms=50.0, status="completed", span_id="s2"),
            ),
        )
        html = log._repr_html_()
        assert "<table" in html
        assert "test" in html


class TestGraphRepr:
    def test_basic_repr(self):
        from hypergraph import Graph, node

        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        @node(output_name="result")
        def add_one(doubled: int) -> int:
            return doubled + 1

        g = Graph([double, add_one], name="pipeline")
        r = repr(g)
        assert "Graph: pipeline" in r
        assert "2 nodes" in r
        assert "no cycles" in r

    def test_html_repr(self):
        from hypergraph import Graph, node

        @node(output_name="y")
        def step(x: int) -> int:
            return x + 1

        g = Graph([step], name="simple")
        html = g._repr_html_()
        assert "<table" in html
        assert "step" in html
        assert "simple" in html
