"""Ticket #185 transcript parity for progress renderers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hypergraph._repr import STATUS_COLORS
from hypergraph.events._progress_renderers import (
    _LogRenderer,
    _NotebookRenderer,
    _RichTTYRenderer,
)
from hypergraph.events.rich_progress import RichProgressProcessor
from hypergraph.events.types import (
    InnerCacheEvent,
    NodeEndEvent,
    NodeErrorEvent,
    NodeStartEvent,
    RunEndEvent,
    RunStartEvent,
    RunStatus,
)


def _event_transcript() -> list[object]:
    events: list[object] = [
        RunStartEvent(run_id="root", span_id="root", graph_name="pipeline"),
        NodeStartEvent(
            run_id="root",
            span_id="load-span",
            parent_span_id="root",
            node_name="load",
            graph_name="pipeline",
        ),
        NodeEndEvent(
            run_id="root",
            span_id="load-span",
            parent_span_id="root",
            node_name="load",
            graph_name="pipeline",
            duration_ms=8.0,
        ),
        NodeStartEvent(
            run_id="root",
            span_id="map-node",
            parent_span_id="root",
            node_name="fanout",
            graph_name="pipeline",
        ),
        RunStartEvent(
            run_id="map-run",
            span_id="map",
            parent_span_id="map-node",
            graph_name="inner",
            is_map=True,
            map_size=5,
        ),
    ]

    statuses = (
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.PARTIAL,
        RunStatus.PAUSED,
        RunStatus.STOPPED,
    )
    for index, status in enumerate(statuses):
        item_span = f"item-{index}"
        node_span = f"work-{index}"
        events.extend(
            [
                RunStartEvent(
                    run_id=f"item-run-{index}",
                    span_id=item_span,
                    parent_span_id="map",
                    graph_name="inner",
                ),
                NodeStartEvent(
                    run_id=f"item-run-{index}",
                    span_id=node_span,
                    parent_span_id=item_span,
                    node_name="work<unsafe>",
                    graph_name="inner",
                ),
            ]
        )
        if index == 0:
            events.append(
                InnerCacheEvent(
                    run_id=f"item-run-{index}",
                    parent_span_id=node_span,
                    node_name="work<unsafe>",
                    graph_name="inner",
                    hit=True,
                    refreshing=True,
                )
            )
        if status in (RunStatus.FAILED, RunStatus.PARTIAL):
            events.append(
                NodeErrorEvent(
                    run_id=f"item-run-{index}",
                    span_id=node_span,
                    parent_span_id=item_span,
                    node_name="work<unsafe>",
                    graph_name="inner",
                    error="boom",
                    error_type="ValueError",
                )
            )
        else:
            events.append(
                NodeEndEvent(
                    run_id=f"item-run-{index}",
                    span_id=node_span,
                    parent_span_id=item_span,
                    node_name="work<unsafe>",
                    graph_name="inner",
                    duration_ms=10.0,
                )
            )
        events.append(
            RunEndEvent(
                run_id=f"item-run-{index}",
                span_id=item_span,
                parent_span_id="map",
                graph_name="inner",
                status=status,
            )
        )

    events.append(
        RunEndEvent(
            run_id="root",
            span_id="root",
            graph_name="pipeline",
            status=RunStatus.COMPLETED,
        )
    )
    return events


def _drive(processor: RichProgressProcessor) -> None:
    for event in _event_transcript():
        processor.on_event(event)


def _mock_rich_progress() -> MagicMock:
    progress = MagicMock()
    counter = iter(range(100))
    progress.add_task.side_effect = lambda *_args, **_kwargs: next(counter)
    return progress


def test_one_transcript_preserves_tty_tasks_stats_and_completion() -> None:
    processor = RichProgressProcessor(transient=True, force_mode="tty")
    renderer = processor._renderer
    assert isinstance(renderer, _RichTTYRenderer)
    progress = _mock_rich_progress()
    renderer._progress = progress

    _drive(processor)

    descriptions = [call.args[0] for call in progress.add_task.call_args_list]
    assert descriptions == ["load", "fanout", "◈ fanout", "  └─ work<unsafe>"]
    assert any(call.kwargs.get("visible") is False for call in progress.update.call_args_list)
    stats = [call.kwargs["stats"] for call in progress.update.call_args_list if "stats" in call.kwargs]
    assert "3✓ 2✗ ~6ms 1↩ 1↻" in stats
    assert stats[-1] == "1✓ 2✗"
    progress.console.print.assert_called_once_with("[bold green]✓ pipeline completed![/bold green]")


def test_one_transcript_preserves_notebook_html_refresh_and_completion() -> None:
    display_handle = MagicMock()
    with patch("IPython.display.display", return_value=display_handle) as display:
        processor = RichProgressProcessor(transient=True, force_mode="notebook")
        renderer = processor._renderer
        assert isinstance(renderer, _NotebookRenderer)

        _drive(processor)

        rendered = renderer._progress._render_html()
        assert "◈ fanout" in rendered
        assert "└─ work&lt;unsafe&gt;" in rendered
        assert "3✓" in rendered and "2✗" in rendered
        assert "1↩" in rendered and "1↻" in rendered
        completion_html = display.call_args.args[0].data
        assert "✓ pipeline completed!" in completion_html
        assert display_handle.update.called
        assert renderer.take_async_flush() is True
        assert renderer.take_async_flush() is False


def test_one_transcript_preserves_exact_non_tty_text_and_map_milestones(
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.setattr("hypergraph.events._progress_renderers._timestamp", lambda: "[12:34:56]")
    processor = RichProgressProcessor(force_mode="non-tty")
    assert isinstance(processor._renderer, _LogRenderer)

    _drive(processor)

    assert capsys.readouterr().out.splitlines() == [
        "[12:34:56] ▶ load started",
        "[12:34:56] ✓ load completed",
        "[12:34:56] ▶ fanout started",
        "[12:34:56] ◈ inner: 10% (1/5)",
        "[12:34:56] ◈ inner: 25% (2/5)",
        "[12:34:56] ◈ inner: 50% (3/5)",
        "[12:34:56] ◈ inner: 75% (4/5)",
        "[12:34:56] ◈ inner: 100% (5/5)",
        "[12:34:56] ✓ pipeline completed!",
    ]


def test_invalid_force_mode_still_falls_through_to_non_tty() -> None:
    processor = RichProgressProcessor(force_mode="not-a-mode")  # type: ignore[arg-type]
    assert isinstance(processor._renderer, _LogRenderer)


def test_notebook_stopped_completion_keeps_paused_foreground() -> None:
    with patch("IPython.display.display") as display:
        processor = RichProgressProcessor(force_mode="notebook")
        processor.on_run_start(RunStartEvent(run_id="root", span_id="root", graph_name="pipeline"))
        processor.on_run_end(
            RunEndEvent(
                run_id="root",
                span_id="root",
                graph_name="pipeline",
                status=RunStatus.STOPPED,
            )
        )

    completion_html = display.call_args.args[0].data
    assert f"color:{STATUS_COLORS['paused']}" in completion_html
    assert "◼ pipeline stopped" in completion_html
