"""Attempt-event contract tests (#233, red-green items 1 and 2).

Locked event shape per logical execution of an attempt-managed node:

    1 x NodeStartEvent
    N x NodeAttemptStartEvent
    N x NodeAttemptEndEvent
    1 x (NodeEndEvent | NodeErrorEvent)

Cache hit -> zero attempt events. Intermediate failures never emit
NodeErrorEvent, never bump logical error counts, and never finish progress
rows; only the terminal outcome does.

Repo flaky rule: no wall-clock assertions. Retry delays use tiny
``initial_delay`` with ``jitter="none"``; timeout cases use cooperative
hanging awaits, asserting shape rather than duration.
"""

from __future__ import annotations

import inspect

import pytest

from hypergraph import (
    AsyncRunner,
    AttemptTimeoutError,
    Graph,
    InMemoryCache,
    RetryPolicy,
    SyncRunner,
    node,
)
from hypergraph.events import (
    NodeAttemptEndEvent,
    NodeAttemptStartEvent,
    NodeEndEvent,
    NodeErrorEvent,
    NodeStartEvent,
    RunStartEvent,
)
from hypergraph.events.processor import EventProcessor
from hypergraph.events.rich_progress import RichProgressProcessor


class Recorder(EventProcessor):
    def __init__(self) -> None:
        self.events: list = []

    def on_event(self, event) -> None:
        self.events.append(event)

    def of(self, event_type):
        return [e for e in self.events if type(e) is event_type]


def _policy(**overrides) -> RetryPolicy:
    kwargs = {
        "max_attempts": 3,
        "retry_on": (ConnectionError,),
        "initial_delay": 0.001,
        "jitter": "none",
    }
    kwargs.update(overrides)
    return RetryPolicy(**kwargs)


def _make_runner(family: str, **kwargs):
    return SyncRunner(**kwargs) if family == "sync" else AsyncRunner(**kwargs)


async def _run(runner, *args, **kwargs):
    result = runner.run(*args, **kwargs)
    if inspect.iscoroutine(result):
        result = await result
    return result


@pytest.fixture(params=["sync", "async"])
def family(request) -> str:
    return request.param


# === Red-green item 1: event-count matrix ===


async def test_event_matrix_fail_fail_success(family):
    """(fail, fail, success) -> 1 NodeStart / 3 AttemptStart / 3 AttemptEnd / 1 NodeEnd."""
    calls: list[int] = []

    @node(output_name="fetched", retry=_policy())
    def flaky(x: int) -> int:
        calls.append(x)
        if len(calls) < 3:
            raise ConnectionError("transient")
        return x * 10

    recorder = Recorder()
    result = await _run(_make_runner(family), Graph([flaky]), {"x": 1}, event_processors=[recorder])

    assert result["fetched"] == 10
    assert calls == [1, 1, 1]

    starts = recorder.of(NodeStartEvent)
    attempt_starts = recorder.of(NodeAttemptStartEvent)
    attempt_ends = recorder.of(NodeAttemptEndEvent)
    ends = recorder.of(NodeEndEvent)
    errors = recorder.of(NodeErrorEvent)

    assert (len(starts), len(attempt_starts), len(attempt_ends), len(ends)) == (1, 3, 3, 1)
    assert errors == [], "intermediate failures never emit NodeErrorEvent"

    node_span_id = starts[0].span_id
    series_ids = {e.attempt_series_id for e in attempt_starts + attempt_ends}
    assert len(series_ids) == 1 and "" not in series_ids, "one attempt series per logical execution"

    assert [e.attempt_number for e in attempt_starts] == [1, 2, 3], "attempt_number is one-based"
    assert all(e.max_attempts == 3 for e in attempt_starts)
    assert all(e.parent_span_id == node_span_id for e in attempt_starts + attempt_ends), "attempt events hang off the single logical node span"

    assert [e.attempt_number for e in attempt_ends] == [1, 2, 3]
    assert [e.outcome for e in attempt_ends] == ["failed", "failed", "succeeded"]
    assert [e.settlement for e in attempt_ends] == ["raised", "raised", "returned"]
    assert [e.retry_scheduled for e in attempt_ends] == [True, True, False]
    assert [e.error_type for e in attempt_ends] == ["ConnectionError", "ConnectionError", None]
    for e in attempt_ends[:2]:
        assert e.retry_not_before is not None, "a granted retry carries its absolute wake time"
    assert attempt_ends[2].retry_not_before is None
    for e in attempt_ends:
        assert e.deadline_scope is None
        assert e.deadline_elapsed is False
        assert e.cancellation_requested is False


