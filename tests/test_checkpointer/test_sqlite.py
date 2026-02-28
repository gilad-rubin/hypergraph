"""Tests for SqliteCheckpointer."""

import pytest

from hypergraph.checkpointers import (
    CheckpointPolicy,
    SqliteCheckpointer,
    StepRecord,
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


def _make_step(workflow_id="wf-1", superstep=0, node_name="embed", index=0, **kwargs):
    """Helper to create a StepRecord with defaults."""
    defaults = {
        "status": StepStatus.COMPLETED,
        "input_versions": {"x": 1},
        "values": {"embedding": [1, 2, 3]},
        "duration_ms": 42.5,
    }
    defaults.update(kwargs)
    return StepRecord(
        workflow_id=workflow_id,
        superstep=superstep,
        node_name=node_name,
        index=index,
        **defaults,
    )


class TestWorkflowLifecycle:
    async def test_create_and_get(self, checkpointer):
        wf = await checkpointer.create_workflow("wf-1", graph_name="test_graph")
        assert wf.id == "wf-1"
        assert wf.status == WorkflowStatus.ACTIVE
        assert wf.graph_name == "test_graph"

        fetched = await checkpointer.get_workflow("wf-1")
        assert fetched is not None
        assert fetched.id == "wf-1"
        assert fetched.status == WorkflowStatus.ACTIVE

    async def test_get_nonexistent(self, checkpointer):
        assert await checkpointer.get_workflow("nope") is None

    async def test_update_status(self, checkpointer):
        await checkpointer.create_workflow("wf-1")
        await checkpointer.update_workflow_status("wf-1", WorkflowStatus.COMPLETED)
        wf = await checkpointer.get_workflow("wf-1")
        assert wf.status == WorkflowStatus.COMPLETED
        assert wf.completed_at is not None

    async def test_list_workflows(self, checkpointer):
        await checkpointer.create_workflow("wf-1")
        await checkpointer.create_workflow("wf-2")
        await checkpointer.update_workflow_status("wf-1", WorkflowStatus.COMPLETED)

        all_wfs = await checkpointer.list_workflows()
        assert len(all_wfs) == 2

        completed = await checkpointer.list_workflows(status=WorkflowStatus.COMPLETED)
        assert len(completed) == 1
        assert completed[0].id == "wf-1"

        active = await checkpointer.list_workflows(status=WorkflowStatus.ACTIVE)
        assert len(active) == 1
        assert active[0].id == "wf-2"


class TestStepPersistence:
    async def test_save_and_get_steps(self, checkpointer):
        await checkpointer.create_workflow("wf-1")
        await checkpointer.save_step(_make_step(index=0, node_name="embed"))
        await checkpointer.save_step(_make_step(index=1, node_name="retrieve", superstep=1, values={"docs": ["a", "b"]}))

        steps = await checkpointer.get_steps("wf-1")
        assert len(steps) == 2
        assert steps[0].node_name == "embed"
        assert steps[1].node_name == "retrieve"
        assert steps[0].index == 0
        assert steps[1].index == 1

    async def test_step_values_roundtrip(self, checkpointer):
        await checkpointer.create_workflow("wf-1")
        original_values = {"embedding": [0.1, 0.2, 0.3], "count": 42, "flag": True}
        await checkpointer.save_step(_make_step(values=original_values))

        steps = await checkpointer.get_steps("wf-1")
        assert steps[0].values == original_values

    async def test_step_metadata_roundtrip(self, checkpointer):
        await checkpointer.create_workflow("wf-1")
        await checkpointer.save_step(
            _make_step(
                duration_ms=150.5,
                cached=True,
                decision="route_a",
                error=None,
            )
        )

        steps = await checkpointer.get_steps("wf-1")
        s = steps[0]
        assert s.duration_ms == 150.5
        assert s.cached is True
        assert s.decision == "route_a"
        assert s.error is None

    async def test_step_list_decision(self, checkpointer):
        """Route decisions can be a list of targets."""
        await checkpointer.create_workflow("wf-1")
        await checkpointer.save_step(_make_step(decision=["route_a", "route_b"]))

        steps = await checkpointer.get_steps("wf-1")
        assert steps[0].decision == ["route_a", "route_b"]

    async def test_upsert_semantics(self, checkpointer):
        """Same (workflow_id, superstep, node_name) upserts instead of failing."""
        await checkpointer.create_workflow("wf-1")
        await checkpointer.save_step(_make_step(values={"v": 1}))
        await checkpointer.save_step(_make_step(values={"v": 2}))

        steps = await checkpointer.get_steps("wf-1")
        assert len(steps) == 1
        assert steps[0].values == {"v": 2}

    async def test_get_steps_by_superstep(self, checkpointer):
        await checkpointer.create_workflow("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1))
        await checkpointer.save_step(_make_step(superstep=2, node_name="c", index=2))

        steps = await checkpointer.get_steps("wf-1", superstep=1)
        assert len(steps) == 2
        assert {s.node_name for s in steps} == {"a", "b"}


class TestStateComputation:
    async def test_get_state_folds_values(self, checkpointer):
        """State is computed by folding step values in index order."""
        await checkpointer.create_workflow("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values={"y": 2}))
        await checkpointer.save_step(_make_step(superstep=2, node_name="c", index=2, values={"z": 3}))

        state = await checkpointer.get_state("wf-1")
        assert state == {"x": 1, "y": 2, "z": 3}

    async def test_get_state_through_superstep(self, checkpointer):
        """Time travel: get state at a specific superstep."""
        await checkpointer.create_workflow("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values={"y": 2}))
        await checkpointer.save_step(_make_step(superstep=2, node_name="c", index=2, values={"z": 3}))

        state = await checkpointer.get_state("wf-1", superstep=1)
        assert state == {"x": 1, "y": 2}
        assert "z" not in state

    async def test_state_later_values_overwrite(self, checkpointer):
        """Later steps overwrite earlier values for the same key."""
        await checkpointer.create_workflow("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": "old"}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values={"x": "new"}))

        state = await checkpointer.get_state("wf-1")
        assert state["x"] == "new"

    async def test_state_empty_workflow(self, checkpointer):
        await checkpointer.create_workflow("wf-1")
        state = await checkpointer.get_state("wf-1")
        assert state == {}

    async def test_state_skips_none_values(self, checkpointer):
        """Steps with values=None don't contribute to state."""
        await checkpointer.create_workflow("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values=None))

        state = await checkpointer.get_state("wf-1")
        assert state == {"x": 1}


class TestCheckpoint:
    async def test_get_checkpoint(self, checkpointer):
        await checkpointer.create_workflow("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values={"y": 2}))

        cp = await checkpointer.get_checkpoint("wf-1")
        assert cp.values == {"x": 1, "y": 2}
        assert len(cp.steps) == 2

    async def test_get_checkpoint_at_superstep(self, checkpointer):
        await checkpointer.create_workflow("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values={"y": 2}))

        cp = await checkpointer.get_checkpoint("wf-1", superstep=0)
        assert cp.values == {"x": 1}
        assert len(cp.steps) == 1


class TestLazyInit:
    async def test_lazy_initialize(self, tmp_path):
        """Checkpointer auto-initializes on first use."""
        cp = SqliteCheckpointer(str(tmp_path / "lazy.db"))
        # No explicit initialize() call
        await cp.create_workflow("wf-1")
        wf = await cp.get_workflow("wf-1")
        assert wf is not None
        await cp.close()


class TestPolicyIntegration:
    def test_default_policy(self):
        cp = SqliteCheckpointer(":memory:")
        assert cp.policy.durability == "async"
        assert cp.policy.retention == "full"

    def test_custom_policy(self):
        policy = CheckpointPolicy(durability="sync", retention="latest")
        cp = SqliteCheckpointer(":memory:", policy=policy)
        assert cp.policy.durability == "sync"

    def test_durability_kwarg(self):
        cp = SqliteCheckpointer(":memory:", durability="sync")
        assert cp.policy.durability == "sync"
        assert cp.policy.retention == "full"

    def test_retention_kwarg(self):
        cp = SqliteCheckpointer(":memory:", retention="latest")
        assert cp.policy.durability == "async"
        assert cp.policy.retention == "latest"

    def test_both_kwargs(self):
        cp = SqliteCheckpointer(":memory:", durability="sync", retention="latest")
        assert cp.policy.durability == "sync"
        assert cp.policy.retention == "latest"

    def test_policy_and_kwargs_conflict(self):
        with pytest.raises(ValueError, match="Cannot pass both"):
            SqliteCheckpointer(
                ":memory:",
                policy=CheckpointPolicy(),
                durability="sync",
            )
