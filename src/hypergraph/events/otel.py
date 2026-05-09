"""OpenTelemetry export processor for Hypergraph execution events.

Hypergraph events remain the source of truth. This processor projects that
native event stream into OpenTelemetry spans so external backends can observe
runs, nodes, pauses, stops, and lineage without replacing Hypergraph's own
inspect/checkpoint UX.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.events.processor import TypedEventProcessor

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

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


def _to_ns(timestamp: float) -> int:
    """Convert event timestamps to OTel nanoseconds since epoch."""
    return int(timestamp * 1_000_000_000)


def _lineage_depth(workflow_id: str | None) -> int | None:
    """Approximate nested workflow depth from the workflow ID path."""
    if not workflow_id:
        return None
    return workflow_id.count("/")


def _sequence_attr(values: Sequence[str]) -> tuple[str, ...]:
    """Normalize sequence attributes for OTel exporters."""
    return tuple(values)


def _set_attributes(span: Any, attributes: Mapping[str, object]) -> None:
    """Set only non-null OTel-compatible attributes."""
    for key, value in attributes.items():
        if value is None:
            continue
        span.set_attribute(key, value)


class OpenTelemetryProcessor(TypedEventProcessor):
    """Convert Hypergraph events into OpenTelemetry spans and span events.

    Semantic model:
        - graph/map execution scopes become run spans
        - node execution scopes become node spans
        - route/cache/superstep/pause/stop become span events
        - Hypergraph status stays in explicit attributes, with OTel error status
          used only for actual failures
    """

    def __init__(self, tracer_name: str = "hypergraph") -> None:
        _require_opentelemetry()
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode

        self._tracer = trace.get_tracer(tracer_name)
        self._trace = trace
        self._Status = Status
        self._StatusCode = StatusCode
        self._spans: dict[str, Any] = {}
        self._contexts: dict[str, Any] = {}

    def _run_span_name(self, event: RunStartEvent) -> str:
        graph_name = event.graph_name or "<anonymous>"
        return f"{'map' if event.is_map else 'graph'} {graph_name}"

    def _node_span_name(self, event: NodeStartEvent) -> str:
        node_name = event.node_name or "<anonymous>"
        return f"node {node_name}"

    def _common_attrs(self, *, run_id: str, workflow_id: str | None, item_index: int | None, graph_name: str | None) -> dict[str, object]:
        return {
            "hypergraph.run_id": run_id,
            "hypergraph.workflow_id": workflow_id,
            "hypergraph.item_index": item_index,
            "hypergraph.graph_name": graph_name,
            "hypergraph.lineage_depth": _lineage_depth(workflow_id),
        }

    def _run_attrs(self, event: RunStartEvent) -> dict[str, object]:
        attrs = self._common_attrs(
            run_id=event.run_id,
            workflow_id=event.workflow_id,
            item_index=event.item_index,
            graph_name=event.graph_name,
        )
        attrs.update(
            {
                "hypergraph.span.kind": "run",
                "hypergraph.run.kind": "map" if event.is_map else "graph",
                "hypergraph.map_size": event.map_size,
                "hypergraph.parent_workflow_id": event.parent_workflow_id,
                "hypergraph.forked_from": event.forked_from,
                "hypergraph.fork_superstep": event.fork_superstep,
                "hypergraph.retry_of": event.retry_of,
                "hypergraph.retry_index": event.retry_index,
                "hypergraph.is_resume": event.is_resume,
            }
        )
        return attrs

    def _node_attrs(self, event: NodeStartEvent) -> dict[str, object]:
        attrs = self._common_attrs(
            run_id=event.run_id,
            workflow_id=event.workflow_id,
            item_index=event.item_index,
            graph_name=event.graph_name,
        )
        attrs.update(
            {
                "hypergraph.span.kind": "node",
                "hypergraph.node_name": event.node_name,
                "hypergraph.superstep": event.superstep,
            }
        )
        return attrs

    def _lifecycle_event_attrs(self, event: RunStartEvent) -> dict[str, object]:
        return {
            "hypergraph.workflow_id": event.workflow_id,
            "hypergraph.parent_workflow_id": event.parent_workflow_id,
            "hypergraph.forked_from": event.forked_from,
            "hypergraph.fork_superstep": event.fork_superstep,
            "hypergraph.retry_of": event.retry_of,
            "hypergraph.retry_index": event.retry_index,
            "hypergraph.item_index": event.item_index,
        }

    def _exception_event_attrs(self, error_type: str | None, error: str | None) -> dict[str, object]:
        return {
            "exception.type": error_type or "Exception",
            "exception.message": error or "",
        }

    def _end_span(self, span_id: str, *, end_timestamp: float | None = None) -> Any | None:
        span = self._spans.pop(span_id, None)
        self._contexts.pop(span_id, None)
        if span is None:
            return None
        span.end(end_time=_to_ns(end_timestamp) if end_timestamp is not None else None)
        return span

    def on_run_start(self, event: RunStartEvent) -> None:
        parent_ctx = self._contexts.get(event.parent_span_id) if event.parent_span_id else None
        span = self._tracer.start_span(
            name=self._run_span_name(event),
            context=parent_ctx,
            start_time=_to_ns(event.timestamp),
            attributes=self._run_attrs(event),
        )
        self._spans[event.span_id] = span
        self._contexts[event.span_id] = self._trace.set_span_in_context(span)

        if event.retry_of:
            span.add_event(
                "hypergraph.retry",
                attributes=self._lifecycle_event_attrs(event),
                timestamp=_to_ns(event.timestamp),
            )
        elif event.forked_from:
            span.add_event(
                "hypergraph.fork",
                attributes=self._lifecycle_event_attrs(event),
                timestamp=_to_ns(event.timestamp),
            )
        elif event.is_resume:
            span.add_event(
                "hypergraph.resume",
                attributes=self._lifecycle_event_attrs(event),
                timestamp=_to_ns(event.timestamp),
            )

    def on_run_end(self, event: RunEndEvent) -> None:
        span = self._spans.get(event.span_id)
        if span is None:
            return

        _set_attributes(
            span,
            {
                "hypergraph.duration_ms": event.duration_ms,
                "hypergraph.status": event.status.value,
                "hypergraph.parent_workflow_id": event.parent_workflow_id,
                "hypergraph.forked_from": event.forked_from,
                "hypergraph.fork_superstep": event.fork_superstep,
                "hypergraph.retry_of": event.retry_of,
                "hypergraph.retry_index": event.retry_index,
                "hypergraph.is_resume": event.is_resume,
            },
        )

        if event.status.value == "failed":
            if event.error:
                span.add_event(
                    "exception",
                    attributes=self._exception_event_attrs("hypergraph.run", event.error),
                    timestamp=_to_ns(event.timestamp),
                )
                _set_attributes(
                    span,
                    {
                        "error.type": "hypergraph.run",
                        "hypergraph.error.message": event.error,
                    },
                )
            span.set_status(self._Status(self._StatusCode.ERROR, event.error or "run failed"))
        elif event.status.value == "completed":
            span.set_status(self._Status(self._StatusCode.OK))
        else:
            span.add_event(
                f"hypergraph.{event.status.value}",
                attributes={"hypergraph.status": event.status.value},
                timestamp=_to_ns(event.timestamp),
            )

        self._end_span(event.span_id, end_timestamp=event.timestamp)

    def on_node_start(self, event: NodeStartEvent) -> None:
        parent_ctx = self._contexts.get(event.parent_span_id) if event.parent_span_id else None
        span = self._tracer.start_span(
            name=self._node_span_name(event),
            context=parent_ctx,
            start_time=_to_ns(event.timestamp),
            attributes=self._node_attrs(event),
        )
        self._spans[event.span_id] = span
        self._contexts[event.span_id] = self._trace.set_span_in_context(span)

    def on_node_end(self, event: NodeEndEvent) -> None:
        span = self._spans.get(event.span_id)
        if span is None:
            return
        _set_attributes(
            span,
            {
                "hypergraph.duration_ms": event.duration_ms,
                "hypergraph.cached": event.cached,
                "hypergraph.superstep": event.superstep,
            },
        )
        span.set_status(self._Status(self._StatusCode.OK))
        self._end_span(event.span_id, end_timestamp=event.timestamp)

    def on_cache_hit(self, event: CacheHitEvent) -> None:
        span = self._spans.get(event.span_id)
        if span is None:
            return
        span.add_event(
            "hypergraph.cache.hit",
            attributes={"hypergraph.superstep": event.superstep},
            timestamp=_to_ns(event.timestamp),
        )
        span.set_attribute("hypergraph.cached", True)

    def on_route_decision(self, event: RouteDecisionEvent) -> None:
        node_span = self._spans.get(event.node_span_id or event.parent_span_id or event.span_id)
        if node_span is None:
            return
        decision: object = _sequence_attr(event.decision) if isinstance(event.decision, list) else event.decision
        node_span.add_event(
            "hypergraph.route.decision",
            attributes={
                "hypergraph.node_name": event.node_name,
                "hypergraph.decision": decision,
                "hypergraph.superstep": event.superstep,
            },
            timestamp=_to_ns(event.timestamp),
        )

    def on_superstep_start(self, event: SuperstepStartEvent) -> None:
        run_span = self._spans.get(event.parent_span_id) if event.parent_span_id else None
        if run_span is None:
            return
        run_span.add_event(
            "hypergraph.superstep.start",
            attributes={"hypergraph.superstep": event.superstep},
            timestamp=_to_ns(event.timestamp),
        )

    def on_interrupt(self, event: InterruptEvent) -> None:
        span = self._spans.get(event.span_id)
        if span is None:
            return
        span.add_event(
            "hypergraph.pause",
            attributes={
                "hypergraph.response_param": event.response_param,
                "hypergraph.superstep": event.superstep,
            },
            timestamp=_to_ns(event.timestamp),
        )
        span.set_attribute("hypergraph.status", "paused")
        self._end_span(event.span_id, end_timestamp=event.timestamp)

    def on_stop_requested(self, event: StopRequestedEvent) -> None:
        run_span = self._spans.get(event.span_id)
        if run_span is None:
            return

        attrs: dict[str, object] = {
            "hypergraph.info_present": event.info is not None,
        }
        if isinstance(event.info, dict):
            kind = event.info.get("kind")
            if isinstance(kind, (str, bool, int, float)):
                attrs["hypergraph.stop.kind"] = kind

        run_span.add_event(
            "hypergraph.stop.requested",
            attributes=attrs,
            timestamp=_to_ns(event.timestamp),
        )

    def on_node_error(self, event: NodeErrorEvent) -> None:
        span = self._spans.get(event.span_id)
        if span is None:
            return
        _set_attributes(
            span,
            {
                "error.type": event.error_type or "Exception",
                "hypergraph.error.message": event.error,
                "hypergraph.superstep": event.superstep,
            },
        )
        span.add_event(
            "exception",
            attributes=self._exception_event_attrs(event.error_type, event.error),
            timestamp=_to_ns(event.timestamp),
        )
        span.set_status(self._Status(self._StatusCode.ERROR, event.error or event.error_type or "node failed"))
        self._end_span(event.span_id, end_timestamp=event.timestamp)

    def shutdown(self) -> None:
        """End any remaining open spans as a safety net."""
        for span_id in list(self._spans):
            self._end_span(span_id)
