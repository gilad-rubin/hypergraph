"""Tests for the backend-neutral in-memory checkpointer."""

import pytest

from hypergraph import AsyncRunner, Graph, RunStatus, interrupt, node
from hypergraph.checkpointers import CheckpointPolicy, MemoryCheckpointer, StepRecord, StepStatus, WorkflowStatus


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3


@pytest.fixture
def checkpointer():
    return MemoryCheckpointer()


class TestMemoryCheckpointer:
    async def test_retry_workflow_requires_existing_source(self, checkpointer):
        with pytest.raises(ValueError, match="Unknown source workflow_id"):
            await checkpointer.retry_workflow_async("missing-run")

    async def test_retry_workflow_counts_all_matching_retries(self, checkpointer):
        await checkpointer.create_run("wf-root")
        for idx in range(1, 4):
            await checkpointer.create_run(
                f"wf-root-retry-{idx}",
                forked_from="wf-root",
                retry_of="wf-root",
                retry_index=idx,
            )

        retry_id, checkpoint = await checkpointer.retry_workflow_async("wf-root")

        assert retry_id == "wf-root-retry-4"
        assert checkpoint.retry_of == "wf-root"
        assert checkpoint.retry_index == 4

    async def test_async_runner_nested_graph_works_with_memory_checkpointer(self, checkpointer):
        runner = AsyncRunner(checkpointer=checkpointer)
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node(name="embed"), triple], name="outer")

        # x is private to "embed" GraphNode → addressed via dot-path
        result = await runner.run(outer, {"embed.x": 5}, workflow_id="nested-memory")

        assert result["tripled"] == 30

        root = await checkpointer.get_run_async("nested-memory")
        child = await checkpointer.get_run_async("nested-memory/embed")
        steps = await checkpointer.get_steps("nested-memory")

        assert root is not None
        assert root.status == WorkflowStatus.COMPLETED
        assert child is not None
        assert child.parent_run_id == "nested-memory"
        assert any(step.node_name == "embed" and step.child_run_id == "nested-memory/embed" for step in steps)

    async def test_list_runs_none_limit_returns_full_result_set(self, checkpointer):
        for run_id in ("wf-1", "wf-2", "wf-3"):
            await checkpointer.create_run(run_id)

        runs = await checkpointer.list_runs(limit=None)

        assert {run.id for run in runs} == {"wf-1", "wf-2", "wf-3"}

    async def test_forked_nested_pause_resumes_child_from_source_lineage(self, checkpointer):
        @interrupt(output_name="decision")
        def approval(draft: str) -> str: ...

        inner = Graph([approval], name="inner")

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return f"Draft for: {query}"

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return f"Final: {decision}"

        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([make_draft, inner.as_node(), finalize])

        paused = await runner.run(graph, {"query": "hello"}, workflow_id="wf-nested-paused")
        assert paused.status == RunStatus.PAUSED
        assert paused.pause is not None

        resumed = await runner.run(
            graph,
            {paused.pause.response_key: "approved"},
            fork_from="wf-nested-paused",
            workflow_id="wf-nested-paused-fork",
        )

        assert resumed.status == RunStatus.COMPLETED
        assert resumed["result"] == "Final: approved"

        child = await checkpointer.get_run_async("wf-nested-paused-fork/inner")
        assert child is not None
        assert child.parent_run_id == "wf-nested-paused-fork"
        assert child.forked_from == "wf-nested-paused/inner"

    async def test_retried_nested_pause_resumes_child_from_source_lineage(self, checkpointer):
        @interrupt(output_name="decision")
        def approval(draft: str) -> str: ...

        inner = Graph([approval], name="inner")

        @node(output_name="draft")
        def make_draft(query: str) -> str:
            return f"Draft for: {query}"

        @node(output_name="result")
        def finalize(decision: str) -> str:
            return f"Final: {decision}"

        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([make_draft, inner.as_node(), finalize])

        paused = await runner.run(graph, {"query": "hello"}, workflow_id="wf-nested-pause-retry")
        assert paused.status == RunStatus.PAUSED
        assert paused.pause is not None

        resumed = await runner.run(
            graph,
            {paused.pause.response_key: "approved"},
            retry_from="wf-nested-pause-retry",
            workflow_id="wf-nested-pause-retry-1",
        )

        assert resumed.status == RunStatus.COMPLETED
        assert resumed["result"] == "Final: approved"

        child = await checkpointer.get_run_async("wf-nested-pause-retry-1/inner")
        assert child is not None
        assert child.parent_run_id == "wf-nested-pause-retry-1"
        assert child.retry_of == "wf-nested-pause-retry/inner"

    async def test_latest_retention_preserves_reconstructible_state(self, checkpointer):
        checkpointer.policy = CheckpointPolicy(retention="latest")
        await checkpointer.create_run("wf-retained")
        await checkpointer.save_step(
            StepRecord(
                run_id="wf-retained",
                superstep=0,
                node_name="a",
                index=0,
                status=StepStatus.COMPLETED,
                input_versions={},
                values={"x": 1},
            )
        )
        await checkpointer.save_step(
            StepRecord(
                run_id="wf-retained",
                superstep=1,
                node_name="b",
                index=1,
                status=StepStatus.COMPLETED,
                input_versions={},
                values={"y": 2},
            )
        )
        await checkpointer.save_step(
            StepRecord(
                run_id="wf-retained",
                superstep=2,
                node_name="a",
                index=2,
                status=StepStatus.COMPLETED,
                input_versions={},
                values={"x": 3},
            )
        )

        state = await checkpointer.get_state("wf-retained")
        assert state == {"x": 3, "y": 2}
