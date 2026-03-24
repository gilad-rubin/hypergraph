"""OpenTelemetry export processor for Hypergraph execution events.

Hypergraph events remain the source of truth. This processor projects those
events into an OpenTelemetry span tree so runs can be exported to external
observability backends without replacing Hypergraph's native inspect/debug UX.
"""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from typing import TYPE_CHECKING, Any

from hypergraph.events.processor import TypedEventProcessor

if TYPE_CHECKING:
    from hypergraph.events.types import (
        CacheHitEvent,
        InterruptEvent,
        NodeEndEvent,
        NodeErrorEvent,
        NodeStartEvent,
        RouteDecisionEvent,
        RunEndEvent,
        RunStartEvent,
        StopRequestedEvent,
        SuperstepStartEvent,
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


def _set_attrs(span: Any, attributes: dict[str, Any]) -> None:
    """Set only bounded, non-null attributes on a span."""
    for key, value in attributes.items():
        if value is None:
            continue
        span.set_attribute(key, value)


def _clean_attrs(attributes: dict[str, Any]) -> dict[str, Any]:
    """Drop null attributes before sending them to the OTel SDK."""
    return {key: value for key, value in attributes.items() if value is not None}


def _base_attrs(event: Any) -> dict[str, Any]:
    return {
        "hypergraph.run_id": event.run_id,
        "hypergraph.workflow_id": event.workflow_id,
        "hypergraph.item_index": event.item_index,
    }


def _lineage_kind(event: RunStartEvent) -> str | None:
    if event.retry_of:
        return "retry"
    if event.forked_from:
        return "fork"
    if event.is_resume:
        return "resume"
    return None


_MAX_LINEAGE_CONTEXTS = 256


class OpenTelemetryProcessor(TypedEventProcessor):
    """Convert Hypergraph events into OTel spans and span events."""

    def __init__(self, tracer_name: str = "hypergraph") -> None:
        _require_opentelemetry()
        from opentelemetry import trace
        from opentelemetry.trace import Link, Status, StatusCode

        self._trace = trace
        self._tracer = trace.get_tracer(tracer_name)
        self._Link = Link
        self._Status = Status
        self._StatusCode = StatusCode
        self._spans: dict[str, Any] = {}
        self._contexts: dict[str, Any] = {}
        self._workflow_span_contexts: OrderedDict[str, Any] = OrderedDict()
        self._workflow_span_contexts_lock = Lock()

    def _get_linked_workflow_context(self, workflow_id: str | None) -> Any | None:
        """Return a recent workflow span context, or None if evicted/missing."""
        if workflow_id is None:
            return None
        with self._workflow_span_contexts_lock:
            span_context = self._workflow_span_contexts.pop(workflow_id, None)
            if span_context is None:
                return None
            self._workflow_span_contexts[workflow_id] = span_context
            return span_context

    def _remember_workflow_context(self, workflow_id: str | None, span_context: Any) -> None:
        """Remember the most recent span context for a workflow in a bounded cache."""
        if workflow_id is None:
            return
        with self._workflow_span_contexts_lock:
            self._workflow_span_contexts.pop(workflow_id, None)
            self._workflow_span_contexts[workflow_id] = span_context
            if len(self._workflow_span_contexts) > _MAX_LINEAGE_CONTEXTS:
                self._workflow_span_contexts.popitem(last=False)

    def on_run_start(self, event: RunStartEvent) -> None:
        parent_ctx = self._contexts.get(event.parent_span_id) if event.parent_span_id else None
        links = []
        lineage_kind = _lineage_kind(event)
        source_workflow_id = event.retry_of or event.forked_from or (event.workflow_id if event.is_resume else None)
        if lineage_kind is not None and source_workflow_id is not None:
            source_ctx = self._get_linked_workflow_context(source_workflow_id)
            if source_ctx is not None:
                links.append(
                    self._Link(
                        source_ctx,
                        attributes={"hypergraph.lineage.relationship": lineage_kind},
                    )
                )

        name = f"{'map' if event.is_map else 'graph'} {event.graph_name}"
        attributes = {
            **_base_attrs(event),
            "hypergraph.graph_name": event.graph_name,
            "hypergraph.is_map": event.is_map,
            "hypergraph.map_size": event.map_size,
            "hypergraph.parent_workflow_id": event.parent_workflow_id,
            "hypergraph.forked_from": event.forked_from,
            "hypergraph.fork_superstep": event.fork_superstep,
            "hypergraph.retry_of": event.retry_of,
            "hypergraph.retry_index": event.retry_index,
            "hypergraph.is_resume": event.is_resume,
        }
        span = self._tracer.start_span(
            name=name,
            context=parent_ctx,
            attributes=_clean_attrs(attributes),
            links=links,
        )
        self._spans[event.span_id] = span
        self._contexts[event.span_id] = self._trace.set_span_in_context(span)

        if event.is_resume:
            span.add_event(
                "hypergraph.resume",
                attributes=_clean_attrs({"hypergraph.source_workflow_id": event.workflow_id}),
            )
        if event.forked_from is not None:
            span.add_event(
                "hypergraph.fork",
                attributes=_clean_attrs(
                    {
                        "hypergraph.source_workflow_id": event.forked_from,
                        "hypergraph.source_superstep": event.fork_superstep,
                    }
                ),
            )
        if event.retry_of is not None:
            span.add_event(
                "hypergraph.retry",
                attributes=_clean_attrs(
                    {
                        "hypergraph.source_workflow_id": event.retry_of,
                        "hypergraph.retry_index": event.retry_index,
                    }
                ),
            )

    def on_run_end(self, event: RunEndEvent) -> None:
        span = self._spans.pop(event.span_id, None)
        self._contexts.pop(event.span_id, None)
        if span is None:
            return

        _set_attrs(
            span,
            {
                "hypergraph.graph_name": event.graph_name,
                "hypergraph.run.outcome": event.status.value,
                "hypergraph.duration_ms": event.duration_ms,
                "hypergraph.batch.total_items": event.batch_total_items,
                "hypergraph.batch.completed_items": event.batch_completed_items,
                "hypergraph.batch.failed_items": event.batch_failed_items,
                "hypergraph.batch.paused_items": event.batch_paused_items,
                "hypergraph.batch.stopped_items": event.batch_stopped_items,
                "hypergraph.batch.outcome": event.batch_outcome,
            },
        )
        if event.error:
            span.set_status(self._Status(self._StatusCode.ERROR, event.error))
            span.add_event(
                "exception",
                attributes=_clean_attrs(
                    {
                        "exception.message": event.error,
                    }
                ),
            )
        if event.workflow_id is not None:
            self._remember_workflow_context(event.workflow_id, span.get_span_context())
        span.end()

    def on_node_start(self, event: NodeStartEvent) -> None:
        parent_ctx = self._contexts.get(event.parent_span_id) if event.parent_span_id else None
        span = self._tracer.start_span(
            name=f"node {event.node_name}",
            context=parent_ctx,
            attributes={
                k: v
                for k, v in {
                    **_base_attrs(event),
                    "hypergraph.node_name": event.node_name,
                    "hypergraph.graph_name": event.graph_name,
                    "hypergraph.superstep": event.superstep,
                }.items()
                if v is not None
            },
        )
        self._spans[event.span_id] = span
        self._contexts[event.span_id] = self._trace.set_span_in_context(span)

    def on_node_end(self, event: NodeEndEvent) -> None:
        span = self._spans.pop(event.span_id, None)
        self._contexts.pop(event.span_id, None)
        if span is None:
            return
        _set_attrs(
            span,
            {
                "hypergraph.duration_ms": event.duration_ms,
                "hypergraph.cached": event.cached,
                "hypergraph.superstep": event.superstep,
            },
        )
        span.end()

    def on_node_error(self, event: NodeErrorEvent) -> None:
        span = self._spans.pop(event.span_id, None)
        self._contexts.pop(event.span_id, None)
        if span is None:
            return
        _set_attrs(
            span,
            {
                "hypergraph.error_type": event.error_type,
                "hypergraph.superstep": event.superstep,
            },
        )
        span.set_status(self._Status(self._StatusCode.ERROR, event.error))
        span.add_event(
            "exception",
            attributes=_clean_attrs(
                {
                    "exception.type": event.error_type,
                    "exception.message": event.error,
                }
            ),
        )
        span.end()

    def on_superstep_start(self, event: SuperstepStartEvent) -> None:
        span = self._spans.get(event.parent_span_id) if event.parent_span_id else None
        if span is None:
            return
        span.add_event(
            "hypergraph.superstep.start",
            attributes=_clean_attrs(
                {
                    "hypergraph.superstep": event.superstep,
                    "hypergraph.graph_name": event.graph_name,
                    "hypergraph.item_index": event.item_index,
                }
            ),
        )

    def on_route_decision(self, event: RouteDecisionEvent) -> None:
        target_span = self._spans.get(event.node_span_id) or (self._spans.get(event.parent_span_id) if event.parent_span_id else None)
        if target_span is None:
            return
        decision = event.decision if isinstance(event.decision, str) else ",".join(event.decision)
        target_span.add_event(
            "hypergraph.route.decision",
            attributes=_clean_attrs(
                {
                    "hypergraph.node_name": event.node_name,
                    "hypergraph.graph_name": event.graph_name,
                    "hypergraph.decision": decision,
                    "hypergraph.superstep": event.superstep,
                    "hypergraph.item_index": event.item_index,
                }
            ),
        )

    def on_cache_hit(self, event: CacheHitEvent) -> None:
        span = self._spans.get(event.span_id)
        if span is None:
            return
        span.add_event(
            "hypergraph.cache.hit",
            attributes=_clean_attrs(
                {
                    "hypergraph.node_name": event.node_name,
                    "hypergraph.graph_name": event.graph_name,
                    "hypergraph.superstep": event.superstep,
                }
            ),
        )

    def on_interrupt(self, event: InterruptEvent) -> None:
        span = self._spans.get(event.span_id) or (self._spans.get(event.parent_span_id) if event.parent_span_id else None)
        if span is None:
            return
        span.add_event(
            "hypergraph.pause",
            attributes=_clean_attrs(
                {
                    "hypergraph.node_name": event.node_name,
                    "hypergraph.graph_name": event.graph_name,
                    "hypergraph.response_param": event.response_param,
                    "hypergraph.superstep": event.superstep,
                    "hypergraph.item_index": event.item_index,
                }
            ),
        )
        # InterruptEvent should normally reuse the paused node span id.
        # If a fallback run span id slips through, don't end the parent span early.
        if event.span_id in self._spans and event.span_id != event.parent_span_id:
            paused_span = self._spans.pop(event.span_id)
            self._contexts.pop(event.span_id, None)
            paused_span.end()

    def on_stop_requested(self, event: StopRequestedEvent) -> None:
        span = self._spans.get(event.span_id) or (self._spans.get(event.parent_span_id) if event.parent_span_id else None)
        if span is None:
            return
        span.add_event(
            "hypergraph.stop.requested",
            attributes=_clean_attrs(
                {
                    "hypergraph.graph_name": event.graph_name,
                    "hypergraph.item_index": event.item_index,
                }
            ),
        )

    def shutdown(self) -> None:
        """End any remaining spans while preserving bounded lineage history."""
        for span in self._spans.values():
            span.end()
        self._spans.clear()
        self._contexts.clear()
