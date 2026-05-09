"""Tests for the OpenTelemetryProcessor.

The import guard test always runs. SDK-dependent tests are skipped
if opentelemetry-sdk is not installed.
"""

import asyncio

import pytest

from hypergraph import AsyncRunner, Graph, NodeContext, SyncRunner, interrupt, node

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

        with pytest.raises(ImportError, match="pip install 'hypergraph\\[otel\\]'"):
            _require_opentelemetry()


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled + doubled // 2


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

    @staticmethod
    def _span_id(span) -> str:
        return format(span.context.span_id, "016x")

    @staticmethod
    def _parent_span_id(span) -> str | None:
        return None if span.parent is None else format(span.parent.span_id, "016x")

    def _find_span(self, spans, *, name=None, attr_key=None, attr_value=None):
        for span in spans:
            if name is not None and span.name != name:
                continue
            if attr_key is not None and span.attributes.get(attr_key) != attr_value:
                continue
            return span
        raise AssertionError(f"Span not found: name={name!r}, {attr_key!r}={attr_value!r}")

    def test_sync_run_exports_run_and_node_semantics(self):
        """Sync runs export graph/node spans with explicit Hypergraph attributes."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        graph = Graph([double, triple], name="math")
        processor = OpenTelemetryProcessor()
        result = SyncRunner().run(graph, {"x": 10}, workflow_id="wf-math", event_processors=[processor])

        assert result.completed
        spans = self.exporter.get_finished_spans()
        run_span = self._find_span(spans, name="graph math")
        double_span = self._find_span(spans, name="node double")
        triple_span = self._find_span(spans, name="node triple")

        assert run_span.attributes["hypergraph.workflow_id"] == "wf-math"
        assert run_span.attributes["hypergraph.run.kind"] == "graph"
        assert run_span.attributes["hypergraph.status"] == "completed"
        assert any(event.name == "hypergraph.superstep.start" for event in run_span.events)

        assert double_span.attributes["hypergraph.node_name"] == "double"
        assert double_span.attributes["hypergraph.workflow_id"] == "wf-math"
        assert double_span.attributes["hypergraph.superstep"] == 0
        assert self._parent_span_id(double_span) == self._span_id(run_span)
        assert self._parent_span_id(triple_span) == self._span_id(run_span)

    def test_failure_marks_node_and_run_spans(self):
        """Failures set ERROR status on both the node span and the run span."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        @node(output_name="boom")
        def explode(doubled: int) -> int:
            raise ValueError("boom")

        graph = Graph([double, explode], name="failure_graph")
        processor = OpenTelemetryProcessor()
        result = SyncRunner().run(graph, {"x": 5}, workflow_id="wf-fail", error_handling="continue", event_processors=[processor])

        assert result.failed
        spans = self.exporter.get_finished_spans()
        run_span = self._find_span(spans, name="graph failure_graph")
        node_span = self._find_span(spans, name="node explode")

        assert node_span.status.status_code is StatusCode.ERROR
        assert run_span.status.status_code is StatusCode.ERROR
        assert node_span.attributes["error.type"] == "builtins.ValueError"
        assert any(event.name == "exception" for event in node_span.events)
        assert run_span.attributes["hypergraph.status"] == "failed"

    @pytest.mark.asyncio
    async def test_nested_graph_creates_child_run_span(self):
        """Nested graph runs are exported as child run spans under the GraphNode span."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        inner = Graph([double], name="inner")

        @node(output_name="seed")
        def passthrough(x: int) -> int:
            return x

        outer = Graph([passthrough, inner.as_node().with_inputs(x="seed")], name="outer")
        processor = OpenTelemetryProcessor()
        result = await AsyncRunner().run(outer, {"x": 4}, workflow_id="wf-nested", event_processors=[processor])

        assert result.completed
        spans = self.exporter.get_finished_spans()
        outer_run = self._find_span(spans, name="graph outer")
        inner_node = self._find_span(spans, name="node inner")
        inner_run = self._find_span(spans, name="graph inner")

        assert inner_run.attributes["hypergraph.workflow_id"] == "wf-nested/inner"
        assert inner_run.attributes["hypergraph.parent_workflow_id"] == "wf-nested"
        assert self._parent_span_id(inner_node) == self._span_id(outer_run)
        assert self._parent_span_id(inner_run) == self._span_id(inner_node)

    @pytest.mark.asyncio
    async def test_pause_and_resume_emit_lifecycle_events(self):
        """Paused/resumed runs export explicit pause and resume semantics."""
        from hypergraph.checkpointers import MemoryCheckpointer
        from hypergraph.events.otel import OpenTelemetryProcessor

        @interrupt(output_name="decision")
        def approval(draft: str) -> str | None:
            return None

        graph = Graph([approval], name="review")
        checkpointer = MemoryCheckpointer()
        runner = AsyncRunner(checkpointer=checkpointer)
        processor = OpenTelemetryProcessor()

        paused = await runner.run(graph, {"draft": "hello"}, workflow_id="wf-review", event_processors=[processor])
        assert paused.paused

        spans = self.exporter.get_finished_spans()
        paused_run = self._find_span(spans, name="graph review")
        paused_node = self._find_span(spans, name="node approval")
        assert paused_run.attributes["hypergraph.status"] == "paused"
        assert paused_node.attributes["hypergraph.status"] == "paused"
        assert any(event.name == "hypergraph.pause" for event in paused_node.events)

        self.exporter.clear()
        resumed = await runner.run(
            graph,
            {paused.pause.response_key: "approved"},
            workflow_id="wf-review",
            event_processors=[processor],
        )
        assert resumed.completed

        resumed_spans = self.exporter.get_finished_spans()
        resumed_run = self._find_span(resumed_spans, name="graph review")
        assert resumed_run.attributes["hypergraph.is_resume"] is True
        assert any(event.name == "hypergraph.resume" for event in resumed_run.events)

    @pytest.mark.asyncio
    async def test_stop_marks_run_and_emits_stop_event(self):
        """Stopped runs keep a distinct run status and stop-requested event."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        @node(output_name="result")
        async def slow_node(ctx: NodeContext) -> str:
            for _ in range(100):
                if ctx.stop_requested:
                    return "stopped"
                await asyncio.sleep(0.001)
            return "done"

        runner = AsyncRunner()
        processor = OpenTelemetryProcessor()

        async def stop_soon():
            await asyncio.sleep(0.01)
            runner.stop("wf-stop", info={"kind": "user_stop"})

        stop_task = asyncio.create_task(stop_soon())
        result = await runner.run(Graph([slow_node], name="stopper"), workflow_id="wf-stop", event_processors=[processor])
        await stop_task

        assert result.stopped
        spans = self.exporter.get_finished_spans()
        run_span = self._find_span(spans, name="graph stopper")
        assert run_span.attributes["hypergraph.status"] == "stopped"
        stop_events = [event for event in run_span.events if event.name == "hypergraph.stop.requested"]
        assert len(stop_events) == 1
        assert stop_events[0].attributes["hypergraph.stop.kind"] == "user_stop"

    @pytest.mark.asyncio
    async def test_fork_emits_lineage_metadata(self):
        """Forked runs export explicit lineage attributes and lifecycle events."""
        from hypergraph.checkpointers import MemoryCheckpointer
        from hypergraph.events.otel import OpenTelemetryProcessor

        graph = Graph([double], name="math")
        checkpointer = MemoryCheckpointer()
        runner = AsyncRunner(checkpointer=checkpointer)
        processor = OpenTelemetryProcessor()

        await runner.run(graph, {"x": 5}, workflow_id="wf-source")
        self.exporter.clear()

        forked = await runner.run(graph, {"x": 5}, fork_from="wf-source", workflow_id="wf-fork", event_processors=[processor])
        assert forked.completed

        spans = self.exporter.get_finished_spans()
        fork_run = self._find_span(spans, name="graph math")
        assert fork_run.attributes["hypergraph.forked_from"] == "wf-source"
        assert fork_run.attributes["hypergraph.is_resume"] is True
        assert any(event.name == "hypergraph.fork" for event in fork_run.events)
