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
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        trace.set_tracer_provider(provider)
        yield
        self.exporter.clear()

    def test_sync_run_produces_spans(self):
        """SyncRunner with OTelProcessor exports spans."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        graph = Graph([double, triple])
        processor = OpenTelemetryProcessor()
        result = SyncRunner().run(graph, {"x": 10}, event_processors=[processor])

        assert result.completed
        spans = self.exporter.get_finished_spans()
        span_names = [s.name for s in spans]
        assert any("node:double" in n for n in span_names)
        assert any("node:triple" in n for n in span_names)
        assert any("run:" in n for n in span_names)

    def test_span_attributes_include_node_info(self):
        """Node spans carry hypergraph-specific attributes."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        graph = Graph([double])
        processor = OpenTelemetryProcessor()
        SyncRunner().run(graph, {"x": 5}, event_processors=[processor])

        spans = self.exporter.get_finished_spans()
        node_spans = [s for s in spans if s.name.startswith("node:")]
        assert len(node_spans) >= 1
        attrs = dict(node_spans[0].attributes)
        assert attrs["hypergraph.node_name"] == "double"
