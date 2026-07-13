"""Typed, opt-in execution inspection artifacts.

Inspection is ephemeral execution evidence. It is deliberately independent of
checkpoint persistence and ordinary result serialization.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from hypergraph.runners._shared.results import FailureEvidence, RunResult

NodeInspectionStatus = Literal[
    "running",
    "completed",
    "failed",
    "paused",
    "stopped",
    "restored",
]


@dataclass(frozen=True, slots=True)
class NodeInspection:
    """One node execution captured at the executor boundary."""

    run_id: str
    span_id: str
    node_name: str
    qualified_name: str
    graph_name: str
    item_index: int | None
    superstep: int
    sequence: int
    status: NodeInspectionStatus
    values_captured: bool
    inputs: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    outputs: dict[str, Any] | None = field(default=None, repr=False, compare=False)
    failure: FailureEvidence | None = field(default=None, repr=False, compare=False)
    started_at_ms: float | None = None
    ended_at_ms: float | None = None
    duration_ms: float = 0.0
    cached: bool = False

    def __post_init__(self) -> None:
        """Own only the top-level mappings; contained values retain identity."""
        if self.inputs is not None:
            object.__setattr__(self, "inputs", dict(self.inputs))
        if self.outputs is not None:
            object.__setattr__(self, "outputs", dict(self.outputs))

    @property
    def execution_id(self) -> tuple[str, str, int, int]:
        """Stable identity for this execution, including cycles/concurrency."""
        return (self.run_id, self.span_id, self.superstep, self.sequence)


@dataclass(frozen=True, slots=True)
class RunInspection:
    """Immutable snapshot used by both live and settled inspection views."""

    run_id: str
    graph_name: str
    workflow_id: str | None
    item_index: int | None
    status: str
    nodes: tuple[NodeInspection, ...]
    failures: tuple[FailureEvidence, ...]
    total_duration_ms: float
    captured: bool
    terminal: bool


InspectionSubscriber = Callable[[RunInspection, bool], None]


class InspectionSession:
    """Thread-safe source of immutable inspection snapshots for one top-level run."""

    def __init__(
        self,
        *,
        graph_name: str,
        workflow_id: str | None,
        item_index: int | None,
    ) -> None:
        self._lock = threading.RLock()
        self._artifact = RunInspection(
            run_id="pending",
            graph_name=graph_name,
            workflow_id=workflow_id,
            item_index=item_index,
            status="running",
            nodes=(),
            failures=(),
            total_duration_ms=0.0,
            captured=True,
            terminal=False,
        )
        self._subscribers: dict[int, InspectionSubscriber] = {}
        self._next_subscriber = 0
        self._next_sequence = 0
        self._failure_span_ids: set[str] = set()

    def bind_run(self, run_id: str) -> None:
        """Bind the top-level run ID before node execution begins."""
        with self._lock:
            if self._artifact.run_id not in {"pending", run_id}:
                raise RuntimeError("InspectionSession is already bound to another top-level run.")
            self._artifact = replace(self._artifact, run_id=run_id)

    def start_node(
        self,
        *,
        run_id: str,
        span_id: str,
        node_name: str,
        qualified_name: str,
        graph_name: str,
        item_index: int | None,
        superstep: int,
        inputs: dict[str, Any],
        started_at_ms: float,
    ) -> None:
        """Publish a running execution with a shallow input snapshot."""
        with self._lock:
            sequence = self._next_sequence
            self._next_sequence += 1
            node = NodeInspection(
                run_id=run_id,
                span_id=span_id,
                node_name=node_name,
                qualified_name=qualified_name,
                graph_name=graph_name,
                item_index=item_index,
                superstep=superstep,
                sequence=sequence,
                status="running",
                values_captured=True,
                inputs=inputs,
                started_at_ms=started_at_ms,
            )
            self._artifact = replace(
                self._artifact,
                nodes=(*self._artifact.nodes, node),
            )
            artifact, subscribers = self._publication_locked()
        self._notify(subscribers, artifact, urgent=False)

    def pause_node(
        self,
        *,
        span_id: str,
        ended_at_ms: float,
        duration_ms: float,
    ) -> None:
        """Mark the executor boundary that raised a pause."""
        with self._lock:
            self._replace_node_locked(
                span_id,
                status="paused",
                ended_at_ms=ended_at_ms,
                duration_ms=duration_ms,
            )
            artifact, subscribers = self._publication_locked()
        self._notify(subscribers, artifact, urgent=True)

    def restore_node(
        self,
        *,
        run_id: str,
        span_id: str,
        node_name: str,
        qualified_name: str,
        graph_name: str,
        item_index: int | None,
        superstep: int,
        duration_ms: float,
        cached: bool,
    ) -> None:
        """Publish checkpoint metadata without loading persisted values."""
        with self._lock:
            sequence = self._next_sequence
            self._next_sequence += 1
            node = NodeInspection(
                run_id=run_id,
                span_id=span_id,
                node_name=node_name,
                qualified_name=qualified_name,
                graph_name=graph_name,
                item_index=item_index,
                superstep=superstep,
                sequence=sequence,
                status="restored",
                values_captured=False,
                duration_ms=duration_ms,
                cached=cached,
            )
            self._artifact = replace(
                self._artifact,
                nodes=(*self._artifact.nodes, node),
            )
            artifact, subscribers = self._publication_locked()
        self._notify(subscribers, artifact, urgent=False)

    def abort_node(
        self,
        *,
        span_id: str,
        ended_at_ms: float,
        duration_ms: float,
    ) -> None:
        """Mark infrastructure failure without inventing executor evidence."""
        with self._lock:
            self._replace_node_locked(
                span_id,
                status="failed",
                ended_at_ms=ended_at_ms,
                duration_ms=duration_ms,
            )
            artifact, subscribers = self._publication_locked()
        self._notify(subscribers, artifact, urgent=True)

    def finish_node(
        self,
        *,
        span_id: str,
        outputs: dict[str, Any],
        ended_at_ms: float,
        duration_ms: float,
        cached: bool,
    ) -> None:
        """Publish successful outputs for the matching execution span."""
        with self._lock:
            self._replace_node_locked(
                span_id,
                status="completed",
                outputs=dict(outputs),
                ended_at_ms=ended_at_ms,
                duration_ms=duration_ms,
                cached=cached,
            )
            artifact, subscribers = self._publication_locked()
        self._notify(subscribers, artifact, urgent=False)

    def fail_node(
        self,
        *,
        span_id: str,
        failure: FailureEvidence,
        ended_at_ms: float,
        record_failure: bool = True,
    ) -> None:
        """Mark a failed execution and optionally publish its leaf evidence."""
        with self._lock:
            self._replace_node_locked(
                span_id,
                status="failed",
                failure=failure,
                ended_at_ms=ended_at_ms,
                duration_ms=failure.duration_ms,
            )
            if record_failure:
                self._failure_span_ids.add(span_id)
            self._artifact = replace(
                self._artifact,
                failures=tuple(node.failure for node in self._artifact.nodes if node.span_id in self._failure_span_ids and node.failure is not None),
            )
            artifact, subscribers = self._publication_locked()
        self._notify(subscribers, artifact, urgent=True)

    def finish(
        self,
        *,
        status: str,
        total_duration_ms: float,
        failures: tuple[FailureEvidence, ...] = (),
    ) -> RunInspection:
        """Publish the terminal snapshot and return it."""
        with self._lock:
            terminal_failures = failures or self._artifact.failures
            self._artifact = replace(
                self._artifact,
                status=status,
                failures=tuple(terminal_failures),
                total_duration_ms=total_duration_ms,
                terminal=True,
            )
            artifact, subscribers = self._publication_locked()
        self._notify(subscribers, artifact, urgent=True)
        return artifact

    def snapshot(self) -> RunInspection:
        """Return the latest immutable artifact."""
        with self._lock:
            return self._artifact

    def subscribe(self, callback: InspectionSubscriber) -> Callable[[], None]:
        """Subscribe to latest-artifact publications and return an unsubscriber."""
        with self._lock:
            key = self._next_subscriber
            self._next_subscriber += 1
            self._subscribers[key] = callback

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(key, None)

        return unsubscribe

    def _publication_locked(
        self,
    ) -> tuple[RunInspection, tuple[InspectionSubscriber, ...]]:
        return self._artifact, tuple(self._subscribers.values())

    def _replace_node_locked(self, span_id: str, **changes: Any) -> None:
        nodes = list(self._artifact.nodes)
        for index in range(len(nodes) - 1, -1, -1):
            if nodes[index].span_id == span_id:
                nodes[index] = replace(nodes[index], **changes)
                self._artifact = replace(self._artifact, nodes=tuple(nodes))
                return
        raise RuntimeError(f"No inspected node execution has span_id {span_id!r}.")

    @staticmethod
    def _notify(
        subscribers: tuple[InspectionSubscriber, ...],
        artifact: RunInspection,
        *,
        urgent: bool,
    ) -> None:
        # Inspection presentation is observational: a broken subscriber must
        # never change workflow behavior.
        for callback in subscribers:
            with contextlib.suppress(Exception):
                callback(artifact, urgent)


@dataclass(frozen=True, slots=True)
class _InspectionContext:
    session: InspectionSession
    path: tuple[str, ...]


_CURRENT_INSPECTION: contextvars.ContextVar[_InspectionContext | None] = contextvars.ContextVar("hypergraph_current_inspection", default=None)


@contextlib.contextmanager
def inspection_scope(
    session: InspectionSession | None,
    path: tuple[str, ...] = (),
) -> Iterator[None]:
    """Bind or explicitly clear inspection for this runner execution."""
    context = _InspectionContext(session=session, path=path) if session is not None else None
    token = _CURRENT_INSPECTION.set(context)
    try:
        yield
    finally:
        _CURRENT_INSPECTION.reset(token)


def current_inspection() -> tuple[InspectionSession, tuple[str, ...]] | None:
    """Return the current session and qualified-path prefix, if enabled."""
    context = _CURRENT_INSPECTION.get()
    if context is None:
        return None
    return context.session, context.path


def degraded_run_inspection(result: RunResult) -> RunInspection:
    """Build an honest view from always-on facts when values were not captured."""
    failures_by_key = {(failure.node_name, failure.superstep): failure for failure in result.node_failures}
    nodes: list[NodeInspection] = []
    if result.log is not None:
        for sequence, step in enumerate(result.log.steps):
            failure = failures_by_key.get((step.node_name, step.superstep))
            nodes.append(
                NodeInspection(
                    run_id=result.run_id,
                    span_id=step.span_id,
                    node_name=step.node_name,
                    qualified_name=step.node_name,
                    graph_name=result.log.graph_name,
                    item_index=failure.item_index if failure is not None else None,
                    superstep=step.superstep,
                    sequence=sequence,
                    status=step.status,
                    values_captured=False,
                    inputs=failure.inputs if failure is not None else None,
                    failure=failure,
                    duration_ms=step.duration_ms,
                    cached=step.cached,
                )
            )
    return RunInspection(
        run_id=result.run_id,
        graph_name=result.log.graph_name if result.log is not None else "",
        workflow_id=result.workflow_id,
        item_index=(result.failure.item_index if result.failure is not None else None),
        status=result.status.value,
        nodes=tuple(nodes),
        failures=result.node_failures,
        total_duration_ms=(result.log.total_duration_ms if result.log is not None else 0.0),
        captured=False,
        terminal=True,
    )
