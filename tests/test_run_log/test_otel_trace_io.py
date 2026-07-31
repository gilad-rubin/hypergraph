"""Opt-in node input/output capture on spans (``trace_io``).

Phoenix and friends render ``input.value``/``output.value`` natively, so an
opted-in node shows what it received and what it produced. Opt-in because
payloads are the expensive, sensitive part of a trace.

Precedence: processor kill switch > node explicit > graph default > off.
Payloads ride SPANS ONLY — no durable record changes shape or content.
"""

from __future__ import annotations

import json

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, node

try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    try:
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    except ImportError:  # pragma: no cover - compatibility with older sdk layout
        from opentelemetry.sdk.trace.export.in_memory import InMemorySpanExporter

    HAS_OTEL_SDK = True
except ImportError:  # pragma: no cover - optional dependency
    HAS_OTEL_SDK = False

requires_otel = pytest.mark.skipif(not HAS_OTEL_SDK, reason="opentelemetry-sdk not installed")


@node(output_name="loud", trace_io=True)
def traced(text: str) -> str:
    return text.upper()


@node(output_name="quiet")
def untraced(text: str) -> str:
    return text.lower()


@node(output_name="opted_out", trace_io=False)
def opted_out(text: str) -> str:
    return text.title()


@node(output_name="big", trace_io=True)
def big_output(size: int) -> str:
    return "x" * size


class Unserializable:
    __slots__ = ()

    def __repr__(self) -> str:
        raise RuntimeError("repr is broken too")


@node(output_name="weird", trace_io=True)
def unserializable(x: int) -> Unserializable:
    return Unserializable()


@node(output_name="cached_value", trace_io=True, cache=True)
def cached_traced(text: str) -> str:
    return text.upper()


@pytest.fixture
def exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    exporter.clear()


def _span(spans, name):
    return next(span for span in spans if span.name == name)


class TestTraceIoResolution:
    """Precedence lives on the node/graph seam, independent of any exporter."""

    def test_node_default_is_undeclared(self):
        assert untraced.trace_io is None
        assert traced.trace_io is True
        assert opted_out.trace_io is False

    def test_graph_default_is_off(self):
        assert Graph([untraced]).trace_io is False
        assert Graph([untraced], trace_io=True).trace_io is True
        assert Graph([untraced]).with_trace_io().trace_io is True
        assert Graph([untraced], trace_io=True).with_trace_io(False).trace_io is False

    def test_node_explicit_overrides_graph_default(self):
        from hypergraph.runners._shared.event_helpers import trace_io_enabled

        on = Graph([traced, untraced, opted_out], trace_io=True)
        off = Graph([traced, untraced, opted_out])

        assert trace_io_enabled(traced, off) is True, "node True beats graph off"
        assert trace_io_enabled(opted_out, on) is False, "node False beats graph on"
        assert trace_io_enabled(untraced, on) is True, "undeclared follows the graph"
        assert trace_io_enabled(untraced, off) is False

    def test_trace_io_must_be_a_bool(self):
        with pytest.raises(TypeError, match="trace_io must be"):

            @node(output_name="x", trace_io="yes")
            def bad(text: str) -> str:
                return text

    @pytest.mark.parametrize("bad", ["false", "true", 0, 1, None])
    def test_graph_trace_io_is_never_coerced(self, bad):
        """bool("false") is True — coercing would turn capture ON from config."""
        with pytest.raises(TypeError, match="trace_io must be"):
            Graph([untraced], trace_io=bad)
        with pytest.raises(TypeError, match="trace_io must be"):
            Graph([untraced]).with_trace_io(bad)

    def test_add_nodes_preserves_the_graph_default(self):
        graph = Graph([untraced], trace_io=True).add_nodes(opted_out)
        assert graph.trace_io is True

    def test_trace_io_does_not_change_graph_identity(self):
        plain = Graph([untraced], name="g")
        traced_graph = Graph([untraced], name="g", trace_io=True)
        assert plain.definition_hash == traced_graph.definition_hash


