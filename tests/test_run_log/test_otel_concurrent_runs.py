"""One shared OpenTelemetryProcessor under CONCURRENT top-level runs.

A single processor instance (passed explicitly, or carried by a graph via
``Graph.with_processors``) is the natural way to export a long-lived service.
The runner calls ``processor.shutdown()`` at the end of EVERY top-level run,
so before this module the first run to finish tore down the shared span
bookkeeping of every other in-flight run: their root spans were ended early
(a ~200ms run exported as ~22ms), their later node spans lost their parent
context and started fresh traces, and their run-end attributes were dropped.

These tests pin the fixed contract: each concurrent top-level run exports a
complete, correctly parented span tree with its own trace id and its own
duration, whichever run finishes first.
"""

from __future__ import annotations

import asyncio
import threading
import time

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

SLOW_SECONDS = 0.20
FAST_SECONDS = 0.01


@node(output_name="slow_value")
def slow_step(x: int) -> int:
    time.sleep(SLOW_SECONDS)
    return x


@node(output_name="slow_tail")
def slow_tail(slow_value: int) -> int:
    time.sleep(SLOW_SECONDS)
    return slow_value


@node(output_name="fast_value")
def fast_step(x: int) -> int:
    time.sleep(FAST_SECONDS)
    return x


@node(output_name="slow_value")
async def async_slow_step(x: int) -> int:
    await asyncio.sleep(SLOW_SECONDS)
    return x


@node(output_name="slow_tail")
async def async_slow_tail(slow_value: int) -> int:
    await asyncio.sleep(SLOW_SECONDS)
    return slow_value


@node(output_name="fast_value")
async def async_fast_step(x: int) -> int:
    await asyncio.sleep(FAST_SECONDS)
    return x


@pytest.fixture
def exporter():
    """An isolated provider/exporter pair AND a clean ambient OTel context.

    Both halves matter. The provider is private so these tests never touch the
    global one. The empty ambient context is what makes "each top-level run
    roots its own trace" a meaningful assertion: ``start_span(context=None)``
    parents under whatever span is ambient, so a span left current by an
    earlier test in the same worker legitimately merges both runs into ONE
    trace — that is the documented ambient-nesting feature working, not a bug.
    Attaching an empty context isolates the test from inherited state and,
    because detach restores what was there, from leaking its own.
    """
    from opentelemetry import context as otel_context

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    token = otel_context.attach(otel_context.Context())
    try:
        yield provider, exporter
    finally:
        otel_context.detach(token)
        exporter.clear()


def _by_name(spans, name):
    return [span for span in spans if span.name == name]


def _duration_ms(span) -> float:
    return (span.end_time - span.start_time) / 1_000_000


def _assert_complete_run_tree(spans, graph_name: str, node_names: tuple[str, ...], *, min_duration_ms: float) -> None:
    """One graph root, its nodes beneath it, one trace, real duration."""
    roots = _by_name(spans, graph_name)
    assert len(roots) == 1, f"expected exactly one {graph_name!r} root span, got {len(roots)}"
    root = roots[0]

    assert root.attributes.get("hypergraph.run.outcome") == "completed", (
        f"{graph_name!r} root never received its run-end attributes — it was ended by another run's shutdown"
    )
    assert root.attributes.get("hypergraph.duration_ms") is not None
    assert _duration_ms(root) >= min_duration_ms, f"{graph_name!r} exported {_duration_ms(root):.1f}ms, expected at least {min_duration_ms}ms"

    for node_name in node_names:
        node_spans = _by_name(spans, node_name)
        assert len(node_spans) == 1, f"expected exactly one {node_name!r} span, got {len(node_spans)}"
        node_span = node_spans[0]
        assert node_span.parent is not None, f"{node_name!r} lost its parent context and started a new trace"
        assert node_span.parent.span_id == root.context.span_id
        assert node_span.context.trace_id == root.context.trace_id


