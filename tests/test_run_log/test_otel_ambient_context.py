"""Ambient OTel context activation around run roots and node bodies.

The OpenTelemetryProcessor makes each span it creates the AMBIENT OTel
context for the code that span covers, so third-party instrumentation
(openinference, agent SDKs) started inside a node body parents under the
node span with zero coupling. These tests prove the falsifiers via exporter
read-back on LOCAL TracerProvider instances — no global provider is mutated.

Time budget: rendezvous waits are bounded by asyncio.wait_for(timeout=5);
the subprocess purity test is one interpreter start (<5s).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import textwrap

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, interrupt, node
from tests._interrupt_questions import StringQuestion

try:
    from opentelemetry import context as otel_context
    from opentelemetry.context import Context
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    try:
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    except ImportError:  # pragma: no cover - compatibility with older sdk layout
        from opentelemetry.sdk.trace.export.in_memory import InMemorySpanExporter

    HAS_OTEL_SDK = True
except ImportError:
    HAS_OTEL_SDK = False

requires_otel = pytest.mark.skipif(not HAS_OTEL_SDK, reason="opentelemetry-sdk not installed")


def _local_provider():
    """A private SDK TracerProvider with an in-memory exporter attached."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider, exporter


def _by_name(spans, name):
    matches = [s for s in spans if s.name == name]
    assert len(matches) == 1, f"expected exactly one {name!r} span, got {len(matches)}"
    return matches[0]


def _assert_no_detach_failures(caplog):
    """The loud OTel 'Failed to detach context' error must never fire."""
    failures = [r for r in caplog.records if "Failed to detach context" in r.getMessage()]
    assert failures == [], failures


@requires_otel
class TestNodeBodyNesting:
    """Falsifier 1: an instrumented call inside a node body parents to the node span."""

    def test_sync_node_body_span_parents_to_node_span(self):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, exporter = _local_provider()
        tracer = provider.get_tracer("test.instrumentation")

        @node(output_name="doubled")
        def double(x: int) -> int:
            with tracer.start_as_current_span("inner-llm-call"):
                return x * 2

        result = SyncRunner().run(
            Graph([double], name="nest_sync"),
            {"x": 2},
            event_processors=[OpenTelemetryProcessor(tracer_provider=provider)],
        )

        assert result.completed
        spans = exporter.get_finished_spans()
        node_span = _by_name(spans, "node double")
        inner = _by_name(spans, "inner-llm-call")
        assert inner.parent is not None
        assert inner.parent.span_id == node_span.context.span_id
        assert inner.context.trace_id == node_span.context.trace_id

    async def test_async_node_body_span_parents_to_node_span(self):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, exporter = _local_provider()
        tracer = provider.get_tracer("test.instrumentation")

        @node(output_name="doubled")
        async def double(x: int) -> int:
            with tracer.start_as_current_span("inner-llm-call"):
                return x * 2

        result = await AsyncRunner().run(
            Graph([double], name="nest_async"),
            {"x": 2},
            event_processors=[OpenTelemetryProcessor(tracer_provider=provider)],
        )

        assert result.completed
        spans = exporter.get_finished_spans()
        node_span = _by_name(spans, "node double")
        inner = _by_name(spans, "inner-llm-call")
        assert inner.parent is not None
        assert inner.parent.span_id == node_span.context.span_id
        assert inner.context.trace_id == node_span.context.trace_id


@requires_otel
class TestPreexistingRootNotInherited:
    """The observed bug shape: with an ambient root active before .run(), a
    node-body span used to flatten to that root as a sibling of the node."""

    def test_node_body_span_does_not_flatten_to_the_pre_run_root(self):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, exporter = _local_provider()
        tracer = provider.get_tracer("test.instrumentation")

        @node(output_name="doubled")
        def call_llm(x: int) -> int:
            with tracer.start_as_current_span("ChatCompletion"):
                return x * 2

        with tracer.start_as_current_span("host-root"):
            result = SyncRunner().run(
                Graph([call_llm], name="bracketed"),
                {"x": 2},
                event_processors=[OpenTelemetryProcessor(tracer_provider=provider)],
            )

        assert result.completed
        spans = exporter.get_finished_spans()
        host_root = _by_name(spans, "host-root")
        node_span = _by_name(spans, "node call_llm")
        inner = _by_name(spans, "ChatCompletion")
        # Nested under the node — NOT a sibling hanging off the host root.
        assert inner.parent.span_id == node_span.context.span_id
        assert inner.parent.span_id != host_root.context.span_id
        # And the whole run still joins the host's trace.
        assert {s.context.trace_id for s in spans} == {host_root.context.trace_id}


