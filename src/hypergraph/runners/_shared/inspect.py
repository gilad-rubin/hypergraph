"""Shared inspect data structures, live state, and widget bridge."""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hypergraph._repr import unique_dom_id
from hypergraph.runners._shared.inspect_html import render_inspect_widget, render_map_inspect_widget
from hypergraph.runners._shared.types import RunStatus

if TYPE_CHECKING:
    from hypergraph.runners._shared.types import NodeRecord, RunResult


@dataclass(frozen=True)
class FailureCase:
    """Structured failure payload for a node execution."""

    node_name: str
    error: BaseException
    inputs: dict[str, Any]
    superstep: int
    duration_ms: float
    started_at_ms: float | None = None
    ended_at_ms: float | None = None
    item_index: int | None = None


@dataclass(frozen=True)
class NodeSnapshot:
    """Captured node inputs and outputs for inspect views."""

    node_name: str
    superstep: int
    inputs: dict[str, Any]
    outputs: dict[str, Any]
    duration_ms: float
    started_at_ms: float | None
    ended_at_ms: float | None
    cached: bool


class InspectCollector:
    """Collect node snapshots during execution."""

    def __init__(self) -> None:
        self._snapshots: list[NodeSnapshot] = []

    def record(
        self,
        node_name: str,
        superstep: int,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
        duration_ms: float,
        started_at_ms: float | None,
        ended_at_ms: float | None,
        cached: bool,
    ) -> None:
        self._snapshots.append(
            NodeSnapshot(
                node_name=node_name,
                superstep=superstep,
                inputs=dict(inputs),
                outputs=dict(outputs),
                duration_ms=duration_ms,
                started_at_ms=started_at_ms,
                ended_at_ms=ended_at_ms,
                cached=cached,
            )
        )

    @property
    def snapshots(self) -> tuple[NodeSnapshot, ...]:
        return tuple(self._snapshots)


@dataclass(frozen=True)
class NodeView:
    """Inspectable view of one executed node."""

    node_name: str
    status: str
    superstep: int
    duration_ms: float
    inputs: dict[str, Any] | None
    outputs: dict[str, Any] | None
    error: str | None
    cached: bool
    started_at_ms: float | None = None
    ended_at_ms: float | None = None


@dataclass(frozen=True)
class RunView:
    """Reusable inspect artifact for a single run."""

    run_id: str
    status: RunStatus | str
    nodes: tuple[NodeView, ...]
    failure: FailureCase | None
    total_duration_ms: float
    graph_html: str | None = None

    def __getitem__(self, node_name: str) -> NodeView:
        for node in reversed(self.nodes):
            if node.node_name == node_name:
                return node
        raise KeyError(node_name)

    @property
    def failures(self) -> list[FailureCase]:
        return [self.failure] if self.failure is not None else []

    def _repr_html_(self) -> str:
        return render_inspect_widget(self)


def _sort_nodes(nodes: list[NodeView]) -> tuple[NodeView, ...]:
    return tuple(
        sorted(
            nodes,
            key=lambda node: (
                float("inf") if node.started_at_ms is None else node.started_at_ms,
                node.superstep,
                node.node_name,
            ),
        )
    )


def build_live_run_view(
    run_id: str,
    snapshots: tuple[NodeSnapshot, ...],
    *,
    running_nodes: tuple[NodeView, ...] = (),
    status: RunStatus | str = "running",
    failure: FailureCase | None = None,
    total_duration_ms: float = 0.0,
    graph_html: str | None = None,
) -> RunView:
    """Build a RunView from live snapshots before a run completes."""

    nodes = list(running_nodes)
    seen = {(node.node_name, node.superstep) for node in nodes}
    for snapshot in snapshots:
        key = (snapshot.node_name, snapshot.superstep)
        if key in seen:
            continue
        nodes.append(
            NodeView(
                node_name=snapshot.node_name,
                status="cached" if snapshot.cached else "completed",
                superstep=snapshot.superstep,
                duration_ms=snapshot.duration_ms,
                inputs=snapshot.inputs,
                outputs=snapshot.outputs,
                error=None,
                cached=snapshot.cached,
                started_at_ms=snapshot.started_at_ms,
                ended_at_ms=snapshot.ended_at_ms,
            )
        )
        seen.add(key)
    if failure is not None and (failure.node_name, failure.superstep) not in seen:
        nodes.append(
            NodeView(
                node_name=failure.node_name,
                status="failed",
                superstep=failure.superstep,
                duration_ms=failure.duration_ms,
                inputs=failure.inputs,
                outputs=None,
                error=str(failure.error),
                cached=False,
                started_at_ms=failure.started_at_ms,
                ended_at_ms=failure.ended_at_ms,
            )
        )
    return RunView(
        run_id=run_id,
        status=status,
        nodes=_sort_nodes(nodes),
        failure=failure,
        total_duration_ms=total_duration_ms,
        graph_html=graph_html,
    )


