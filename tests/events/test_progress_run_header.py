"""Gap-close tests: retries reach the bars, and the run-level header line.

Issue #392: the progress tracker consumes ``NodeAttemptEndEvent`` (cumulative
retry truth on the node bar AND the root map header row), and the completion
message carries "N/total · F failed · R retries · C cached".
"""

from __future__ import annotations

from unittest.mock import MagicMock

from hypergraph.events._progress_renderers import _RichTTYRenderer
from hypergraph.events.rich_progress import RichProgressProcessor
from hypergraph.events.types import (
    NodeAttemptEndEvent,
    NodeEndEvent,
    NodeStartEvent,
    RunEndEvent,
    RunStartEvent,
    RunStatus,
)


def _tty_processor() -> tuple[RichProgressProcessor, MagicMock]:
    processor = RichProgressProcessor(transient=True, force_mode="tty")
    renderer = processor._renderer
    assert isinstance(renderer, _RichTTYRenderer)
    progress = MagicMock()
    counter = iter(range(100))
    progress.add_task.side_effect = lambda *_a, **_k: next(counter)
    renderer._progress = progress
    processor._started = True
    return processor, progress


def _drive_map_with_retry(processor: RichProgressProcessor) -> None:
    processor.on_event(RunStartEvent(run_id="r", span_id="map", graph_name="jobs", is_map=True, map_size=2))
    for index in range(2):
        item_span = f"item-{index}"
        node_span = f"node-{index}"
        processor.on_event(RunStartEvent(run_id="r", span_id=item_span, parent_span_id="map", graph_name="jobs", item_index=index))
        processor.on_event(NodeStartEvent(run_id="r", span_id=node_span, parent_span_id=item_span, node_name="work", graph_name="jobs"))
        if index == 0:
            processor.on_event(
                NodeAttemptEndEvent(
                    run_id="r",
                    span_id=f"attempt-{index}",
                    parent_span_id=node_span,
                    node_name="work",
                    graph_name="jobs",
                    attempt_number=1,
                    outcome="failed",
                    retry_scheduled=True,
                )
            )
        processor.on_event(
            NodeEndEvent(
                run_id="r",
                span_id=node_span,
                parent_span_id=item_span,
                node_name="work",
                graph_name="jobs",
                duration_ms=10.0,
                cached=index == 1,
            )
        )
        processor.on_event(RunEndEvent(run_id="r", span_id=item_span, parent_span_id="map", graph_name="jobs", status=RunStatus.COMPLETED))
    processor.on_event(RunEndEvent(run_id="r", span_id="map", graph_name="jobs", status=RunStatus.COMPLETED))


def test_retries_reach_the_node_bar_and_the_root_header_row() -> None:
    processor, progress = _tty_processor()
    _drive_map_with_retry(processor)
    stats = [call.kwargs["stats"] for call in progress.update.call_args_list if "stats" in call.kwargs]
    # The node bar carries its own cumulative retry count.
    assert any("1↺" in value and "✓" in value for value in stats)
    # The ROOT map row is the live header: item counts plus run-level
    # cache/retry truth.
    assert any("2✓" in value and "1↺" in value and "1◉" in value for value in stats)


def test_completion_line_carries_the_run_level_header() -> None:
    processor, progress = _tty_processor()
    _drive_map_with_retry(processor)
    printed = progress.console.print.call_args.args[0]
    assert "✓ jobs completed!" in printed
    assert "2/2" in printed
    assert "0 failed" in printed
    assert "1 retries" in printed
    assert "1 cached" in printed


def test_a_final_failure_is_not_a_retry() -> None:
    processor, progress = _tty_processor()
    processor.on_event(RunStartEvent(run_id="r", span_id="root", graph_name="g"))
    processor.on_event(NodeStartEvent(run_id="r", span_id="n", parent_span_id="root", node_name="work", graph_name="g"))
    # An attempt that settles WITHOUT another attempt granted: no retry.
    processor.on_event(
        NodeAttemptEndEvent(
            run_id="r",
            span_id="a",
            parent_span_id="n",
            node_name="work",
            graph_name="g",
            attempt_number=2,
            outcome="failed",
            retry_scheduled=False,
        )
    )
    stats = [call.kwargs.get("stats", "") for call in progress.update.call_args_list]
    assert not any("↺" in value for value in stats)