async def test_event_matrix_terminal_failure(family):
    """Exhaustion: 1 NodeStart / 2 AttemptStart / 2 AttemptEnd / 1 NodeError, 0 NodeEnd."""

    @node(output_name="fetched", retry=_policy(max_attempts=2))
    def doomed(x: int) -> int:
        raise ConnectionError("always")

    recorder = Recorder()
    result = await _run(
        _make_runner(family),
        Graph([doomed]),
        {"x": 1},
        event_processors=[recorder],
        error_handling="continue",
    )

    assert result.failed
    counts = (
        len(recorder.of(NodeStartEvent)),
        len(recorder.of(NodeAttemptStartEvent)),
        len(recorder.of(NodeAttemptEndEvent)),
        len(recorder.of(NodeEndEvent)),
        len(recorder.of(NodeErrorEvent)),
    )
    assert counts == (1, 2, 2, 0, 1)

    attempt_ends = recorder.of(NodeAttemptEndEvent)
    assert [e.retry_scheduled for e in attempt_ends] == [True, False]
    assert attempt_ends[-1].outcome == "failed"

    # Logical error counts are per logical step, never per attempt.
    assert result.log is not None
    assert len(result.log.errors) == 1
    assert result.log.node_stats["doomed"].errors == 1
    assert result.log.node_stats["doomed"].count == 1


async def test_cache_hit_emits_zero_attempt_events(family, tmp_path):
    """Cache hit -> 1 NodeStart / 0 AttemptStart / 0 AttemptEnd / 1 NodeEnd."""
    calls: list[int] = []

    @node(output_name="fetched", cache=True, retry=_policy())
    def flaky(x: int) -> int:
        calls.append(x)
        return x * 10

    graph = Graph([flaky])
    cache = InMemoryCache()
    runner = _make_runner(family, cache=cache)

    warm = Recorder()
    await _run(runner, graph, {"x": 1}, event_processors=[warm])
    assert len(warm.of(NodeAttemptStartEvent)) == 1, "the warming run consumes one real attempt"

    recorder = Recorder()
    result = await _run(runner, graph, {"x": 1}, event_processors=[recorder])

    assert result["fetched"] == 10
    assert calls == [1], "cache hit invoked nothing"
    counts = (
        len(recorder.of(NodeStartEvent)),
        len(recorder.of(NodeAttemptStartEvent)),
        len(recorder.of(NodeAttemptEndEvent)),
        len(recorder.of(NodeEndEvent)),
    )
    assert counts == (1, 0, 0, 1)
    assert recorder.of(NodeEndEvent)[0].cached is True


async def test_plain_node_without_policy_emits_no_attempt_events(family):
    """Attempt events exist only where the attempt machinery is engaged."""

    @node(output_name="doubled")
    def double(x: int) -> int:
        return x * 2

    recorder = Recorder()
    await _run(_make_runner(family), Graph([double]), {"x": 2}, event_processors=[recorder])

    assert recorder.of(NodeAttemptStartEvent) == []
    assert recorder.of(NodeAttemptEndEvent) == []


async def test_timed_out_attempt_end_carries_deadline_evidence():
    """A timed-out attempt distinguishes deadline, cancellation request, and settlement."""
    import asyncio

    @node(output_name="value", timeout=0.05)
    async def hang(x: int) -> int:
        await asyncio.Event().wait()
        return x

    recorder = Recorder()
    result = await AsyncRunner().run(
        Graph([hang]),
        {"x": 1},
        event_processors=[recorder],
        error_handling="continue",
    )

    assert result.failed
    assert isinstance(result.error, AttemptTimeoutError)

    attempt_ends = recorder.of(NodeAttemptEndEvent)
    assert len(attempt_ends) == 1
    end = attempt_ends[0]
    assert end.outcome == "timed_out"
    assert end.settlement == "cancelled"
    assert end.deadline_scope == "attempt"
    assert end.deadline_elapsed is True
    assert end.cancellation_requested is True
    assert end.retry_scheduled is False
    assert end.error_type == "hypergraph.exceptions.AttemptTimeoutError"
    assert len(recorder.of(NodeErrorEvent)) == 1

    starts = recorder.of(NodeAttemptStartEvent)
    assert len(starts) == 1
    assert starts[0].timeout_seconds == 0.05
    assert starts[0].attempt_deadline_at is not None