def _build_node_view(
    record: NodeRecord,
    snapshots_by_key: dict[tuple[str, int], NodeSnapshot],
    failure: FailureCase | None = None,
) -> NodeView:
    snapshot = snapshots_by_key.get((record.node_name, record.superstep))
    failure_match = failure if failure is not None and failure.node_name == record.node_name and failure.superstep == record.superstep else None
    return NodeView(
        node_name=record.node_name,
        status="cached" if record.cached else record.status,
        superstep=record.superstep,
        duration_ms=record.duration_ms,
        inputs=failure_match.inputs if snapshot is None and failure_match is not None else None if snapshot is None else snapshot.inputs,
        outputs=None if snapshot is None else snapshot.outputs,
        error=record.error,
        cached=record.cached,
        started_at_ms=(
            failure_match.started_at_ms if snapshot is None and failure_match is not None else None if snapshot is None else snapshot.started_at_ms
        ),
        ended_at_ms=(
            failure_match.ended_at_ms if snapshot is None and failure_match is not None else None if snapshot is None else snapshot.ended_at_ms
        ),
    )


def build_run_view(result: RunResult) -> RunView:
    """Build a reusable inspect view from a terminal RunResult."""

    snapshots = getattr(result, "_inspect_data", None) or ()
    snapshots_by_key = {(snapshot.node_name, snapshot.superstep): snapshot for snapshot in snapshots}
    steps = result.log.steps if result.log is not None else ()
    nodes = tuple(_build_node_view(step, snapshots_by_key, result.failure) for step in steps)
    total_duration_ms = result.log.total_duration_ms if result.log is not None else 0.0
    return RunView(
        run_id=result.run_id,
        status=result.status,
        nodes=nodes,
        failure=result.failure,
        total_duration_ms=total_duration_ms,
        graph_html=getattr(result, "_inspect_graph_html", None),
    )


