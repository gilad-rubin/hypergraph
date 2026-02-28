"""OpenTelemetry export processor — converts Hypergraph events to OTel spans.

Opt-in via::

    pip install hypergraph[otel]

Usage::

    from hypergraph.events.otel import OpenTelemetryProcessor

    runner = AsyncRunner(processors=[OpenTelemetryProcessor()])

Events stream to whatever OTel backend is configured (Logfire, Jaeger,
Datadog, Honeycomb, etc.). RunLog still works in parallel — they're
independent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.events.processor import TypedEventProcessor

if TYPE_CHECKING:
    from hypergraph.events.types import (
        NodeEndEvent,
        NodeErrorEvent,
        NodeStartEvent,
        RouteDecisionEvent,
        RunEndEvent,
        RunStartEvent,
    )


def _require_opentelemetry() -> None:
    """Raise a clear error if opentelemetry is not installed."""
    try:
        import opentelemetry  # noqa: F401
    except ImportError:
        raise ImportError(
            "The 'opentelemetry' package is required for OpenTelemetryProcessor. "
            "Install with: pip install 'hypergraph[otel]' "
            "or: pip install opentelemetry-api opentelemetry-sdk"
        ) from None


class OpenTelemetryProcessor(TypedEventProcessor):
    """Converts Hypergraph events to OpenTelemetry spans.

    Mapping:
        RunStartEvent    → root span (``run:{graph_name}``)
        NodeStartEvent   → child span (``node:{node_name}``)
        NodeEndEvent     → end child span with attributes
        RouteDecisionEvent → span event on the run span
        NodeErrorEvent   → error status + end child span
        RunEndEvent      → end root span
    """

    def __init__(self, tracer_name: str = "hypergraph") -> None:
        _require_opentelemetry()
        from opentelemetry import trace
        from opentelemetry.trace import StatusCode

        self._tracer = trace.get_tracer(tracer_name)
        self._trace = trace
        self._StatusCode = StatusCode
        self._spans: dict[str, Any] = {}  # span_id → OTel Span
        self._contexts: dict[str, Any] = {}  # span_id → OTel Context

    def on_run_start(self, event: RunStartEvent) -> None:
        parent_ctx = self._contexts.get(event.parent_span_id) if event.parent_span_id else None
        span = self._tracer.start_span(
            name=f"run:{event.graph_name}",
            context=parent_ctx,
            attributes={
                "hypergraph.graph_name": event.graph_name,
                "hypergraph.run_id": event.run_id,
                "hypergraph.is_map": event.is_map,
            },
        )
        self._spans[event.span_id] = span
        self._contexts[event.span_id] = self._trace.set_span_in_context(span)

    def on_run_end(self, event: RunEndEvent) -> None:
        span = self._spans.pop(event.span_id, None)
        self._contexts.pop(event.span_id, None)
        if span is None:
            return
        span.set_attribute("hypergraph.duration_ms", event.duration_ms)
        span.set_attribute("hypergraph.status", event.status.value)
        if event.error:
            span.set_status(self._StatusCode.ERROR, event.error)
        span.end()

    def on_node_start(self, event: NodeStartEvent) -> None:
        parent_ctx = self._contexts.get(event.parent_span_id) if event.parent_span_id else None
        span = self._tracer.start_span(
            name=f"node:{event.node_name}",
            context=parent_ctx,
            attributes={
                "hypergraph.node_name": event.node_name,
                "hypergraph.graph_name": event.graph_name,
            },
        )
        self._spans[event.span_id] = span
        self._contexts[event.span_id] = self._trace.set_span_in_context(span)

    def on_node_end(self, event: NodeEndEvent) -> None:
        span = self._spans.pop(event.span_id, None)
        self._contexts.pop(event.span_id, None)
        if span is None:
            return
        span.set_attribute("hypergraph.duration_ms", event.duration_ms)
        span.set_attribute("hypergraph.cached", event.cached)
        span.end()

    def on_route_decision(self, event: RouteDecisionEvent) -> None:
        # Attach as event on the run span (parent)
        run_span = self._spans.get(event.parent_span_id) if event.parent_span_id else None
        if run_span is None:
            return
        decision = event.decision if isinstance(event.decision, str) else ",".join(event.decision)
        run_span.add_event(
            "route_decision",
            attributes={"node_name": event.node_name, "decision": decision},
        )

    def on_node_error(self, event: NodeErrorEvent) -> None:
        span = self._spans.pop(event.span_id, None)
        self._contexts.pop(event.span_id, None)
        if span is None:
            return
        span.set_status(self._StatusCode.ERROR, event.error)
        span.set_attribute("hypergraph.error_type", event.error_type)
        span.end()

    def shutdown(self) -> None:
        """End any remaining open spans (safety net)."""
        for span in self._spans.values():
            span.end()
        self._spans.clear()
        self._contexts.clear()
