"""``OpenTelemetryProcessor.trace_id_for`` — link a run id to its trace.

Without it, going from a ``RunResult`` to the trace in Phoenix/Jaeger means
adding a throwaway graph node that reads the ambient span. The processor
already knows the answer at span-start time; this just exposes it.
"""

from __future__ import annotations

import asyncio

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


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="value")
async def async_double(x: int) -> int:
    await asyncio.sleep(0)
    return x * 2


@pytest.fixture
def exporter():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider, exporter
    exporter.clear()


@requires_otel
class TestTraceIdForRun:
    def test_returns_the_hex_trace_id_of_the_exported_run(self, exporter):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, span_exporter = exporter
        processor = OpenTelemetryProcessor(tracer_provider=provider)
        result = SyncRunner().run(Graph([double]), {"x": 3}, event_processors=[processor])

        trace_id = processor.trace_id_for(result.run_id)
        assert trace_id is not None
        assert len(trace_id) == 32 and int(trace_id, 16) != 0

        spans = span_exporter.get_finished_spans()
        assert {format(span.context.trace_id, "032x") for span in spans} == {trace_id}

    def test_survives_shutdown_so_callers_can_read_it_after_run_returns(self, exporter):
        """The runner shuts the processor down BEFORE run() returns."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, _ = exporter
        processor = OpenTelemetryProcessor(tracer_provider=provider)
        result = SyncRunner().run(Graph([double]), {"x": 3}, event_processors=[processor])

        assert processor.trace_id_for(result.run_id) is not None
        processor.shutdown()
        assert processor.trace_id_for(result.run_id) is not None

    def test_unknown_run_id_returns_none(self, exporter):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, _ = exporter
        processor = OpenTelemetryProcessor(tracer_provider=provider)
        assert processor.trace_id_for("run-never-seen") is None

    async def test_concurrent_runs_get_distinct_trace_ids(self, exporter):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, _ = exporter
        processor = OpenTelemetryProcessor(tracer_provider=provider)
        runner = AsyncRunner()

        first, second = await asyncio.gather(
            runner.run(Graph([async_double], name="one"), {"x": 1}, event_processors=[processor]),
            runner.run(Graph([async_double], name="two"), {"x": 2}, event_processors=[processor]),
        )

        one = processor.trace_id_for(first.run_id)
        two = processor.trace_id_for(second.run_id)
        assert one is not None and two is not None
        assert one != two

    def test_nested_run_shares_the_parent_trace(self, exporter):
        from hypergraph.events import RunStartEvent
        from hypergraph.events.otel import OpenTelemetryProcessor
        from hypergraph.events.processor import EventProcessor

        class RunIds(EventProcessor):
            def __init__(self):
                self.ids = []

            def on_event(self, event):
                if isinstance(event, RunStartEvent):
                    self.ids.append(event.run_id)

        provider, _ = exporter
        processor = OpenTelemetryProcessor(tracer_provider=provider)
        run_ids = RunIds()
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node(name="nested")], name="outer")
        SyncRunner().run(outer, {"x": 2}, event_processors=[processor, run_ids])

        assert len(run_ids.ids) == 2, "one top-level run and one nested run"
        trace_ids = {processor.trace_id_for(run_id) for run_id in run_ids.ids}
        assert None not in trace_ids
        assert len(trace_ids) == 1, "a nested run belongs to its parent's trace"

    def test_mapping_is_bounded(self, exporter):
        from hypergraph.events.otel import _MAX_TRACE_IDS, OpenTelemetryProcessor
        from hypergraph.events.types import RunStartEvent

        provider, _ = exporter
        processor = OpenTelemetryProcessor(tracer_provider=provider)
        for index in range(_MAX_TRACE_IDS + 5):
            processor.on_run_start(RunStartEvent(run_id=f"run-{index}", span_id=f"s{index}", graph_name="g"))

        assert processor.trace_id_for("run-0") is None, "oldest entries are evicted"
        assert processor.trace_id_for(f"run-{_MAX_TRACE_IDS + 4}") is not None

    def test_noop_provider_reports_no_trace(self, exporter):
        from opentelemetry import trace

        from hypergraph.events.otel import OpenTelemetryProcessor

        processor = OpenTelemetryProcessor(tracer_provider=trace.NoOpTracerProvider())
        result = SyncRunner().run(Graph([double]), {"x": 3}, event_processors=[processor])

        assert processor.trace_id_for(result.run_id) is None
