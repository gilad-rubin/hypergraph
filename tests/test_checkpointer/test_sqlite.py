"""Tests for SqliteCheckpointer."""

import asyncio
from datetime import timedelta, timezone
from pathlib import Path

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
def checkpointer(tmp_path):
    """Create a fresh SqliteCheckpointer for each test."""
    cp = SqliteCheckpointer(str(tmp_path / "test.db"))
    yield cp


def _make_step(run_id="wf-1", superstep=0, node_name="embed", index=0, **kwargs):
    """Helper to create a StepRecord with defaults."""
    defaults = {
        "status": StepStatus.COMPLETED,
        "input_versions": {"x": 1},
        "values": {"embedding": [1, 2, 3]},
        "duration_ms": 42.5,
    }
    defaults.update(kwargs)
    return StepRecord(
        run_id=run_id,
        superstep=superstep,
        node_name=node_name,
        index=index,
        **defaults,
    )


class TestRunLifecycle:
    async def test_create_and_get(self, checkpointer):
        r = await checkpointer.create_run("wf-1", graph_name="test_graph")
        assert r.id == "wf-1"
        assert r.status == WorkflowStatus.ACTIVE
        assert r.graph_name == "test_graph"

        fetched = checkpointer.get_run("wf-1")
        assert fetched is not None
        assert fetched.id == "wf-1"
        assert fetched.status == WorkflowStatus.ACTIVE

    async def test_get_nonexistent(self, checkpointer):
        assert checkpointer.get_run("nope") is None

    async def test_update_status(self, checkpointer):
        await checkpointer.create_run("wf-1")
        await checkpointer.update_run_status("wf-1", WorkflowStatus.COMPLETED)
        r = checkpointer.get_run("wf-1")
        assert r.status == WorkflowStatus.COMPLETED
        assert r.completed_at is not None

    async def test_update_status_with_stats(self, checkpointer):
        await checkpointer.create_run("wf-1")
        await checkpointer.update_run_status(
            "wf-1",
            WorkflowStatus.COMPLETED,
            duration_ms=150.5,
            node_count=3,
            error_count=1,
        )
        r = checkpointer.get_run("wf-1")
        assert r.duration_ms == 150.5
        assert r.node_count == 3
        assert r.error_count == 1

    async def test_list_runs(self, checkpointer):
        await checkpointer.create_run("wf-1")
        await checkpointer.create_run("wf-2")
        await checkpointer.update_run_status("wf-1", WorkflowStatus.COMPLETED)

        all_runs = checkpointer.runs()
        assert len(all_runs) == 2

        completed = checkpointer.runs(status=WorkflowStatus.COMPLETED)
        assert len(completed) == 1
        assert completed[0].id == "wf-1"

        active = checkpointer.runs(status=WorkflowStatus.ACTIVE)
        assert len(active) == 1
        assert active[0].id == "wf-2"

    async def test_create_run_with_lineage_fields(self, checkpointer):
        run = await checkpointer.create_run(
            "wf-child",
            forked_from="wf-parent",
            fork_superstep=3,
            retry_of="wf-root",
            retry_index=2,
        )
        assert run.forked_from == "wf-parent"
        assert run.fork_superstep == 3
        assert run.retry_of == "wf-root"
        assert run.retry_index == 2

        fetched = checkpointer.get_run("wf-child")
        assert fetched is not None
        assert fetched.forked_from == "wf-parent"
        assert fetched.fork_superstep == 3
        assert fetched.retry_of == "wf-root"
        assert fetched.retry_index == 2

    async def test_fork_and_retry_workflow_helpers(self, checkpointer):
        await checkpointer.create_run("wf-root")
        await checkpointer.save_step(_make_step(run_id="wf-root", values={"x_seed": 7}))

        fork_id, fork_cp = checkpointer.fork_workflow("wf-root")
        assert fork_id.startswith("wf-root-fork-")
        assert fork_cp.source_run_id == "wf-root"
        assert fork_cp.retry_of is None

        retry_id, retry_cp = checkpointer.retry_workflow("wf-root")
        assert retry_id == "wf-root-retry-1"
        assert retry_cp.source_run_id == "wf-root"
        assert retry_cp.retry_of == "wf-root"
        assert retry_cp.retry_index == 1


