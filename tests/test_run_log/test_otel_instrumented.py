"""Tests for OpenTelemetryProcessor(extra_attributes=…, tracer_provider=…).

Unlike the legacy tests in test_otel_processor.py, these observe spans through
LOCAL ``TracerProvider`` instances passed via ``tracer_provider=`` — no shared
global provider is mutated. The one test that must witness the GLOBAL path
swaps in a fresh, test-owned provider via pytest's ``monkeypatch`` (restored
automatically) instead of registering span processors on the shared provider.

The OTLP-wire tests export through a real in-test OTLP/HTTP receiver and
assert attributes as they arrive on the wire (protobuf-decoded), not just on
in-memory span objects. Time budget: each wire test performs a handful of
loopback HTTP POSTs (<5s total); the exporter's own timeout bounds the worst
case at 10s.
"""

from __future__ import annotations

import http.server
import threading

import pytest

from hypergraph import Graph, SyncRunner, node

# Check if the full OTel SDK is available
try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    try:
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    except ImportError:  # pragma: no cover - compatibility with older sdk layout
        from opentelemetry.sdk.trace.export.in_memory import InMemorySpanExporter

    HAS_OTEL_SDK = True
except ImportError:
    HAS_OTEL_SDK = False

# Check if the OTLP/HTTP exporter is available (dev-only test dependency)
try:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    HAS_OTLP_HTTP = True
except ImportError:
    HAS_OTLP_HTTP = False

requires_otel = pytest.mark.skipif(not HAS_OTEL_SDK, reason="opentelemetry-sdk not installed")
requires_otlp_http = pytest.mark.skipif(
    not (HAS_OTEL_SDK and HAS_OTLP_HTTP),
    reason="opentelemetry-exporter-otlp-proto-http not installed",
)


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled + doubled // 2


def _local_provider():
    """A private SDK TracerProvider with an in-memory exporter attached."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


@requires_otel
class TestTracerProvider:
    def test_spans_land_only_in_provided_provider(self, monkeypatch):
        """Provided provider gets the spans; the global path records ZERO."""
        from opentelemetry import trace

        from hypergraph.events.otel import OpenTelemetryProcessor

        # Witness: a fresh, test-owned provider swapped in as the process
        # global for this test only (monkeypatch restores the previous value).
        witness_provider, witness_exporter = _local_provider()
        monkeypatch.setattr(trace, "_TRACER_PROVIDER", witness_provider)
        global_before = trace.get_tracer_provider()
        assert global_before is witness_provider

        local_provider, local_exporter = _local_provider()
        processor = OpenTelemetryProcessor(tracer_provider=local_provider)
        result = SyncRunner().run(Graph([double, triple], name="private"), {"x": 4}, event_processors=[processor])

        assert result.completed
        local_names = [s.name for s in local_exporter.get_finished_spans()]
        assert "graph private" in local_names
        assert "node double" in local_names
        assert "node triple" in local_names

        # The global provider recorded ZERO spans from this run …
        assert witness_exporter.get_finished_spans() == ()
        # … and the global provider object itself was left untouched.
        assert trace.get_tracer_provider() is global_before

    def test_none_uses_global_provider(self, monkeypatch):
        """tracer_provider=None keeps today's global-lookup behavior."""
        from opentelemetry import trace

        from hypergraph.events.otel import OpenTelemetryProcessor

        witness_provider, witness_exporter = _local_provider()
        monkeypatch.setattr(trace, "_TRACER_PROVIDER", witness_provider)

        processor = OpenTelemetryProcessor()
        result = SyncRunner().run(Graph([double], name="global_path"), {"x": 2}, event_processors=[processor])

        assert result.completed
        names = [s.name for s in witness_exporter.get_finished_spans()]
        assert "graph global_path" in names
        assert "node double" in names


