"""Tests for structured failure capture and inspect views."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass

import pytest

from hypergraph import AsyncRunner, Graph, RunStatus, SyncRunner, node
from hypergraph.runners._shared.inspect import (
    FailureCase,
    InspectWidget,
    LiveInspectState,
    NodeView,
    RunView,
    build_live_run_view,
)
from hypergraph.runners._shared.inspect_html import (
    build_inspect_update_script,
    build_run_view_payload,
    generate_inspect_document,
    render_map_inspect_widget,
    serialize_inspect_value,
)


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3


@node(output_name="boom")
def fail_after_double(doubled: int) -> int:
    raise ValueError("boom")


@node(output_name="slow")
async def slow_double(x: int) -> int:
    await asyncio.sleep(0.01)
    return x * 2


@node(output_name="doubled")
async def double_async(x: int) -> int:
    await asyncio.sleep(0.01)
    return x * 2


@node(output_name="boom")
async def fail_after_double_async(doubled: int) -> int:
    raise ValueError("boom")


@node(output_name="slow_sync")
def slow_sync_double(x: int) -> int:
    time.sleep(0.01)
    return x * 2


class TestSyncFailureCase:
    def test_continue_result_exposes_structured_failure_case(self):
        graph = Graph([double, fail_after_double])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5}, error_handling="continue")

        assert result.status == RunStatus.FAILED
        assert result.failure is not None
        assert result.failure.node_name == "fail_after_double"
        assert result.failure.inputs == {"doubled": 10}
        assert isinstance(result.failure.error, ValueError)

    def test_inspect_true_captures_successful_node_inputs_and_outputs(self):
        graph = Graph([double, triple])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5}, inspect=True)
        view = result.inspect()

        assert view.status == RunStatus.COMPLETED
        assert view["double"].inputs == {"x": 5}
        assert view["double"].outputs == {"doubled": 10}
        assert view["triple"].inputs == {"doubled": 10}
        assert view["triple"].outputs == {"tripled": 30}
        assert view["double"].started_at_ms is not None
        assert view["double"].ended_at_ms is not None
        assert view["double"].ended_at_ms >= view["double"].started_at_ms


class TestAsyncFailureCase:
    @pytest.mark.asyncio
    async def test_continue_result_exposes_structured_failure_case(self):
        graph = Graph([double, fail_after_double])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5}, error_handling="continue")

        assert result.status == RunStatus.FAILED
        assert result.failure is not None
        assert result.failure.node_name == "fail_after_double"
        assert result.failure.inputs == {"doubled": 10}
        assert isinstance(result.failure.error, ValueError)

    @pytest.mark.asyncio
    async def test_inspect_true_captures_successful_node_inputs_and_outputs(self):
        graph = Graph([slow_double])
        runner = AsyncRunner()

        result = await runner.run(graph, {"x": 5}, inspect=True)
        view = result.inspect()

        assert view.status == RunStatus.COMPLETED
        assert view["slow_double"].inputs == {"x": 5}
        assert view["slow_double"].outputs == {"slow": 10}


class TestLiveInspectView:
    def test_live_view_includes_failed_node_when_only_failure_case_exists(self):
        view = build_live_run_view(
            "run-test",
            (),
            status=RunStatus.FAILED,
            failure=FailureCase(
                node_name="fail_after_double",
                error=ValueError("boom"),
                inputs={"doubled": 10},
                superstep=1,
                duration_ms=0.05,
            ),
        )

        assert [node.node_name for node in view.nodes] == ["fail_after_double"]
        assert view["fail_after_double"].status == "failed"
        assert view["fail_after_double"].inputs == {"doubled": 10}
        assert view["fail_after_double"].error == "boom"


class TestInspectWidgetHtml:
    def test_repr_html_uses_iframe_widget(self):
        graph = Graph([double, triple])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5}, inspect=True)

        html = result.inspect()._repr_html_()

        assert "<iframe" in html
        assert "hypergraph-inspect-frame" in html
        assert "Timeline" in html
        assert "Graph" in html

    def test_live_widget_updates_existing_display_handle(self, monkeypatch: pytest.MonkeyPatch):
        ipy_display = pytest.importorskip("IPython.display")

        class FakeHandle:
            def __init__(self) -> None:
                self.updates: list[object] = []

            def update(self, obj: object) -> None:
                self.updates.append(obj)

        display_calls: list[tuple[object, bool]] = []
        handle = FakeHandle()

        def fake_display(obj: object, *, display_id: object = False) -> FakeHandle:
            display_calls.append((obj, display_id))
            return handle

        monkeypatch.setattr(ipy_display, "display", fake_display)

        state = LiveInspectState()
        state.set_run_id("run-test")
        state.mark_running("double", 0, {"x": 5}, 10.0)

        widget = InspectWidget(state)
        widget._enabled = True

        widget.refresh()
        state.record_snapshot("double", 0, {"x": 5}, {"doubled": 10}, 1.0, 10.0, 11.0, False)
        widget.refresh()

        assert len(display_calls) == 1
        assert display_calls[0][1] == widget._widget_id
        assert isinstance(display_calls[0][0], ipy_display.HTML)
        assert "<iframe" in display_calls[0][0].data
        assert len(handle.updates) == 1
        assert isinstance(handle.updates[0], ipy_display.HTML)
        assert "<iframe" in handle.updates[0].data

    def test_repeated_render_inspect_widget_uses_distinct_iframe_ids(self):
        view = build_live_run_view(
            "run-test",
            (),
            status="running",
        )

        first = view._repr_html_()
        second = view._repr_html_()

        assert first != second
        assert 'id="hg-' in first
        assert 'id="hg-' in second

    def test_update_script_uses_postmessage_transport(self):
        view = build_live_run_view(
            "run-test",
            (),
            status="running",
        )

        script = build_inspect_update_script("inspect-frame-123", view)

        assert "hypergraph-inspect-update" in script
        assert "inspect-frame-123" in script
        assert "postMessage" in script

    def test_generate_document_escapes_script_end_markers_in_graph_html(self):
        doc = generate_inspect_document(
            payload={"run_id": "run-test", "status": "running", "total_duration_ms": 0.0, "failure": None, "nodes": []},
            widget_id="inspect-frame-123",
            graph_html='<script>window.demo = true;</script><div id="fallback">Rendering interactive view…</div>',
        )

        assert 'graphHtml: "<script>window.demo = true;<\\/script><div id=\\"fallback\\">Rendering interactive view…<\\/div>"' in doc
        assert doc.count("</script>") == 2

    def test_generate_document_includes_host_theme_sync_and_light_theme_rules(self):
        doc = generate_inspect_document(
            payload={"run_id": "run-test", "status": "running", "total_duration_ms": 0.0, "failure": None, "nodes": []},
            widget_id="inspect-frame-123",
        )

        assert 'data-hg-theme="light"' in doc
        assert "data-vscode-theme-kind" in doc
        assert "data-jp-theme-light" in doc
        assert "prefers-color-scheme" in doc
        assert "color-scheme: light dark;" in doc
        assert "frameElement" in doc
        assert "ResizeObserver" in doc

    def test_repr_html_uses_auto_sizing_iframe_baseline(self):
        view = build_live_run_view(
            "run-test",
            (),
            status=RunStatus.COMPLETED,
        )

        html = view._repr_html_()

        assert 'height="360"' in html
        assert "min-height:260px" in html

    def test_render_map_inspect_widget_shows_batch_summary_and_child_view(self):
        graph = Graph([double, fail_after_double])
        runner = SyncRunner()
        result = runner.map(graph, {"x": [1, 2]}, map_over="x", inspect=True, error_handling="continue")

        html = render_map_inspect_widget(result=result, graph_name=graph.name)

        assert "Inspect Batch" in html
        assert "2 items" in html
        assert "failed" in html.lower()
        assert "<iframe" in html

    def test_start_map_inspect_shows_one_widget_and_updates_it(self, monkeypatch: pytest.MonkeyPatch):
        ipy_display = pytest.importorskip("IPython.display")

        class FakeHandle:
            def __init__(self) -> None:
                self.updates: list[object] = []

            def update(self, obj: object) -> None:
                self.updates.append(obj)

        display_calls: list[tuple[object, object]] = []
        handle = FakeHandle()

        def fake_display(obj: object, *, display_id: object = False) -> FakeHandle:
            display_calls.append((obj, display_id))
            return handle

        monkeypatch.setattr(ipy_display, "display", fake_display)
        monkeypatch.setattr("hypergraph.runners._shared.inspect._is_notebook", lambda: True)

        graph = Graph([double, fail_after_double])
        runner = SyncRunner()

        batch = runner.start_map(graph, {"x": [1, 2, 3]}, map_over="x", inspect=True)
        result = batch.result(raise_on_failure=False)

        assert result.status == RunStatus.FAILED
        assert len(display_calls) == 1
        assert display_calls[0][1] == batch._inspect_widget._widget_id  # type: ignore[union-attr]
        assert isinstance(display_calls[0][0], ipy_display.HTML)
        assert "Inspect Batch" in display_calls[0][0].data
        assert len(handle.updates) == 1
        assert isinstance(handle.updates[0], ipy_display.HTML)
        assert "<iframe" in handle.updates[0].data

    def test_sync_map_inspect_does_not_render_child_widgets(self, monkeypatch: pytest.MonkeyPatch):
        ipy_display = pytest.importorskip("IPython.display")
        display_calls: list[tuple[object, object]] = []

        def fake_display(obj: object, *, display_id: object = False) -> object:
            display_calls.append((obj, display_id))

            class _Handle:
                def update(self, obj: object) -> None:
                    display_calls.append((obj, "update"))

            return _Handle()

        monkeypatch.setattr(ipy_display, "display", fake_display)
        monkeypatch.setattr("hypergraph.runners._shared.inspect._is_notebook", lambda: True)

        graph = Graph([double, fail_after_double])
        runner = SyncRunner()

        result = runner.map(graph, {"x": [1, 2, 3]}, map_over="x", inspect=True, error_handling="continue")

        assert result.status == RunStatus.FAILED
        assert display_calls == []

    @pytest.mark.asyncio
    async def test_async_start_map_inspect_shows_one_widget_and_updates_it(self, monkeypatch: pytest.MonkeyPatch):
        ipy_display = pytest.importorskip("IPython.display")

        class FakeHandle:
            def __init__(self) -> None:
                self.updates: list[object] = []

            def update(self, obj: object) -> None:
                self.updates.append(obj)

        display_calls: list[tuple[object, object]] = []
        handle = FakeHandle()

        def fake_display(obj: object, *, display_id: object = False) -> FakeHandle:
            display_calls.append((obj, display_id))
            return handle

        monkeypatch.setattr(ipy_display, "display", fake_display)
        monkeypatch.setattr("hypergraph.runners._shared.inspect._is_notebook", lambda: True)

        graph = Graph([double_async, fail_after_double_async])
        runner = AsyncRunner()

        batch = runner.start_map(graph, {"x": [1, 2]}, map_over="x", inspect=True)
        result = await batch.result(raise_on_failure=False)

        assert result.status == RunStatus.FAILED
        assert len(display_calls) == 1
        assert display_calls[0][1] == batch._inspect_widget._widget_id  # type: ignore[union-attr]
        assert isinstance(display_calls[0][0], ipy_display.HTML)
        assert "Inspect Batch" in display_calls[0][0].data
        assert len(handle.updates) == 1
        assert isinstance(handle.updates[0], ipy_display.HTML)
        assert "<iframe" in handle.updates[0].data

    @pytest.mark.asyncio
    async def test_async_map_inspect_does_not_render_child_widgets(self, monkeypatch: pytest.MonkeyPatch):
        ipy_display = pytest.importorskip("IPython.display")
        display_calls: list[tuple[object, object]] = []

        def fake_display(obj: object, *, display_id: object = False) -> object:
            display_calls.append((obj, display_id))

            class _Handle:
                def update(self, obj: object) -> None:
                    display_calls.append((obj, "update"))

            return _Handle()

        monkeypatch.setattr(ipy_display, "display", fake_display)
        monkeypatch.setattr("hypergraph.runners._shared.inspect._is_notebook", lambda: True)

        graph = Graph([double_async, fail_after_double_async])
        runner = AsyncRunner()

        result = await runner.map(graph, {"x": [1, 2]}, map_over="x", inspect=True, error_handling="continue")

        assert result.status == RunStatus.FAILED
        assert display_calls == []


@dataclass
class DemoDataclass:
    foo: int
    bar: str


class DemoModel:
    def __init__(self, *, city: str, count: int) -> None:
        self.city = city
        self.count = count

    def model_dump(self, *, mode: str = "python") -> dict[str, object]:
        assert mode == "python"
        return {"city": self.city, "count": self.count}


class TestInspectValueSerialization:
    def test_detects_markdown(self):
        payload = serialize_inspect_value("# Hello\n\n- item one\n- item two")

        assert payload["kind"] == "markdown"
        assert payload["html"] != ""

    def test_detects_dataclass(self):
        payload = serialize_inspect_value(DemoDataclass(foo=1, bar="ok"))

        assert payload["kind"] == "dataclass"
        assert payload["type_name"] == "DemoDataclass"
        assert payload["entries"][0]["key"] == "foo"

    def test_detects_pydantic_model(self):
        payload = serialize_inspect_value(DemoModel(city="Tel Aviv", count=3))

        assert payload["kind"] == "pydantic"
        assert payload["type_name"] == "DemoModel"
        assert {entry["key"] for entry in payload["entries"]} == {"city", "count"}

    def test_detects_table_like_values(self):
        rows = [{"city": "Tel Aviv", "count": 2}, {"city": "Haifa", "count": 1}]

        payload = serialize_inspect_value(rows)

        assert payload["kind"] == "table"
        assert payload["columns"] == ["city", "count"]
        assert payload["row_count"] == 2

    def test_array_payload_keeps_expandable_length(self):
        payload = serialize_inspect_value(list(range(40)))

        assert payload["kind"] == "array"
        assert payload["length"] == 40
        assert len(payload["items"]) == 40


class TestInspectPayload:
    def test_payload_includes_timing_and_failure_details(self):
        graph = Graph([double, fail_after_double])
        runner = SyncRunner()

        result = runner.run(graph, {"x": 5}, inspect=True, error_handling="continue")
        payload = build_run_view_payload(result.inspect())

        assert payload["run_id"] == result.run_id
        assert payload["failure"]["node_name"] == "fail_after_double"
        assert payload["nodes"][0]["started_at_ms"] is not None
        assert payload["nodes"][0]["ended_at_ms"] is not None
        assert payload["nodes"][1]["started_at_ms"] is not None
        assert payload["nodes"][1]["inputs"]["kind"] == "mapping"

    def test_timeline_collapses_instrumentation_gaps_but_preserves_order(self):
        view = RunView(
            run_id="run-test",
            status=RunStatus.FAILED,
            total_duration_ms=36.4,
            failure=FailureCase(
                node_name="fail_after_double",
                error=ValueError("boom"),
                inputs={"doubled": 10},
                superstep=1,
                duration_ms=11.2,
                started_at_ms=25.2,
                ended_at_ms=36.4,
            ),
            nodes=(
                NodeView(
                    node_name="double",
                    status="completed",
                    superstep=0,
                    duration_ms=12.3,
                    inputs={"x": 5},
                    outputs={"doubled": 10},
                    error=None,
                    cached=False,
                    started_at_ms=0.0,
                    ended_at_ms=12.3,
                ),
                NodeView(
                    node_name="fail_after_double",
                    status="failed",
                    superstep=1,
                    duration_ms=11.2,
                    inputs={"doubled": 10},
                    outputs=None,
                    error="boom",
                    cached=False,
                    started_at_ms=25.2,
                    ended_at_ms=36.4,
                ),
            ),
        )

        payload = build_run_view_payload(view)

        assert payload["timeline_total_duration_ms"] == pytest.approx(23.5, abs=0.1)
        assert payload["nodes"][0]["timeline_started_at_ms"] == pytest.approx(0.0, abs=0.001)
        assert payload["nodes"][0]["timeline_ended_at_ms"] == pytest.approx(12.3, abs=0.1)
        assert payload["nodes"][1]["timeline_started_at_ms"] == pytest.approx(12.3, abs=0.1)
        assert payload["nodes"][1]["timeline_ended_at_ms"] == pytest.approx(23.5, abs=0.1)


class TestStartRunHandles:
    def test_sync_start_run_returns_handle_with_failure_and_view(self):
        graph = Graph([double, fail_after_double])
        runner = SyncRunner()

        run = runner.start_run(graph, {"x": 5}, inspect=True)
        result = run.result(raise_on_failure=False)

        assert result.status == RunStatus.FAILED
        assert run.failure is not None
        assert run.failure.node_name == "fail_after_double"
        assert run.view()["double"].outputs == {"doubled": 10}

    @pytest.mark.asyncio
    async def test_async_start_run_returns_handle_with_failure_and_view(self):
        graph = Graph([double, fail_after_double])
        runner = AsyncRunner()

        run = runner.start_run(graph, {"x": 5}, inspect=True)
        result = await run.result(raise_on_failure=False)

        assert result.status == RunStatus.FAILED
        assert run.failure is not None
        assert run.failure.inputs == {"doubled": 10}
        assert run.view()["double"].outputs == {"doubled": 10}


class TestStartMapHandles:
    def test_sync_start_map_tracks_failed_item_index(self):
        graph = Graph([double, fail_after_double])
        runner = SyncRunner()

        batch = runner.start_map(
            graph,
            {"x": [1, 2]},
            map_over="x",
            inspect=True,
        )
        result = batch.result(raise_on_failure=False)

        assert result.status == RunStatus.FAILED
        assert batch.failures[0].item_index == 0
        assert batch.failures[1].item_index == 1


class TestStartRunStop:
    def test_sync_start_run_stop_stops_before_next_superstep(self):
        started = threading.Event()

        @node(output_name="doubled")
        def slow_sync_seed(x: int) -> int:
            started.set()
            time.sleep(0.05)
            return x * 2

        graph = Graph([slow_sync_seed, triple])
        runner = SyncRunner()

        run = runner.start_run(graph, {"x": 5}, inspect=True)
        assert started.wait(timeout=1)
        assert run.view().status == "running"
        run.stop(info={"kind": "test-stop"})
        result = run.result(raise_on_failure=False)

        assert result.status == RunStatus.STOPPED
        assert "tripled" not in result.values

    @pytest.mark.asyncio
    async def test_async_start_run_stop_stops_before_next_superstep(self):
        started = asyncio.Event()

        @node(output_name="doubled")
        async def slow_async_seed(x: int) -> int:
            started.set()
            await asyncio.sleep(0.05)
            return x * 2

        graph = Graph([slow_async_seed, triple])
        runner = AsyncRunner()

        run = runner.start_run(graph, {"x": 5}, inspect=True)
        await asyncio.wait_for(started.wait(), timeout=1)
        assert run.view().status == "running"
        run.stop(info={"kind": "test-stop"})
        result = await run.result(raise_on_failure=False)

        assert result.status == RunStatus.STOPPED
        assert "tripled" not in result.values

    @pytest.mark.asyncio
    async def test_async_start_map_tracks_failed_item_index(self):
        graph = Graph([double, fail_after_double])
        runner = AsyncRunner()

        batch = runner.start_map(
            graph,
            {"x": [1, 2]},
            map_over="x",
            inspect=True,
        )
        result = await batch.result(raise_on_failure=False)

        assert result.status == RunStatus.FAILED
        assert batch.failures[0].item_index == 0
        assert batch.failures[1].item_index == 1