class LiveInspectState:
    """Thread-safe live inspect state shared by handles and widgets."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._run_id = "pending"
        self._status: RunStatus | str = "running"
        self._failure: FailureCase | None = None
        self._total_duration_ms = 0.0
        self._graph_html: str | None = None
        self._created_at = time.time()
        self._collector = InspectCollector()
        self._running_nodes: dict[tuple[str, int], NodeView] = {}

    def set_run_id(self, run_id: str) -> None:
        with self._lock:
            self._run_id = run_id

    def set_graph_html(self, graph_html: str | None) -> None:
        with self._lock:
            self._graph_html = graph_html

    def mark_running(
        self,
        node_name: str,
        superstep: int,
        inputs: dict[str, Any],
        started_at_ms: float | None,
    ) -> None:
        with self._lock:
            self._running_nodes[(node_name, superstep)] = NodeView(
                node_name=node_name,
                status="running",
                superstep=superstep,
                duration_ms=0.0,
                inputs=dict(inputs),
                outputs=None,
                error=None,
                cached=False,
                started_at_ms=started_at_ms,
                ended_at_ms=None,
            )

    def record_snapshot(
        self,
        node_name: str,
        superstep: int,
        inputs: dict[str, Any],
        outputs: dict[str, Any],
        duration_ms: float,
        started_at_ms: float | None,
        ended_at_ms: float | None,
        cached: bool,
    ) -> None:
        with self._lock:
            self._collector.record(
                node_name,
                superstep,
                inputs,
                outputs,
                duration_ms,
                started_at_ms,
                ended_at_ms,
                cached,
            )
            self._running_nodes[(node_name, superstep)] = NodeView(
                node_name=node_name,
                status="cached" if cached else "completed",
                superstep=superstep,
                duration_ms=duration_ms,
                inputs=dict(inputs),
                outputs=dict(outputs),
                error=None,
                cached=cached,
                started_at_ms=started_at_ms,
                ended_at_ms=ended_at_ms,
            )

    def record_failure(self, failure: FailureCase | None) -> None:
        if failure is None:
            return
        with self._lock:
            self._failure = failure
            self._running_nodes[(failure.node_name, failure.superstep)] = NodeView(
                node_name=failure.node_name,
                status="failed",
                superstep=failure.superstep,
                duration_ms=failure.duration_ms,
                inputs=dict(failure.inputs),
                outputs=None,
                error=str(failure.error),
                cached=False,
                started_at_ms=failure.started_at_ms,
                ended_at_ms=failure.ended_at_ms,
            )

    def finish(
        self,
        *,
        status: RunStatus | str,
        total_duration_ms: float,
        failure: FailureCase | None = None,
    ) -> None:
        with self._lock:
            self._status = status
            self._failure = failure
            self._total_duration_ms = total_duration_ms
            if failure is not None:
                self._running_nodes[(failure.node_name, failure.superstep)] = NodeView(
                    node_name=failure.node_name,
                    status="failed",
                    superstep=failure.superstep,
                    duration_ms=failure.duration_ms,
                    inputs=dict(failure.inputs),
                    outputs=None,
                    error=str(failure.error),
                    cached=False,
                    started_at_ms=failure.started_at_ms,
                    ended_at_ms=failure.ended_at_ms,
                )

    def view(self) -> RunView:
        with self._lock:
            live_duration = self._total_duration_ms
            if self._status == "running" and live_duration <= 0:
                live_duration = (time.time() - self._created_at) * 1000
            return build_live_run_view(
                self._run_id,
                self._collector.snapshots,
                running_nodes=tuple(self._running_nodes.values()),
                status=self._status,
                failure=self._failure,
                total_duration_ms=live_duration,
                graph_html=self._graph_html,
            )


def _is_notebook() -> bool:
    try:
        from IPython import get_ipython

        shell = get_ipython()
        return shell is not None and hasattr(shell, "kernel")
    except Exception:
        return False


class InspectWidget:
    """Notebook display handle for live inspect updates."""

    def __init__(self, state: LiveInspectState) -> None:
        self._state = state
        self._display_handle: Any = None
        self._widget_id = unique_dom_id("hypergraph-inspect-frame", "live")
        self._enabled = _is_notebook()

    def refresh(self) -> None:
        if not self._enabled:
            return
        from IPython.display import HTML, display

        view = self._state.view()
        html = render_inspect_widget(view, widget_id=self._widget_id)
        if self._display_handle is None:
            self._display_handle = display(HTML(html), display_id=self._widget_id)
        else:
            with contextlib.suppress(Exception):
                self._display_handle.update(HTML(html))


class MapInspectWidget:
    """Notebook display handle for batch-level inspect updates."""

    def __init__(self, *, graph_name: str | None = None) -> None:
        self._graph_name = graph_name
        self._display_handle: Any = None
        self._widget_id = unique_dom_id("hypergraph-inspect-map", graph_name or "live")
        self._enabled = _is_notebook()
        self._last_key: str | None = None

    def _update_html(self, html: str, *, key: str) -> None:
        if not self._enabled:
            return
        if self._last_key == key:
            return
        from IPython.display import HTML, display

        if self._display_handle is None:
            self._display_handle = display(HTML(html), display_id=self._widget_id)
        else:
            with contextlib.suppress(Exception):
                self._display_handle.update(HTML(html))
        self._last_key = key

    def refresh_running(self) -> None:
        self._update_html(render_map_inspect_widget(graph_name=self._graph_name), key="running")

    def refresh_result(self, result: Any) -> None:
        result_key = f"result:{getattr(result, 'run_id', None)}:{getattr(getattr(result, 'status', None), 'value', getattr(result, 'status', None))}"
        self._update_html(render_map_inspect_widget(result=result, graph_name=self._graph_name), key=result_key)

    def refresh_error(self, error: BaseException) -> None:
        self._update_html(
            render_map_inspect_widget(graph_name=self._graph_name, error=error),
            key=f"error:{type(error).__name__}:{error}",
        )