class TestStepPersistence:
    async def test_save_and_get_steps(self, checkpointer):
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(index=0, node_name="embed"))
        await checkpointer.save_step(_make_step(index=1, node_name="retrieve", superstep=1, values={"docs": ["a", "b"]}))

        steps = checkpointer.steps("wf-1")
        assert len(steps) == 2
        assert steps[0].node_name == "embed"
        assert steps[1].node_name == "retrieve"
        assert steps[0].index == 0
        assert steps[1].index == 1

    async def test_step_values_roundtrip(self, checkpointer):
        await checkpointer.create_run("wf-1")
        original_values = {"embedding": [0.1, 0.2, 0.3], "count": 42, "flag": True}
        await checkpointer.save_step(_make_step(values=original_values))

        steps = checkpointer.steps("wf-1")
        assert steps[0].values == original_values

    async def test_step_metadata_roundtrip(self, checkpointer):
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(
            _make_step(
                duration_ms=150.5,
                cached=True,
                decision="route_a",
                error=None,
                node_type="FunctionNode",
            )
        )

        steps = checkpointer.steps("wf-1")
        s = steps[0]
        assert s.duration_ms == 150.5
        assert s.cached is True
        assert s.decision == "route_a"
        assert s.error is None
        assert s.node_type == "FunctionNode"

    async def test_step_list_decision(self, checkpointer):
        """Route decisions can be a list of targets."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(decision=["route_a", "route_b"]))

        steps = checkpointer.steps("wf-1")
        assert steps[0].decision == ["route_a", "route_b"]

    async def test_upsert_semantics(self, checkpointer):
        """Same (run_id, superstep, node_name) upserts instead of failing."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(values={"v": 1}))
        await checkpointer.save_step(_make_step(values={"v": 2}))

        steps = checkpointer.steps("wf-1")
        assert len(steps) == 1
        assert steps[0].values == {"v": 2}

    async def test_get_steps_by_superstep(self, checkpointer):
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1))
        await checkpointer.save_step(_make_step(superstep=2, node_name="c", index=2))

        steps = checkpointer.steps("wf-1", superstep=1)
        assert len(steps) == 2
        assert {s.node_name for s in steps} == {"a", "b"}

    async def test_get_steps_orders_by_timestamp_not_index(self, checkpointer):
        """Execution order is derived from timestamps, not raw step_index alone."""
        await checkpointer.create_run("wf-1")
        base = _make_step().created_at.replace(tzinfo=timezone.utc)
        earlier = base
        later = base + timedelta(seconds=10)

        # Insert later first with same index to simulate tied/legacy indices.
        await checkpointer.save_step(_make_step(node_name="later", index=0, created_at=later, completed_at=later))
        await checkpointer.save_step(_make_step(node_name="earlier", index=0, created_at=earlier, completed_at=earlier))

        async_steps = checkpointer.steps("wf-1")
        sync_steps = checkpointer.steps("wf-1")
        assert [s.node_name for s in async_steps] == ["earlier", "later"]
        assert [s.node_name for s in sync_steps] == ["earlier", "later"]


class TestStateComputation:
    async def test_get_state_folds_values(self, checkpointer):
        """State is computed by folding step values in index order."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values={"y": 2}))
        await checkpointer.save_step(_make_step(superstep=2, node_name="c", index=2, values={"z": 3}))

        state = checkpointer.state("wf-1")
        assert state == {"x": 1, "y": 2, "z": 3}

    async def test_get_state_through_superstep(self, checkpointer):
        """Time travel: get state at a specific superstep."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values={"y": 2}))
        await checkpointer.save_step(_make_step(superstep=2, node_name="c", index=2, values={"z": 3}))

        state = checkpointer.state("wf-1", superstep=1)
        assert state == {"x": 1, "y": 2}
        assert "z" not in state

    async def test_state_later_values_overwrite(self, checkpointer):
        """Later steps overwrite earlier values for the same key."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": "old"}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values={"x": "new"}))

        state = checkpointer.state("wf-1")
        assert state["x"] == "new"

    async def test_state_empty_run(self, checkpointer):
        await checkpointer.create_run("wf-1")
        state = checkpointer.state("wf-1")
        assert state == {}

    async def test_state_skips_none_values(self, checkpointer):
        """Steps with values=None don't contribute to state."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values=None))

        state = checkpointer.state("wf-1")
        assert state == {"x": 1}

    async def test_state_folds_in_timestamp_order_when_indices_tie(self, checkpointer):
        """State folding should follow execution timestamps, even with tied indices."""
        await checkpointer.create_run("wf-1")
        base = _make_step().created_at.replace(tzinfo=timezone.utc)
        earlier = base
        later = base + timedelta(seconds=10)

        # Insert newer value first with same index, then older value.
        await checkpointer.save_step(_make_step(node_name="newer", index=0, values={"x": "new"}, created_at=later, completed_at=later))
        await checkpointer.save_step(_make_step(node_name="older", index=0, values={"x": "old"}, created_at=earlier, completed_at=earlier))

        assert checkpointer.state("wf-1") == {"x": "new"}
        assert checkpointer.state("wf-1") == {"x": "new"}


