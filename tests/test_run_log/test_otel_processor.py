"""Tests for the OpenTelemetryProcessor.

The import guard test always runs. SDK-dependent tests are skipped
if opentelemetry-sdk is not installed.
"""

import pytest

from hypergraph import Graph, SyncRunner, node

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
        assert any(name == "node double" for name in span_names)
        assert any(name == "node triple" for name in span_names)
        assert any(name.startswith("graph ") for name in span_names)

    def test_span_attributes_include_node_info(self):
        """Node spans carry Hypergraph execution attributes."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        graph = Graph([double])
        processor = OpenTelemetryProcessor()
        SyncRunner().run(graph, {"x": 5}, event_processors=[processor])

        spans = self.exporter.get_finished_spans()
        node_spans = [s for s in spans if s.name == "node double"]
        assert len(node_spans) >= 1
        attrs = dict(node_spans[0].attributes)
        assert attrs["hypergraph.node_name"] == "double"
        assert attrs["hypergraph.superstep"] == 0

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
        assert attrs["hypergraph.batch.outcome"] == "partial"
        assert map_span.status.status_code == StatusCode.UNSET

        child_item_spans = [span for span in spans if span.name.startswith("graph ") and span.attributes.get("hypergraph.item_index") is not None]
        assert sorted(span.attributes["hypergraph.item_index"] for span in child_item_spans) == [0, 1, 2]

    def test_forked_run_adds_lineage_link_when_source_span_is_known(self, tmp_path):
        """Forked runs link back to the source run span when both are in-process."""
        from hypergraph.checkpointers import SqliteCheckpointer
        from hypergraph.events.otel import OpenTelemetryProcessor

        cp = SqliteCheckpointer(str(tmp_path / "otel-lineage.db"))
        processor = OpenTelemetryProcessor()
        runner = SyncRunner(checkpointer=cp)

        try:
            root = runner.run(Graph([double]), {"x": 5}, workflow_id="wf-root", event_processors=[processor])
            fork = runner.run(
                Graph([double]),
                {"x": 9},
                workflow_id="wf-root-fork",
                fork_from="wf-root",
                event_processors=[processor],
            )
        finally:
            import asyncio

            asyncio.run(cp.close())

        assert root.completed
        assert fork.completed
        spans = self.exporter.get_finished_spans()
        fork_span = next(span for span in spans if span.name.startswith("graph ") and span.attributes.get("hypergraph.workflow_id") == "wf-root-fork")
        assert len(fork_span.links) == 1
        link_attrs = dict(fork_span.links[0].attributes or {})
        assert link_attrs["hypergraph.lineage.relationship"] == "fork"

        event_names = [event.name for event in fork_span.events]
        assert "hypergraph.fork" in event_names

    def test_evicted_lineage_context_does_not_create_stale_link(self, tmp_path, monkeypatch):
        """Eviction should degrade to no lineage link instead of reusing stale context."""
        import hypergraph.events.otel as otel_module
        from hypergraph.checkpointers import SqliteCheckpointer

        monkeypatch.setattr(otel_module, "_MAX_LINEAGE_CONTEXTS", 1)

        cp = SqliteCheckpointer(str(tmp_path / "otel-eviction.db"))
        processor = otel_module.OpenTelemetryProcessor()
        runner = SyncRunner(checkpointer=cp)

        try:
            runner.run(Graph([double]), {"x": 1}, workflow_id="wf-root-1", event_processors=[processor])
            runner.run(Graph([double]), {"x": 2}, workflow_id="wf-root-2", event_processors=[processor])
            fork = runner.run(
                Graph([double]),
                {"x": 3},
                workflow_id="wf-root-1-fork",
                fork_from="wf-root-1",
                event_processors=[processor],
            )
        finally:
            import asyncio

            asyncio.run(cp.close())

        assert fork.completed
        spans = self.exporter.get_finished_spans()
        fork_span = next(
            span for span in spans if span.name.startswith("graph ") and span.attributes.get("hypergraph.workflow_id") == "wf-root-1-fork"
        )
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
        run_span = next(span for span in spans if span.name == "graph approval_flow")
        attrs = dict(run_span.attributes)
        assert attrs["hypergraph.run.outcome"] == "paused"
        assert attrs["hypergraph.duration_ms"] == 12.5
        assert "wf-sync-pause" in processor._workflow_span_contexts
