"""Tests for MapResult and RunResult enhancements."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from hypergraph import Graph, MapResult, RunResult, RunStatus, node
from hypergraph.runners import AsyncRunner, SyncRunner
from hypergraph.runners._shared.types import RunLog

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
