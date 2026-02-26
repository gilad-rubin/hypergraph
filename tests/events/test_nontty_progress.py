"""Tests for non-TTY fallback in RichProgressProcessor."""

from __future__ import annotations

import re
from unittest.mock import patch

from hypergraph.events.rich_progress import RichProgressProcessor
from hypergraph.events.types import (
    NodeEndEvent,
    NodeErrorEvent,
    NodeStartEvent,
    RunEndEvent,
    RunStartEvent,
    RunStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = r"\[\d{2}:\d{2}:\d{2}\]"  # Matches [HH:MM:SS]


def _make_processor() -> RichProgressProcessor:
    """Create a non-TTY processor."""
    return RichProgressProcessor(force_mode="non-tty")


def _run_start(span_id: str, parent: str | None = None, graph: str = "g", is_map: bool = False, map_size: int | None = None) -> RunStartEvent:
    return RunStartEvent(run_id="run", span_id=span_id, parent_span_id=parent, graph_name=graph, is_map=is_map, map_size=map_size)


def _node_start(span_id: str, parent: str, graph: str = "g", node: str = "n") -> NodeStartEvent:
    return NodeStartEvent(run_id="run", span_id=span_id, parent_span_id=parent, graph_name=graph, node_name=node)


def _node_end(span_id: str, parent: str, graph: str = "g", node: str = "n") -> NodeEndEvent:
    return NodeEndEvent(run_id="run", span_id=span_id, parent_span_id=parent, graph_name=graph, node_name=node)


def _node_error(span_id: str, parent: str, graph: str = "g", node: str = "n") -> NodeErrorEvent:
    return NodeErrorEvent(run_id="run", span_id=span_id, parent_span_id=parent, graph_name=graph, node_name=node, error="boom")


def _run_end(span_id: str, parent: str | None = None, graph: str = "g", status: RunStatus = RunStatus.COMPLETED) -> RunEndEvent:
    return RunEndEvent(run_id="run", span_id=span_id, parent_span_id=parent, graph_name=graph, status=status, error=None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNonTTYNodeProgress:
    """Test basic node start/end logging in non-TTY mode."""

    def test_node_start_end_logged(self, capsys):
        proc = _make_processor()
        proc.on_run_start(_run_start("r1"))
        proc.on_node_start(_node_start("n1", "r1", node="fetch_data"))
        proc.on_node_end(_node_end("n1", "r1", node="fetch_data"))

        output = capsys.readouterr().out
        assert re.search(rf"{_TS} ▶ fetch_data started", output)
        assert re.search(rf"{_TS} ✓ fetch_data completed", output)

    def test_node_error_logged(self, capsys):
        proc = _make_processor()
        proc.on_run_start(_run_start("r1"))
        proc.on_node_start(_node_start("n1", "r1", node="bad_node"))
        proc.on_node_error(_node_error("n1", "r1", node="bad_node"))

        output = capsys.readouterr().out
        assert re.search(rf"{_TS} ✗ bad_node FAILED", output)

    def test_run_completion_logged(self, capsys):
        proc = _make_processor()
        proc.on_run_start(_run_start("r1"))
        proc.on_run_end(_run_end("r1"))

        output = capsys.readouterr().out
        assert re.search(rf"{_TS} ✓ g completed!", output)

    def test_run_failure_logged(self, capsys):
        proc = _make_processor()
        proc.on_run_start(_run_start("r1"))
        proc.on_run_end(RunEndEvent(run_id="run", span_id="r1", parent_span_id=None, graph_name="g", status=RunStatus.FAILED, error="timeout"))

        output = capsys.readouterr().out
        assert re.search(rf"{_TS} ✗ g failed: timeout", output)


class TestNonTTYMapMilestones:
    """Test milestone-based logging for map operations."""

    def test_map_milestones_logged(self, capsys):
        proc = _make_processor()
        # Root run with map
        proc.on_run_start(_run_start("r1"))
        proc.on_run_start(_run_start("map1", parent="r1", graph="process", is_map=True, map_size=10))

        # Simulate 10 map-item runs completing
        for i in range(10):
            item_span = f"item_{i}"
            proc.on_run_start(_run_start(item_span, parent="map1", graph="process"))
            proc.on_node_start(_node_start(f"n_{i}", item_span, node="work"))
            proc.on_node_end(_node_end(f"n_{i}", item_span, node="work"))
            proc.on_run_end(_run_end(item_span, parent="map1"))

        output = capsys.readouterr().out
        # Should have milestone logs for 10%, 25%, 50%, 75%, 100%
        assert "10%" in output
        assert "25%" in output
        assert "50%" in output
        assert "75%" in output
        assert "100%" in output

    def test_map_items_not_logged_individually(self, capsys):
        """Map item nodes should NOT produce individual start/end logs."""
        proc = _make_processor()
        proc.on_run_start(_run_start("r1"))
        proc.on_run_start(_run_start("map1", parent="r1", graph="process", is_map=True, map_size=4))

        item_span = "item_0"
        proc.on_run_start(_run_start(item_span, parent="map1", graph="process"))
        proc.on_node_start(_node_start("n_0", item_span, node="work"))
        proc.on_node_end(_node_end("n_0", item_span, node="work"))

        output = capsys.readouterr().out
        # Should NOT have individual node start/end for map items
        assert "▶ work started" not in output
        assert "✓ work completed" not in output

    def test_milestones_not_repeated(self, capsys):
        """Each milestone should be logged exactly once."""
        proc = _make_processor()
        proc.on_run_start(_run_start("r1"))
        proc.on_run_start(_run_start("map1", parent="r1", graph="process", is_map=True, map_size=4))

        for i in range(4):
            item_span = f"item_{i}"
            proc.on_run_start(_run_start(item_span, parent="map1", graph="process"))
            proc.on_run_end(_run_end(item_span, parent="map1"))

        output = capsys.readouterr().out
        assert output.count("100%") == 1
        assert output.count("25%") == 1


class TestNonTTYAutoDetect:
    """Test auto-detection of TTY mode."""

    def test_auto_detects_nontty(self):
        with patch("hypergraph.events.rich_progress._is_tty", return_value=False):
            proc = RichProgressProcessor(force_mode="auto")
            assert proc._tty_mode is False

    def test_auto_detects_tty(self):
        with patch("hypergraph.events.rich_progress._is_tty", return_value=True):
            proc = RichProgressProcessor(force_mode="auto")
            assert proc._tty_mode is True

    def test_force_nontty(self):
        proc = RichProgressProcessor(force_mode="non-tty")
        assert proc._tty_mode is False

    def test_shutdown_nontty_no_error(self):
        proc = _make_processor()
        proc.on_run_start(_run_start("r1"))
        proc.shutdown()
        # Should not raise
