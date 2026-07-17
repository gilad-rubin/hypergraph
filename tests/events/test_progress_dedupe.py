"""Issue #207: ``show_progress=True`` must not double-render progress.

The auto-synthesized default ``RichProgressProcessor`` must inspect the MERGED
processor sequence — ``[*graph.default_event_processors, *event_processors]`` —
and only be added when no Rich processor exists anywhere in it. Explicit
user-supplied duplicates are always preserved; only the auto default is
suppressed.

Every witness compares against a call-site control transcript (same graph, one
explicit non-TTY Rich processor, no auto progress) — counting transcript lines,
never wall-clock.
"""

from __future__ import annotations

import re

from hypergraph import AsyncRunner, Graph, SyncRunner, node
from hypergraph.events import EventProcessor
from hypergraph.events.rich_progress import RichProgressProcessor
from hypergraph.events.types import RunStartEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TS = re.compile(r"\[\d{2}:\d{2}:\d{2}\]")


def _transcript(capsys) -> list[str]:
    """Read captured stdout as timestamp-normalized non-empty lines."""
    out = capsys.readouterr().out
    return [_TS.sub("[TS]", line) for line in out.splitlines() if line.strip()]


def _rich() -> RichProgressProcessor:
    return RichProgressProcessor(force_mode="non-tty")


class Recorder(EventProcessor):
    """Non-Rich processor; optionally journals (label, event) into a shared list."""

    def __init__(self, label: str = "", journal: list | None = None) -> None:
        self.label = label
        self.events: list = []
        self._journal = journal

    def on_event(self, event) -> None:
        self.events.append(event)
        if self._journal is not None:
            self._journal.append((self.label, event))


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3


def _flat_graph() -> Graph:
    return Graph([double], name="wf")


def _nested_graph() -> Graph:
    inner = Graph([double], name="inner")
    return Graph([inner.as_node(), triple], name="outer")


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


class TestSyncProgressDedupe:
    def test_run_carried_rich_with_show_progress_renders_once(self, capsys):
        """The #207 witness: carried Rich + show_progress=True → one transcript."""
        control_result = SyncRunner().run(_flat_graph(), {"x": 3}, event_processors=[_rich()])
        control = _transcript(capsys)
        assert control_result["doubled"] == 6
        assert len(control) == 3  # node start, node end, run end

        graph = _flat_graph().with_processors(_rich())
        result = SyncRunner(show_progress=True).run(graph, {"x": 3})

        assert result["doubled"] == 6
        assert _transcript(capsys) == control

    def test_run_callsite_rich_with_show_progress_renders_once(self, capsys):
        control_result = SyncRunner().run(_flat_graph(), {"x": 3}, event_processors=[_rich()])
        control = _transcript(capsys)
        assert control_result.completed

        result = SyncRunner(show_progress=True).run(_flat_graph(), {"x": 3}, event_processors=[_rich()])

        assert result["doubled"] == 6
        assert _transcript(capsys) == control

    def test_run_explicit_duplicate_rich_processors_are_kept(self, capsys):
        """Two DISTINCT user-supplied Rich processors are never deduplicated."""
        SyncRunner().run(_flat_graph(), {"x": 3}, event_processors=[_rich()])
        control = _transcript(capsys)

        graph = _flat_graph().with_processors(_rich())
        result = SyncRunner(show_progress=True).run(graph, {"x": 3}, event_processors=[_rich()])

        assert result["doubled"] == 6
        lines = _transcript(capsys)
        # Both explicit processors render (2x control) — and no third auto default.
        assert len(lines) == 2 * len(control)
        for line in control:
            assert lines.count(line) == 2

    def test_run_carried_non_rich_still_synthesizes_default(self, capsys):
        """A carried non-Rich processor must not suppress the auto default."""
        SyncRunner().run(_flat_graph(), {"x": 3}, event_processors=[_rich()])
        control = _transcript(capsys)

        recorder = Recorder("carried")
        graph = _flat_graph().with_processors(recorder)
        result = SyncRunner(show_progress=True).run(graph, {"x": 3})

        assert result["doubled"] == 6
        assert _transcript(capsys) == control
        assert any(isinstance(e, RunStartEvent) for e in recorder.events)

    def test_run_show_progress_preserves_carried_before_callsite_order(self, capsys):
        journal: list = []
        carried = Recorder("carried", journal)
        callsite = Recorder("callsite", journal)
        graph = _flat_graph().with_processors(carried)

        result = SyncRunner(show_progress=True).run(graph, {"x": 3}, event_processors=[callsite])

        assert result["doubled"] == 6
        run_start_labels = [label for label, e in journal if isinstance(e, RunStartEvent)]
        assert run_start_labels == ["carried", "callsite"]
        capsys.readouterr()  # drain synthesized-progress output

    def test_map_carried_rich_with_show_progress_renders_once(self, capsys):
        control_results = SyncRunner().map(_flat_graph(), {"x": [1, 2, 3]}, map_over="x", event_processors=[_rich()])
        control = _transcript(capsys)
        assert control_results["doubled"] == [2, 4, 6]
        assert len(control) > 0

        graph = _flat_graph().with_processors(_rich())
        results = SyncRunner(show_progress=True).map(graph, {"x": [1, 2, 3]}, map_over="x")

        assert results["doubled"] == [2, 4, 6]
        assert _transcript(capsys) == control

    def test_nested_graphnode_carried_rich_renders_once(self, capsys):
        control_result = SyncRunner().run(_nested_graph(), {"x": 2}, event_processors=[_rich()])
        control = _transcript(capsys)
        assert control_result["tripled"] == 12

        graph = _nested_graph().with_processors(_rich())
        result = SyncRunner(show_progress=True).run(graph, {"x": 2})

        assert result["tripled"] == 12
        assert _transcript(capsys) == control