class TestCheckpoint:
    async def test_get_checkpoint(self, checkpointer):
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values={"y": 2}))

        cp = checkpointer.checkpoint("wf-1")
        assert cp.values == {"x": 1, "y": 2}
        assert len(cp.steps) == 2

    async def test_get_checkpoint_at_superstep(self, checkpointer):
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values={"y": 2}))

        cp = checkpointer.checkpoint("wf-1", superstep=0)
        assert cp.values == {"x": 1}
        assert len(cp.steps) == 1


class TestLazyInit:
    async def test_lazy_initialize(self, tmp_path):
        """Checkpointer auto-initializes on first use."""
        cp = SqliteCheckpointer(str(tmp_path / "lazy.db"))
        # No explicit initialize() call
        await cp.create_run("wf-1")
        r = cp.get_run("wf-1")
        assert r is not None

    async def test_concurrent_lazy_initialize_runs_once(self, tmp_path):
        """Concurrent first-use calls should serialize initialization."""
        cp = SqliteCheckpointer(str(tmp_path / "lazy-race.db"))
        init_calls = 0
        original_initialize = cp.initialize

        async def counted_initialize():
            nonlocal init_calls
            init_calls += 1
            await asyncio.sleep(0)
            await original_initialize()

        cp.initialize = counted_initialize  # type: ignore[method-assign]

        await asyncio.gather(
            cp.create_run("wf-a"),
            cp.create_run("wf-b"),
            cp.create_run("wf-c"),
        )

        assert init_calls == 1

    async def test_memory_db_schema_available_after_initialize(self):
        """In-memory DB initialization creates schema for async operations."""
        cp = SqliteCheckpointer(":memory:")
        await cp.create_run("wf-mem")
        run = cp.get_run("wf-mem")
        assert run is not None


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

    async def test_accepts_path_instance(self, tmp_path):
        """Constructor accepts pathlib.Path for filesystem databases."""
        db_path = tmp_path / "path-instance.db"
        cp = SqliteCheckpointer(db_path)
        await cp.create_run("wf-1")
        run = cp.get_run("wf-1")
        assert run is not None

    async def test_accepts_relative_path_instance(self, tmp_path, monkeypatch):
        """Constructor accepts relative pathlib.Path values."""
        monkeypatch.chdir(tmp_path)
        cp = SqliteCheckpointer(Path("relative-path.db"))
        await cp.create_run("wf-1")
        run = cp.get_run("wf-1")
        assert run is not None