@requires_otel
class TestExtraAttributes:
    def test_extra_attributes_on_every_span(self):
        """extra_attributes land on root graph spans, map spans, AND node spans."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, exporter = _local_provider()
        processor = OpenTelemetryProcessor(
            extra_attributes={"sp.resolution": "res-A", "sp.tenant": 7},
            tracer_provider=provider,
        )

        SyncRunner().run(Graph([double, triple], name="tagged"), {"x": 2}, event_processors=[processor])
        SyncRunner().map(Graph([double], name="tagged_map"), {"x": [1, 2]}, map_over="x", event_processors=[processor])

        spans = exporter.get_finished_spans()
        names = {s.name for s in spans}
        assert {"graph tagged", "node double", "node triple", "map tagged_map", "graph tagged_map"} <= names
        for span in spans:
            attrs = dict(span.attributes)
            assert attrs["sp.resolution"] == "res-A", f"missing on {span.name}"
            assert attrs["sp.tenant"] == 7, f"missing on {span.name}"

    def test_without_extra_attributes_no_new_keys(self):
        """No extras → spans carry only hypergraph.* keys, exactly as before."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, exporter = _local_provider()
        processor = OpenTelemetryProcessor(tracer_provider=provider)

        SyncRunner().run(Graph([double], name="plain"), {"x": 2}, event_processors=[processor])

        spans = exporter.get_finished_spans()
        assert spans
        for span in spans:
            for key in dict(span.attributes):
                assert key.startswith("hypergraph."), f"unexpected key {key!r} on {span.name}"

    def test_hypergraph_attributes_win_on_collision(self):
        """Extras cannot clobber hypergraph's own span attributes."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, exporter = _local_provider()
        processor = OpenTelemetryProcessor(
            extra_attributes={"hypergraph.graph_name": "spoofed"},
            tracer_provider=provider,
        )

        SyncRunner().run(Graph([double], name="honest"), {"x": 2}, event_processors=[processor])

        root = next(s for s in exporter.get_finished_spans() if s.name == "graph honest")
        assert dict(root.attributes)["hypergraph.graph_name"] == "honest"


def _start_otlp_receiver():
    """Minimal in-test OTLP/HTTP receiver collecting raw POST bodies."""
    bodies: list[bytes] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            bodies.append(self.rfile.read(length))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):  # silence request logging
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, bodies


def _wire_spans(bodies: list[bytes]):
    """Decode every span received on the wire from raw OTLP request bodies."""
    spans = []
    for body in bodies:
        request = ExportTraceServiceRequest()
        request.ParseFromString(body)
        for resource_spans in request.resource_spans:
            for scope_spans in resource_spans.scope_spans:
                spans.extend(scope_spans.spans)
    return spans


def _is_root(span) -> bool:
    return len(span.parent_span_id) == 0 or not any(span.parent_span_id)


@requires_otlp_http
class TestOTLPWire:
    def _export_root_attr_value(self, resolution: str) -> str:
        """Run a graph exporting via real OTLP/HTTP; return the sp.resolution
        value that arrived ON THE WIRE on the root span."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        server, thread, bodies = _start_otlp_receiver()
        try:
            endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1/traces"
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=10)))
            processor = OpenTelemetryProcessor(
                extra_attributes={"sp.resolution": resolution},
                tracer_provider=provider,
            )

            result = SyncRunner().run(Graph([double, triple], name="wire"), {"x": 3}, event_processors=[processor])
            assert result.completed
            provider.shutdown()  # flush any pending exports
        finally:
            server.shutdown()
            thread.join(timeout=10)
            server.server_close()

        root_spans = [s for s in _wire_spans(bodies) if s.name == "graph wire" and _is_root(s)]
        assert len(root_spans) == 1, f"expected exactly one root span on the wire, got {len(root_spans)}"
        attributes = {kv.key: kv.value for kv in root_spans[0].attributes}
        assert "sp.resolution" in attributes, f"sp.resolution missing on the wire; keys: {sorted(attributes)}"
        return attributes["sp.resolution"].string_value

    def test_extra_attribute_arrives_on_the_wire(self):
        assert self._export_root_attr_value("res-XYZ") == "res-XYZ"

    def test_flipping_the_attribute_changes_the_exported_value(self):
        assert self._export_root_attr_value("res-XYZ") == "res-XYZ"
        assert self._export_root_attr_value("res-ABC") == "res-ABC"
