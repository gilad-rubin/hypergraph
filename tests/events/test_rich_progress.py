"""Unit tests for RichProgressProcessor with mocked Rich Progress."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hypergraph.events.rich_progress import RichProgressProcessor
from hypergraph.events.types import (
    NodeEndEvent,
    NodeErrorEvent,
    NodeStartEvent,
    RunEndEvent,
    RunStartEvent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_processor() -> tuple[RichProgressProcessor, MagicMock]:
    """Create a RichProgressProcessor with a mocked Progress object."""
    proc = RichProgressProcessor(transient=True)
    mock_progress = MagicMock()
    # Mock tasks list for description updates
    mock_progress.tasks = {}
    # add_task returns incrementing task IDs
    task_counter = [0]

    def _add_task(desc, total=1):
        tid = task_counter[0]
        task_counter[0] += 1
        mock_task = MagicMock()
        mock_task.description = desc
        mock_progress.tasks[tid] = mock_task
        return tid

    mock_progress.add_task.side_effect = _add_task
    proc._progress = mock_progress
    proc._started = True
    return proc, mock_progress


def _run_start(
    run_id: str = "r1",
    span_id: str = "s1",
    parent_span_id: str | None = None,
    graph_name: str = "g",
    is_map: bool = False,
    map_size: int | None = None,
) -> RunStartEvent:
    return RunStartEvent(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        graph_name=graph_name,
        is_map=is_map,
        map_size=map_size,
    )


def _run_end(
    run_id: str = "r1",
    span_id: str = "s1",
    parent_span_id: str | None = None,
    graph_name: str = "g",
    status: str = "completed",
) -> RunEndEvent:
    return RunEndEvent(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        graph_name=graph_name,
        status=status,
    )


def _node_start(
    run_id: str = "r1",
    span_id: str = "ns1",
    parent_span_id: str = "s1",
    node_name: str = "nodeA",
    graph_name: str = "g",
) -> NodeStartEvent:
    return NodeStartEvent(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        node_name=node_name,
        graph_name=graph_name,
    )


def _node_end(
    run_id: str = "r1",
    span_id: str = "ns1",
    parent_span_id: str = "s1",
    node_name: str = "nodeA",
    graph_name: str = "g",
) -> NodeEndEvent:
    return NodeEndEvent(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        node_name=node_name,
        graph_name=graph_name,
    )


# ---------------------------------------------------------------------------
# Scenario 1: Single run
# ---------------------------------------------------------------------------


class TestSingleRun:
    def test_creates_node_bars(self):
        proc, mock = _make_processor()
        proc.on_run_start(_run_start())
        proc.on_node_start(_node_start(node_name="load"))
        proc.on_node_start(_node_start(span_id="ns2", node_name="transform"))

        assert mock.add_task.call_count == 2
        descs = [call.args[0] for call in mock.add_task.call_args_list]
        assert any("load" in d for d in descs)
        assert any("transform" in d for d in descs)

    def test_advances_on_node_end(self):
        proc, mock = _make_processor()
        proc.on_run_start(_run_start())
        proc.on_node_start(_node_start(node_name="load"))
        proc.on_node_end(_node_end(node_name="load"))

        mock.advance.assert_called_once_with(0, 1)

    def test_total_is_1_for_single_run(self):
        proc, mock = _make_processor()
        proc.on_run_start(_run_start())
        proc.on_node_start(_node_start(node_name="load"))

        mock.add_task.assert_called_once()
        assert mock.add_task.call_args.kwargs.get("total", mock.add_task.call_args[1].get("total")) == 1

    def test_completion_message_on_root_run_end(self):
        proc, mock = _make_processor()
        proc.on_run_start(_run_start())
        proc.on_run_end(_run_end(graph_name="my_graph"))

        mock.console.print.assert_called_once()
        msg = mock.console.print.call_args[0][0]
        assert "my_graph" in msg
        assert "completed" in msg

    def test_icon_is_box_at_depth_0(self):
        proc, mock = _make_processor()
        proc.on_run_start(_run_start())
        proc.on_node_start(_node_start(node_name="load"))

        desc = mock.add_task.call_args[0][0]
        assert "📦" in desc


# ---------------------------------------------------------------------------
# Scenario 2: Map operation
# ---------------------------------------------------------------------------


class TestMapOperation:
    def test_creates_map_bar(self):
        proc, mock = _make_processor()
        proc.on_run_start(_run_start(is_map=True, map_size=3))

        assert mock.add_task.call_count == 1
        desc = mock.add_task.call_args[0][0]
        assert "🗺️" in desc

    def test_node_bars_have_map_total(self):
        proc, mock = _make_processor()
        # Map run
        proc.on_run_start(_run_start(span_id="map1", is_map=True, map_size=5))
        # Item run (child of map)
        proc.on_run_start(_run_start(run_id="r2", span_id="item1", parent_span_id="map1"))
        # Node in item
        proc.on_node_start(_node_start(run_id="r2", span_id="ns1", parent_span_id="item1", node_name="load"))

        # First call is map bar, second is node bar
        node_call = mock.add_task.call_args_list[1]
        assert node_call.kwargs.get("total", node_call[1].get("total")) == 5

    def test_map_bar_advances_on_item_run_end(self):
        proc, mock = _make_processor()
        proc.on_run_start(_run_start(span_id="map1", is_map=True, map_size=2))
        proc.on_run_start(_run_start(run_id="r2", span_id="item1", parent_span_id="map1"))
        proc.on_run_end(_run_end(run_id="r2", span_id="item1", parent_span_id="map1"))

        # advance called on map task (task_id=0)
        mock.advance.assert_called_with(0, 1)

    def test_node_bars_reused_across_items(self):
        proc, mock = _make_processor()
        proc.on_run_start(_run_start(span_id="map1", is_map=True, map_size=2))

        # Item 1
        proc.on_run_start(_run_start(run_id="r2", span_id="item1", parent_span_id="map1"))
        proc.on_node_start(_node_start(run_id="r2", span_id="ns1", parent_span_id="item1", node_name="load"))
        proc.on_node_end(_node_end(run_id="r2", span_id="ns1", parent_span_id="item1", node_name="load"))

        # Item 2
        proc.on_run_start(_run_start(run_id="r3", span_id="item2", parent_span_id="map1"))
        proc.on_node_start(_node_start(run_id="r3", span_id="ns2", parent_span_id="item2", node_name="load"))
        proc.on_node_end(_node_end(run_id="r3", span_id="ns2", parent_span_id="item2", node_name="load"))

        # add_task: 1 for map bar + 1 for "load" node (reused)
        assert mock.add_task.call_count == 2
        # advance called twice for node bar
        assert mock.advance.call_count == 2


# ---------------------------------------------------------------------------
# Scenario 3: Nested graph
# ---------------------------------------------------------------------------


class TestNestedGraph:
    def test_inner_nodes_have_tree_icon(self):
        proc, mock = _make_processor()
        # Outer run
        proc.on_run_start(_run_start(span_id="run1"))
        # Outer node (graph node)
        proc.on_node_start(_node_start(span_id="outer_n", parent_span_id="run1", node_name="outer"))
        # Inner run (child of the outer node)
        proc.on_run_start(_run_start(run_id="r2", span_id="inner_run", parent_span_id="outer_n", graph_name="inner"))
        # Inner node
        proc.on_node_start(_node_start(run_id="r2", span_id="inner_n", parent_span_id="inner_run", node_name="step1", graph_name="inner"))

        # Last add_task call should have tree icon and indentation
        inner_desc = mock.add_task.call_args_list[-1][0][0]
        assert "🌳" in inner_desc
        assert inner_desc.startswith("  ")  # indented

    def test_outer_node_at_depth_0(self):
        proc, mock = _make_processor()
        proc.on_run_start(_run_start(span_id="run1"))
        proc.on_node_start(_node_start(span_id="outer_n", parent_span_id="run1", node_name="outer"))

        desc = mock.add_task.call_args[0][0]
        assert "📦" in desc
        assert not desc.startswith("  ")


# ---------------------------------------------------------------------------
# Scenario: Node error
# ---------------------------------------------------------------------------


class TestMapFailures:
    def test_failed_items_shown_in_map_bar(self):
        proc, mock = _make_processor()
        proc.on_run_start(_run_start(span_id="map1", is_map=True, map_size=3))

        # Item 1 succeeds
        proc.on_run_start(_run_start(run_id="r2", span_id="item1", parent_span_id="map1"))
        proc.on_run_end(_run_end(run_id="r2", span_id="item1", parent_span_id="map1", status="completed"))

        # Item 2 fails
        proc.on_run_start(_run_start(run_id="r3", span_id="item2", parent_span_id="map1"))
        proc.on_run_end(_run_end(run_id="r3", span_id="item2", parent_span_id="map1", status="failed"))

        # Map bar should show failure count
        update_calls = [c for c in mock.update.call_args_list if "description" in c.kwargs]
        assert len(update_calls) == 1
        desc = update_calls[0].kwargs["description"]
        assert "1 failed" in desc
        assert "red" in desc.lower()

    def test_multiple_failures_accumulate(self):
        proc, mock = _make_processor()
        proc.on_run_start(_run_start(span_id="map1", is_map=True, map_size=3))

        # Two items fail
        for i, sid in enumerate(["item1", "item2"]):
            proc.on_run_start(_run_start(run_id=f"r{i + 2}", span_id=sid, parent_span_id="map1"))
            proc.on_run_end(_run_end(run_id=f"r{i + 2}", span_id=sid, parent_span_id="map1", status="failed"))

        update_calls = [c for c in mock.update.call_args_list if "description" in c.kwargs]
        last_desc = update_calls[-1].kwargs["description"]
        assert "2 failed" in last_desc


class TestNodeError:
    def test_marks_bar_as_failed(self):
        proc, mock = _make_processor()
        proc.on_run_start(_run_start())
        proc.on_node_start(_node_start(node_name="broken"))
        proc.on_node_error(
            NodeErrorEvent(
                run_id="r1",
                span_id="ns1",
                parent_span_id="s1",
                node_name="broken",
                graph_name="g",
                error="boom",
                error_type="ValueError",
            )
        )

        mock.update.assert_called()
        desc_arg = mock.update.call_args.kwargs.get("description", "")
        assert "FAILED" in desc_arg


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_shutdown_stops_progress(self):
        proc, mock = _make_processor()
        proc.shutdown()
        mock.stop.assert_called_once()
        assert not proc._started

    def test_lazy_start(self):
        """Progress is not started until first event."""
        proc = RichProgressProcessor()
        assert not proc._started

    def test_no_map_bar_for_regular_run(self):
        proc, mock = _make_processor()
        proc.on_run_start(_run_start(is_map=False))
        mock.add_task.assert_not_called()


# ---------------------------------------------------------------------------
# Import guard
# ---------------------------------------------------------------------------


class TestImportGuard:
    def test_raises_without_rich(self):
        with patch.dict("sys.modules", {"rich": None}), pytest.raises(ImportError, match="rich"):
            from hypergraph.events.rich_progress import _require_rich

            _require_rich()
