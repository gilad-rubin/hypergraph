"""Focused contracts for shared runner decisions extracted by ticket #184."""

from __future__ import annotations

import pytest

from hypergraph import Graph
from hypergraph.checkpointers.types import Checkpoint, Run, WorkflowStatus
from hypergraph.events.types import NodeEndEvent, NodeErrorEvent
from hypergraph.exceptions import (
    GraphChangedError,
    InputOverrideRequiresForkError,
    WorkflowAlreadyCompletedError,
    WorkflowForkError,
    WorkflowStoppedError,
)
from hypergraph.runners._shared.lineage import (
    ResumeAction,
    plan_lineage,
    resolve_existing_run,
    validate_lineage_request,
)
from hypergraph.runners._shared.map_resume import (
    MAP_SIGNATURE_CONFIG_KEY,
    claim_completed_child_run_id,
    compute_map_item_signature,
    index_completed_child_runs,
)
from hypergraph.runners._shared.run_log import RunLogCollector


def test_map_signatures_normalize_mapping_and_set_order() -> None:
    first = {"payload": {"b": {3, 1, 2}, "a": [1, 2]}}
    second = {"payload": {"a": [1, 2], "b": {2, 3, 1}}}

    first_signature = compute_map_item_signature(first, ["payload"], "zip")
    second_signature = compute_map_item_signature(second, ["payload"], "zip")

    assert first_signature == second_signature
    assert len(first_signature) == 16


def test_map_resume_claims_distinct_sorted_signatures_before_legacy_index() -> None:
    child_runs = [
        Run(
            id="batch/b",
            status=WorkflowStatus.COMPLETED,
            config={MAP_SIGNATURE_CONFIG_KEY: "same"},
        ),
        Run(
            id="batch/a",
            status=WorkflowStatus.COMPLETED,
            config={MAP_SIGNATURE_CONFIG_KEY: "same"},
        ),
        Run(id="batch/3", status=WorkflowStatus.COMPLETED),
    ]
    by_signature, by_index = index_completed_child_runs(child_runs, "batch")

    assert claim_completed_child_run_id(idx=3, signature="same", by_signature=by_signature, by_index=by_index) == "batch/a"
    assert claim_completed_child_run_id(idx=3, signature="same", by_signature=by_signature, by_index=by_index) == "batch/b"
    assert claim_completed_child_run_id(idx=3, signature="missing", by_signature=by_signature, by_index=by_index) == "batch/3"


@pytest.mark.parametrize(
    ("fork_from", "retry_from", "checkpoint", "message"),
    [
        ("fork", "retry", None, "Cannot pass both fork_from and retry_from"),
        ("fork", None, Checkpoint(values={}, steps=[]), "Cannot combine checkpoint with fork_from/retry_from"),
    ],
)
def test_lineage_request_conflicts_precede_io(fork_from, retry_from, checkpoint, message) -> None:
    with pytest.raises(ValueError, match=message):
        validate_lineage_request(
            checkpoint=checkpoint,
            fork_from=fork_from,
            retry_from=retry_from,
        )


def test_existing_run_resolution_actions_and_lineage_projection() -> None:
    graph = Graph([])
    checkpoint = Checkpoint(
        values={},
        steps=[],
        source_run_id="source",
        source_superstep=3,
        retry_of="retry-source",
        retry_index=2,
    )
    existing = Run(
        id="workflow",
        status=WorkflowStatus.FAILED,
        config={"graph_struct_hash": graph.structural_hash},
    )

    assert (
        resolve_existing_run(
            existing_run=None,
            checkpoint=None,
            override_workflow=False,
            workflow_id="workflow",
            graph_hash=graph.structural_hash,
            graph=graph,
            resume_values={},
        )
        is ResumeAction.START_NEW
    )
    assert (
        resolve_existing_run(
            existing_run=None,
            checkpoint=checkpoint,
            override_workflow=False,
            workflow_id="workflow",
            graph_hash=graph.structural_hash,
            graph=graph,
            resume_values={},
        )
        is ResumeAction.USE_CHECKPOINT
    )
    assert (
        resolve_existing_run(
            existing_run=existing,
            checkpoint=None,
            override_workflow=True,
            workflow_id="workflow",
            graph_hash="changed",
            graph=graph,
            resume_values={"x": 1},
        )
        is ResumeAction.FORK_EXISTING
    )
    assert (
        resolve_existing_run(
            existing_run=existing,
            checkpoint=None,
            override_workflow=False,
            workflow_id="workflow",
            graph_hash=graph.structural_hash,
            graph=graph,
            resume_values={},
        )
        is ResumeAction.RESUME_EXISTING
    )

    resumed = plan_lineage(
        parent_workflow_id="parent",
        checkpoint=checkpoint,
        action=ResumeAction.RESUME_EXISTING,
    )
    assert resumed.is_resume
    assert resumed.forked_from is None

    retry = plan_lineage(
        parent_workflow_id="parent",
        checkpoint=checkpoint,
        action=ResumeAction.USE_CHECKPOINT,
    )
    assert not retry.is_resume
    assert retry.forked_from == "source"
    assert retry.fork_superstep == 3
    assert retry.retry_of == "retry-source"
    assert retry.retry_index == 2


@pytest.mark.parametrize(
    ("run", "checkpoint", "values", "error"),
    [
        (Run(id="workflow", status=WorkflowStatus.ACTIVE), Checkpoint(values={}, steps=[]), {}, WorkflowForkError),
        (
            Run(id="workflow", status=WorkflowStatus.ACTIVE, config={"graph_struct_hash": "old"}),
            None,
            {},
            GraphChangedError,
        ),
        (Run(id="workflow", status=WorkflowStatus.STOPPED), None, {}, WorkflowStoppedError),
        (Run(id="workflow", status=WorkflowStatus.FAILED), None, {"x": 1}, InputOverrideRequiresForkError),
        (Run(id="workflow", status=WorkflowStatus.COMPLETED), None, {}, WorkflowAlreadyCompletedError),
    ],
)
def test_existing_run_rejections_keep_exception_precedence(run, checkpoint, values, error) -> None:
    graph = Graph([])
    with pytest.raises(error):
        resolve_existing_run(
            existing_run=run,
            checkpoint=checkpoint,
            override_workflow=False,
            workflow_id="workflow",
            graph_hash=graph.structural_hash,
            graph=graph,
            resume_values=values,
        )


def test_run_log_collector_owns_step_counts() -> None:
    collector = RunLogCollector()
    collector.on_node_end(NodeEndEvent(run_id="run", node_name="ok", duration_ms=1.0))
    collector.on_node_error(NodeErrorEvent(run_id="run", node_name="bad", error="boom"))

    assert collector.step_count == 2
    assert collector.failed_step_count == 1
