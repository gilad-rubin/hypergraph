"""The console: deterministic liveness assertions over synthetic events.

Ports the proven prototype's liveness proof into the suite: monotone
done-counts across snapshots, retry and cache facts reaching the payload,
a bounded settled frame, ONE display handle, and the show_progress
notebook selection — all over synthetic events, no sleeps, no network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from hypergraph.events.console import ConsoleProcessor, LiveConsole, render_console
from hypergraph.events.rich_progress import RichProgressProcessor
from hypergraph.events.types import (
    InnerCacheEvent,
    NodeAttemptEndEvent,
    NodeEndEvent,
    NodeErrorEvent,
    NodeStartEvent,
    RunEndEvent,
    RunStartEvent,
    RunStatus,
)
from hypergraph.runners._shared.scheduling import ensure_progress_processor

# ---------------------------------------------------------------------------
# synthetic transcripts
# ---------------------------------------------------------------------------


def _map_events(
    items: int = 6,
    *,
    fail: set[int] = frozenset(),
    retry_on: set[int] = frozenset(),
    cached_on: set[int] = frozenset(),
    inner_hit_on: set[int] = frozenset(),
) -> list[list[Any]]:
    """One root map of ``items`` runs, grouped per item so tests can sample.

    The first group holds the root RunStart; each following group is one
    item's full life (start, node, end).
    """
    groups: list[list[Any]] = [
        [
            RunStartEvent(
                run_id="root",
                span_id="root",
                graph_name="pipeline",
                is_map=True,
                map_size=items,
            )
        ]
    ]
    for index in range(items):
        item_span = f"item-{index}"
        node_span = f"node-{index}"
        group: list[Any] = [
            RunStartEvent(
                run_id=f"run-{index}",
                span_id=item_span,
                parent_span_id="root",
                graph_name="pipeline",
                item_index=index,
            ),
            NodeStartEvent(
                run_id=f"run-{index}",
                span_id=node_span,
                parent_span_id=item_span,
                node_name="work",
                graph_name="pipeline",
            ),
        ]
        if index in retry_on:
            group.append(
                NodeAttemptEndEvent(
                    run_id=f"run-{index}",
                    span_id=f"attempt-{index}",
                    parent_span_id=node_span,
                    node_name="work",
                    graph_name="pipeline",
                    attempt_number=1,
                    outcome="failed",
                    retry_scheduled=True,
                )
            )
        if index in inner_hit_on:
            group.append(
                InnerCacheEvent(
                    run_id=f"run-{index}",
                    span_id=f"cache-{index}",
                    parent_span_id=node_span,
                    node_name="work",
                    graph_name="pipeline",
                    hit=True,
                )
            )
        if index in fail:
            group.append(
                NodeErrorEvent(
                    run_id=f"run-{index}",
                    span_id=node_span,
                    parent_span_id=item_span,
                    node_name="work",
                    graph_name="pipeline",
                    error="boom",
                    error_type="ValueError",
                )
            )
            status = RunStatus.FAILED
        else:
            group.append(
                NodeEndEvent(
                    run_id=f"run-{index}",
                    span_id=node_span,
                    parent_span_id=item_span,
                    node_name="work",
                    graph_name="pipeline",
                    duration_ms=12.0,
                    cached=index in cached_on,
                )
            )
            status = RunStatus.COMPLETED
        group.append(
            RunEndEvent(
                run_id=f"run-{index}",
                span_id=item_span,
                parent_span_id="root",
                graph_name="pipeline",
                status=status,
                duration_ms=15.0,
            )
        )
        groups.append(group)
    groups.append(
        [
            RunEndEvent(
                run_id="root",
                span_id="root",
                graph_name="pipeline",
                status=RunStatus.PARTIAL if fail else RunStatus.COMPLETED,
            )
        ]
    )
    return groups


def _nested_map_events() -> list[Any]:
    """A plain run whose node fans out into a nested 3-item map."""
    events: list[Any] = [
        RunStartEvent(run_id="r", span_id="root", graph_name="outer"),
        NodeStartEvent(run_id="r", span_id="fan-node", parent_span_id="root", node_name="fanout", graph_name="outer"),
        RunStartEvent(
            run_id="r",
            span_id="inner-map",
            parent_span_id="fan-node",
            graph_name="inner",
            is_map=True,
            map_size=3,
        ),
    ]
    for index in range(3):
        child_span = f"child-{index}"
        node_span = f"child-node-{index}"
        events.extend(
            [
                RunStartEvent(
                    run_id="r",
                    span_id=child_span,
                    parent_span_id="inner-map",
                    graph_name="inner",
                    item_index=index,
                ),
                NodeStartEvent(
                    run_id="r",
                    span_id=node_span,
                    parent_span_id=child_span,
                    node_name="derive",
                    graph_name="inner",
                ),
                NodeEndEvent(
                    run_id="r",
                    span_id=node_span,
                    parent_span_id=child_span,
                    node_name="derive",
                    graph_name="inner",
                    duration_ms=5.0,
                ),
                RunEndEvent(
                    run_id="r",
                    span_id=child_span,
                    parent_span_id="inner-map",
                    graph_name="inner",
                    status=RunStatus.COMPLETED,
                ),
            ]
        )
    events.extend(
        [
            RunEndEvent(run_id="r", span_id="inner-map", parent_span_id="fan-node", graph_name="inner"),
            NodeEndEvent(run_id="r", span_id="fan-node", parent_span_id="root", node_name="fanout", graph_name="outer", duration_ms=30.0),
            RunEndEvent(run_id="r", span_id="root", graph_name="outer", status=RunStatus.COMPLETED),
        ]
    )
    return events


def _feed(processor: ConsoleProcessor, events: list[Any]) -> None:
    for event in events:
        processor.on_event(event)


# ---------------------------------------------------------------------------
# fold: cumulative truth
# ---------------------------------------------------------------------------


def test_done_count_is_monotone_and_queued_never_grows() -> None:
    console = ConsoleProcessor()
    snapshots = []
    for group in _map_events(items=6, fail={2}):
        _feed(console, group)
        payload = console.payload()
        snapshots.append((payload["done"], payload["queued"], payload["failed"]))
    dones = [done for done, _, _ in snapshots]
    queues = [queued for _, queued, _ in snapshots]
    assert len(snapshots) >= 5
    assert all(later >= earlier for earlier, later in zip(dones, dones[1:], strict=False))
    assert dones[-1] == 5
    assert all(later <= earlier for earlier, later in zip(queues, queues[1:], strict=False))
    final = console.payload()
    assert final["terminal"] and final["failed"] == 1 and final["total"] == 6


def test_retry_cache_and_failure_facts_reach_the_payload() -> None:
    console = ConsoleProcessor()
    for group in _map_events(items=5, fail={4}, retry_on={1, 3}, cached_on={2}, inner_hit_on={0}):
        _feed(console, group)
    payload = console.payload()
    assert payload["retries"] == 2
    assert payload["cache_hits"] == 2  # one node-level, one inner call
    assert payload["failed"] == 1
    assert payload["failures"] and payload["failures"][0]["step"] == "work"
    assert payload["failures"][0]["item"] == "item #4"
    step = payload["tree"]["children"][0]
    assert step["retries"] == 2 and step["errors"] == 1
    assert step["cached_units"] == 1 and step["cached_calls"] == 1


def test_nested_map_is_first_class_fan_out() -> None:
    console = ConsoleProcessor()
    _feed(console, _nested_map_events())
    payload = console.payload()
    fan = payload["tree"]["children"][0]
    assert fan["name"] == "fanout" and fan["is_map"]
    assert fan["child_expected"] == 3 and fan["child_done"] == 3
    derive = fan["children"][0]
    assert derive["name"] == "derive" and derive["count"] == 3


def test_item_labels_name_the_units_per_level() -> None:
    console = ConsoleProcessor(item_labels=("orders", "line items"))
    _feed(console, _nested_map_events())
    payload = console.payload()
    assert payload["unit"] == "orders"
    fan = payload["tree"]["children"][0]
    assert fan["fan_unit"] == "line items"
    html = render_console(payload)
    assert "orders" in html and "line items" in html


def test_a_new_root_run_resets_the_fold() -> None:
    console = ConsoleProcessor()
    for group in _map_events(items=3):
        _feed(console, group)
    assert console.payload()["total"] == 3
    _feed(console, _nested_map_events())
    payload = console.payload()
    assert payload["mode"] == "run" and payload["total"] == 1
    assert payload["title"] == "outer"


# ---------------------------------------------------------------------------
# rendering: bounded, settled, honest
# ---------------------------------------------------------------------------


def test_settled_frame_is_bounded_and_carries_the_header_line() -> None:
    console = ConsoleProcessor()
    for group in _map_events(items=40, fail={7}, retry_on={3}, cached_on={5}):
        _feed(console, group)
    html = render_console(console.payload())
    assert "Settled" in html
    assert "retries" in html and "cached" in html and "failed" in html
    assert "\x00" not in html
    assert len(html.encode()) <= 50_000, f"settled frame too heavy: {len(html.encode()):,} bytes"


def test_lanes_render_only_when_the_capacity_hook_is_provided() -> None:
    lanes = [{"name": "chat", "busy": 2, "cap": 8, "waiting": 1, "paused_seconds": 0.0}]
    console = ConsoleProcessor(capacity=lambda: lanes)
    for group in _map_events(items=2):
        _feed(console, group)
    html = render_console(console.payload())
    assert "Work lanes" in html and "chat" in html

    bare = ConsoleProcessor()
    for group in _map_events(items=2):
        _feed(bare, group)
    assert "Work lanes" not in render_console(bare.payload())


# ---------------------------------------------------------------------------
# live display: one handle, settled at shutdown
# ---------------------------------------------------------------------------


def test_live_console_claims_one_handle_and_settles_it_at_shutdown() -> None:
    handle = MagicMock()
    with patch("IPython.display.display", return_value=handle) as display:
        console = LiveConsole()
        for group in _map_events(items=4, fail={1}):
            _feed(console, group)
        console.shutdown()

    assert display.call_count == 1  # ONE display handle, ever
    assert display.call_args.kwargs["display_id"] == console.uid
    assert handle.update.called
    settled = handle.update.call_args.args[0].data
    assert "Settled" in settled and "3/4" in settled


def test_live_console_without_a_loop_still_renders_throttled_frames() -> None:
    clock = {"now": 0.0}
    handle = MagicMock()
    with patch("IPython.display.display", return_value=handle):
        console = LiveConsole(clock=lambda: clock["now"])
        for group in _map_events(items=5):
            _feed(console, group)
            clock["now"] += 1.0  # every group lands past the refresh window
        console.shutdown()
    # First frame on the handle plus in-flight refreshes plus the settled one.
    assert console.frames >= 3
    assert handle.update.call_count >= 2


# ---------------------------------------------------------------------------
# selection: show_progress picks the console in notebooks, bars elsewhere
# ---------------------------------------------------------------------------


def test_notebook_mode_selects_the_live_console() -> None:
    with patch("hypergraph.events.rich_progress._detect_mode", return_value="notebook"):
        processors = ensure_progress_processor(None)
    assert len(processors) == 1 and isinstance(processors[0], LiveConsole)


def test_tty_mode_keeps_the_bars() -> None:
    with patch("hypergraph.events.rich_progress._detect_mode", return_value="tty"):
        processors = ensure_progress_processor(None)
    assert len(processors) == 1 and isinstance(processors[0], RichProgressProcessor)


def test_an_explicit_rich_processor_is_the_notebook_escape_hatch() -> None:
    bars = RichProgressProcessor(force_mode="non-tty")
    with patch("hypergraph.events.rich_progress._detect_mode", return_value="notebook"):
        processors = ensure_progress_processor([bars])
    assert processors == [bars]


def test_an_explicit_console_suppresses_the_synthesized_default() -> None:
    console = ConsoleProcessor()
    with patch("hypergraph.events.rich_progress._detect_mode", return_value="tty"):
        processors = ensure_progress_processor([console])
    assert processors == [console]


def test_a_graph_carried_console_suppresses_the_default_too() -> None:
    console = ConsoleProcessor()
    with patch("hypergraph.events.rich_progress._detect_mode", return_value="notebook"):
        processors = ensure_progress_processor(None, carried=(console,))
    assert processors == []