# === Red-green item 2: progress rows ===


def _feed(processor: RichProgressProcessor, events) -> None:
    for event in events:
        processor.on_event(event)


def _node_bar(processor: RichProgressProcessor):
    bars = processor._tracker.node_bars
    assert len(bars) == 1
    return next(iter(bars.values()))


def test_progress_row_stays_running_across_intermediate_failures():
    """An intermediate attempt failure must not finish or fail the node row."""
    processor = RichProgressProcessor(force_mode="non-tty")
    common = {"run_id": "r-1", "workflow_id": None, "item_index": None}
    _feed(
        processor,
        [
            RunStartEvent(span_id="run", graph_name="g", **common),
            NodeStartEvent(span_id="n1", parent_span_id="run", node_name="flaky", graph_name="g", **common),
            NodeAttemptStartEvent(
                span_id="a1",
                parent_span_id="n1",
                node_name="flaky",
                graph_name="g",
                attempt_series_id="series-x",
                attempt_number=1,
                max_attempts=3,
                **common,
            ),
            NodeAttemptEndEvent(
                span_id="a1e",
                parent_span_id="n1",
                node_name="flaky",
                graph_name="g",
                attempt_series_id="series-x",
                attempt_number=1,
                outcome="failed",
                settlement="raised",
                error_type="ConnectionError",
                retry_scheduled=True,
                **common,
            ),
        ],
    )

    bar = _node_bar(processor)
    assert bar.completed == 0, "the logical row is still running between attempts"
    assert bar.failures == 0, "intermediate failures never bump the logical failure count"

    # Terminal success finishes the row exactly once.
    _feed(
        processor,
        [
            NodeAttemptStartEvent(
                span_id="a2",
                parent_span_id="n1",
                node_name="flaky",
                graph_name="g",
                attempt_series_id="series-x",
                attempt_number=2,
                max_attempts=3,
                **common,
            ),
            NodeAttemptEndEvent(
                span_id="a2e",
                parent_span_id="n1",
                node_name="flaky",
                graph_name="g",
                attempt_series_id="series-x",
                attempt_number=2,
                outcome="succeeded",
                settlement="returned",
                **common,
            ),
            NodeEndEvent(span_id="n1", parent_span_id="run", node_name="flaky", graph_name="g", duration_ms=5.0, **common),
        ],
    )
    bar = _node_bar(processor)
    assert bar.completed == 1
    assert bar.failures == 0
    assert bar.succeeded == 1


def test_progress_row_finishes_only_on_terminal_error():
    processor = RichProgressProcessor(force_mode="non-tty")
    common = {"run_id": "r-1", "workflow_id": None, "item_index": None}
    _feed(
        processor,
        [
            RunStartEvent(span_id="run", graph_name="g", **common),
            NodeStartEvent(span_id="n1", parent_span_id="run", node_name="doomed", graph_name="g", **common),
            NodeAttemptEndEvent(
                span_id="a1e",
                parent_span_id="n1",
                node_name="doomed",
                graph_name="g",
                attempt_series_id="series-x",
                attempt_number=1,
                outcome="failed",
                settlement="raised",
                retry_scheduled=True,
                **common,
            ),
        ],
    )
    bar = _node_bar(processor)
    assert (bar.completed, bar.failures) == (0, 0)

    _feed(
        processor,
        [
            NodeErrorEvent(span_id="n1", parent_span_id="run", node_name="doomed", graph_name="g", error="x", error_type="ConnectionError", **common),
        ],
    )
    bar = _node_bar(processor)
    assert (bar.completed, bar.failures) == (1, 1), "only the terminal outcome finishes the row"
