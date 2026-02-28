"""Tests for MapResult and RunResult enhancements."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from hypergraph import Graph, MapResult, RunResult, RunStatus, node
from hypergraph.runners import AsyncRunner, SyncRunner
from hypergraph.runners._shared.types import MapLog, NodeRecord, NodeStats, RunLog

# === Fixtures ===


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="result")
def fail_on_odd(x: int) -> int:
    if x % 2 != 0:
        raise ValueError(f"odd: {x}")
    return x * 2


def _make_result(
    status: RunStatus = RunStatus.COMPLETED,
    values: dict | None = None,
    error: Exception | None = None,
    log: RunLog | None = None,
) -> RunResult:
    return RunResult(
        values=values or {},
        status=status,
        error=error,
        log=log,
    )


def _make_map_result(
    results: list[RunResult] | None = None,
    **kwargs,
) -> MapResult:
    defaults = {
        "run_id": "run-abc123",
        "total_duration_ms": 42.0,
        "map_over": ("x",),
        "map_mode": "zip",
        "graph_name": "test",
    }
    defaults.update(kwargs)
    return MapResult(
        results=tuple(results or []),
        **defaults,
    )


# === RunResult Enhancement Tests ===


class TestRunResultFailed:
    def test_failed_when_status_is_failed(self):
        r = _make_result(status=RunStatus.FAILED, error=ValueError("boom"))
        assert r.failed is True

    def test_not_failed_when_completed(self):
        r = _make_result(status=RunStatus.COMPLETED)
        assert r.failed is False

    def test_not_failed_when_paused(self):
        r = _make_result(status=RunStatus.PAUSED)
        assert r.failed is False


class TestRunResultSummary:
    def test_delegates_to_log(self):
        log = RunLog(
            graph_name="test",
            run_id="run-123",
            total_duration_ms=10.0,
            steps=(),
        )
        r = _make_result(log=log)
        assert r.summary() == log.summary()

    def test_fallback_with_error(self):
        r = _make_result(
            status=RunStatus.FAILED,
            error=ValueError("bad value"),
        )
        s = r.summary()
        assert "failed" in s
        assert "ValueError" in s
        assert "bad value" in s

    def test_fallback_no_log_no_error(self):
        r = _make_result(status=RunStatus.COMPLETED)
        assert r.summary() == "completed"


class TestRunResultToDict:
    def test_includes_status_and_ids(self):
        r = _make_result()
        d = r.to_dict()
        assert d["status"] == "completed"
        assert "run_id" in d

    def test_includes_log_when_present(self):
        log = RunLog(
            graph_name="test",
            run_id="run-123",
            total_duration_ms=10.0,
            steps=(),
        )
        r = _make_result(log=log)
        d = r.to_dict()
        assert "log" in d
        assert d["log"]["graph_name"] == "test"

    def test_includes_error_string(self):
        r = _make_result(
            status=RunStatus.FAILED,
            error=ValueError("oops"),
        )
        d = r.to_dict()
        assert d["error"] == "ValueError: oops"

    def test_no_log_key_when_none(self):
        r = _make_result()
        d = r.to_dict()
        assert "log" not in d

    def test_no_error_key_when_none(self):
        r = _make_result()
        d = r.to_dict()
        assert "error" not in d


# === MapResult Unit Tests ===


class TestMapResultSequenceProtocol:
    def test_len(self):
        items = [_make_result(), _make_result()]
        mr = _make_map_result(items)
        assert len(mr) == 2

    def test_iter(self):
        items = [_make_result(), _make_result()]
        mr = _make_map_result(items)
        assert list(mr) == items

    def test_getitem_int(self):
        items = [_make_result(values={"a": 1}), _make_result(values={"a": 2})]
        mr = _make_map_result(items)
        assert mr[0].values == {"a": 1}
        assert mr[1].values == {"a": 2}

    def test_getitem_negative_index(self):
        items = [_make_result(values={"a": 1}), _make_result(values={"a": 2})]
        mr = _make_map_result(items)
        assert mr[-1].values == {"a": 2}

    def test_getitem_slice_returns_list(self):
        items = [_make_result(values={"a": i}) for i in range(5)]
        mr = _make_map_result(items)
        sliced = mr[1:3]
        assert isinstance(sliced, list)
        assert not isinstance(sliced, MapResult)
        assert len(sliced) == 2

    def test_bool_true(self):
        mr = _make_map_result([_make_result()])
        assert bool(mr) is True

    def test_bool_false(self):
        mr = _make_map_result([])
        assert bool(mr) is False

    def test_reversed(self):
        items = [_make_result(values={"a": 1}), _make_result(values={"a": 2})]
        mr = _make_map_result(items)
        rev = list(reversed(mr))
        assert rev[0].values == {"a": 2}
        assert rev[1].values == {"a": 1}

    def test_contains_run_result(self):
        r1 = _make_result(values={"a": 1})
        r2 = _make_result(values={"a": 2})
        mr = _make_map_result([r1])
        assert r1 in mr
        assert r2 not in mr

    def test_contains_string_is_list_semantics(self):
        """String __contains__ checks RunResult membership, NOT key lookup."""
        mr = _make_map_result([_make_result(values={"doubled": 4})])
        # "doubled" is not a RunResult, so this is False
        assert "doubled" not in mr

    def test_isinstance_sequence(self):
        mr = _make_map_result([])
        assert isinstance(mr, Sequence)

    def test_not_isinstance_list(self):
        mr = _make_map_result([])
        assert not isinstance(mr, list)


class TestMapResultStringKeyAccess:
    def test_getitem_string(self):
        items = [
            _make_result(values={"doubled": 2}),
            _make_result(values={"doubled": 4}),
        ]
        mr = _make_map_result(items)
        assert mr["doubled"] == [2, 4]

    def test_getitem_string_with_failures(self):
        items = [
            _make_result(values={"doubled": 2}),
            _make_result(status=RunStatus.FAILED, error=ValueError("boom")),
            _make_result(values={"doubled": 6}),
        ]
        mr = _make_map_result(items)
        assert mr["doubled"] == [2, None, 6]

    def test_get_with_default(self):
        items = [
            _make_result(values={"doubled": 2}),
            _make_result(status=RunStatus.FAILED, error=ValueError("boom")),
            _make_result(values={"doubled": 6}),
        ]
        mr = _make_map_result(items)
        assert mr.get("doubled", 0) == [2, 0, 6]

    def test_get_missing_key(self):
        items = [_make_result(values={"a": 1})]
        mr = _make_map_result(items)
        assert mr.get("nonexistent") == [None]
        assert mr.get("nonexistent", -1) == [-1]


class TestMapResultAggregateStatus:
    def test_all_completed(self):
        items = [_make_result(), _make_result()]
        mr = _make_map_result(items)
        assert mr.status == RunStatus.COMPLETED
        assert mr.completed is True
        assert mr.failed is False
        assert mr.paused is False

    def test_any_failed(self):
        items = [
            _make_result(),
            _make_result(status=RunStatus.FAILED, error=ValueError("x")),
        ]
        mr = _make_map_result(items)
        assert mr.status == RunStatus.FAILED
        assert mr.failed is True
        assert mr.completed is False

    def test_failed_takes_precedence_over_paused(self):
        items = [
            _make_result(status=RunStatus.PAUSED),
            _make_result(status=RunStatus.FAILED, error=ValueError("x")),
        ]
        mr = _make_map_result(items)
        assert mr.status == RunStatus.FAILED

    def test_paused_when_no_failures(self):
        items = [
            _make_result(),
            _make_result(status=RunStatus.PAUSED),
        ]
        mr = _make_map_result(items)
        assert mr.status == RunStatus.PAUSED
        assert mr.paused is True

    def test_empty_is_completed(self):
        mr = _make_map_result([])
        assert mr.status == RunStatus.COMPLETED
        assert mr.completed is True

    def test_failures_list(self):
        r_ok = _make_result()
        r_fail = _make_result(status=RunStatus.FAILED, error=ValueError("x"))
        mr = _make_map_result([r_ok, r_fail, r_ok])
        assert mr.failures == [r_fail]

    def test_failures_empty(self):
        mr = _make_map_result([_make_result()])
        assert mr.failures == []


class TestMapResultProgressiveDisclosure:
    def test_summary(self):
        items = [
            _make_result(),
            _make_result(),
            _make_result(status=RunStatus.FAILED, error=ValueError("x")),
        ]
        mr = _make_map_result(items, total_duration_ms=123.0)
        s = mr.summary()
        assert "3 items" in s
        assert "2 completed" in s
        assert "1 failed" in s
        assert "123ms" in s

    def test_to_dict_envelope(self):
        items = [_make_result()]
        mr = _make_map_result(items, run_id="run-abc", total_duration_ms=10.0)
        d = mr.to_dict()
        assert d["run_id"] == "run-abc"
        assert d["total_duration_ms"] == 10.0
        assert d["map_over"] == ["x"]
        assert d["map_mode"] == "zip"
        assert d["graph_name"] == "test"
        assert d["item_count"] == 1
        assert d["completed_count"] == 1
        assert d["failed_count"] == 0
        assert len(d["items"]) == 1
        # Items delegate to RunResult.to_dict()
        assert d["items"][0]["status"] == "completed"

    def test_repr(self):
        items = [_make_result(), _make_result()]
        mr = _make_map_result(items, total_duration_ms=42.0)
        r = repr(mr)
        assert "MapResult" in r
        assert "2 items" in r
        assert "2 completed" in r

    def test_repr_pretty(self):
        mr = _make_map_result([_make_result()])

        class FakePP:
            def __init__(self):
                self.result = ""

            def text(self, s):
                self.result = s

        pp = FakePP()
        mr._repr_pretty_(pp, cycle=False)
        assert "MapResult" in pp.result

        pp2 = FakePP()
        mr._repr_pretty_(pp2, cycle=True)
        assert pp2.result == "MapResult(...)"


class TestMapResultEquality:
    def test_equal_map_results(self):
        items = [_make_result(values={"a": 1})]
        mr1 = _make_map_result(items)
        mr2 = _make_map_result(items)
        assert mr1 == mr2

    def test_equal_to_list(self):
        items = [_make_result(values={"a": 1})]
        mr = _make_map_result(items)
        assert mr == items

    def test_empty_equals_empty_list(self):
        mr = _make_map_result([])
        assert mr == []

    def test_not_equal_different_results(self):
        mr1 = _make_map_result([_make_result(values={"a": 1})])
        mr2 = _make_map_result([_make_result(values={"a": 2})])
        assert mr1 != mr2

    def test_not_equal_to_unrelated_type(self):
        mr = _make_map_result([])
        assert mr != "hello"


class TestMapResultImmutability:
    def test_frozen(self):
        mr = _make_map_result([_make_result()])
        with pytest.raises(AttributeError):
            mr.results = ()  # type: ignore[misc]


class TestMapResultGetitemTypeError:
    def test_invalid_key_type(self):
        mr = _make_map_result([_make_result()])
        with pytest.raises(TypeError, match="indices must be"):
            mr[3.14]  # type: ignore[index]


# === Integration Tests ===


class TestSyncRunnerMapResult:
    def test_returns_map_result(self):
        graph = Graph([double])
        runner = SyncRunner()
        results = runner.map(graph, {"x": [1, 2, 3]}, map_over="x")

        assert isinstance(results, MapResult)
        assert len(results) == 3
        assert results.run_id is not None
        assert results.total_duration_ms > 0
        assert results.map_over == ("x",)
        assert results.map_mode == "zip"

    def test_string_key_access(self):
        graph = Graph([double])
        runner = SyncRunner()
        results = runner.map(graph, {"x": [1, 2, 3]}, map_over="x")

        assert results["doubled"] == [2, 4, 6]

    def test_empty_returns_map_result(self):
        graph = Graph([double])
        runner = SyncRunner()
        results = runner.map(graph, {"x": []}, map_over="x")

        assert isinstance(results, MapResult)
        assert len(results) == 0
        assert results.run_id is None
        assert results.total_duration_ms == 0

    def test_backward_compat_iteration(self):
        graph = Graph([double])
        runner = SyncRunner()
        results = runner.map(graph, {"x": [1, 2]}, map_over="x")

        # Old-style iteration still works
        values = [r["doubled"] for r in results]
        assert values == [2, 4]

    def test_backward_compat_indexing(self):
        graph = Graph([double])
        runner = SyncRunner()
        results = runner.map(graph, {"x": [10]}, map_over="x")

        assert results[0]["doubled"] == 20

    def test_summary(self):
        graph = Graph([double])
        runner = SyncRunner()
        results = runner.map(graph, {"x": [1, 2]}, map_over="x")

        s = results.summary()
        assert "2 items" in s
        assert "2 completed" in s

    def test_to_dict(self):
        graph = Graph([double])
        runner = SyncRunner()
        results = runner.map(graph, {"x": [1]}, map_over="x")

        d = results.to_dict()
        assert d["item_count"] == 1
        assert d["completed_count"] == 1
        assert d["map_over"] == ["x"]

    def test_with_failures(self):
        graph = Graph([fail_on_odd])
        runner = SyncRunner()
        results = runner.map(
            graph,
            {"x": [2, 3, 4]},
            map_over="x",
            error_handling="continue",
        )

        assert len(results) == 3
        assert results.failed is True
        assert len(results.failures) == 1
        assert results["result"] == [4, None, 8]

    def test_product_mode(self):
        graph = Graph([double])
        runner = SyncRunner()
        results = runner.map(
            graph,
            {"x": [1, 2]},
            map_over="x",
            map_mode="product",
        )

        assert results.map_mode == "product"
        assert len(results) == 2


@pytest.mark.asyncio
class TestAsyncRunnerMapResult:
    async def test_returns_map_result(self):
        graph = Graph([double])
        runner = AsyncRunner()
        results = await runner.map(graph, {"x": [1, 2]}, map_over="x")

        assert isinstance(results, MapResult)
        assert len(results) == 2
        assert results.run_id is not None
        assert results["doubled"] == [2, 4]

    async def test_empty_returns_map_result(self):
        graph = Graph([double])
        runner = AsyncRunner()
        results = await runner.map(graph, {"x": []}, map_over="x")

        assert isinstance(results, MapResult)
        assert len(results) == 0
        assert results.run_id is None


# === MapLog Unit Tests ===


def _make_run_log(
    graph_name: str = "test",
    steps: tuple[NodeRecord, ...] | None = None,
    duration: float = 1.0,
) -> RunLog:
    if steps is None:
        steps = (
            NodeRecord(
                node_name="a",
                superstep=0,
                duration_ms=0.5,
                status="completed",
                span_id="span-1",
            ),
        )
    return RunLog(
        graph_name=graph_name,
        run_id="run-123",
        total_duration_ms=duration,
        steps=steps,
    )


def _make_failed_run_log() -> RunLog:
    return RunLog(
        graph_name="test",
        run_id="run-456",
        total_duration_ms=2.0,
        steps=(
            NodeRecord(
                node_name="a",
                superstep=0,
                duration_ms=1.0,
                status="completed",
                span_id="span-1",
            ),
            NodeRecord(
                node_name="b",
                superstep=1,
                duration_ms=1.0,
                status="failed",
                span_id="span-2",
                error="ValueError: boom",
            ),
        ),
    )


class TestMapLog:
    def test_map_result_has_log(self):
        """results.log returns a MapLog."""
        log = _make_run_log()
        r = _make_result(log=log)
        mr = _make_map_result([r, r])
        assert isinstance(mr.log, MapLog)

    def test_map_log_summary(self):
        """summary() is a one-liner with item count, completed, duration, errors."""
        log = _make_run_log(duration=1.0)
        failed_log = _make_failed_run_log()
        r_ok = _make_result(log=log)
        r_fail = _make_result(status=RunStatus.FAILED, error=ValueError("x"), log=failed_log)
        mr = _make_map_result([r_ok, r_fail, r_ok], total_duration_ms=4.0)

        s = mr.log.summary()
        assert "3 items" in s
        assert "2 completed" in s
        assert "4ms" in s
        assert "1 errors" in s

    def test_map_log_str_table(self):
        """str() shows per-item rows with indices."""
        log = _make_run_log()
        r = _make_result(log=log)
        mr = _make_map_result([r, r], total_duration_ms=2.0)

        output = str(mr.log)
        assert "MapLog:" in output
        assert "2 items" in output
        assert "completed" in output

    def test_map_log_str_footer(self):
        """str() footer contains [i] guidance."""
        log = _make_run_log()
        r = _make_result(log=log)
        mr = _make_map_result([r])

        output = str(mr.log)
        assert "[i]" in output

    def test_map_log_errors(self):
        """errors aggregates failed NodeRecords across items."""
        log_ok = _make_run_log()
        log_fail = _make_failed_run_log()
        r_ok = _make_result(log=log_ok)
        r_fail = _make_result(status=RunStatus.FAILED, error=ValueError("x"), log=log_fail)
        mr = _make_map_result([r_ok, r_fail])

        errors = mr.log.errors
        assert len(errors) == 1
        assert errors[0].node_name == "b"
        assert errors[0].status == "failed"

    def test_map_log_node_stats(self):
        """node_stats aggregates across all items."""
        log = _make_run_log(
            steps=(
                NodeRecord(node_name="a", superstep=0, duration_ms=1.0, status="completed", span_id="s1"),
                NodeRecord(node_name="b", superstep=1, duration_ms=2.0, status="completed", span_id="s2"),
            ),
            duration=3.0,
        )
        r = _make_result(log=log)
        mr = _make_map_result([r, r, r])

        stats = mr.log.node_stats
        assert isinstance(stats["a"], NodeStats)
        assert stats["a"].count == 3
        assert stats["b"].count == 3
        assert stats["b"].total_ms == 6.0

    def test_map_log_indexing(self):
        """results.log[i] returns RunLog."""
        log1 = _make_run_log(graph_name="g1")
        log2 = _make_run_log(graph_name="g2")
        r1 = _make_result(log=log1)
        r2 = _make_result(log=log2)
        mr = _make_map_result([r1, r2])

        assert isinstance(mr.log[0], RunLog)
        assert mr.log[0].graph_name == "g1"
        assert mr.log[1].graph_name == "g2"

    def test_map_log_len(self):
        """len() matches item count."""
        log = _make_run_log()
        r = _make_result(log=log)
        mr = _make_map_result([r, r, r])

        assert len(mr.log) == 3

    def test_map_log_to_dict(self):
        """to_dict() is JSON serializable."""
        import json

        log = _make_run_log()
        r = _make_result(log=log)
        mr = _make_map_result([r])

        d = mr.log.to_dict()
        assert d["graph_name"] == "test"
        assert len(d["items"]) == 1
        json.dumps(d)  # must not raise

    def test_map_log_repr(self):
        """repr is concise."""
        log = _make_run_log()
        r = _make_result(log=log)
        mr = _make_map_result([r, r])

        r_str = repr(mr.log)
        assert "MapLog" in r_str
        assert "items=2" in r_str

    def test_map_log_empty(self):
        """Empty MapResult produces empty MapLog."""
        mr = _make_map_result([])
        ml = mr.log
        assert len(ml) == 0
        assert ml.errors == ()
        assert ml.summary().startswith("0 items")

    def test_map_log_repr_pretty(self):
        """_repr_pretty_ outputs the table (same as str)."""
        log = _make_run_log()
        r = _make_result(log=log)
        mr = _make_map_result([r])

        class FakePP:
            def __init__(self):
                self.result = ""

            def text(self, s):
                self.result = s

        pp = FakePP()
        mr.log._repr_pretty_(pp, cycle=False)
        assert pp.result == str(mr.log)

        pp2 = FakePP()
        mr.log._repr_pretty_(pp2, cycle=True)
        assert pp2.result == "MapLog(...)"


class TestSyncRunnerMapLog:
    """Integration: SyncRunner.map() → results.log."""

    def test_map_produces_map_log(self):
        graph = Graph([double], name="pipeline")
        runner = SyncRunner()
        results = runner.map(graph, {"x": [1, 2, 3]}, map_over="x")

        ml = results.log
        assert isinstance(ml, MapLog)
        assert len(ml) == 3
        assert ml.graph_name == "pipeline"

    def test_map_log_drill_down(self):
        graph = Graph([double])
        runner = SyncRunner()
        results = runner.map(graph, {"x": [1, 2]}, map_over="x")

        item_log = results.log[0]
        assert isinstance(item_log, RunLog)
        assert len(item_log.steps) >= 1
