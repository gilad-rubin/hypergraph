"""RunLog collector — passive event listener that builds execution traces.

The RunLogCollector is a TypedEventProcessor that listens to the event
stream already emitted during execution. It produces an immutable RunLog
containing per-node timing, status, errors, and routing decisions.

Thread safety: The EventDispatcher processes events sequentially (even in
async mode), so concurrent writes to internal dicts are not a concern.
"""

from __future__ import annotations

from hypergraph.events.processor import TypedEventProcessor
from hypergraph.events.types import (
    NodeEndEvent,
    NodeErrorEvent,
    NodeStartEvent,
    RouteDecisionEvent,
    SuperstepStartEvent,
)
from hypergraph.runners._shared.types import NodeRecord, RunLog


class RunLogCollector(TypedEventProcessor):
    """Collects execution events into NodeRecords, then builds a RunLog.

    Lifecycle:
        1. Runner prepends collector to processor list
        2. Events flow through on_* handlers during execution
        3. Runner calls build() after execution to get the frozen RunLog
    """

    def __init__(self) -> None:
        self._current_superstep: int = 0
        self._records: list[NodeRecord] = []
        self._decision_buffer: dict[str, str | list[str]] = {}
        self._node_start_times: dict[str, float] = {}  # span_id → timestamp

    def on_superstep_start(self, event: SuperstepStartEvent) -> None:
        self._current_superstep = event.superstep

    def on_node_start(self, event: NodeStartEvent) -> None:
        self._node_start_times[event.span_id] = event.timestamp

    def on_route_decision(self, event: RouteDecisionEvent) -> None:
        self._decision_buffer[event.node_name] = event.decision

    def on_node_end(self, event: NodeEndEvent) -> None:
        decision = self._decision_buffer.pop(event.node_name, None)
        self._node_start_times.pop(event.span_id, None)
        self._records.append(
            NodeRecord(
                node_name=event.node_name,
                superstep=self._current_superstep,
                duration_ms=event.duration_ms,
                status="completed",
                span_id=event.span_id,
                cached=event.cached,
                decision=decision,
                _inner_logs=getattr(event, "inner_logs", ()),
            )
        )

    def on_node_error(self, event: NodeErrorEvent) -> None:
        start_time = self._node_start_times.pop(event.span_id, None)
        duration_ms = (event.timestamp - start_time) * 1000 if start_time else 0.0
        decision = self._decision_buffer.pop(event.node_name, None)
        self._records.append(
            NodeRecord(
                node_name=event.node_name,
                superstep=self._current_superstep,
                duration_ms=duration_ms,
                status="failed",
                span_id=event.span_id,
                error=event.error,
                decision=decision,
            )
        )

    def build(self, graph_name: str, run_id: str, total_duration_ms: float) -> RunLog:
        """Produce the frozen RunLog from collected records."""
        return RunLog(
            graph_name=graph_name,
            run_id=run_id,
            total_duration_ms=total_duration_ms,
            steps=tuple(self._records),
        )
