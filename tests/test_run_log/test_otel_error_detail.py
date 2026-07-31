"""Full exception detail on the OTel export, redaction as explicit opt-in.

A trace whose ``exception.message`` reads "Node 'x' raised ValueError." cannot
be debugged, and it deviates from the OTel ecosystem norm — standard
``Span.record_exception`` carries the real message. So the export carries the
real message, type and traceback BY DEFAULT, and
``OpenTelemetryProcessor(redact_errors=True)`` restores the scrubbed export
for regulated deployments.

The privacy boundary itself did not move: durable records (RunLog,
StepRecord, checkpoints, attempt ledger, ``RunResult.to_dict()``) still store
only the safe projection. That is pinned in ``tests/test_privacy_projection.py``.
"""

from __future__ import annotations

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, node

try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.trace import StatusCode

    try:
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    except ImportError:  # pragma: no cover - compatibility with older sdk layout
        from opentelemetry.sdk.trace.export.in_memory import InMemorySpanExporter

    HAS_OTEL_SDK = True
except ImportError:  # pragma: no cover - optional dependency
    HAS_OTEL_SDK = False

requires_otel = pytest.mark.skipif(not HAS_OTEL_SDK, reason="opentelemetry-sdk not installed")

RAW_MESSAGE = "row 41 of shipment ABC-9 has a negative weight"


@node(output_name="value")
def explode(x: int) -> int:
    raise ValueError(RAW_MESSAGE)


@pytest.fixture
def exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    exporter.clear()


def _exception_events(spans):
    return [(span, event) for span in spans for event in span.events if event.name == "exception"]


class TestErrorDetailOnEvents:
    """The events carry both shapes; only the safe one reaches durable records."""

    def test_node_error_event_carries_both_projections(self):
        from hypergraph.events import NodeErrorEvent
        from hypergraph.events.processor import EventProcessor

        class Recorder(EventProcessor):
            def __init__(self):
                self.events = []

            def on_event(self, event):
                self.events.append(event)

        recorder = Recorder()
        SyncRunner().run(Graph([explode]), {"x": 1}, event_processors=[recorder], error_handling="continue")

        errors = [e for e in recorder.events if isinstance(e, NodeErrorEvent)]
        assert len(errors) == 1
        event = errors[0]
        assert RAW_MESSAGE not in event.error, "the safe projection is unchanged"
        assert event.error_detail is not None
        assert event.error_detail.message == RAW_MESSAGE
        assert event.error_detail.type_name == "ValueError"
        assert "explode" in (event.error_detail.traceback or ""), "the traceback names the failing frame"

    def test_run_end_event_carries_both_projections(self):
        from hypergraph.events import RunEndEvent
        from hypergraph.events.processor import EventProcessor

        class Recorder(EventProcessor):
            def __init__(self):
                self.events = []

            def on_event(self, event):
                self.events.append(event)

        recorder = Recorder()
        SyncRunner().run(Graph([explode]), {"x": 1}, event_processors=[recorder], error_handling="continue")

        ends = [e for e in recorder.events if isinstance(e, RunEndEvent)]
        assert ends
        end = ends[-1]
        assert end.error is not None and RAW_MESSAGE not in end.error
        assert end.error_detail is not None
        assert end.error_detail.message == RAW_MESSAGE

    def test_run_log_and_result_dict_stay_on_the_safe_projection(self):
        import json

        result = SyncRunner().run(Graph([explode]), {"x": 1}, error_handling="continue")

        assert result.failed
        assert RAW_MESSAGE in str(result.error), "the local exception object is untouched"
        assert RAW_MESSAGE not in json.dumps(result.to_dict())
        assert result.log is not None
        assert RAW_MESSAGE not in json.dumps(result.log.to_dict())


