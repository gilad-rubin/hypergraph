"""Typed, opt-in execution inspection artifacts.

Inspection is ephemeral execution evidence. It is deliberately independent of
checkpoint persistence and ordinary result serialization.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal, cast

from hypergraph.runners._shared.results import FailureEvidence, MapResult, RunResult

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
    inputs: Mapping[str, Any] | None = field(default=None, repr=False, compare=False)
    outputs: Mapping[str, Any] | None = field(default=None, repr=False, compare=False)
    failure: FailureEvidence | None = field(default=None, repr=False, compare=False)
    started_at_ms: float | None = None
    ended_at_ms: float | None = None
    duration_ms: float = 0.0
    cached: bool = False

    def __post_init__(self) -> None:
        """Own only the top-level mappings; contained values retain identity."""
        if self.inputs is not None:
            object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))
        if self.outputs is not None:
            object.__setattr__(self, "outputs", MappingProxyType(dict(self.outputs)))

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
    error: BaseException | None = field(default=None, repr=False, compare=False)
    revision: int = field(default=0, repr=False, compare=False)


MapItemInspectionStatus = Literal[
    "running",
    "completed",
    "failed",
    "paused",
    "stopped",
    "restored",
]


@dataclass(frozen=True, slots=True)
class MapItemInspection:
    """Inspection evidence for one real claimed map item."""

    item_index: int
    status: MapItemInspectionStatus
    requested_inputs: Mapping[str, Any] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    run: RunInspection | None = field(default=None, repr=False, compare=False)
    restored: bool = False

    def __post_init__(self) -> None:
        """Own the requested-input mapping without copying contained values."""
        if self.requested_inputs is not None:
            object.__setattr__(self, "requested_inputs", MappingProxyType(dict(self.requested_inputs)))


@dataclass(frozen=True, slots=True)
class MapInspection:
    """One immutable batch artifact shared by live and settled views."""

    run_id: str | None
    graph_name: str
    workflow_id: str | None
    status: str
    map_over: tuple[str, ...]
    map_mode: str
    requested_count: int
    items: tuple[MapItemInspection, ...]
    unstarted_item_indexes: tuple[int, ...]
    total_duration_ms: float
    captured: bool
    terminal: bool
    error: BaseException | None = field(default=None, repr=False, compare=False)
    revision: int = field(default=0, repr=False, compare=False)

    @property
    def completed_count(self) -> int:
        """Claimed children that completed, including restored children."""
        return sum(item.status in {"completed", "restored"} for item in self.items)

    @property
    def failed_count(self) -> int:
        """Claimed children that failed."""
        return sum(item.status == "failed" for item in self.items)

    @property
    def restored_count(self) -> int:
        """Completed children restored without executing again."""
        return sum(item.restored for item in self.items)

    @property
    def unstarted_count(self) -> int:
        """Requested inputs never claimed by the scheduler."""
        return len(self.unstarted_item_indexes)


InspectionSubscriber = Callable[[RunInspection, bool], None]
MapInspectionSubscriber = Callable[[MapInspection, bool], None]


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
            self._replace_artifact_locked(run_id=run_id)

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
            self._replace_artifact_locked(
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
            self._replace_artifact_locked(
                nodes=(*self._artifact.nodes, node),
            )
            artifact, subscribers = self._publication_locked()
        self._notify(subscribers, artifact, urgent=False)

    def abort_node(
        self,
        *,
        span_id: str,
        error: BaseException,
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
            if self._artifact.error is None:
                self._replace_artifact_locked(error=error)
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
        duration_ms: float,
        record_failure: bool = True,
    ) -> None:
        """Mark a failed execution and optionally publish its leaf evidence."""
        with self._lock:
            self._replace_node_locked(
                span_id,
                status="failed",
                failure=failure,
                ended_at_ms=ended_at_ms,
                duration_ms=duration_ms,
            )
            if record_failure:
                self._failure_span_ids.add(span_id)
            self._replace_artifact_locked(
                failures=tuple(node.failure for node in self._artifact.nodes if node.span_id in self._failure_span_ids and node.failure is not None),
                error=self._artifact.error if self._artifact.error is not None else failure.error,
            )
            artifact, subscribers = self._publication_locked()
        self._notify(subscribers, artifact, urgent=True)

    def finish(
        self,
        *,
        status: str,
        total_duration_ms: float,
        failures: tuple[FailureEvidence, ...] = (),
        error: BaseException | None = None,
    ) -> RunInspection:
        """Publish the terminal snapshot and return it."""
        with self._lock:
            terminal_failures = failures or self._artifact.failures
            settled_status = cast(
                NodeInspectionStatus,
                status if status in {"completed", "failed", "paused", "stopped"} else "failed",
            )
            settled_nodes = tuple(replace(node, status=settled_status) if node.status == "running" else node for node in self._artifact.nodes)
            self._replace_artifact_locked(
                status=status,
                nodes=settled_nodes,
                failures=tuple(terminal_failures),
                total_duration_ms=total_duration_ms,
                terminal=True,
                error=error,
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

    def subscribe_with_snapshot(
        self,
        callback: InspectionSubscriber,
    ) -> tuple[RunInspection, Callable[[], None] | None]:
        """Atomically subscribe and return the snapshot covered by that subscription."""
        with self._lock:
            artifact = self._artifact
            if artifact.terminal:
                return artifact, None
            key = self._next_subscriber
            self._next_subscriber += 1
            self._subscribers[key] = callback

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(key, None)

        return artifact, unsubscribe

    def _publication_locked(
        self,
    ) -> tuple[RunInspection, tuple[InspectionSubscriber, ...]]:
        return self._artifact, tuple(self._subscribers.values())

    def _replace_node_locked(self, span_id: str, **changes: Any) -> None:
        nodes = list(self._artifact.nodes)
        for index in range(len(nodes) - 1, -1, -1):
            if nodes[index].span_id == span_id:
                nodes[index] = replace(nodes[index], **changes)
                self._replace_artifact_locked(nodes=tuple(nodes))
                return
        raise RuntimeError(f"No inspected node execution has span_id {span_id!r}.")

    def _replace_artifact_locked(self, **changes: Any) -> None:
        self._artifact = replace(
            self._artifact,
            revision=self._artifact.revision + 1,
            **changes,
        )

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


class MapInspectionSession:
    """Thread-safe aggregation of real child runs into one batch artifact."""

    def __init__(
        self,
        *,
        graph_name: str,
        workflow_id: str | None,
        requested_count: int,
        map_over: tuple[str, ...],
        map_mode: str,
    ) -> None:
        self._lock = threading.RLock()
        self._artifact = MapInspection(
            run_id="pending",
            graph_name=graph_name,
            workflow_id=workflow_id,
            status="running",
            map_over=map_over,
            map_mode=map_mode,
            requested_count=requested_count,
            items=(),
            unstarted_item_indexes=(),
            total_duration_ms=0.0,
            captured=True,
            terminal=False,
        )
        self._subscribers: dict[int, MapInspectionSubscriber] = {}
        self._next_subscriber = 0

    def bind_run(self, run_id: str | None) -> None:
        """Bind the parent batch run ID, including ``None`` for an empty map."""
        with self._lock:
            if self._artifact.run_id not in {"pending", run_id}:
                raise RuntimeError("MapInspectionSession is already bound to another batch run.")
            self._replace_artifact_locked(run_id=run_id)

    def claim_item(
        self,
        *,
        item_index: int,
        requested_inputs: dict[str, Any],
        workflow_id: str | None,
    ) -> InspectionSession:
        """Record one real scheduler claim and return its child session."""
        with self._lock:
            if any(item.item_index == item_index for item in self._artifact.items):
                raise RuntimeError(f"Map item {item_index} was already claimed.")
            item = MapItemInspection(
                item_index=item_index,
                status="running",
                requested_inputs=requested_inputs,
            )
            self._replace_artifact_locked(
                items=tuple(
                    sorted(
                        (*self._artifact.items, item),
                        key=lambda current: current.item_index,
                    )
                ),
            )
            artifact, subscribers = self._publication_locked()
        self._notify(subscribers, artifact, urgent=False)

        child = InspectionSession(
            graph_name=self._artifact.graph_name,
            workflow_id=workflow_id,
            item_index=item_index,
        )
        child.subscribe(
            lambda run, urgent: self._publish_child(
                item_index=item_index,
                run=run,
                urgent=urgent,
            )
        )
        return child

    def settle_item(self, *, item_index: int, result: RunResult) -> None:
        """Attach the exact settled child artifact, or an honest degraded one."""
        run = result._inspection or degraded_run_inspection(result)
        status: MapItemInspectionStatus = "restored" if result.restored else result.status.value
        with self._lock:
            current = self._item_locked(item_index)
            if current.run is run and current.status == status:
                return
            self._replace_item_locked(
                item_index,
                replace(
                    current,
                    status=status,
                    run=run,
                    restored=result.restored,
                ),
            )
            artifact, subscribers = self._publication_locked()
        self._notify(subscribers, artifact, urgent=status == "failed")

    def finish(
        self,
        *,
        status: str,
        total_duration_ms: float,
        unstarted_item_indexes: tuple[int, ...] = (),
        error: BaseException | None = None,
    ) -> MapInspection:
        """Publish the terminal batch snapshot and return it."""
        with self._lock:
            self._replace_artifact_locked(
                status=status,
                unstarted_item_indexes=tuple(unstarted_item_indexes),
                total_duration_ms=total_duration_ms,
                terminal=True,
                error=error,
            )
            artifact, subscribers = self._publication_locked()
        self._notify(subscribers, artifact, urgent=True)
        return artifact

    def snapshot(self) -> MapInspection:
        """Return the latest immutable batch artifact."""
        with self._lock:
            return self._artifact

    def subscribe(self, callback: MapInspectionSubscriber) -> Callable[[], None]:
        """Subscribe to batch publications and return an unsubscriber."""
        with self._lock:
            key = self._next_subscriber
            self._next_subscriber += 1
            self._subscribers[key] = callback

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(key, None)

        return unsubscribe

    def subscribe_with_snapshot(
        self,
        callback: MapInspectionSubscriber,
    ) -> tuple[MapInspection, Callable[[], None] | None]:
        """Atomically subscribe and return the snapshot covered by that subscription."""
        with self._lock:
            artifact = self._artifact
            if artifact.terminal:
                return artifact, None
            key = self._next_subscriber
            self._next_subscriber += 1
            self._subscribers[key] = callback

        def unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(key, None)

        return artifact, unsubscribe

    def _publish_child(
        self,
        *,
        item_index: int,
        run: RunInspection,
        urgent: bool,
    ) -> None:
        with self._lock:
            current = self._item_locked(item_index)
            if current.run is not None:
                if current.run.terminal and not run.terminal:
                    return
                if run.revision < current.run.revision:
                    return
            status: MapItemInspectionStatus = run.status
            self._replace_item_locked(
                item_index,
                replace(current, status=status, run=run),
            )
            artifact, subscribers = self._publication_locked()
        self._notify(subscribers, artifact, urgent=urgent)

    def _item_locked(self, item_index: int) -> MapItemInspection:
        for item in self._artifact.items:
            if item.item_index == item_index:
                return item
        raise RuntimeError(f"Map item {item_index} has not been claimed.")

    def _replace_item_locked(
        self,
        item_index: int,
        replacement: MapItemInspection,
    ) -> None:
        self._replace_artifact_locked(
            items=tuple(replacement if item.item_index == item_index else item for item in self._artifact.items),
        )

    def _replace_artifact_locked(self, **changes: Any) -> None:
        self._artifact = replace(
            self._artifact,
            revision=self._artifact.revision + 1,
            **changes,
        )

    def _publication_locked(
        self,
    ) -> tuple[MapInspection, tuple[MapInspectionSubscriber, ...]]:
        return self._artifact, tuple(self._subscribers.values())

    @staticmethod
    def _notify(
        subscribers: tuple[MapInspectionSubscriber, ...],
        artifact: MapInspection,
        *,
        urgent: bool,
    ) -> None:
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
    failure_indexes_by_key: dict[tuple[str, int], list[int]] = {}
    for failure_index, failure in enumerate(result.node_failures):
        failure_indexes_by_key.setdefault((failure.node_name, failure.superstep), []).append(failure_index)
    unmatched_failure_indexes = set(range(len(result.node_failures)))
    nodes: list[NodeInspection] = []
    if result.log is not None:
        for sequence, step in enumerate(result.log.steps):
            matching_indexes = failure_indexes_by_key.get((step.node_name, step.superstep), [])
            failure_index = matching_indexes.pop(0) if matching_indexes else None
            failure = result.node_failures[failure_index] if failure_index is not None else None
            if failure_index is not None:
                unmatched_failure_indexes.remove(failure_index)
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
    for failure_index in sorted(unmatched_failure_indexes):
        failure = result.node_failures[failure_index]
        sequence = len(nodes)
        nodes.append(
            NodeInspection(
                run_id=result.run_id,
                span_id=f"{result.run_id}:failure:{sequence}",
                node_name=failure.node_name.rsplit("/", 1)[-1],
                qualified_name=failure.node_name,
                graph_name=failure.graph_name,
                item_index=failure.item_index,
                superstep=failure.superstep,
                sequence=sequence,
                status="failed",
                values_captured=False,
                inputs=failure.inputs,
                failure=failure,
                duration_ms=failure.duration_ms,
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
        error=result.error,
    )


def degraded_map_inspection(result: MapResult) -> MapInspection:
    """Build one honest batch view from always-on settled result facts."""
    unstarted = set(result.unstarted_item_indexes)
    item_indexes = tuple(index for index in range(result.requested_count) if index not in unstarted)
    items = tuple(
        MapItemInspection(
            item_index=item_index,
            status="restored" if item.restored else item.status.value,
            run=item._inspection or degraded_run_inspection(item),
            restored=item.restored,
        )
        for item_index, item in zip(item_indexes, result.results, strict=True)
    )
    return MapInspection(
        run_id=result.run_id,
        graph_name=result.graph_name,
        workflow_id=None,
        status=result.status.value,
        map_over=result.map_over,
        map_mode=result.map_mode,
        requested_count=result.requested_count,
        items=items,
        unstarted_item_indexes=result.unstarted_item_indexes,
        total_duration_ms=result.total_duration_ms,
        captured=False,
        terminal=True,
    )