class TestSyncReads:
    """Sync read methods use stdlib sqlite3 — no await needed."""

    async def test_state(self, checkpointer):
        """Sync state() returns same results as async get_state()."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values={"y": 2}))
        await checkpointer.save_step(_make_step(superstep=2, node_name="c", index=2, values={"z": 3}))

        assert checkpointer.state("wf-1") == {"x": 1, "y": 2, "z": 3}
        assert checkpointer.state("wf-1", superstep=1) == {"x": 1, "y": 2}

    async def test_steps(self, checkpointer):
        """Sync steps() returns same records as async get_steps()."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1))
        await checkpointer.save_step(_make_step(superstep=2, node_name="c", index=2))

        steps = checkpointer.steps("wf-1")
        assert len(steps) == 3
        assert [s.node_name for s in steps] == ["a", "b", "c"]

        steps_filtered = checkpointer.steps("wf-1", superstep=1)
        assert len(steps_filtered) == 2

    async def test_run(self, checkpointer):
        """Sync run() returns same metadata as async get_run()."""
        await checkpointer.create_run("wf-1", graph_name="test_graph")

        r = checkpointer.get_run("wf-1")
        assert r is not None
        assert r.id == "wf-1"
        assert r.graph_name == "test_graph"
        assert r.status == WorkflowStatus.ACTIVE

        assert checkpointer.get_run("nonexistent") is None

    async def test_runs(self, checkpointer):
        """Sync runs() returns same list as async list_runs()."""
        await checkpointer.create_run("wf-1")
        await checkpointer.create_run("wf-2")
        await checkpointer.update_run_status("wf-1", WorkflowStatus.COMPLETED)

        all_runs = checkpointer.runs()
        assert len(all_runs) == 2

        completed = checkpointer.runs(status=WorkflowStatus.COMPLETED)
        assert len(completed) == 1
        assert completed[0].id == "wf-1"

    async def test_checkpoint(self, checkpointer):
        """Sync checkpoint() composes state + steps."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values={"y": 2}))

        cp = checkpointer.checkpoint("wf-1")
        assert cp.values == {"x": 1, "y": 2}
        assert len(cp.steps) == 2

        cp_at_0 = checkpointer.checkpoint("wf-1", superstep=0)
        assert cp_at_0.values == {"x": 1}
        assert len(cp_at_0.steps) == 1

    def test_sync_reads_before_async_init(self, tmp_path):
        """Sync reads on a fresh db with no data return empty results."""
        cp = SqliteCheckpointer(str(tmp_path / "fresh.db"))
        assert cp.state("wf-1") == {}
        assert cp.steps("wf-1") == []
        assert cp.get_run("wf-1") is None
        assert cp.runs() == []

    async def test_stats(self, checkpointer):
        """Sync stats() returns per-node breakdown."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(node_name="a", index=0, duration_ms=100.0, node_type="FunctionNode"))
        await checkpointer.save_step(_make_step(node_name="b", index=1, superstep=1, duration_ms=50.0, node_type="GateNode"))

        node_stats = checkpointer.stats("wf-1")
        assert "a" in node_stats
        assert "b" in node_stats
        assert node_stats["a"]["total_ms"] == 100.0
        assert node_stats["a"]["node_type"] == "FunctionNode"
        assert node_stats["b"]["node_type"] == "GateNode"

    async def test_values(self, checkpointer):
        """Sync values() returns state, optionally filtered by key."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(node_name="a", index=0, values={"x": 1, "y": 2}))

        assert checkpointer.values("wf-1") == {"x": 1, "y": 2}
        assert checkpointer.values("wf-1", key="x") == {"x": 1}
        assert checkpointer.values("wf-1", key="missing") == {}

    async def test_lineage_git_like_tree(self, checkpointer):
        """lineage() returns root+fork tree with expandable step views."""
        await checkpointer.create_run("wf-root")
        await checkpointer.save_step(_make_step(run_id="wf-root", node_name="seed", index=0))

        await checkpointer.create_run("wf-a", forked_from="wf-root", fork_superstep=0)
        await checkpointer.save_step(_make_step(run_id="wf-a", node_name="a1", index=0))

        await checkpointer.create_run("wf-b", forked_from="wf-root", fork_superstep=0)
        await checkpointer.save_step(_make_step(run_id="wf-b", node_name="b1", index=0))

        await checkpointer.create_run("wf-a1", forked_from="wf-a", fork_superstep=1)
        await checkpointer.save_step(_make_step(run_id="wf-a1", node_name="a2", index=0))

        lineage = checkpointer.lineage("wf-a1")
        assert lineage.selected_run_id == "wf-a1"
        assert lineage.root_run_id == "wf-root"
        assert lineage[0].run.id == "wf-root"
        assert {row.run.id for row in lineage} == {"wf-root", "wf-a", "wf-b", "wf-a1"}
        assert any(row.is_selected and row.run.id == "wf-a1" for row in lineage)

        text = repr(lineage)
        assert "LineageView: wf-a1 (root=wf-root)" in text
        assert "<selected>" in text
        assert "wf-a1" in text

        html = lineage._repr_html_()
        assert "Workflow Lineage: wf-a1" in html
        assert "Lineage from root wf-root" in html
        assert "wf-root" in html
        assert "wf-a1" in html

    async def test_lineage_unknown_workflow_raises(self, checkpointer):
        with pytest.raises(ValueError, match="Unknown workflow_id"):
            checkpointer.lineage("no-such-run")

    async def test_lineage_includes_retry_and_cache_counts(self, checkpointer):
        """Retry branches and cached-step counts appear in lineage output."""
        await checkpointer.create_run("wf-root")
        await checkpointer.save_step(_make_step(run_id="wf-root", node_name="seed", index=0, cached=True))

        retry_id, retry_cp = checkpointer.retry_workflow("wf-root")
        await checkpointer.create_run(
            retry_id,
            forked_from=retry_cp.source_run_id,
            retry_of=retry_cp.retry_of,
            retry_index=retry_cp.retry_index,
        )
        await checkpointer.save_step(_make_step(run_id=retry_id, node_name="retry-step", index=0, cached=False))

        lineage = checkpointer.lineage(retry_id)
        assert {row.run.id for row in lineage} == {"wf-root", retry_id}
        text = repr(lineage)
        assert "(retry)" in text
        assert "cached=" in text

        html = lineage._repr_html_()
        assert "retry" in html
        assert "Cached" in html


class TestRetentionPolicyBehavior:
    async def test_latest_retention_keeps_latest_per_node(self, checkpointer):
        """latest retention should prune older executions of the same node."""
        checkpointer.policy = CheckpointPolicy(retention="latest")
        await checkpointer.create_run("wf-latest")
        await checkpointer.save_step(_make_step(run_id="wf-latest", superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(run_id="wf-latest", superstep=0, node_name="b", index=1, values={"y": 2}))
        await checkpointer.save_step(_make_step(run_id="wf-latest", superstep=1, node_name="a", index=2, values={"x": 3}))

        steps = checkpointer.steps("wf-latest")
        assert len(steps) == 2
        assert {(s.node_name, s.superstep) for s in steps} == {("a", 1), ("b", 0)}
        assert checkpointer.state("wf-latest") == {"x": 3, "y": 2}

    async def test_windowed_retention_keeps_recent_supersteps(self, checkpointer):
        """windowed retention should keep only the latest N supersteps."""
        checkpointer.policy = CheckpointPolicy(retention="windowed", window=2)
        await checkpointer.create_run("wf-windowed")
        await checkpointer.save_step(_make_step(run_id="wf-windowed", superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(run_id="wf-windowed", superstep=1, node_name="b", index=1, values={"y": 2}))
        await checkpointer.save_step(_make_step(run_id="wf-windowed", superstep=2, node_name="c", index=2, values={"z": 3}))

        steps = checkpointer.steps("wf-windowed")
        assert {s.superstep for s in steps} == {1, 2}
        assert checkpointer.state("wf-windowed") == {"y": 2, "z": 3}

    def test_sync_save_step_applies_retention(self, tmp_path):
        """Sync write path should apply windowed retention as well."""
        cp = SqliteCheckpointer(
            str(tmp_path / "sync-retention.db"),
            policy=CheckpointPolicy(retention="windowed", window=1),
        )
        cp.create_run_sync("wf-sync")
        cp.save_step_sync(_make_step(run_id="wf-sync", superstep=0, node_name="a", index=0, values={"x": 1}))
        cp.save_step_sync(_make_step(run_id="wf-sync", superstep=1, node_name="b", index=1, values={"y": 2}))

        steps = cp.steps("wf-sync")
        assert len(steps) == 1
        assert steps[0].superstep == 1
        assert cp.state("wf-sync") == {"y": 2}
        if cp._sync_conn:
            cp._sync_conn.close()


class TestSearch:
    async def test_search_by_node_name(self, checkpointer):
        """FTS5 search finds steps by node name."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(node_name="embed", index=0))
        await checkpointer.save_step(_make_step(node_name="retrieve", index=1, superstep=1))

        results = checkpointer.search("embed")
        assert len(results) == 1
        assert results[0].node_name == "embed"

    async def test_search_by_error(self, checkpointer):
        """FTS5 search finds steps by error text."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(node_name="fail", index=0, status=StepStatus.FAILED, error="TimeoutError: connection timed out"))

        results = checkpointer.search("TimeoutError")
        assert len(results) == 1
        assert "TimeoutError" in results[0].error

    async def test_search_async(self, checkpointer):
        """Async search works via the ABC method."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(node_name="embed", index=0))

        results = checkpointer.search("embed")
        assert len(results) == 1

    async def test_fts_consistent_after_multiple_saves(self, checkpointer):
        """FTS index stays consistent when steps are saved across supersteps."""
        await checkpointer.create_run("wf-1")
        # Simulate a cyclic graph: same node appears in multiple supersteps
        await checkpointer.save_step(_make_step(node_name="generate", superstep=0, index=0))
        await checkpointer.save_step(_make_step(node_name="evaluate", superstep=0, index=1))
        await checkpointer.save_step(_make_step(node_name="generate", superstep=1, index=2))
        await checkpointer.save_step(_make_step(node_name="evaluate", superstep=1, index=3))

        results = checkpointer.search("generate")
        assert len(results) == 2
        assert all(r.node_name == "generate" for r in results)

        results = checkpointer.search("evaluate")
        assert len(results) == 2

    async def test_search_orders_by_step_time_desc(self, checkpointer):
        """Search results should be ordered by most recent execution time."""
        await checkpointer.create_run("wf-1")
        base = _make_step().created_at.replace(tzinfo=timezone.utc)
        older = base
        newer = base + timedelta(seconds=10)

        await checkpointer.save_step(_make_step(node_name="embed", index=0, created_at=older, completed_at=older))
        await checkpointer.save_step(_make_step(node_name="embed", index=1, superstep=1, created_at=newer, completed_at=newer))

        sync_results = checkpointer.search("embed")
        async_results = checkpointer.search("embed")
        assert [r.superstep for r in sync_results][:2] == [1, 0]
        assert [r.superstep for r in async_results][:2] == [1, 0]


