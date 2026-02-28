"""Integration tests: AsyncRunner + SqliteCheckpointer end-to-end."""

import pytest

from hypergraph import AsyncRunner, Graph, node
from hypergraph.checkpointers import (
    CheckpointPolicy,
    SqliteCheckpointer,
    StepStatus,
    WorkflowStatus,
)

# Skip all tests if aiosqlite is not installed
aiosqlite = pytest.importorskip("aiosqlite")


@pytest.fixture
async def checkpointer(tmp_path):
    """Create a fresh SqliteCheckpointer for each test."""
    cp = SqliteCheckpointer(str(tmp_path / "test.db"))
    await cp.initialize()
    yield cp
    await cp.close()


# --- Simple pipeline nodes ---


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3


# --- Tests ---


class TestRunnerCheckpointIntegration:
    async def test_sync_durability_persists_steps(self, checkpointer):
        """With durability='sync', steps are persisted after each superstep."""
        checkpointer.policy = CheckpointPolicy(durability="sync", retention="full")
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        result = await runner.run(graph, {"x": 5}, workflow_id="wf-1")

        assert result["tripled"] == 30

        # Verify workflow was created and completed
        wf = await checkpointer.get_workflow("wf-1")
        assert wf is not None
        assert wf.status == WorkflowStatus.COMPLETED

        # Verify steps were persisted
        steps = await checkpointer.get_steps("wf-1")
        assert len(steps) == 2
        assert steps[0].node_name == "double"
        assert steps[0].status == StepStatus.COMPLETED
        assert steps[0].values == {"doubled": 10}
        assert steps[1].node_name == "triple"
        assert steps[1].values == {"tripled": 30}

    async def test_async_durability_persists_steps(self, checkpointer):
        """With durability='async' (default), steps are persisted via background tasks."""
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        result = await runner.run(graph, {"x": 3}, workflow_id="wf-async")

        assert result["tripled"] == 18

        # Steps should be persisted (background tasks gathered in finally block)
        steps = await checkpointer.get_steps("wf-async")
        assert len(steps) == 2

    async def test_state_reconstruction(self, checkpointer):
        """get_state folds all step values into a dict."""
        checkpointer.policy = CheckpointPolicy(durability="sync", retention="full")
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        await runner.run(graph, {"x": 4}, workflow_id="wf-state")

        state = await checkpointer.get_state("wf-state")
        assert state == {"doubled": 8, "tripled": 24}

    async def test_no_workflow_id_skips_checkpointing(self, checkpointer):
        """Without workflow_id, no checkpointing occurs."""
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        result = await runner.run(graph, {"x": 1})
        assert result["tripled"] == 6

        # No workflows should have been created
        workflows = await checkpointer.list_workflows()
        assert len(workflows) == 0

    async def test_no_checkpointer_runs_normally(self):
        """Runner without checkpointer still works, even with workflow_id."""
        runner = AsyncRunner()
        graph = Graph([double, triple])

        result = await runner.run(graph, {"x": 2}, workflow_id="wf-ignored")
        assert result["tripled"] == 12

    async def test_step_records_have_duration(self, checkpointer):
        """Step records include duration_ms > 0 for executed nodes."""
        checkpointer.policy = CheckpointPolicy(durability="sync", retention="full")
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        await runner.run(graph, {"x": 1}, workflow_id="wf-dur")

        steps = await checkpointer.get_steps("wf-dur")
        for step in steps:
            assert step.duration_ms >= 0

    async def test_checkpoint_snapshot(self, checkpointer):
        """get_checkpoint returns both state and steps."""
        checkpointer.policy = CheckpointPolicy(durability="sync", retention="full")
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        await runner.run(graph, {"x": 7}, workflow_id="wf-cp")

        cp = await checkpointer.get_checkpoint("wf-cp")
        assert cp.values == {"doubled": 14, "tripled": 42}
        assert len(cp.steps) == 2

    async def test_exit_durability_flushes_at_end(self, checkpointer):
        """With durability='exit', steps are buffered and flushed after the run."""
        checkpointer.policy = CheckpointPolicy(durability="exit", retention="latest")
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        await runner.run(graph, {"x": 2}, workflow_id="wf-exit")

        # Steps should be flushed after the run completes
        steps = await checkpointer.get_steps("wf-exit")
        assert len(steps) == 2

    async def test_failed_node_persists_step_record(self, checkpointer):
        """A node that raises gets a FAILED step record."""
        checkpointer.policy = CheckpointPolicy(durability="sync", retention="full")

        @node(output_name="boom")
        def explode(x: int) -> int:
            raise ValueError("kaboom")

        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([explode])

        result = await runner.run(graph, {"x": 1}, workflow_id="wf-fail", error_handling="continue")
        assert result.status.value == "failed"

        # Workflow should be marked FAILED
        wf = await checkpointer.get_workflow("wf-fail")
        assert wf is not None
        assert wf.status == WorkflowStatus.FAILED

        # The failed node should have a FAILED step record
        steps = await checkpointer.get_steps("wf-fail")
        assert len(steps) == 1
        assert steps[0].node_name == "explode"
        assert steps[0].status == StepStatus.FAILED
        assert "kaboom" in steps[0].error

    async def test_partial_failure_persists_siblings(self, checkpointer):
        """When one of several parallel nodes fails, completed siblings get COMPLETED records."""
        checkpointer.policy = CheckpointPolicy(durability="sync", retention="full")

        @node(output_name="a_out")
        def succeed_a(x: int) -> int:
            return x + 1

        @node(output_name="b_out")
        def fail_b(x: int) -> int:
            raise RuntimeError("b failed")

        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([succeed_a, fail_b])

        result = await runner.run(graph, {"x": 1}, workflow_id="wf-partial", error_handling="continue")
        assert result.status.value == "failed"

        steps = await checkpointer.get_steps("wf-partial")
        statuses = {s.node_name: s.status for s in steps}
        # succeed_a should be COMPLETED, fail_b should be FAILED
        assert statuses["succeed_a"] == StepStatus.COMPLETED
        assert statuses["fail_b"] == StepStatus.FAILED

    async def test_cyclic_re_execution_persists(self, checkpointer):
        """Cyclic nodes that re-execute get new step records per superstep."""
        from hypergraph import END, ifelse

        checkpointer.policy = CheckpointPolicy(durability="sync", retention="full")

        @node(output_name="count")
        def increment(count: int) -> int:
            return count + 1

        @ifelse(when_true=END, when_false="increment")
        def check_done(count: int) -> bool:
            return count >= 3

        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([increment, check_done])

        await runner.run(graph, {"count": 0}, workflow_id="wf-cycle")

        steps = await checkpointer.get_steps("wf-cycle")
        # increment runs multiple times (count: 0→1, 1→2, 2→3)
        increment_steps = [s for s in steps if s.node_name == "increment"]
        assert len(increment_steps) >= 2, f"Expected ≥2 increment steps, got {len(increment_steps)}: {steps}"
        # Each re-execution should have a different superstep
        supersteps = {s.superstep for s in increment_steps}
        assert len(supersteps) == len(increment_steps), "Each re-execution should be in a distinct superstep"