@requires_otel
class TestConcurrentTopLevelRunsShareOneProcessor:
    async def test_async_concurrent_runs_each_export_a_complete_tree(self, exporter):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, span_exporter = exporter
        processor = OpenTelemetryProcessor(tracer_provider=provider)

        slow = Graph([async_slow_step, async_slow_tail], name="slow_graph")
        fast = Graph([async_fast_step], name="fast_graph")
        runner = AsyncRunner()

        await asyncio.gather(
            runner.run(slow, {"x": 1}, event_processors=[processor]),
            runner.run(fast, {"x": 2}, event_processors=[processor]),
        )

        spans = span_exporter.get_finished_spans()
        _assert_complete_run_tree(
            spans,
            "slow_graph",
            ("async_slow_step", "async_slow_tail"),
            min_duration_ms=SLOW_SECONDS * 2 * 1000 * 0.8,
        )
        _assert_complete_run_tree(spans, "fast_graph", ("async_fast_step",), min_duration_ms=0.0)

        # With no ambient parent span (see the `exporter` fixture), each
        # top-level run roots its own trace. Under a caller-supplied ambient
        # span they would legitimately share one — that is ambient nesting.
        trace_ids = {_by_name(spans, name)[0].context.trace_id for name in ("slow_graph", "fast_graph")}
        assert len(trace_ids) == 2, "with no ambient parent, concurrent top-level runs root separate traces"

    def test_sync_concurrent_runs_in_threads_each_export_a_complete_tree(self, exporter):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, span_exporter = exporter
        processor = OpenTelemetryProcessor(tracer_provider=provider)

        slow = Graph([slow_step, slow_tail], name="slow_graph")
        fast = Graph([fast_step], name="fast_graph")

        started = threading.Barrier(2)

        def run(graph, x):
            started.wait()
            SyncRunner().run(graph, {"x": x}, event_processors=[processor])

        threads = [
            threading.Thread(target=run, args=(slow, 1)),
            threading.Thread(target=run, args=(fast, 2)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        spans = span_exporter.get_finished_spans()
        _assert_complete_run_tree(
            spans,
            "slow_graph",
            ("slow_step", "slow_tail"),
            min_duration_ms=SLOW_SECONDS * 2 * 1000 * 0.8,
        )
        _assert_complete_run_tree(spans, "fast_graph", ("fast_step",), min_duration_ms=0.0)

    async def test_graph_carried_processor_survives_the_first_run_finishing(self, exporter):
        """``Graph.with_processors`` is the shared-processor shape users reach for."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, span_exporter = exporter
        processor = OpenTelemetryProcessor(tracer_provider=provider)

        slow = Graph([async_slow_step, async_slow_tail], name="slow_graph").with_processors(processor)
        fast = Graph([async_fast_step], name="fast_graph").with_processors(processor)
        runner = AsyncRunner()

        await asyncio.gather(runner.run(slow, {"x": 1}), runner.run(fast, {"x": 2}))

        spans = span_exporter.get_finished_spans()
        _assert_complete_run_tree(
            spans,
            "slow_graph",
            ("async_slow_step", "async_slow_tail"),
            min_duration_ms=SLOW_SECONDS * 2 * 1000 * 0.8,
        )
        _assert_complete_run_tree(spans, "fast_graph", ("async_fast_step",), min_duration_ms=0.0)

    async def test_last_run_out_still_sweeps_leftover_spans(self, exporter):
        """The sweep is deferred, not lost: it runs when the last run leaves."""
        from hypergraph.events.otel import OpenTelemetryProcessor
        from hypergraph.events.types import NodeStartEvent, RunEndEvent, RunStartEvent

        provider, span_exporter = exporter
        processor = OpenTelemetryProcessor(tracer_provider=provider)

        # Two top-level runs start; the first abandons a live node span.
        processor.on_run_start(RunStartEvent(run_id="a", span_id="ra", graph_name="a"))
        processor.on_run_start(RunStartEvent(run_id="b", span_id="rb", graph_name="b"))
        processor.on_node_start(NodeStartEvent(run_id="a", span_id="na", parent_span_id="ra", node_name="leaked", graph_name="a"))
        processor.on_run_end(RunEndEvent(run_id="a", span_id="ra", graph_name="a"))
        processor.shutdown()

        assert {span.name for span in span_exporter.get_finished_spans()} == {"a"}, "b must still be live after a's shutdown"

        processor.on_run_end(RunEndEvent(run_id="b", span_id="rb", graph_name="b"))
        processor.shutdown()

        assert {span.name for span in span_exporter.get_finished_spans()} == {"a", "b", "leaked"}

    def test_a_run_starting_during_the_sweep_is_made_to_wait(self, exporter):
        """The sweep holds the lifecycle lock, so a starting run cannot slip in.

        Checking the count under the lock but sweeping outside it reopens the
        original bug in a narrower window: the new run registers its root span
        into the very dicts the sweep is about to clear. White-box by
        necessity — the window is invisible from the public surface, so the
        test wedges itself into the sweep and proves ``on_run_start`` blocks.
        """
        from hypergraph.events.otel import OpenTelemetryProcessor
        from hypergraph.events.types import RunEndEvent, RunStartEvent

        provider, span_exporter = exporter
        processor = OpenTelemetryProcessor(tracer_provider=provider)
        processor.on_run_start(RunStartEvent(run_id="a", span_id="ra", graph_name="a"))
        processor.on_run_end(RunEndEvent(run_id="a", span_id="ra", graph_name="a"))

        sweeping = threading.Event()
        b_registered = threading.Event()

        class WedgedSpans(dict):
            """Pauses inside the sweep, giving run B a real chance to race."""

            def values(self):
                sweeping.set()
                b_registered.wait(timeout=0.5)
                return super().values()

        processor._spans = WedgedSpans(processor._spans)

        def start_b():
            sweeping.wait(timeout=5)
            processor.on_run_start(RunStartEvent(run_id="b", span_id="rb", graph_name="b"))
            b_registered.set()

        thread = threading.Thread(target=start_b)
        thread.start()
        processor.shutdown()
        raced = b_registered.is_set()
        thread.join(timeout=5)

        assert not raced, "on_run_start must block until the sweep finishes"
        assert "rb" in processor._spans, "B registered after the sweep, so the sweep cannot have cleared it"
        assert {span.name for span in span_exporter.get_finished_spans()} == {"a"}, "B must still be live"

    def test_sequential_runs_still_sweep_on_each_shutdown(self, exporter):
        """Sequential behavior is unchanged: one run in, one full sweep out."""
        from hypergraph.events.otel import OpenTelemetryProcessor
        from hypergraph.events.types import NodeStartEvent, RunStartEvent

        provider, span_exporter = exporter
        processor = OpenTelemetryProcessor(tracer_provider=provider)

        processor.on_run_start(RunStartEvent(run_id="a", span_id="ra", graph_name="a"))
        processor.on_node_start(NodeStartEvent(run_id="a", span_id="na", parent_span_id="ra", node_name="leaked", graph_name="a"))
        processor.shutdown()

        assert {span.name for span in span_exporter.get_finished_spans()} == {"a", "leaked"}