@requires_otel
class TestOTelErrorExport:
    @pytest.mark.parametrize("runner_factory", [SyncRunner, AsyncRunner])
    async def test_default_exports_the_real_message_and_traceback(self, exporter, runner_factory):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, span_exporter = exporter
        processor = OpenTelemetryProcessor(tracer_provider=provider)
        result = runner_factory().run(Graph([explode]), {"x": 1}, event_processors=[processor], error_handling="continue")
        if hasattr(result, "__await__"):
            result = await result
        assert result.failed

        spans = span_exporter.get_finished_spans()
        node_span = next(span for span in spans if span.name == "explode")
        assert node_span.status.status_code == StatusCode.ERROR
        assert RAW_MESSAGE in (node_span.status.description or "")

        events = _exception_events(spans)
        assert events, "a failed node must record an exception span event"
        node_exception = next(event for span, event in events if span.name == "explode")
        assert node_exception.attributes["exception.message"] == RAW_MESSAGE
        # ``exception.type`` is identical in both modes: only message text and
        # the stacktrace are redacted.
        assert node_exception.attributes["exception.type"] == "builtins.ValueError"
        assert "explode" in node_exception.attributes["exception.stacktrace"]

        run_span = next(span for span in spans if span.name == "graph")
        assert RAW_MESSAGE in (run_span.status.description or "")

    def test_redact_errors_restores_the_scrubbed_export(self, exporter):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, span_exporter = exporter
        processor = OpenTelemetryProcessor(tracer_provider=provider, redact_errors=True)
        SyncRunner().run(Graph([explode]), {"x": 1}, event_processors=[processor], error_handling="continue")

        spans = span_exporter.get_finished_spans()
        for span in spans:
            assert RAW_MESSAGE not in (span.status.description or "")
            for event in span.events:
                for value in (event.attributes or {}).values():
                    assert RAW_MESSAGE not in str(value)
            for value in (span.attributes or {}).values():
                assert RAW_MESSAGE not in str(value)

        node_exception = next(event for span, event in _exception_events(spans) if span.name == "explode")
        assert node_exception.attributes["exception.type"] == "builtins.ValueError"
        assert "HG_NODE_FAILED" in node_exception.attributes["exception.message"]
        assert "exception.stacktrace" not in node_exception.attributes

    def test_redacted_export_matches_the_pre_change_shape(self, exporter):
        """redact_errors=True is byte-identical to the old default."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, span_exporter = exporter
        processor = OpenTelemetryProcessor(tracer_provider=provider, redact_errors=True)
        SyncRunner().run(Graph([explode]), {"x": 1}, event_processors=[processor], error_handling="continue")

        spans = span_exporter.get_finished_spans()
        node_span = next(span for span in spans if span.name == "explode")
        node_exception = next(event for event in node_span.events if event.name == "exception")
        assert set(node_exception.attributes) == {"exception.type", "exception.message"}
        assert node_span.status.description == node_exception.attributes["exception.message"]

        run_span = next(span for span in spans if span.name == "graph")
        run_exception = next(event for event in run_span.events if event.name == "exception")
        assert set(run_exception.attributes) == {"exception.message"}


class TestFullErrorDetailCapture:
    def test_never_raised_exception_has_no_traceback(self):
        from hypergraph.diagnostics import full_error_detail

        detail = full_error_detail(ValueError("bare"))
        assert detail.message == "bare"
        assert detail.type_name == "ValueError"
        assert detail.traceback is None

    def test_traceback_is_truncated(self):
        from hypergraph.diagnostics import _MAX_TRACEBACK_CHARS, full_error_detail

        try:
            raise ValueError("x" * (_MAX_TRACEBACK_CHARS * 2))
        except ValueError as exc:
            detail = full_error_detail(exc)
        assert detail.traceback is not None
        assert detail.traceback.endswith("... [truncated]")
        assert len(detail.traceback) <= _MAX_TRACEBACK_CHARS + len("\n... [truncated]")

    def test_qualified_type_name_for_non_builtin(self):
        from hypergraph.diagnostics import full_error_detail
        from hypergraph.graph.validation import GraphConfigError

        detail = full_error_detail(GraphConfigError("nope"))
        assert detail.type_name == "hypergraph.graph.validation.GraphConfigError"
