"""Tests for readable reprs and _repr_html_ on all user-facing types."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hypergraph._repr import (
    _compact_html,
    error_html,
    html_detail,
    html_table_controls_script,
    theme_wrap,
    unique_dom_id,
    values_html,
    widget_state_key,
)
from hypergraph._utils import format_datetime, format_duration_ms
from hypergraph.checkpointers.types import (
    Checkpoint,
    LineageRow,
    LineageView,
    Run,
    RunTable,
    StepRecord,
    StepStatus,
    StepTable,
    WorkflowStatus,
)
from hypergraph.runners._shared.types import MapLog, MapResult, NodeRecord, NodeStats, RunLog, RunResult, RunStatus

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
        assert "RunTable: 1 run" in r
        assert "Run: r-1" in r

    def test_empty(self):
        assert "(empty)" in repr(RunTable())

    def test_repr_html(self):
        table = RunTable([Run(id="r-1", status=WorkflowStatus.COMPLETED, graph_name="test")])
        html = table._repr_html_()
        assert "<table" in html
        assert "<details" in html
        assert "Run Traces" in html
        assert "r-1" in html
        assert "completed" in html
        assert "View:" in html
        assert "Status:" in html
        assert "Sort:" in html
        assert "Show:" in html
        assert "Open" not in html

    def test_repr_html_groups_children_under_parent_controls(self):
        parent = Run(id="batch-1", status=WorkflowStatus.COMPLETED, graph_name="g")
        child = Run(
            id="batch-1/0",
            status=WorkflowStatus.COMPLETED,
            graph_name="g",
            parent_run_id="batch-1",
        )
        html = RunTable([parent, child])._repr_html_()
        assert "Parents only" in html
        assert 'data-parent="1"' in html
        assert "batch-1/0" in html


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
        assert "StepTable: 1 step" in r
        assert "classify" in r

    def test_empty(self):
        assert "(empty)" in repr(StepTable())

    def test_repr_html_has_step_drilldown(self):
        table = StepTable(
            [
                StepRecord(
                    run_id="r",
                    superstep=0,
                    node_name="classify",
                    index=0,
                    status=StepStatus.COMPLETED,
                    input_versions={},
                    values={"x": 1},
                    duration_ms=95.0,
                )
            ]
        )
        html = table._repr_html_()
        assert "<table" in html
        assert "<details" in html
        assert "Values" in html


class TestLineageView:
    def test_repr_and_html(self):
        root = Run(id="wf-root", status=WorkflowStatus.COMPLETED)
        child = Run(id="wf-child", status=WorkflowStatus.ACTIVE, forked_from="wf-root", fork_superstep=1)
        view = LineageView(
            [
                LineageRow(lane="● ", run=root, depth=0),
                LineageRow(lane="└─ ", run=child, depth=1, is_selected=True),
            ],
            selected_run_id="wf-child",
            root_run_id="wf-root",
        )

        text = repr(view)
        assert "LineageView: wf-child (root=wf-root)" in text
        assert "wf-root" in text
        assert "wf-child" in text
        assert "<selected>" in text

        html = view._repr_html_()
        assert "Workflow Lineage: wf-child" in html
        assert "Lineage from root wf-root" in html
        assert "Kind" in html
        assert "Cached" in html
        assert "wf-root" in html
        assert "wf-child" in html


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
        assert "<details" not in html
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
        assert "Node: step" not in html


# ---------------------------------------------------------------------------
# Value rendering helpers
# ---------------------------------------------------------------------------


class TestCompactHtml:
    def test_none(self):
        assert "None" in _compact_html(None)

    def test_int(self):
        assert "42" in _compact_html(42)

    def test_string(self):
        html = _compact_html("hello")
        assert "hello" in html
        assert "<code" in html

    def test_long_string_truncated(self):
        long_str = "a" * 300
        html = _compact_html(long_str)
        assert "len=300" in html

    def test_dict(self):
        html = _compact_html({"a": 1, "b": 2})
        assert "2 keys" in html

    def test_list(self):
        html = _compact_html([1, 2, 3])
        assert "3 items" in html

    def test_empty_dict(self):
        assert "{}" in _compact_html({})

    def test_empty_list(self):
        assert "[]" in _compact_html([])


class TestValuesHtml:
    def test_basic(self):
        html = values_html({"x": 42, "name": "hello"})
        assert "<table" in html
        assert "x" in html
        assert "42" in html

    def test_empty(self):
        html = values_html({})
        assert "no values" in html

    def test_truncated(self):
        big = {f"key_{i}": i for i in range(20)}
        html = values_html(big, max_items=5)
        assert "more key" in html

    def test_html_escaping(self):
        html = values_html({"x": "<script>alert(1)</script>"})
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestErrorHtml:
    def test_exception(self):
        html = error_html(ValueError("boom"))
        assert "ValueError" in html
        assert "boom" in html

    def test_none(self):
        assert error_html(None) == ""


class TestWidgetStatePersistence:
    def test_theme_wrap_includes_state_key(self):
        html = theme_wrap("<div>hello</div>", state_key="abc")
        assert 'data-hg-state-key="abc"' in html
        assert "hypergraph:details:" in html

    def test_html_detail_marks_persistable_details(self):
        html = html_detail("Summary", "<div>Body</div>", state_key="section-1")
        assert 'data-hg-persist="1"' in html
        assert 'data-hg-key="section-1"' in html

    def test_widget_state_key_is_stable(self):
        key1 = widget_state_key("checkpointer", "/tmp/runs.db")
        key2 = widget_state_key("checkpointer", "/tmp/runs.db")
        key3 = widget_state_key("checkpointer", "/tmp/other.db")
        assert key1 == key2
        assert key1 != key3

    def test_table_controls_script_targets_direct_rows_only(self):
        script = html_table_controls_script(table_id="t", view_id="v", status_id="s", sort_id="o", show_id="l")
        assert "tb.children" in script

    def test_unique_dom_id_changes_per_render(self):
        first = unique_dom_id("map-log", "g", 5)
        second = unique_dom_id("map-log", "g", 5)
        assert first != second


# ---------------------------------------------------------------------------
# PARTIAL status
# ---------------------------------------------------------------------------


class TestPartialStatus:
    def _make_result(self, status=RunStatus.COMPLETED, error=None):
        return RunResult(values={}, status=status, error=error)

    def test_mixed_is_partial(self):
        mr = MapResult(
            results=(self._make_result(), self._make_result(RunStatus.FAILED, ValueError("x"))),
            run_id="r",
            total_duration_ms=10.0,
            map_over=("x",),
            map_mode="zip",
            graph_name="test",
        )
        assert mr.status == RunStatus.PARTIAL
        assert mr.partial is True
        assert mr.failed is True  # any() check

    def test_all_failed(self):
        mr = MapResult(
            results=(
                self._make_result(RunStatus.FAILED, ValueError("a")),
                self._make_result(RunStatus.FAILED, ValueError("b")),
            ),
            run_id="r",
            total_duration_ms=10.0,
            map_over=("x",),
            map_mode="zip",
            graph_name="test",
        )
        assert mr.status == RunStatus.FAILED
        assert mr.partial is False

    def test_partial_badge_in_html(self):
        mr = MapResult(
            results=(self._make_result(), self._make_result(RunStatus.FAILED, ValueError("x"))),
            run_id="r",
            total_duration_ms=10.0,
            map_over=("x",),
            map_mode="zip",
            graph_name="test",
        )
        html = mr._repr_html_()
        assert "partial" in html


# ---------------------------------------------------------------------------
# Progressive disclosure
# ---------------------------------------------------------------------------


class TestRunResultProgressiveDisclosure:
    def test_values_collapsible(self):
        r = RunResult(values={"x": 42, "name": "hello"}, status=RunStatus.COMPLETED)
        html = r._repr_html_()
        assert "<details" in html
        assert "Values" in html
        assert "x" in html

    def test_no_values_section_when_empty(self):
        r = RunResult(values={}, status=RunStatus.COMPLETED)
        html = r._repr_html_()
        # No collapsible values section for empty dict
        assert "Values (0 keys)" not in html

    def test_log_collapsible(self):
        log = RunLog(
            graph_name="test",
            run_id="r-1",
            total_duration_ms=100.0,
            steps=(NodeRecord(node_name="a", superstep=0, duration_ms=50.0, status="completed", span_id="s1"),),
        )
        r = RunResult(values={"result": 42}, status=RunStatus.COMPLETED, log=log)
        html = r._repr_html_()
        assert "Run Log" in html

    def test_error_shown(self):
        r = RunResult(values={}, status=RunStatus.FAILED, error=ValueError("boom"))
        html = r._repr_html_()
        assert "ValueError" in html
        assert "boom" in html


class TestMapResultProgressiveDisclosure:
    def _make_result(self, status=RunStatus.COMPLETED, error=None):
        return RunResult(values={}, status=status, error=error)

    def test_per_item_breakdown_collapsible(self):
        mr = MapResult(
            results=(self._make_result(), self._make_result()),
            run_id="r",
            total_duration_ms=10.0,
            map_over=("x",),
            map_mode="zip",
            graph_name="test",
        )
        html = mr._repr_html_()
        assert "<details" in html
        assert "Per-item breakdown" in html

    def test_nested_drilldown_contains_run_result(self):
        """Each item in the drill-down should be a full RunResult panel."""
        mr = MapResult(
            results=(
                RunResult(values={"x": 42}, status=RunStatus.COMPLETED),
                RunResult(values={"x": 99}, status=RunStatus.COMPLETED),
            ),
            run_id="r",
            total_duration_ms=10.0,
            map_over=("x",),
            map_mode="zip",
            graph_name="test",
        )
        html = mr._repr_html_()
        # Each item drills down to a nested RunResult panel
        assert "Item 0:" in html
        assert "Item 1:" in html
        assert "RunResult:" in html

    def test_shows_error_type_for_failed_items(self):
        mr = MapResult(
            results=(self._make_result(), self._make_result(RunStatus.FAILED, ValueError("boom"))),
            run_id="r",
            total_duration_ms=10.0,
            map_over=("x",),
            map_mode="zip",
            graph_name="test",
        )
        html = mr._repr_html_()
        assert "ValueError" in html

    def test_per_item_breakdown_has_status_filter(self):
        mr = MapResult(
            results=(
                self._make_result(),
                self._make_result(RunStatus.FAILED, ValueError("boom")),
            ),
            run_id="r",
            total_duration_ms=10.0,
            map_over=("x",),
            map_mode="zip",
            graph_name="test",
        )
        html = mr._repr_html_()
        assert "Filter:" in html
        assert "Completed (1)" in html
        assert "Failed (1)" in html
        assert "Page size:" in html
        assert "Prev" in html
        assert "Next" in html
        assert 'data-hg-map-item="1"' in html
        assert 'data-status="completed"' in html
        assert 'data-status="failed"' in html

    def test_per_item_breakdown_paginates_without_truncating_items(self):
        mr = MapResult(
            results=tuple(self._make_result() for _ in range(100)),
            run_id="r",
            total_duration_ms=10.0,
            map_over=("x",),
            map_mode="zip",
            graph_name="test",
        )
        html = mr._repr_html_()
        assert "All (100)" in html
        assert "Page size:" in html
        assert "Prev" in html
        assert "Next" in html
        assert "Item 99:" in html
        assert "more item" not in html


class TestMapLogProgressiveDisclosure:
    def test_map_log_html_has_item_drilldown(self):
        log_a = RunLog(
            graph_name="test",
            run_id="r-a",
            total_duration_ms=10.0,
            steps=(NodeRecord(node_name="a", superstep=0, duration_ms=10.0, status="completed", span_id="s1"),),
        )
        log_b = RunLog(
            graph_name="test",
            run_id="r-b",
            total_duration_ms=12.0,
            steps=(NodeRecord(node_name="a", superstep=0, duration_ms=12.0, status="completed", span_id="s2"),),
        )
        mlog = MapLog(graph_name="test", total_duration_ms=22.0, items=(log_a, log_b))
        html = mlog._repr_html_()
        assert "<table" in html
        assert "<details" in html
        assert "Item Traces" in html
        assert "Filter:" in html
        assert "Page size:" in html

    def test_map_log_html_has_show_more_control(self):
        logs = tuple(
            RunLog(
                graph_name="test",
                run_id=f"r-{i}",
                total_duration_ms=10.0 + i,
                steps=(NodeRecord(node_name="a", superstep=0, duration_ms=10.0, status="completed", span_id=f"s{i}"),),
            )
            for i in range(25)
        )
        mlog = MapLog(graph_name="test", total_duration_ms=100.0, items=logs)
        html = mlog._repr_html_()
        assert "Page size:" in html
        assert "Prev" in html
        assert "Next" in html
        assert "All (25)" in html


# ---------------------------------------------------------------------------
# SqliteCheckpointer repr
# ---------------------------------------------------------------------------


class TestSqliteCheckpointerRepr:
    def test_repr(self):
        pytest.importorskip("aiosqlite")
        from hypergraph.checkpointers.sqlite import SqliteCheckpointer

        cp = SqliteCheckpointer(":memory:")
        r = repr(cp)
        assert "SqliteCheckpointer" in r
        assert "0 runs" in r

    def test_repr_html(self):
        pytest.importorskip("aiosqlite")
        from hypergraph.checkpointers.sqlite import SqliteCheckpointer

        cp = SqliteCheckpointer(":memory:")
        html = cp._repr_html_()
        assert "SqliteCheckpointer" in html
        assert "Runs" in html
        assert ".runs()" in html
        assert "data-hg-state-key" in html
