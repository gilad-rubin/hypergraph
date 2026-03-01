"""Shared checkpoint helpers for building StepRecords.

Pure data transformation — no I/O, no durability dispatch.
Used by both AsyncRunner and SyncRunner to build step records
after each superstep completes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.checkpointers.types import StepRecord, StepStatus, _utcnow

if TYPE_CHECKING:
    from hypergraph.graph import Graph
    from hypergraph.runners._shared.types import GraphState


def build_superstep_records(
    workflow_id: str,
    superstep_idx: int,
    state: GraphState,
    ready_node_names: list[str],
    prev_input_versions: dict[str, dict[str, int]],
    node_order: dict[str, int],
    step_counter: int,
    graph: Graph,
    superstep_error: BaseException | None = None,
) -> tuple[list[StepRecord], int]:
    """Build StepRecords for nodes scheduled in a completed superstep.

    Uses ready_node_names (not set-diff on node_executions keys) so that
    cyclic re-executions are captured. Distinguishes fresh vs stale entries
    via input_versions comparison to correctly mark failed nodes.

    Returns (records, updated_step_counter).
    """
    sorted_names = sorted(ready_node_names, key=lambda name: node_order.get(name, 0))
    records: list[StepRecord] = []

    for name in sorted_names:
        execution = state.node_executions.get(name)
        now = _utcnow()
        node_type = type(graph._nodes[name]).__name__ if name in graph._nodes else None
        child_run_id = _compute_child_run_id(workflow_id, name, graph)

        if execution is not None:
            is_fresh = name not in prev_input_versions or execution.input_versions != prev_input_versions[name]
            if is_fresh:
                record = StepRecord(
                    run_id=workflow_id,
                    superstep=superstep_idx,
                    node_name=name,
                    index=step_counter,
                    status=StepStatus.COMPLETED,
                    input_versions=execution.input_versions,
                    values=execution.outputs,
                    duration_ms=execution.duration_ms,
                    cached=execution.cached,
                    decision=_normalize_decision(state.routing_decisions.get(name)),
                    node_type=node_type,
                    created_at=now,
                    completed_at=now,
                    child_run_id=child_run_id,
                )
            elif superstep_error is not None:
                record = StepRecord(
                    run_id=workflow_id,
                    superstep=superstep_idx,
                    node_name=name,
                    index=step_counter,
                    status=StepStatus.FAILED,
                    input_versions=execution.input_versions,
                    error=_extract_error_message(superstep_error),
                    node_type=node_type,
                    created_at=now,
                    child_run_id=child_run_id,
                )
            else:
                continue
        elif superstep_error is not None:
            record = StepRecord(
                run_id=workflow_id,
                superstep=superstep_idx,
                node_name=name,
                index=step_counter,
                status=StepStatus.FAILED,
                input_versions={},
                error=_extract_error_message(superstep_error),
                node_type=node_type,
                created_at=now,
                child_run_id=child_run_id,
            )
        else:
            continue

        records.append(record)
        step_counter += 1

    return records, step_counter


def _compute_child_run_id(workflow_id: str, node_name: str, graph: Graph) -> str | None:
    """Deterministic child_run_id for GraphNode steps."""
    from hypergraph.nodes.graph_node import GraphNode

    node = graph._nodes.get(node_name)
    if isinstance(node, GraphNode):
        return f"{workflow_id}/{node_name}"
    return None


def _extract_error_message(error: BaseException) -> str:
    """Extract a human-readable error message from a (possibly wrapped) exception."""
    cause = error.__cause__ if error.__cause__ is not None else error
    return str(cause)


def _normalize_decision(decision: Any) -> str | list[str] | None:
    """Convert routing decision to a JSON-serializable form.

    Gate nodes store the END sentinel (a class) as a decision value.
    This converts it to the string "END" for persistence.
    """
    if decision is None:
        return None
    from hypergraph.nodes.gate import END as _END

    if decision is _END:
        return "END"
    if isinstance(decision, list):
        return [("END" if d is _END else d) for d in decision]
    return decision