class TestMigration:
    def test_fresh_db_gets_v3_schema(self, tmp_path):
        """A new database gets v3 schema automatically."""
        cp = SqliteCheckpointer(str(tmp_path / "fresh.db"))
        # Trigger sync schema creation
        assert cp.runs() == []

        import sqlite3

        conn = sqlite3.connect(str(tmp_path / "fresh.db"))
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "runs" in tables
        assert "steps" in tables
        assert "_schema_version" in tables

        version = conn.execute("SELECT version FROM _schema_version").fetchone()[0]
        assert version == 3
        conn.close()

    def test_migration_idempotent(self, tmp_path):
        """Running migration twice doesn't break anything."""
        import sqlite3

        from hypergraph.checkpointers._migrate import ensure_schema

        db_path = str(tmp_path / "idempotent.db")
        conn = sqlite3.connect(db_path)
        ensure_schema(conn)
        ensure_schema(conn)  # Second time should be a no-op
        version = conn.execute("SELECT version FROM _schema_version").fetchone()[0]
        assert version == 3
        conn.close()

    def test_unknown_schema_version_raises(self, tmp_path):
        """ensure_schema raises for schema versions newer than the current install."""
        import sqlite3

        from hypergraph.checkpointers._migrate import ensure_schema

        db_path = str(tmp_path / "future.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE _schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO _schema_version VALUES (999)")
        conn.commit()

        with pytest.raises(ValueError, match="Unsupported database schema version 999"):
            ensure_schema(conn)
        conn.close()

    def test_parse_dt_z_suffix(self):
        """_parse_dt handles Z suffix for Python 3.10 compatibility."""
        from datetime import timezone

        from hypergraph.checkpointers.sqlite import _parse_dt

        dt = _parse_dt("2024-01-15T10:30:00.123Z")
        assert dt is not None
        assert dt.tzinfo == timezone.utc
        assert dt.year == 2024 and dt.month == 1 and dt.day == 15

    def test_parse_dt_none_returns_none(self):
        """_parse_dt returns None for None or empty string."""
        from hypergraph.checkpointers.sqlite import _parse_dt

        assert _parse_dt(None) is None
        assert _parse_dt("") is None