@requires_otel
class TestConcurrentIsolation:
    """Falsifier 2: concurrent contexts never leak into each other."""

    async def test_map_concurrent_items_no_cross_item_bleed(self):
        """Two map items provably in flight together; each inner span parents
        to ITS OWN item's node span."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, exporter = _local_provider()
        tracer = provider.get_tracer("test.instrumentation")
        entered = 0
        all_in = asyncio.Event()

        @node(output_name="doubled")
        async def double(x: int) -> int:
            nonlocal entered
            entered += 1
            if entered == 2:
                all_in.set()
            # Rendezvous: both items are inside their node bodies before either
            # creates its inner span — real concurrency, bounded at 5s.
            await asyncio.wait_for(all_in.wait(), timeout=5)
            with tracer.start_as_current_span("inner-llm-call") as inner:
                inner.set_attribute("test.x", x)
            return x * 2

        results = await AsyncRunner().map(
            Graph([double], name="nest_map"),
            {"x": [10, 20]},
            map_over="x",
            event_processors=[OpenTelemetryProcessor(tracer_provider=provider)],
        )

        assert results.completed
        spans = exporter.get_finished_spans()
        node_by_item = {s.attributes["hypergraph.item_index"]: s for s in spans if s.name == "node double"}
        assert sorted(node_by_item) == [0, 1]
        inner_by_x = {s.attributes["test.x"]: s for s in spans if s.name == "inner-llm-call"}
        assert sorted(inner_by_x) == [10, 20]

        # x=10 is item 0, x=20 is item 1: each inner parents to its own node span.
        assert inner_by_x[10].parent.span_id == node_by_item[0].context.span_id
        assert inner_by_x[20].parent.span_id == node_by_item[1].context.span_id
        assert node_by_item[0].context.span_id != node_by_item[1].context.span_id

    async def test_concurrent_nodes_in_one_run_do_not_inherit_each_other(self):
        """Two parallel nodes of ONE run, in flight together: each node body's
        inner span parents to its own node span."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, exporter = _local_provider()
        tracer = provider.get_tracer("test.instrumentation")
        entered = 0
        all_in = asyncio.Event()

        async def _instrumented(name: str, x: int) -> int:
            nonlocal entered
            entered += 1
            if entered == 2:
                all_in.set()
            await asyncio.wait_for(all_in.wait(), timeout=5)
            with tracer.start_as_current_span(f"inner-{name}"):
                return x

        @node(output_name="a_out")
        async def branch_a(x: int) -> int:
            return await _instrumented("a", x)

        @node(output_name="b_out")
        async def branch_b(x: int) -> int:
            return await _instrumented("b", x)

        result = await AsyncRunner().run(
            Graph([branch_a, branch_b], name="parallel"),
            {"x": 1},
            event_processors=[OpenTelemetryProcessor(tracer_provider=provider)],
        )

        assert result.completed
        spans = exporter.get_finished_spans()
        assert _by_name(spans, "inner-a").parent.span_id == _by_name(spans, "node branch_a").context.span_id
        assert _by_name(spans, "inner-b").parent.span_id == _by_name(spans, "node branch_b").context.span_id