# ---------------------------------------------------------------------------
# Async
# ---------------------------------------------------------------------------


class TestAsyncProgressDedupe:
    async def test_run_carried_rich_with_show_progress_renders_once(self, capsys):
        control_result = await AsyncRunner().run(_flat_graph(), {"x": 3}, event_processors=[_rich()])
        control = _transcript(capsys)
        assert control_result["doubled"] == 6
        assert len(control) == 3

        graph = _flat_graph().with_processors(_rich())
        result = await AsyncRunner(show_progress=True).run(graph, {"x": 3})

        assert result["doubled"] == 6
        assert _transcript(capsys) == control

    async def test_run_callsite_rich_with_show_progress_renders_once(self, capsys):
        control_result = await AsyncRunner().run(_flat_graph(), {"x": 3}, event_processors=[_rich()])
        control = _transcript(capsys)
        assert control_result.completed

        result = await AsyncRunner(show_progress=True).run(_flat_graph(), {"x": 3}, event_processors=[_rich()])

        assert result["doubled"] == 6
        assert _transcript(capsys) == control

    async def test_run_explicit_duplicate_rich_processors_are_kept(self, capsys):
        await AsyncRunner().run(_flat_graph(), {"x": 3}, event_processors=[_rich()])
        control = _transcript(capsys)

        graph = _flat_graph().with_processors(_rich())
        result = await AsyncRunner(show_progress=True).run(graph, {"x": 3}, event_processors=[_rich()])

        assert result["doubled"] == 6
        lines = _transcript(capsys)
        assert len(lines) == 2 * len(control)
        for line in control:
            assert lines.count(line) == 2

    async def test_run_carried_non_rich_still_synthesizes_default(self, capsys):
        await AsyncRunner().run(_flat_graph(), {"x": 3}, event_processors=[_rich()])
        control = _transcript(capsys)

        recorder = Recorder("carried")
        graph = _flat_graph().with_processors(recorder)
        result = await AsyncRunner(show_progress=True).run(graph, {"x": 3})

        assert result["doubled"] == 6
        assert _transcript(capsys) == control
        assert any(isinstance(e, RunStartEvent) for e in recorder.events)

    async def test_run_show_progress_preserves_carried_before_callsite_order(self, capsys):
        journal: list = []
        carried = Recorder("carried", journal)
        callsite = Recorder("callsite", journal)
        graph = _flat_graph().with_processors(carried)

        result = await AsyncRunner(show_progress=True).run(graph, {"x": 3}, event_processors=[callsite])

        assert result["doubled"] == 6
        run_start_labels = [label for label, e in journal if isinstance(e, RunStartEvent)]
        assert run_start_labels == ["carried", "callsite"]
        capsys.readouterr()  # drain synthesized-progress output

    async def test_map_carried_rich_with_show_progress_renders_once(self, capsys):
        control_results = await AsyncRunner().map(_flat_graph(), {"x": [1, 2, 3]}, map_over="x", event_processors=[_rich()])
        control = _transcript(capsys)
        assert control_results["doubled"] == [2, 4, 6]
        assert len(control) > 0

        graph = _flat_graph().with_processors(_rich())
        results = await AsyncRunner(show_progress=True).map(graph, {"x": [1, 2, 3]}, map_over="x")

        assert results["doubled"] == [2, 4, 6]
        assert _transcript(capsys) == control

    async def test_nested_graphnode_carried_rich_renders_once(self, capsys):
        control_result = await AsyncRunner().run(_nested_graph(), {"x": 2}, event_processors=[_rich()])
        control = _transcript(capsys)
        assert control_result["tripled"] == 12

        graph = _nested_graph().with_processors(_rich())
        result = await AsyncRunner(show_progress=True).run(graph, {"x": 2})

        assert result["tripled"] == 12
        assert _transcript(capsys) == control
