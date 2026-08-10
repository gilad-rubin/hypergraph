"""The console: deterministic liveness assertions over synthetic events.

Ports the proven prototype's liveness proof into the suite: monotone
done-counts across snapshots, retry and cache facts reaching the payload,
a bounded settled frame, ONE display handle, and the show_progress
notebook selection — all over synthetic events, no sleeps, no network.
"""

from __future__ import annotations

import re
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


# ---------------------------------------------------------------------------
# upcoming nodes: the shape of the work is visible before it starts
# ---------------------------------------------------------------------------


def _planned(*entries: tuple[str, bool]) -> tuple:
    from hypergraph.events.types import PlannedNode

    return tuple(PlannedNode(name=name, certain=certain) for name, certain in entries)


def test_the_plan_draws_upcoming_nodes_before_anything_starts() -> None:
    console = ConsoleProcessor()
    console.on_event(
        RunStartEvent(
            run_id="r",
            span_id="root",
            graph_name="pipeline",
            is_map=True,
            map_size=3,
            plan=_planned(("fetch", True), ("enrich", True), ("archive", False)),
        )
    )
    rows = {row["name"]: row["state"] for row in console.payload()["tree"]["children"]}
    # Nothing has run, yet the whole graph is already drawn — in plan order.
    assert [row["name"] for row in console.payload()["tree"]["children"]] == ["fetch", "enrich", "archive"]
    assert rows == {"fetch": "upcoming", "enrich": "upcoming", "archive": "possible"}
    html = render_console(console.payload())
    assert "queued</span>" in html and "may run</span>" in html


def test_a_started_node_stops_being_upcoming() -> None:
    console = ConsoleProcessor()
    console.on_event(
        RunStartEvent(run_id="r", span_id="root", graph_name="p", is_map=True, map_size=1, plan=_planned(("work", True), ("after", True)))
    )
    console.on_event(RunStartEvent(run_id="r", span_id="item", parent_span_id="root", graph_name="p", item_index=0))
    console.on_event(NodeStartEvent(run_id="r", span_id="n", parent_span_id="item", node_name="work", graph_name="p"))
    states = {row["name"]: row["state"] for row in console.payload()["tree"]["children"]}
    assert states == {"work": "running", "after": "upcoming"}
    console.on_event(NodeEndEvent(run_id="r", span_id="n", parent_span_id="item", node_name="work", graph_name="p", duration_ms=4.0))
    assert {r["name"]: r["state"] for r in console.payload()["tree"]["children"]}["work"] == "done"


def test_a_gated_node_that_never_ran_stays_possible_not_failed() -> None:
    """The branch not taken is not an omission and not a failure."""
    console = ConsoleProcessor()
    console.on_event(
        RunStartEvent(run_id="r", span_id="root", graph_name="p", is_map=True, map_size=1, plan=_planned(("taken", False), ("not_taken", False)))
    )
    console.on_event(RunStartEvent(run_id="r", span_id="item", parent_span_id="root", graph_name="p", item_index=0))
    console.on_event(NodeStartEvent(run_id="r", span_id="n", parent_span_id="item", node_name="taken", graph_name="p"))
    console.on_event(NodeEndEvent(run_id="r", span_id="n", parent_span_id="item", node_name="taken", graph_name="p", duration_ms=2.0))
    console.on_event(RunEndEvent(run_id="r", span_id="item", parent_span_id="root", graph_name="p", status=RunStatus.COMPLETED))
    console.on_event(RunEndEvent(run_id="r", span_id="root", graph_name="p", status=RunStatus.COMPLETED))
    states = {row["name"]: row["state"] for row in console.payload()["tree"]["children"]}
    assert states == {"taken": "done", "not_taken": "possible"}
    assert console.payload()["failed"] == 0


def test_a_producer_without_a_plan_still_works() -> None:
    """Every plan field is optional — an old producer loses nothing."""
    console = ConsoleProcessor()
    for group in _map_events(items=2):
        _feed(console, group)
    payload = console.payload()
    assert [row["name"] for row in payload["tree"]["children"]] == ["work"]
    assert payload["tree"]["children"][0]["state"] == "done"


# ---------------------------------------------------------------------------
# the reader's collapse survives the refresh, and the theme is the repo's
# ---------------------------------------------------------------------------


def _row_ids(frame: str) -> set[str]:
    return set(re.findall(r'id="hgc[0-9a-f]+-[sc]-([a-z0-9]+)"', frame))


def test_every_frame_carries_the_state_script_with_a_stable_key() -> None:
    console = ConsoleProcessor()
    frames = []
    for group in _map_events(items=4):
        _feed(console, group)
        frames.append(render_console(console.payload()))
    assert all("data-hg-console-restored" in frame for frame in frames)
    # A row id IS the reader's collapse, addressed. Rows may be ADDED as work
    # is discovered, but an existing id must never move — restoring onto a
    # renumbered row would apply the reader's choice to the wrong step.
    ids = [_row_ids(frame) for frame in frames]
    assert ids[0] and all(earlier <= later for earlier, later in zip(ids, ids[1:], strict=False))
    keys = set(re.findall(r"K='([^']+)'", "".join(frames)))
    assert len(keys) == 1, f"the storage key moved between frames: {keys}"
    assert "hypergraph:console:" in frames[0]


def test_a_planned_run_has_every_row_id_from_the_very_first_frame() -> None:
    """With a plan, not even an ADDED row disturbs the reader's collapse."""
    console = ConsoleProcessor()
    plan = _planned(("fetch", True), ("enrich", True), ("publish", True))
    console.on_event(RunStartEvent(run_id="r", span_id="root", graph_name="p", is_map=True, map_size=2, plan=plan))
    frames = [render_console(console.payload())]
    for index in range(2):
        console.on_event(RunStartEvent(run_id="r", span_id=f"i{index}", parent_span_id="root", graph_name="p", item_index=index))
        for name in ("fetch", "enrich", "publish"):
            span = f"n{index}{name}"
            console.on_event(NodeStartEvent(run_id="r", span_id=span, parent_span_id=f"i{index}", node_name=name, graph_name="p"))
            console.on_event(NodeEndEvent(run_id="r", span_id=span, parent_span_id=f"i{index}", node_name=name, graph_name="p", duration_ms=3.0))
        console.on_event(RunEndEvent(run_id="r", span_id=f"i{index}", parent_span_id="root", graph_name="p", status=RunStatus.COMPLETED))
        frames.append(render_console(console.payload()))
    assert all(_row_ids(frame) == _row_ids(frames[0]) for frame in frames)


def test_the_frame_uses_the_repo_theme_mechanism_and_never_hardcodes_light() -> None:
    console = ConsoleProcessor()
    for group in _map_events(items=2):
        _feed(console, group)
    html = render_console(console.payload())
    # hypergraph's own detector wraps every widget; the console must use it
    # rather than inventing a second one.
    assert "color-scheme:light dark" in html
    assert "data-jp-theme-light" in html or "jpThemeLight" in html
    assert "data-vscode-theme-kind" in html
    assert "color-scheme:light;" not in html
    # Colors resolve per theme through CSS light-dark(), not a fixed palette.
    assert html.count("light-dark(") >= 20
    assert not re.search(r"--ink:#", html)


def test_the_settled_frame_stays_within_budget_with_plan_theme_and_script() -> None:
    console = ConsoleProcessor()
    for group in _map_events(items=40, fail={7}, retry_on={3}, cached_on={5}):
        _feed(console, group)
    html = render_console(console.payload())
    assert len(html.encode()) <= 50_000, f"settled frame too heavy: {len(html.encode()):,} bytes"
