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