@requires_otel
class TestSingleTrace:
    """Falsifier 3: with no ambient context before .run(), one run = one trace."""

    def test_all_spans_share_one_trace_without_preexisting_context(self):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, exporter = _local_provider()
        tracer = provider.get_tracer("test.instrumentation")

        @node(output_name="doubled")
        def double(x: int) -> int:
            with tracer.start_as_current_span("inner-llm-call"):
                return x * 2

        # Explicitly empty ambient context: no pre-existing root to inherit —
        # the bracketless fragmentation case.
        token = otel_context.attach(Context())
        try:
            result = SyncRunner().run(
                Graph([double], name="one_trace"),
                {"x": 3},
                event_processors=[OpenTelemetryProcessor(tracer_provider=provider)],
            )
        finally:
            otel_context.detach(token)

        assert result.completed
        spans = exporter.get_finished_spans()
        assert {s.name for s in spans} == {"graph one_trace", "node double", "inner-llm-call"}
        trace_ids = {s.context.trace_id for s in spans}
        assert len(trace_ids) == 1, f"run fragmented into {len(trace_ids)} traces"
        assert _by_name(spans, "inner-llm-call").parent.span_id == _by_name(spans, "node double").context.span_id


@requires_otel
class TestFailureDetach:
    """Falsifier 4: a raising node leaves no leaked ambient context."""

    def test_sync_raising_node_restores_context(self, caplog):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, _ = _local_provider()

        @node(output_name="value")
        def boom(x: int) -> int:
            raise ValueError("boom")

        before = otel_context.get_current()
        with caplog.at_level(logging.ERROR, logger="opentelemetry.context"), pytest.raises(ValueError, match="boom"):
            SyncRunner().run(
                Graph([boom], name="fail_sync"),
                {"x": 1},
                event_processors=[OpenTelemetryProcessor(tracer_provider=provider)],
            )

        assert otel_context.get_current() == before
        _assert_no_detach_failures(caplog)

    async def test_async_raising_node_restores_context(self, caplog):
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, _ = _local_provider()

        @node(output_name="value")
        async def boom(x: int) -> int:
            raise ValueError("boom")

        before = otel_context.get_current()
        with caplog.at_level(logging.ERROR, logger="opentelemetry.context"), pytest.raises(ValueError, match="boom"):
            await AsyncRunner().run(
                Graph([boom], name="fail_async"),
                {"x": 1},
                event_processors=[OpenTelemetryProcessor(tracer_provider=provider)],
            )

        assert otel_context.get_current() == before
        _assert_no_detach_failures(caplog)

    async def test_async_pause_restores_context_without_detach_noise(self, caplog):
        """An async pause raises out of the node's task with no node end/error
        event — the token is dropped, never cross-context-detached."""
        from hypergraph.events.otel import OpenTelemetryProcessor

        provider, exporter = _local_provider()

        @interrupt(answer_name="decision")
        def approval(draft: str) -> StringQuestion:
            return StringQuestion(prompt="Approve?", evidence=(draft,))

        before = otel_context.get_current()
        with caplog.at_level(logging.ERROR, logger="opentelemetry.context"):
            result = await AsyncRunner().run(
                Graph([approval], name="pause_flow"),
                {"draft": "v1"},
                event_processors=[OpenTelemetryProcessor(tracer_provider=provider)],
            )

        assert result.paused
        assert otel_context.get_current() == before
        _assert_no_detach_failures(caplog)
        run_span = _by_name(exporter.get_finished_spans(), "graph pause_flow")
        assert dict(run_span.attributes)["hypergraph.run.outcome"] == "paused"


@requires_otel
class TestZeroCostWithoutProcessor:
    """Falsifier 5: no OpenTelemetryProcessor → no context attach, no import."""

    def test_no_processor_attaches_no_context(self):
        seen: list = []

        @node(output_name="doubled")
        def double(x: int) -> int:
            seen.append(otel_context.get_current())
            return x * 2

        before = otel_context.get_current()
        result = SyncRunner().run(Graph([double], name="plain"), {"x": 2})

        assert result.completed
        assert seen == [before]  # node body saw the untouched ambient context
        assert otel_context.get_current() == before


def test_no_processor_run_imports_no_opentelemetry():
    """Running without a processor must not import opentelemetry at all."""
    code = textwrap.dedent(
        """
        import sys

        from hypergraph import Graph, SyncRunner, node

        @node(output_name="doubled")
        def double(x: int) -> int:
            return x * 2

        result = SyncRunner().run(Graph([double], name="pure"), {"x": 2})
        assert result.completed
        loaded = [m for m in sys.modules if m == "opentelemetry" or m.startswith("opentelemetry.")]
        assert not loaded, f"opentelemetry imported on the uninstrumented hot path: {loaded}"
        """
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
