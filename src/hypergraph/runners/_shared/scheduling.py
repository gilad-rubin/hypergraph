"""Execution planning and scheduling policy shared by runners."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import networkx as nx

from hypergraph.nodes.base import HyperNode
from hypergraph.runners._shared.state import GraphState

if TYPE_CHECKING:
    from hypergraph.graph import Graph


@dataclass(frozen=True)
class ExecutionComponent:
    """A strongly connected component in execution order."""

    node_names: tuple[str, ...]
    is_cyclic: bool


@dataclass(frozen=True)
class ExecutionScope:
    """Resolved execution scope shared by scheduler and validation."""

    active_nodes: frozenset[str] | None
    startup_predecessors: dict[str, frozenset[str]]
    execution_plan: tuple[ExecutionComponent, ...]
    execution_predecessors: dict[ExecutionComponent, frozenset[ExecutionComponent]]
    execution_successors: dict[ExecutionComponent, frozenset[ExecutionComponent]]


@dataclass
class ExecutionFrontier:
    """Runtime scheduler state for SCC-level execution."""

    ordered_components: tuple[ExecutionComponent, ...]
    execution_successors: dict[ExecutionComponent, frozenset[ExecutionComponent]]
    remaining_predecessors: dict[ExecutionComponent, int]
    local_iterations: dict[ExecutionComponent, int]
    runnable_components: set[ExecutionComponent]
    completed_components: set[ExecutionComponent]
    max_iterations: int

    @classmethod
    def from_scope(cls, scope: ExecutionScope, max_iterations: int) -> ExecutionFrontier:
        """Create a frontier scheduler for the given execution scope."""
        return cls(
            ordered_components=scope.execution_plan,
            execution_successors=scope.execution_successors,
            remaining_predecessors={component: len(scope.execution_predecessors[component]) for component in scope.execution_plan},
            local_iterations={component: 0 for component in scope.execution_plan if component.is_cyclic},
            runnable_components={component for component in scope.execution_plan if not scope.execution_predecessors[component]},
            completed_components=set(),
            max_iterations=max_iterations,
        )

    def has_pending_components(self) -> bool:
        """Whether there is still work left to schedule."""
        return bool(self.runnable_components)

    def next_ready_batch(
        self,
        graph: Graph,
        state: GraphState,
        *,
        active_nodes: set[str] | frozenset[str] | None,
        startup_predecessors: dict[str, frozenset[str]],
    ) -> list[HyperNode]:
        """Return the next executable batch across runnable SCCs.

        Components that have no more ready nodes are marked complete and their
        successors become runnable immediately.
        """
        ordered_components = tuple(component for component in self.ordered_components if component in self.runnable_components)

        # Local import breaks the real scheduling/readiness ownership cycle:
        # readiness consumes the scheduling plan, while the frontier executes it.
        from hypergraph.runners._shared.readiness import get_ready_nodes_in_component

        ready_by_component: dict[ExecutionComponent, list[HyperNode]] = {}
        for component in ordered_components:
            ready = get_ready_nodes_in_component(
                graph,
                state,
                component=component,
                active_nodes=active_nodes,
                startup_predecessors=startup_predecessors,
            )
            if ready:
                ready_by_component[component] = ready

        quiescent_components = tuple(component for component in ordered_components if component not in ready_by_component)
        if quiescent_components:
            self._complete_components(quiescent_components)

        if not ready_by_component:
            return []

        for component in ready_by_component:
            self._record_iteration(component)

        return [node for component in ordered_components if component in ready_by_component for node in ready_by_component[component]]

    def _record_iteration(self, component: ExecutionComponent) -> None:
        """Consume one local iteration budget for a cyclic component."""
        if not component.is_cyclic:
            return
        if self.local_iterations[component] >= self.max_iterations:
            from hypergraph.exceptions import InfiniteLoopError

            raise InfiniteLoopError(self.max_iterations)
        self.local_iterations[component] += 1

    def _complete_components(
        self,
        components: tuple[ExecutionComponent, ...],
    ) -> None:
        """Mark components quiescent and release any newly unblocked successors."""
        for component in components:
            if component in self.completed_components:
                continue
            self.completed_components.add(component)
            self.runnable_components.discard(component)
            for successor in self.execution_successors[component]:
                self.remaining_predecessors[successor] -= 1
                if self.remaining_predecessors[successor] == 0:
                    self.runnable_components.add(successor)


def compute_execution_scope(graph: Graph) -> ExecutionScope:
    """Resolve active nodes and startup predecessors from graph configuration.

    Scope is computed from graph-level entrypoint/select settings (no runtime
    overrides). Startup predecessors include DATA + ORDERING edges and exclude
    CONTROL edges (gate activation is handled separately by routing logic).
    Execution planning uses CONTROL edges to preserve gate-first ordering and
    to treat gate-driven feedback loops as cyclic execution regions.
    """
    from hypergraph.graph.input_spec import _compute_active_scope

    if graph.entrypoints_config is None and graph.selected is None:
        active_nodes: set[str] | None = None
        active_subgraph = graph._nx_graph
    else:
        active_nodes_dict, active_subgraph = _compute_active_scope(
            graph._nodes,
            graph._nx_graph,
            entrypoints=graph.entrypoints_config,
            selected=graph.selected,
        )
        active_nodes = frozenset(active_nodes_dict)

    startup_predecessors = _compute_startup_predecessors_from_graph(active_subgraph)
    execution_plan, execution_predecessors, execution_successors = _build_execution_plan(
        graph,
        active_nodes=active_nodes,
        planning_graph=active_subgraph,
    )

    return ExecutionScope(
        active_nodes=active_nodes,
        startup_predecessors=startup_predecessors,
        execution_plan=execution_plan,
        execution_predecessors=execution_predecessors,
        execution_successors=execution_successors,
    )


def build_execution_plan(
    graph: Graph,
    *,
    active_nodes: set[str] | frozenset[str] | None = None,
    planning_graph: nx.DiGraph | None = None,
) -> tuple[ExecutionComponent, ...]:
    """Build a stable SCC execution plan for the active scope."""
    plan, _, _ = _build_execution_plan(
        graph,
        active_nodes=active_nodes,
        planning_graph=planning_graph,
    )
    return plan


def _build_execution_plan(
    graph: Graph,
    *,
    active_nodes: set[str] | frozenset[str] | None = None,
    planning_graph: nx.DiGraph | None = None,
) -> tuple[
    tuple[ExecutionComponent, ...],
    dict[ExecutionComponent, frozenset[ExecutionComponent]],
    dict[ExecutionComponent, frozenset[ExecutionComponent]],
]:
    """Build a stable SCC execution plan for the active scope.

    SCC planning includes CONTROL edges so gate-driven cycles stay local to one
    execution component and so gates are scheduled before their targets.
    """
    if planning_graph is None:
        planning_graph = graph._nx_graph

    scoped_graph = _build_planning_graph(planning_graph, active_nodes=active_nodes)
    if scoped_graph.number_of_nodes() == 0:
        return (), {}, {}

    node_order = {name: idx for idx, name in enumerate(graph._nodes)}
    sccs = list(nx.strongly_connected_components(scoped_graph))
    condensation = nx.condensation(scoped_graph, scc=sccs)
    component_by_scc = _build_execution_components(condensation, scoped_graph, node_order)

    def _scc_sort_key(scc_idx: int) -> int:
        return min(node_order[name] for name in condensation.nodes[scc_idx]["members"])

    ordered_sccs = tuple(
        nx.lexicographical_topological_sort(
            condensation,
            key=_scc_sort_key,
        )
    )
    components = tuple(component_by_scc[scc_idx] for scc_idx in ordered_sccs)
    predecessors, successors = _build_component_relations(condensation, component_by_scc)
    return components, predecessors, successors


def _build_execution_components(
    condensation: nx.DiGraph,
    scoped_graph: nx.DiGraph,
    node_order: dict[str, int],
) -> dict[int, ExecutionComponent]:
    """Build stable component objects for each SCC in the condensation graph."""
    component_by_scc: dict[int, ExecutionComponent] = {}
    for scc_idx in condensation.nodes:
        members = tuple(sorted(condensation.nodes[scc_idx]["members"], key=node_order.get))
        is_cyclic = len(members) > 1 or scoped_graph.has_edge(members[0], members[0])
        component_by_scc[scc_idx] = ExecutionComponent(node_names=members, is_cyclic=is_cyclic)
    return component_by_scc


def _build_component_relations(
    condensation: nx.DiGraph,
    component_by_scc: dict[int, ExecutionComponent],
) -> tuple[
    dict[ExecutionComponent, frozenset[ExecutionComponent]],
    dict[ExecutionComponent, frozenset[ExecutionComponent]],
]:
    """Map each execution component to its predecessor and successor SCCs."""
    predecessors: dict[ExecutionComponent, frozenset[ExecutionComponent]] = {}
    successors: dict[ExecutionComponent, frozenset[ExecutionComponent]] = {}
    for scc_idx, component in component_by_scc.items():
        predecessors[component] = frozenset(component_by_scc[pred] for pred in condensation.predecessors(scc_idx))
        successors[component] = frozenset(component_by_scc[succ] for succ in condensation.successors(scc_idx))
    return predecessors, successors


def compute_startup_predecessors(
    graph: Graph,
    *,
    active_nodes: set[str] | None,
) -> dict[str, frozenset[str]]:
    """Compute startup predecessors from graph topology.

    Uses DATA + ORDERING edges and excludes CONTROL edges.
    """
    return _compute_startup_predecessors_from_graph(
        graph._nx_graph,
        active_nodes=active_nodes,
    )


def _compute_startup_predecessors_from_graph(
    nx_graph: nx.DiGraph,
    *,
    active_nodes: set[str] | frozenset[str] | None = None,
) -> dict[str, frozenset[str]]:
    """Compute startup predecessors from a graph view."""
    predecessors: dict[str, set[str]] = {}
    for src, dst, data in nx_graph.edges(data=True):
        if src == dst:
            continue
        if active_nodes is not None and (src not in active_nodes or dst not in active_nodes):
            continue
        if data.get("edge_type") == "control":
            continue
        predecessors.setdefault(dst, set()).add(src)
    return {name: frozenset(preds) for name, preds in predecessors.items()}


def _build_planning_graph(
    nx_graph: nx.DiGraph,
    *,
    active_nodes: set[str] | frozenset[str] | None = None,
) -> nx.DiGraph:
    """Build the graph used for SCC planning and execution order.

    CONTROL edges participate here so route-driven cycles become a single
    execution region and gates are scheduled before their targets.
    """
    if active_nodes is None:
        return nx_graph.copy()
    return nx_graph.subgraph(active_nodes).copy()


def plan_interrupt_batch(ready_nodes: list[HyperNode]) -> list[HyperNode]:
    """Isolate interrupt nodes so a pause cannot cancel sibling work.

    ``PauseExecution`` extends ``BaseException``; raised inside
    ``asyncio.gather`` it would cancel all sibling tasks mid-flight. When any
    interrupt node is ready, run exactly one interrupt alone this superstep —
    the other ready nodes are deferred to the next superstep, where they
    remain ready. Batches without interrupts pass through unchanged.

    Must be applied once, before the runner captures the batch's node names
    for checkpoint metadata, so recorded batch == executed batch.
    """
    interrupts = [node for node in ready_nodes if node.is_interrupt]
    if interrupts:
        return [interrupts[0]]
    return ready_nodes


def ensure_progress_processor(
    event_processors: list[Any] | None,
) -> list[Any]:
    """Ensure at least one RichProgressProcessor is in the list.

    Returns a new list. If a RichProgressProcessor is already present,
    the list is returned as-is (copied). Otherwise a default one is prepended.
    """
    from hypergraph.events.rich_progress import RichProgressProcessor

    processors = list(event_processors) if event_processors else []
    if not any(isinstance(p, RichProgressProcessor) for p in processors):
        processors.insert(0, RichProgressProcessor())
    return processors
