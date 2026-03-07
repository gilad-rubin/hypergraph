"""E2E lineage semantics for checkpoint/resume/fork.

These tests codify the v3 decisions from the checkpoint design sessions:
- checkpointer + no workflow_id => auto-generated workflow_id
- resume is strict: no input overrides
- completed workflows are terminal
- structural graph changes require fork
- explicit fork uses checkpoint + new workflow_id
- gate outputs are persisted as internal values (_gate_name)
"""

from __future__ import annotations

import pytest

from hypergraph import END, AsyncRunner, Graph, RunStatus, ifelse, interrupt, node
from hypergraph.checkpointers import SqliteCheckpointer
from hypergraph.exceptions import (
    GraphChangedError,
    InputOverrideRequiresForkError,
    WorkflowAlreadyCompletedError,
    WorkflowForkError,
)

aiosqlite = pytest.importorskip("aiosqlite")


@pytest.fixture
def checkpointer(tmp_path):
    cp = SqliteCheckpointer(str(tmp_path / "lineage.db"))
    yield cp


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3


class TestLineageSemantics:
    async def test_auto_generates_workflow_id_when_missing(self, checkpointer):
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double])

        result = await runner.run(graph, {"x": 5})

        assert result.workflow_id is not None
        assert result.workflow_id.startswith("run-")
        run = checkpointer.get_run(result.workflow_id)
        assert run is not None
        assert run.config is not None
        assert "graph_struct_hash" in run.config
        assert "graph_code_hash" in run.config

    async def test_resume_with_values_requires_fork(self, checkpointer):
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        await runner.run(graph, {"x": 5}, workflow_id="wf-resume")

        with pytest.raises(InputOverrideRequiresForkError):
            await runner.run(graph, {"x": 100}, workflow_id="wf-resume")

    async def test_completed_workflow_is_terminal(self, checkpointer):
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        await runner.run(graph, {"x": 5}, workflow_id="wf-done")

        with pytest.raises(WorkflowAlreadyCompletedError):
            await runner.run(graph, workflow_id="wf-done")

    async def test_graph_change_requires_fork(self, checkpointer):
        runner = AsyncRunner(checkpointer=checkpointer)
        graph_v1 = Graph([double])
        graph_v2 = Graph([double, triple])

        await runner.run(graph_v1, {"x": 5}, workflow_id="wf-graph")

        with pytest.raises(GraphChangedError):
            await runner.run(graph_v2, workflow_id="wf-graph")

    async def test_checkpoint_fork_new_workflow_id(self, checkpointer):
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        await runner.run(graph, {"x": 5}, workflow_id="wf-parent")
        checkpoint = checkpointer.checkpoint("wf-parent")

        forked = await runner.run(
            graph,
            {"x": 100},
            checkpoint=checkpoint,
            workflow_id="wf-fork",
        )
        assert forked["doubled"] == 200
        assert forked["tripled"] == 600

    async def test_cannot_fork_into_existing_workflow(self, checkpointer):
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double])

        await runner.run(graph, {"x": 5}, workflow_id="wf-a")
        await runner.run(graph, {"x": 6}, workflow_id="wf-b")
        checkpoint = checkpointer.checkpoint("wf-a")

        with pytest.raises(WorkflowForkError):
            await runner.run(graph, checkpoint=checkpoint, workflow_id="wf-b")

    async def test_gate_outputs_are_persisted_in_state(self, checkpointer):
        @node(output_name="next_count")
        def increment(count: int) -> int:
            return count + 1

        @ifelse(when_true=END, when_false="increment")
        def done(next_count: int) -> bool:
            return next_count >= 2

        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([increment, done], entrypoint="increment")
        await runner.run(graph, {"count": 2}, workflow_id="wf-gate")

        state = checkpointer.state("wf-gate")
        assert "_done" in state
        assert state["_done"] is True

    async def test_failed_workflow_can_resume_same_id_without_values(self, checkpointer):
        should_fail = True

        @node(output_name="seed")
        def prepare(x: int) -> int:
            return x

        @node(output_name="out")
        def maybe_fail(seed: int) -> int:
            nonlocal should_fail
            if should_fail:
                raise RuntimeError("transient")
            return seed * 10

        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([prepare, maybe_fail])

        first = await runner.run(graph, {"x": 5}, workflow_id="wf-failed", error_handling="continue")
        assert first.status == RunStatus.FAILED

        should_fail = False
        retry_graph = graph.with_entrypoint("maybe_fail")
        resumed = await runner.run(retry_graph, workflow_id="wf-failed", on_internal_override="ignore")
        assert resumed.status == RunStatus.COMPLETED
        assert resumed["out"] == 50

    async def test_paused_workflow_accepts_interrupt_response_on_resume(self, checkpointer):
        @interrupt(output_name="decision")
        def approval() -> str: ...

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return f"Final: {decision}"

        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([approval, finalize])

        paused = await runner.run(graph, workflow_id="wf-paused")
        assert paused.status == RunStatus.PAUSED
        assert paused.pause is not None

        resumed = await runner.run(
            graph,
            {paused.pause.response_key: "approved"},
            workflow_id="wf-paused",
        )
        assert resumed.status == RunStatus.COMPLETED
        assert resumed["result"] == "Final: approved"

    async def test_fork_metadata_is_persisted(self, checkpointer):
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double])

        await runner.run(graph, {"x": 5}, workflow_id="wf-root")
        checkpoint = checkpointer.checkpoint("wf-root")
        await runner.run(graph, {"x": 7}, checkpoint=checkpoint, workflow_id="wf-root-fork")

        run = checkpointer.get_run("wf-root-fork")
        assert run is not None
        assert run.forked_from == "wf-root"
        assert run.fork_superstep is None
        assert run.retry_of is None
        assert run.retry_index is None

    async def test_retry_workflow_api_sets_retry_lineage(self, checkpointer):
        should_fail = True

        @node(output_name="x_seed")
        def seed(x: int) -> int:
            return x

        @node(output_name="out")
        def maybe_fail(x_seed: int) -> int:
            nonlocal should_fail
            if should_fail:
                raise RuntimeError("boom")
            return x_seed * 2

        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([seed, maybe_fail])

        failed = await runner.run(graph, {"x": 5}, workflow_id="wf-retry-root", error_handling="continue")
        assert failed.status == RunStatus.FAILED

        should_fail = False
        retry_graph = graph.with_entrypoint("maybe_fail")
        retry_id_1, retry_cp_1 = checkpointer.retry_workflow("wf-retry-root")
        retried_1 = await runner.run(retry_graph, checkpoint=retry_cp_1, workflow_id=retry_id_1, on_internal_override="ignore")
        assert retried_1["out"] == 10
        run_1 = checkpointer.get_run(retry_id_1)
        assert run_1 is not None
        assert run_1.retry_of == "wf-retry-root"
        assert run_1.retry_index == 1
        assert run_1.forked_from == "wf-retry-root"

        retry_id_2, retry_cp_2 = checkpointer.retry_workflow("wf-retry-root")
        await runner.run(retry_graph, checkpoint=retry_cp_2, workflow_id=retry_id_2, on_internal_override="ignore")
        run_2 = checkpointer.get_run(retry_id_2)
        assert run_2 is not None
        assert run_2.retry_of == "wf-retry-root"
        assert run_2.retry_index == 2

    async def test_nested_graph_changes_are_detected_in_lineage_hash(self, checkpointer):
        @node(output_name="inner_out")
        def inner_v1(x: int) -> int:
            return x + 1

        @node(output_name="inner_mid")
        def inner_v2_mid(x: int) -> int:
            return x + 2

        @node(output_name="inner_out")
        def inner_v2_end(inner_mid: int) -> int:
            return inner_mid * 2

        @node(output_name="final")
        def finalize(inner_out: int) -> int:
            return inner_out + 10

        outer_v1 = Graph([Graph([inner_v1], name="inner").as_node(name="nested"), finalize])
        outer_v2 = Graph([Graph([inner_v2_mid, inner_v2_end], name="inner").as_node(name="nested"), finalize])

        runner = AsyncRunner(checkpointer=checkpointer)
        await runner.run(outer_v1, {"x": 5}, workflow_id="wf-nested-hash")

        with pytest.raises(GraphChangedError):
            await runner.run(outer_v2, workflow_id="wf-nested-hash")