@requires_otel
class TestPayloadExport:
    @pytest.mark.parametrize("runner_factory", [SyncRunner, AsyncRunner])
    async def test_opted_in_node_exports_inputs_and_outputs(self, exporter, runner_factory):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, span_exporter = exporter
        result = runner_factory().run(
            Graph([traced]),
            {"text": "hello"},
            event_processors=[OpenTelemetryProcessor(tracer_provider=provider)],
        )
        if hasattr(result, "__await__"):
            await result

        span = _span(span_exporter.get_finished_spans(), "traced")
        assert json.loads(span.attributes["input.value"]) == {"text": "hello"}
        assert span.attributes["input.mime_type"] == "application/json"
        assert json.loads(span.attributes["output.value"]) == {"loud": "HELLO"}
        assert span.attributes["output.mime_type"] == "application/json"

    def test_default_node_exports_no_payloads(self, exporter):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, span_exporter = exporter
        SyncRunner().run(
            Graph([untraced]),
            {"text": "Hello"},
            event_processors=[OpenTelemetryProcessor(tracer_provider=provider)],
        )

        for span in span_exporter.get_finished_spans():
            assert not any(key.startswith(("input.", "output.")) for key in span.attributes)

    def test_graph_default_turns_capture_on_and_node_opt_out_wins(self, exporter):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, span_exporter = exporter
        graph = Graph([untraced, opted_out], trace_io=True)
        SyncRunner().run(graph, {"text": "Hello"}, event_processors=[OpenTelemetryProcessor(tracer_provider=provider)])

        spans = span_exporter.get_finished_spans()
        assert "input.value" in _span(spans, "untraced").attributes, "undeclared node follows the graph default"
        assert "input.value" not in _span(spans, "opted_out").attributes, "node trace_io=False beats the graph default"

    def test_kill_switch_beats_every_opt_in(self, exporter):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, span_exporter = exporter
        graph = Graph([traced, untraced], trace_io=True)
        SyncRunner().run(
            graph,
            {"text": "Hello"},
            event_processors=[OpenTelemetryProcessor(tracer_provider=provider, redact_payloads=True)],
        )

        for span in span_exporter.get_finished_spans():
            assert not any(key.startswith(("input.", "output.")) for key in span.attributes)
            assert "HELLO" not in json.dumps(dict(span.attributes))

    def test_oversized_value_is_truncated(self, exporter):
        from hypergraph.events.otel import _TRUNCATION_SUFFIX, OpenTelemetryProcessor

        provider, span_exporter = exporter
        processor = OpenTelemetryProcessor(tracer_provider=provider, max_payload_chars=64)
        SyncRunner().run(Graph([big_output]), {"size": 5000}, event_processors=[processor])

        span = _span(span_exporter.get_finished_spans(), "big_output")
        value = span.attributes["output.value"]
        assert value.endswith(_TRUNCATION_SUFFIX)
        assert len(value) == 64 + len(_TRUNCATION_SUFFIX)
        assert span.attributes["output.mime_type"] == "text/plain", "a cut JSON document is not JSON"

    @pytest.mark.parametrize("bad", [0, -1, 4096.0, "4096", True])
    def test_max_payload_chars_must_be_a_positive_int(self, exporter, bad):
        """A bad cap would otherwise silently drop the span, not just the payload."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, _ = exporter
        with pytest.raises(ValueError, match="max_payload_chars must be a positive integer"):
            OpenTelemetryProcessor(tracer_provider=provider, max_payload_chars=bad)

    def test_default_cap_is_four_kib(self, exporter):
        from hypergraph.events.otel import _DEFAULT_MAX_PAYLOAD_CHARS, _TRUNCATION_SUFFIX, OpenTelemetryProcessor

        assert _DEFAULT_MAX_PAYLOAD_CHARS == 4096
        provider, span_exporter = exporter
        SyncRunner().run(
            Graph([big_output]),
            {"size": 50_000},
            event_processors=[OpenTelemetryProcessor(tracer_provider=provider)],
        )

        value = _span(span_exporter.get_finished_spans(), "big_output").attributes["output.value"]
        assert len(value) == _DEFAULT_MAX_PAYLOAD_CHARS + len(_TRUNCATION_SUFFIX)

    def test_unserializable_value_is_omitted_and_the_run_still_succeeds(self, exporter):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, span_exporter = exporter
        result = SyncRunner().run(
            Graph([unserializable]),
            {"x": 1},
            event_processors=[OpenTelemetryProcessor(tracer_provider=provider)],
        )

        assert result.completed, "capture failure must never fail the run"
        span = _span(span_exporter.get_finished_spans(), "unserializable")
        assert "input.value" in span.attributes, "the serializable half still exports"
        assert "output.value" not in span.attributes

    def test_cache_hit_still_exports_payloads(self, exporter):
        from hypergraph import InMemoryCache
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, span_exporter = exporter
        graph = Graph([cached_traced])
        runner = SyncRunner(cache=InMemoryCache())
        runner.run(graph, {"text": "hi"}, event_processors=[OpenTelemetryProcessor(tracer_provider=provider)])
        span_exporter.clear()
        runner.run(graph, {"text": "hi"}, event_processors=[OpenTelemetryProcessor(tracer_provider=provider)])

        span = _span(span_exporter.get_finished_spans(), "cached_traced")
        assert span.attributes["hypergraph.cached"] is True
        assert json.loads(span.attributes["output.value"]) == {"cached_value": "HI"}


class TestDurableRecordsAreUnaffected:
    """``trace_io`` is a span concern; no durable record changes because of it."""

    @staticmethod
    def _volatile(step: dict) -> dict:
        return {k: v for k, v in step.items() if k not in {"duration_ms", "span_id"}}

    def test_run_log_does_not_change_shape_or_content(self):
        graph = Graph([traced, untraced], trace_io=True)
        with_capture = SyncRunner().run(graph, {"text": "hello"})
        without_capture = SyncRunner().run(Graph([traced, untraced]), {"text": "hello"})

        assert with_capture.log is not None and without_capture.log is not None
        captured = [self._volatile(step) for step in with_capture.log.to_dict()["steps"]]
        plain = [self._volatile(step) for step in without_capture.log.to_dict()["steps"]]
        assert captured == plain

        serialized = json.dumps(with_capture.log.to_dict())
        assert "input.value" not in serialized and "trace_inputs" not in serialized

    async def test_checkpointed_steps_do_not_change_shape_or_content(self, tmp_path):
        """StepRecord already stores outputs for resume; trace_io adds nothing."""
        from hypergraph.checkpointers import SqliteCheckpointer

        pytest.importorskip("aiosqlite")
        volatile = {"created_at", "completed_at", "duration_ms", "run_id", "workflow_id"}

        async def steps_for(graph, name):
            cp = SqliteCheckpointer(str(tmp_path / f"{name}.db"))
            try:
                SyncRunner(checkpointer=cp).run(graph, {"text": "hello"}, workflow_id=name)
                return [{k: v for k, v in step.to_dict().items() if k not in volatile} for step in await cp.get_steps(name)]
            finally:
                await cp.close()

        captured = await steps_for(Graph([traced], trace_io=True), "on")
        plain = await steps_for(Graph([traced]), "off")

        assert captured and captured == plain
        assert "input.value" not in json.dumps(captured)
        assert "trace_inputs" not in json.dumps(captured)
