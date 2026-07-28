"""Tests for the OpenTelemetryProcessor.

The import guard test always runs. SDK-dependent tests are skipped
if opentelemetry-sdk is not installed.
"""

import asyncio
from types import SimpleNamespace

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, interrupt, node
from tests._interrupt_questions import StringQuestion

# Check if the full OTel SDK is available
try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.trace import StatusCode

    try:
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    except ImportError:  # pragma: no cover - compatibility with older sdk layout
        from opentelemetry.sdk.trace.export.in_memory import InMemorySpanExporter

    HAS_OTEL_SDK = True
except ImportError:
    HAS_OTEL_SDK = False

requires_otel = pytest.mark.skipif(not HAS_OTEL_SDK, reason="opentelemetry-sdk not installed")


@pytest.fixture
def sqlite_checkpointer(tmp_path):
    """Provide an explicitly closed SQLite checkpointer for OTel tests."""
    from hypergraph.checkpointers import SqliteCheckpointer

    cp = SqliteCheckpointer(str(tmp_path / "otel-lineage.db"))
    yield cp
    asyncio.run(cp.close())


class TestImportGuard:
    def test_import_guard_raises_clear_error(self, monkeypatch):
        """Importing without opentelemetry gives a clear install instruction."""
        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "opentelemetry" or name.startswith("opentelemetry."):
                raise ImportError("mocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        from hypergraph.events.otel import _require_opentelemetry

        with pytest.raises(ImportError, match="pip install 'hypergraph-ai\\[otel\\]'"):
            _require_opentelemetry()


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled + doubled // 2


@node(output_name="value")
def unstable(x: int) -> int:
    if x == 2:
        raise ValueError("boom")
    return x


@requires_otel
class TestOTelProcessor:
    @pytest.fixture(autouse=True)
    def otel_setup(self):
        """Set up in-memory OTel exporter for testing."""
        from opentelemetry import trace

        self.exporter = InMemorySpanExporter()
        provider = trace.get_tracer_provider()
        if not isinstance(provider, TracerProvider):
            provider = TracerProvider()
            trace._set_tracer_provider(provider, log=False)  # type: ignore[attr-defined]
        self.provider = provider

        self.span_processor = SimpleSpanProcessor(self.exporter)
        provider.add_span_processor(self.span_processor)
        yield
        self.exporter.clear()
        provider._active_span_processor._span_processors = tuple(  # type: ignore[attr-defined]
            processor
            for processor in provider._active_span_processor._span_processors
            if processor is not self.span_processor  # type: ignore[attr-defined]
        )
        self.span_processor.shutdown()

    def test_sync_run_produces_graph_and_node_spans(self):
        """SyncRunner with OTelProcessor exports graph + node spans."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        graph = Graph([double, triple])
        processor = OpenTelemetryProcessor()
        result = SyncRunner().run(graph, {"x": 10}, event_processors=[processor])

        assert result.completed
        spans = self.exporter.get_finished_spans()
        span_names = [s.name for s in spans]
        assert "double" in span_names
        assert "triple" in span_names
        assert "graph" in span_names
        assert all(span.kind.name == "INTERNAL" for span in spans)
        assert all(span.status.status_code == StatusCode.UNSET for span in spans)
        assert all("openinference.span.kind" not in span.attributes for span in spans)
        assert {span.attributes["hypergraph.span.role"] for span in spans} == {"graph", "node"}

    def test_span_attributes_include_node_info(self):
        """Node spans carry Hypergraph execution attributes."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        graph = Graph([double])
        processor = OpenTelemetryProcessor()
        SyncRunner().run(graph, {"x": 5}, event_processors=[processor])

        spans = self.exporter.get_finished_spans()
        node_spans = [s for s in spans if s.name == "double"]
        assert len(node_spans) >= 1
        attrs = dict(node_spans[0].attributes)
        assert attrs["hypergraph.node_name"] == "double"
        assert attrs["hypergraph.superstep"] == 0

    @pytest.mark.parametrize("runner", [SyncRunner(), AsyncRunner()])
    def test_nested_graph_collapses_at_public_export_seam(self, runner):
        """A GraphNode and its first child run export one physical span."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        inner = Graph([double], name="inner_definition")
        outer = Graph([inner.as_node(name="score")], name="outer")
        processor = OpenTelemetryProcessor(tracer_provider=self.provider)
        if isinstance(runner, AsyncRunner):
            result = asyncio.run(runner.run(outer, {"x": 2}, workflow_id="outer-wf", event_processors=[processor]))
        else:
            result = runner.run(outer, {"x": 2}, workflow_id="outer-wf", event_processors=[processor])

        assert result.completed
        spans = self.exporter.get_finished_spans()
        assert [span.name for span in spans] == ["double", "score", "outer"]
        score = next(span for span in spans if span.name == "score")
        attrs = dict(score.attributes)
        assert attrs["hypergraph.span.role"] == "graph"
        assert attrs["hypergraph.node_name"] == "score"
        assert attrs["hypergraph.graph_name"] == "outer"
        assert attrs["hypergraph.nested.graph_name"] == "inner_definition"
        assert attrs["hypergraph.nested.workflow_id"]
        assert attrs["hypergraph.nested.duration_ms"] >= 0
        double_span = next(span for span in spans if span.name == "double")
        assert double_span.parent.span_id == score.context.span_id

    @pytest.mark.parametrize("runner_type", [SyncRunner, AsyncRunner])
    @pytest.mark.parametrize("depth", [1, 3])
    def test_flat_and_deep_span_shape_matrix(self, runner_type, depth):
        """Sync and async exports have one physical span per user level."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        graph = Graph([double], name="level_0")
        expected = ["double"]
        for level in range(1, depth):
            node_name = f"level_{level}_node"
            graph = Graph([graph.as_node(name=node_name)], name=f"level_{level}")
            expected.append(node_name)
        processor = OpenTelemetryProcessor(tracer_provider=self.provider)
        runner = runner_type()
        if runner_type is AsyncRunner:
            result = asyncio.run(runner.run(graph, {"x": 2}, event_processors=[processor]))
        else:
            result = runner.run(graph, {"x": 2}, event_processors=[processor])

        assert result.completed
        spans = self.exporter.get_finished_spans()
        assert len(spans) == depth + 1
        assert {span.name for span in spans} == {*expected, f"level_{depth - 1}"}
        by_name = {span.name: span for span in spans}
        chain = ["double", *expected[1:], f"level_{depth - 1}"]
        for child_name, parent_name in zip(chain, chain[1:], strict=False):
            assert by_name[child_name].parent.span_id == by_name[parent_name].context.span_id
        assert by_name[f"level_{depth - 1}"].attributes["hypergraph.span.role"] == "graph"
        assert all(by_name[name].attributes["hypergraph.span.role"] == "graph" for name in expected[1:])

    @pytest.mark.parametrize("runner_type", [SyncRunner, AsyncRunner])
    @pytest.mark.parametrize("named", [True, False])
    def test_top_map_shape_matrix_including_unnamed(self, runner_type, named):
        from hypergraph.events.otel import OpenTelemetryProcessor

        graph = Graph([double], name="mapped" if named else None)
        processor = OpenTelemetryProcessor(tracer_provider=self.provider)
        runner = runner_type()
        kwargs = dict(map_over="x", event_processors=[processor])
        result = asyncio.run(runner.map(graph, {"x": [1, 2]}, **kwargs)) if runner_type is AsyncRunner else runner.map(graph, {"x": [1, 2]}, **kwargs)

        assert result.completed
        spans = self.exporter.get_finished_spans()
        root_name = "mapped" if named else "graph"
        item_name = f"{root_name}.item"
        assert len(spans) == 5
        root = next(span for span in spans if span.name == root_name)
        items = [span for span in spans if span.name == item_name]
        leaves = [span for span in spans if span.name == "double"]
        assert root.attributes["hypergraph.span.role"] == "map"
        assert len(items) == len(leaves) == 2
        assert {span.attributes["hypergraph.span.role"] for span in items} == {"graph"}
        for item in items:
            assert item.parent.span_id == root.context.span_id
            leaf = next(span for span in leaves if span.attributes["hypergraph.item_index"] == item.attributes["hypergraph.item_index"])
            assert leaf.parent.span_id == item.context.span_id

    @pytest.mark.parametrize("runner_type", [SyncRunner, AsyncRunner])
    def test_map_over_nested_shape_matrix(self, runner_type):
        from hypergraph.events.otel import OpenTelemetryProcessor

        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node(name="nested")], name="outer")
        processor = OpenTelemetryProcessor(tracer_provider=self.provider)
        runner = runner_type()
        kwargs = dict(map_over="x", event_processors=[processor])
        result = asyncio.run(runner.map(outer, {"x": [1, 2]}, **kwargs)) if runner_type is AsyncRunner else runner.map(outer, {"x": [1, 2]}, **kwargs)

        assert result.completed
        spans = self.exporter.get_finished_spans()
        assert len(spans) == 7
        root = next(span for span in spans if span.name == "outer")
        items = [span for span in spans if span.name == "outer.item"]
        nested = [span for span in spans if span.name == "nested"]
        leaves = [span for span in spans if span.name == "double"]
        assert root.attributes["hypergraph.span.role"] == "map"
        assert len(items) == len(nested) == len(leaves) == 2
        for item in items:
            index = item.attributes["hypergraph.item_index"]
            owner = next(span for span in nested if span.attributes["hypergraph.item_index"] == index)
            leaf = next(span for span in leaves if span.attributes["hypergraph.item_index"] == index)
            assert item.parent.span_id == root.context.span_id
            assert owner.parent.span_id == item.context.span_id
            assert leaf.parent.span_id == owner.context.span_id
            assert owner.attributes["hypergraph.span.role"] == "graph"

    @pytest.mark.parametrize("runner_type", [SyncRunner, AsyncRunner])
    def test_nested_terminal_failure_shape_matrix(self, runner_type):
        from hypergraph.events.otel import OpenTelemetryProcessor

        inner = Graph([unstable], name="inner")
        outer = Graph([inner.as_node(name="nested")], name="outer")
        processor = OpenTelemetryProcessor(tracer_provider=self.provider)
        runner = runner_type()
        with pytest.raises(ValueError, match="boom"):
            if runner_type is AsyncRunner:
                asyncio.run(runner.run(outer, {"x": 2}, event_processors=[processor]))
            else:
                runner.run(outer, {"x": 2}, event_processors=[processor])

        spans = self.exporter.get_finished_spans()
        assert len(spans) == 3
        nested = next(span for span in spans if span.name == "nested")
        leaf = next(span for span in spans if span.name == "unstable")
        root = next(span for span in spans if span.name == "outer")
        assert nested.parent.span_id == root.context.span_id
        assert leaf.parent.span_id == nested.context.span_id
        assert nested.attributes["hypergraph.span.role"] == "graph"
        assert nested.status.status_code == StatusCode.ERROR
        assert len([event for event in nested.events if event.name == "exception"]) == 1

    def test_async_overlapping_sibling_graph_nodes_collapse_independently(self):
        from hypergraph.events.otel import OpenTelemetryProcessor

        entered = 0
        rendezvous = asyncio.Event()

        def child(label):
            @node(output_name=f"{label}_out")
            async def work(x: int) -> int:
                nonlocal entered
                entered += 1
                if entered == 2:
                    rendezvous.set()
                await asyncio.wait_for(rendezvous.wait(), timeout=5)
                return x

            return Graph([work], name=f"{label}_graph").as_node(name=label)

        processor = OpenTelemetryProcessor(tracer_provider=self.provider)
        result = asyncio.run(AsyncRunner().run(Graph([child("left"), child("right")], name="outer"), {"x": 1}, event_processors=[processor]))
        assert result.completed
        spans = self.exporter.get_finished_spans()
        assert len(spans) == 5
        root = next(span for span in spans if span.name == "outer")
        for owner_name in ("left", "right"):
            owners = [span for span in spans if span.name == owner_name]
            assert len(owners) == 1
            owner = owners[0]
            assert owner.attributes["hypergraph.span.role"] == "graph"
            assert owner.parent.span_id == root.context.span_id
        leaves = [span for span in spans if span.name == "work"]
        assert len(leaves) == 2
        assert {leaf.parent.span_id for leaf in leaves} == {
            next(span for span in spans if span.name == "left").context.span_id,
            next(span for span in spans if span.name == "right").context.span_id,
        }

    def test_concurrent_nested_map_items_keep_third_party_span_parenting(self):
        from hypergraph.events.otel import OpenTelemetryProcessor

        tracer = self.provider.get_tracer("test.nested-map")
        entered = 0
        rendezvous = asyncio.Event()

        @node(output_name="y")
        async def work(x: int) -> int:
            nonlocal entered
            entered += 1
            if entered == 2:
                rendezvous.set()
            await asyncio.wait_for(rendezvous.wait(), timeout=5)
            with tracer.start_as_current_span("third-party") as span:
                span.set_attribute("test.x", x)
            return x

        graph = Graph([Graph([work], name="inner").as_node(name="nested")], name="outer")
        processor = OpenTelemetryProcessor(tracer_provider=self.provider)
        result = asyncio.run(AsyncRunner().map(graph, {"x": [10, 20]}, map_over="x", event_processors=[processor]))
        assert result.completed
        spans = self.exporter.get_finished_spans()
        owners = {span.attributes["hypergraph.item_index"]: span for span in spans if span.name == "nested"}
        leaves = {span.attributes["hypergraph.item_index"]: span for span in spans if span.name == "work"}
        third_party = {span.attributes["test.x"]: span for span in spans if span.name == "third-party"}
        assert set(owners) == set(leaves) == {0, 1}
        assert set(third_party) == {10, 20}
        for index, x in enumerate((10, 20)):
            assert leaves[index].parent.span_id == owners[index].context.span_id
            assert third_party[x].parent.span_id == leaves[index].context.span_id
            assert third_party[x].parent.span_id != leaves[1 - index].context.span_id

    def test_async_nested_interrupt_pause_shape(self):
        from hypergraph.events.otel import OpenTelemetryProcessor

        @interrupt(answer_name="decision")
        def approval(draft: str) -> StringQuestion:
            return StringQuestion(prompt="Approve?", evidence=(draft,))

        graph = Graph([Graph([approval], name="inner").as_node(name="nested")], name="outer")
        result = asyncio.run(AsyncRunner().run(graph, {"draft": "v1"}, event_processors=[OpenTelemetryProcessor(tracer_provider=self.provider)]))
        assert result.paused
        spans = self.exporter.get_finished_spans()
        assert len(spans) == 3
        root = next(span for span in spans if span.name == "outer")
        owner = next(span for span in spans if span.name == "nested")
        leaf = next(span for span in spans if span.name == "approval")
        assert owner.attributes["hypergraph.span.role"] == "graph"
        assert owner.attributes["hypergraph.run.outcome"] == "paused"
        assert [event.name for event in owner.events].count("hypergraph.pause") == 1
        assert owner.parent.span_id == root.context.span_id
        assert leaf.parent.span_id == owner.context.span_id

    def test_eventless_delegation_keeps_node_owner(self):
        from hypergraph.events.otel import OpenTelemetryProcessor

        class EventlessRunner:
            """Compatible delegated runner that deliberately forwards no events."""

            @property
            def capabilities(self):
                return SyncRunner().capabilities

            def run(self, graph, values=None, **kwargs):
                for key in (
                    "event_processors",
                    "show_progress",
                    "workflow_id",
                    "_parent_span_id",
                    "_parent_run_id",
                    "_item_index",
                ):
                    kwargs.pop(key, None)
                return SyncRunner().run(graph, values, **kwargs)

        delegated = Graph([double], name="inner").as_node(name="delegated").with_runner(EventlessRunner())
        result = SyncRunner().run(
            Graph([delegated], name="outer"),
            {"x": 2},
            event_processors=[OpenTelemetryProcessor(tracer_provider=self.provider)],
        )
        assert result.completed
        spans = self.exporter.get_finished_spans()
        assert {span.name for span in spans} == {"outer", "delegated"}
        owner = next(span for span in spans if span.name == "delegated")
        assert owner.attributes["hypergraph.span.role"] == "node"
        assert not any(key.startswith("hypergraph.nested.") for key in owner.attributes)

    def test_nested_graph_works_with_public_noop_provider(self):
        """Collapse does not read SDK-private properties from live spans."""
        from opentelemetry import trace

        from hypergraph.events.otel import OpenTelemetryProcessor

        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node(name="nested")], name="outer")
        result = SyncRunner().run(
            outer,
            {"x": 2},
            event_processors=[OpenTelemetryProcessor(tracer_provider=trace.NoOpTracerProvider())],
        )
        assert result.completed

    def test_checkpoint_restored_child_without_run_start_keeps_node_owner(self, sqlite_checkpointer):
        from hypergraph.events.otel import OpenTelemetryProcessor

        runner = SyncRunner(checkpointer=sqlite_checkpointer)
        inner = Graph([double], name="inner")
        runner.run(inner, {"x": 2}, workflow_id="outer/nested", _parent_run_id="outer")
        graph = Graph([inner.as_node(name="nested")], name="outer")
        result = runner.run(graph, {"x": 2}, workflow_id="outer", event_processors=[OpenTelemetryProcessor(tracer_provider=self.provider)])
        assert result.completed
        spans = self.exporter.get_finished_spans()
        assert {span.name for span in spans} == {"outer", "nested"}
        owner = next(span for span in spans if span.name == "nested")
        assert owner.attributes["hypergraph.span.role"] == "node"
        assert not any(key.startswith("hypergraph.nested.") for key in owner.attributes)

    def test_owner_absorbs_only_one_sequential_child_run(self):
        """An ended alias is released, but its live owner remains claimed."""
        from hypergraph.events.otel import OpenTelemetryProcessor
        from hypergraph.events.types import NodeEndEvent, NodeStartEvent, RunEndEvent, RunStartEvent

        processor = OpenTelemetryProcessor(tracer_provider=self.provider)
        processor.on_node_start(NodeStartEvent(run_id="outer", span_id="owner", node_name="delegated", graph_name="outer"))
        for child in ("first", "second"):
            processor.on_run_start(RunStartEvent(run_id=child, span_id=child, parent_span_id="owner", workflow_id=child, graph_name=child))
            processor.on_run_end(RunEndEvent(run_id=child, span_id=child, workflow_id=child, graph_name=child))
        processor.on_node_end(NodeEndEvent(run_id="outer", span_id="owner", node_name="delegated", graph_name="outer"))

        spans = self.exporter.get_finished_spans()
        assert len(spans) == 2
        owner = next(span for span in spans if span.name == "delegated")
        second = next(span for span in spans if span.name == "second")
        assert owner.attributes["hypergraph.nested.graph_name"] == "first"
        assert second.parent.span_id == owner.context.span_id

    def test_shutdown_after_absorbed_start_exports_owner_once(self, caplog):
        from hypergraph.events.otel import OpenTelemetryProcessor
        from hypergraph.events.types import NodeStartEvent, RunStartEvent

        processor = OpenTelemetryProcessor(tracer_provider=self.provider)
        with caplog.at_level("WARNING"):
            processor.on_node_start(NodeStartEvent(run_id="run", span_id="owner", node_name="nested", graph_name="outer"))
            processor.on_run_start(RunStartEvent(run_id="child", span_id="alias", parent_span_id="owner", graph_name="inner"))
            processor.shutdown()
        assert len(self.exporter.get_finished_spans()) == 1
        assert not [record for record in caplog.records if "ended span" in record.getMessage().lower()]

    @pytest.mark.parametrize("outcome", ["partial", "stopped"])
    def test_success_opt_in_does_not_mark_incomplete_collapsed_run_ok(self, outcome):
        from hypergraph.events.otel import OpenTelemetryProcessor
        from hypergraph.events.types import NodeEndEvent, NodeStartEvent, RunEndEvent, RunStartEvent

        processor = OpenTelemetryProcessor(tracer_provider=self.provider, set_success_status=True)
        processor.on_node_start(
            NodeStartEvent(
                run_id="outer",
                span_id="owner",
                workflow_id="outer-wf",
                node_name="nested",
                graph_name="outer",
            )
        )
        processor.on_run_start(
            RunStartEvent(
                run_id="inner",
                span_id="alias",
                parent_span_id="owner",
                workflow_id="inner-wf",
                graph_name="inner",
                is_map=True,
                map_size=4,
            )
        )
        processor.on_run_end(
            RunEndEvent(
                run_id="inner",
                span_id="alias",
                workflow_id="inner-wf",
                graph_name="inner",
                status=outcome,
                duration_ms=8.5,
                batch_total_items=4,
                batch_completed_items=2,
                batch_failed_items=1,
                batch_paused_items=0,
                batch_stopped_items=1,
                batch_restored_items=1,
                batch_outcome=outcome,
            )
        )
        processor.on_node_end(NodeEndEvent(run_id="outer", span_id="owner", node_name="nested", graph_name="outer"))

        span = next(span for span in self.exporter.get_finished_spans() if span.name == "nested")
        assert span.status.status_code != StatusCode.OK
        attrs = dict(span.attributes)
        assert attrs["hypergraph.span.role"] == "map"
        assert attrs["hypergraph.is_map"] is True
        assert attrs["hypergraph.map_size"] == 4
        assert attrs["hypergraph.nested.duration_ms"] == 8.5
        assert attrs["hypergraph.run_id"] == "outer"
        assert attrs["hypergraph.workflow_id"] == "outer-wf"
        assert attrs["hypergraph.nested.run_id"] == "inner"
        assert attrs["hypergraph.nested.workflow_id"] == "inner-wf"
        assert attrs["hypergraph.nested.outcome"] == outcome
        assert {
            key: attrs[f"hypergraph.batch.{key}"]
            for key in ("total_items", "completed_items", "failed_items", "paused_items", "stopped_items", "restored_items")
        } == {
            "total_items": 4,
            "completed_items": 2,
            "failed_items": 1,
            "paused_items": 0,
            "stopped_items": 1,
            "restored_items": 1,
        }

    def test_success_opt_in_requires_absorbed_run_completion(self):
        from hypergraph.events.otel import OpenTelemetryProcessor
        from hypergraph.events.types import NodeEndEvent, NodeStartEvent, RunStartEvent

        processor = OpenTelemetryProcessor(
            tracer_provider=self.provider,
            set_success_status=True,
        )
        processor.on_node_start(
            NodeStartEvent(
                run_id="outer",
                span_id="owner",
                node_name="nested",
                graph_name="outer",
            )
        )
        processor.on_run_start(
            RunStartEvent(
                run_id="inner",
                span_id="alias",
                parent_span_id="owner",
                graph_name="inner",
            )
        )
        processor.on_node_end(
            NodeEndEvent(
                run_id="outer",
                span_id="owner",
                node_name="nested",
                graph_name="outer",
            )
        )

        span = next(span for span in self.exporter.get_finished_spans() if span.name == "nested")
        assert span.status.status_code == StatusCode.UNSET

    def test_failed_run_without_error_projection_is_still_error(self):
        from hypergraph.events.otel import OpenTelemetryProcessor
        from hypergraph.events.types import RunEndEvent, RunStartEvent, RunStatus

        processor = OpenTelemetryProcessor(tracer_provider=self.provider)
        processor.on_run_start(RunStartEvent(run_id="failed", span_id="failed", graph_name="failed"))
        processor.on_run_end(
            RunEndEvent(
                run_id="failed",
                span_id="failed",
                graph_name="failed",
                status=RunStatus.FAILED,
            )
        )

        span = next(span for span in self.exporter.get_finished_spans() if span.name == "failed")
        assert span.status.status_code == StatusCode.ERROR
        assert [event for event in span.events if event.name == "exception"] == []

    def test_completed_nested_run_then_owner_error_is_error_once(self):
        from hypergraph.events.otel import OpenTelemetryProcessor
        from hypergraph.events.types import NodeErrorEvent, NodeStartEvent, RunEndEvent, RunStartEvent

        processor = OpenTelemetryProcessor(tracer_provider=self.provider, set_success_status=True)
        processor.on_node_start(NodeStartEvent(run_id="outer", span_id="owner", node_name="nested", graph_name="outer"))
        processor.on_run_start(RunStartEvent(run_id="inner", span_id="alias", parent_span_id="owner", graph_name="inner"))
        processor.on_run_end(RunEndEvent(run_id="inner", span_id="alias", graph_name="inner"))
        processor.on_node_error(
            NodeErrorEvent(
                run_id="outer",
                span_id="owner",
                node_name="nested",
                graph_name="outer",
                error="nested execution failed",
                error_type="builtins.ValueError",
                diagnostic=SimpleNamespace(code="HG999", docs_ref="errors#hg999"),
            )
        )

        span = next(span for span in self.exporter.get_finished_spans() if span.name == "nested")
        assert span.status.status_code == StatusCode.ERROR
        assert len([event for event in span.events if event.name == "exception"]) == 1
        assert span.attributes["hypergraph.error_type"] == "builtins.ValueError"
        assert span.attributes["hypergraph.diagnostic.code"] == "HG999"
        assert span.attributes["hypergraph.diagnostic.docs_ref"] == "errors#hg999"

    @pytest.mark.parametrize(
        ("relationship", "start_kwargs", "event_name"),
        [
            ("resume", {"is_resume": True}, "hypergraph.resume"),
            ("fork", {"forked_from": "source", "fork_superstep": 2}, "hypergraph.fork"),
            ("retry", {"retry_of": "source", "retry_index": 3}, "hypergraph.retry"),
        ],
    )
    def test_collapsed_lineage_keeps_link_and_event(self, relationship, start_kwargs, event_name):
        from hypergraph.events.otel import OpenTelemetryProcessor
        from hypergraph.events.types import NodeEndEvent, NodeStartEvent, RunEndEvent, RunStartEvent

        processor = OpenTelemetryProcessor(tracer_provider=self.provider)
        processor.on_run_start(RunStartEvent(run_id="source", span_id="source", workflow_id="source", graph_name="source"))
        processor.on_run_end(RunEndEvent(run_id="source", span_id="source", workflow_id="source", graph_name="source"))
        processor.on_node_start(NodeStartEvent(run_id="outer", span_id="owner", node_name="nested", graph_name="outer"))
        workflow_id = "source" if relationship == "resume" else "derived"
        processor.on_run_start(
            RunStartEvent(
                run_id="inner",
                span_id="alias",
                parent_span_id="owner",
                workflow_id=workflow_id,
                graph_name="inner",
                **start_kwargs,
            )
        )
        processor.on_run_end(RunEndEvent(run_id="inner", span_id="alias", workflow_id=workflow_id, graph_name="inner"))
        processor.on_node_end(NodeEndEvent(run_id="outer", span_id="owner", node_name="nested", graph_name="outer"))

        span = next(span for span in self.exporter.get_finished_spans() if span.name == "nested")
        assert len(span.links) == 1
        assert span.links[0].attributes["hypergraph.lineage.relationship"] == relationship
        assert event_name in [event.name for event in span.events]

    def test_public_auxiliary_events_are_projected_on_the_live_span(self):
        from hypergraph.events.otel import OpenTelemetryProcessor
        from hypergraph.events.types import (
            CacheHitEvent,
            NodeAttemptEndEvent,
            NodeAttemptStartEvent,
            NodeEndEvent,
            NodeStartEvent,
            RouteDecisionEvent,
            StopRequestedEvent,
            SuperstepStartEvent,
        )

        processor = OpenTelemetryProcessor(tracer_provider=self.provider)
        processor.on_node_start(NodeStartEvent(run_id="run", span_id="node", node_name="work", graph_name="flow"))
        common = {"run_id": "run", "parent_span_id": "node", "node_name": "work", "graph_name": "flow"}
        processor.on_node_attempt_start(
            NodeAttemptStartEvent(**common, attempt_series_id="series", attempt_number=1, max_attempts=2, timeout_seconds=1.0)
        )
        processor.on_node_attempt_end(
            NodeAttemptEndEvent(
                **common,
                attempt_series_id="series",
                attempt_number=1,
                outcome="failed",
                settlement="raised",
                duration_ms=2.0,
                error_type="ValueError",
                retry_scheduled=True,
            )
        )
        processor.on_superstep_start(SuperstepStartEvent(run_id="run", parent_span_id="node", graph_name="flow", superstep=1))
        processor.on_route_decision(RouteDecisionEvent(**common, node_span_id="node", decision=["left", "right"], superstep=1))
        processor.on_cache_hit(CacheHitEvent(run_id="run", span_id="node", node_name="work", graph_name="flow", cache_key="k"))
        processor.on_stop_requested(StopRequestedEvent(run_id="run", span_id="node", graph_name="flow"))
        processor.on_node_end(NodeEndEvent(run_id="run", span_id="node", node_name="work", graph_name="flow"))

        span = next(span for span in self.exporter.get_finished_spans() if span.name == "work")
        names = [event.name for event in span.events]
        assert names == [
            "hypergraph.attempt.start",
            "hypergraph.attempt.end",
            "hypergraph.superstep.start",
            "hypergraph.route.decision",
            "hypergraph.cache.hit",
            "hypergraph.stop.requested",
        ]
        route = next(event for event in span.events if event.name == "hypergraph.route.decision")
        assert route.attributes["hypergraph.decision"] == "left,right"

    def test_status_openinference_and_spoofing_are_opt_in(self):
        from hypergraph.events.otel import OpenTelemetryProcessor

        processor = OpenTelemetryProcessor(
            tracer_provider=self.provider,
            set_success_status=True,
            enrich_openinference=True,
            extra_attributes={
                "hypergraph.span.role": "spoofed",
                "hypergraph.node_name": "spoofed",
                "hypergraph.nested.graph_name": "spoofed",
            },
        )
        inner = Graph([double], name="true-inner")
        SyncRunner().run(Graph([inner.as_node(name="nested")], name="honest"), {"x": 2}, event_processors=[processor])

        spans = self.exporter.get_finished_spans()
        assert all(span.status.status_code == StatusCode.OK for span in spans)
        root = next(span for span in spans if span.name == "honest")
        leaf = next(span for span in spans if span.name == "nested")
        assert root.attributes["hypergraph.span.role"] == "graph"
        assert "hypergraph.node_name" not in root.attributes
        assert leaf.attributes["hypergraph.span.role"] == "graph"
        assert leaf.attributes["hypergraph.node_name"] == "nested"
        assert leaf.attributes["hypergraph.nested.graph_name"] == "true-inner"
        assert root.attributes["openinference.span.kind"] == "CHAIN"
        assert leaf.attributes["graph.node.parent_id"] == "honest"

    def test_map_parent_span_uses_batch_summary_attributes(self):
        """Parent map spans export bounded aggregate outcome metadata."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        graph = Graph([unstable])
        processor = OpenTelemetryProcessor()
        results = SyncRunner().map(
            graph,
            {"x": [1, 2, 3]},
            map_over="x",
            error_handling="continue",
            event_processors=[processor],
        )

        assert results.partial
        spans = self.exporter.get_finished_spans()
        map_span = next(span for span in spans if span.attributes.get("hypergraph.is_map") is True)
        attrs = dict(map_span.attributes)
        assert attrs["hypergraph.batch.total_items"] == 3
        assert attrs["hypergraph.batch.completed_items"] == 2
        assert attrs["hypergraph.batch.failed_items"] == 1
        assert attrs["hypergraph.batch.restored_items"] == 0
        assert attrs["hypergraph.batch.outcome"] == "partial"
        assert map_span.status.status_code == StatusCode.UNSET

        child_item_spans = [span for span in spans if span.name == "graph.item"]
        assert sorted(span.attributes["hypergraph.item_index"] for span in child_item_spans) == [0, 1, 2]

    def test_restored_batch_subset_is_exported(self, sqlite_checkpointer):
        """A fully restored map keeps completed inclusive and exports its subset."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        runner = SyncRunner(checkpointer=sqlite_checkpointer)
        graph = Graph([double])
        runner.map(graph, {"x": [1, 2]}, map_over="x", workflow_id="otel-restored")

        processor = OpenTelemetryProcessor()
        resumed = runner.map(
            graph,
            {"x": [1, 2]},
            map_over="x",
            workflow_id="otel-restored",
            event_processors=[processor],
        )

        assert resumed.restored_count == 2
        spans = self.exporter.get_finished_spans()
        map_span = next(span for span in spans if span.attributes.get("hypergraph.is_map") is True)
        attrs = dict(map_span.attributes)
        assert attrs["hypergraph.batch.completed_items"] == 2
        assert attrs["hypergraph.batch.restored_items"] == 2

    def test_forked_run_adds_lineage_link_when_source_span_is_known(self, sqlite_checkpointer):
        """Forked runs link back to the source run span when both are in-process."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        processor = OpenTelemetryProcessor()
        runner = SyncRunner(checkpointer=sqlite_checkpointer)

        root = runner.run(Graph([double]), {"x": 5}, workflow_id="wf-root", event_processors=[processor])
        fork = runner.run(
            Graph([double]),
            {"x": 9},
            workflow_id="wf-root-fork",
            fork_from="wf-root",
            event_processors=[processor],
        )

        assert root.completed
        assert fork.completed
        spans = self.exporter.get_finished_spans()
        fork_span = next(span for span in spans if span.name == "graph" and span.attributes.get("hypergraph.workflow_id") == "wf-root-fork")
        assert len(fork_span.links) == 1
        link_attrs = dict(fork_span.links[0].attributes or {})
        assert link_attrs["hypergraph.lineage.relationship"] == "fork"

        event_names = [event.name for event in fork_span.events]
        assert "hypergraph.fork" in event_names

    def test_evicted_lineage_context_does_not_create_stale_link(self, sqlite_checkpointer, monkeypatch):
        """Eviction should degrade to no lineage link instead of reusing stale context."""
        import hypergraph.events.otel as otel_module

        monkeypatch.setattr(otel_module, "_MAX_LINEAGE_CONTEXTS", 1)

        processor = otel_module.OpenTelemetryProcessor()
        runner = SyncRunner(checkpointer=sqlite_checkpointer)

        runner.run(Graph([double]), {"x": 1}, workflow_id="wf-root-1", event_processors=[processor])
        runner.run(Graph([double]), {"x": 2}, workflow_id="wf-root-2", event_processors=[processor])
        fork = runner.run(
            Graph([double]),
            {"x": 3},
            workflow_id="wf-root-1-fork",
            fork_from="wf-root-1",
            event_processors=[processor],
        )

        assert fork.completed
        spans = self.exporter.get_finished_spans()
        fork_span = next(span for span in spans if span.name == "graph" and span.attributes.get("hypergraph.workflow_id") == "wf-root-1-fork")
        assert len(fork_span.links) == 0
        event_names = [event.name for event in fork_span.events]
        assert "hypergraph.fork" in event_names

    def test_interrupt_fallback_does_not_end_run_span_early(self):
        """Fallback interrupt span ids must not prevent paused run metadata from exporting."""
        from hypergraph.events.otel import OpenTelemetryProcessor
        from hypergraph.events.types import InterruptEvent, RunEndEvent, RunStartEvent, RunStatus

        processor = OpenTelemetryProcessor()
        processor.on_run_start(
            RunStartEvent(
                run_id="run-sync-pause",
                span_id="run-span",
                workflow_id="wf-sync-pause",
                graph_name="approval_flow",
            )
        )
        processor.on_interrupt(
            InterruptEvent(
                run_id="run-sync-pause",
                span_id="run-span",
                parent_span_id="run-span",
                workflow_id="wf-sync-pause",
                node_name="approval",
                graph_name="approval_flow",
                response_param="decision",
            )
        )
        processor.on_run_end(
            RunEndEvent(
                run_id="run-sync-pause",
                span_id="run-span",
                workflow_id="wf-sync-pause",
                graph_name="approval_flow",
                status=RunStatus.PAUSED,
                duration_ms=12.5,
            )
        )

        spans = self.exporter.get_finished_spans()
        run_span = next(span for span in spans if span.name == "approval_flow")
        attrs = dict(run_span.attributes)
        assert attrs["hypergraph.run.outcome"] == "paused"
        assert attrs["hypergraph.duration_ms"] == 12.5
