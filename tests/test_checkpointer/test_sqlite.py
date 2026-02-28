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

        fetched = await checkpointer.get_run("wf-1")
        assert fetched is not None
        assert fetched.id == "wf-1"
        assert fetched.status == WorkflowStatus.ACTIVE

    async def test_get_nonexistent(self, checkpointer):
        assert await checkpointer.get_run("nope") is None

    async def test_update_status(self, checkpointer):
        await checkpointer.create_run("wf-1")
        await checkpointer.update_run_status("wf-1", WorkflowStatus.COMPLETED)
        r = await checkpointer.get_run("wf-1")
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
        r = await checkpointer.get_run("wf-1")
        assert r.duration_ms == 150.5
        assert r.node_count == 3
        assert r.error_count == 1

    async def test_list_runs(self, checkpointer):
        await checkpointer.create_run("wf-1")
        await checkpointer.create_run("wf-2")
        await checkpointer.update_run_status("wf-1", WorkflowStatus.COMPLETED)

        all_runs = await checkpointer.list_runs()
        assert len(all_runs) == 2

        completed = await checkpointer.list_runs(status=WorkflowStatus.COMPLETED)
        assert len(completed) == 1
        assert completed[0].id == "wf-1"

        active = await checkpointer.list_runs(status=WorkflowStatus.ACTIVE)
        assert len(active) == 1
        assert active[0].id == "wf-2"


class TestStepPersistence:
    async def test_save_and_get_steps(self, checkpointer):
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(index=0, node_name="embed"))
        await checkpointer.save_step(_make_step(index=1, node_name="retrieve", superstep=1, values={"docs": ["a", "b"]}))

        steps = await checkpointer.get_steps("wf-1")
        assert len(steps) == 2
        assert steps[0].node_name == "embed"
        assert steps[1].node_name == "retrieve"
        assert steps[0].index == 0
        assert steps[1].index == 1

    async def test_step_values_roundtrip(self, checkpointer):
        await checkpointer.create_run("wf-1")
        original_values = {"embedding": [0.1, 0.2, 0.3], "count": 42, "flag": True}
        await checkpointer.save_step(_make_step(values=original_values))

        steps = await checkpointer.get_steps("wf-1")
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

        steps = await checkpointer.get_steps("wf-1")
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

        steps = await checkpointer.get_steps("wf-1")
        assert steps[0].decision == ["route_a", "route_b"]

    async def test_upsert_semantics(self, checkpointer):
        """Same (run_id, superstep, node_name) upserts instead of failing."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(values={"v": 1}))
        await checkpointer.save_step(_make_step(values={"v": 2}))

        steps = await checkpointer.get_steps("wf-1")
        assert len(steps) == 1
        assert steps[0].values == {"v": 2}

    async def test_get_steps_by_superstep(self, checkpointer):
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1))
        await checkpointer.save_step(_make_step(superstep=2, node_name="c", index=2))

        steps = await checkpointer.get_steps("wf-1", superstep=1)
        assert len(steps) == 2
        assert {s.node_name for s in steps} == {"a", "b"}


class TestStateComputation:
    async def test_get_state_folds_values(self, checkpointer):
        """State is computed by folding step values in index order."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values={"y": 2}))
        await checkpointer.save_step(_make_step(superstep=2, node_name="c", index=2, values={"z": 3}))

        state = await checkpointer.get_state("wf-1")
        assert state == {"x": 1, "y": 2, "z": 3}

    async def test_get_state_through_superstep(self, checkpointer):
        """Time travel: get state at a specific superstep."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values={"y": 2}))
        await checkpointer.save_step(_make_step(superstep=2, node_name="c", index=2, values={"z": 3}))

        state = await checkpointer.get_state("wf-1", superstep=1)
        assert state == {"x": 1, "y": 2}
        assert "z" not in state

    async def test_state_later_values_overwrite(self, checkpointer):
        """Later steps overwrite earlier values for the same key."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": "old"}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values={"x": "new"}))

        state = await checkpointer.get_state("wf-1")
        assert state["x"] == "new"

    async def test_state_empty_run(self, checkpointer):
        await checkpointer.create_run("wf-1")
        state = await checkpointer.get_state("wf-1")
        assert state == {}

    async def test_state_skips_none_values(self, checkpointer):
        """Steps with values=None don't contribute to state."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values=None))

        state = await checkpointer.get_state("wf-1")
        assert state == {"x": 1}


class TestCheckpoint:
    async def test_get_checkpoint(self, checkpointer):
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(superstep=0, node_name="a", index=0, values={"x": 1}))
        await checkpointer.save_step(_make_step(superstep=1, node_name="b", index=1, values={"y": 2}))

        cp = await checkpointer.get_checkpoint("wf-1")
        assert cp.values == {"x": 1, "y": 2}
        assert len(cp.steps) == 2

    async def test_get_checkpoint_at_superstep(self, checkpointer):
        await checkpointer.create_run("wf-1")
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
        await cp.create_run("wf-1")
        r = await cp.get_run("wf-1")
        assert r is not None
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

        r = checkpointer.run("wf-1")
        assert r is not None
        assert r.id == "wf-1"
        assert r.graph_name == "test_graph"
        assert r.status == WorkflowStatus.ACTIVE

        assert checkpointer.run("nonexistent") is None

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
        assert cp.run("wf-1") is None
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


class TestSearch:
    async def test_search_by_node_name(self, checkpointer):
        """FTS5 search finds steps by node name."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(node_name="embed", index=0))
        await checkpointer.save_step(_make_step(node_name="retrieve", index=1, superstep=1))

        results = checkpointer.search_sync("embed")
        assert len(results) == 1
        assert results[0].node_name == "embed"

    async def test_search_by_error(self, checkpointer):
        """FTS5 search finds steps by error text."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(node_name="fail", index=0, status=StepStatus.FAILED, error="TimeoutError: connection timed out"))

        results = checkpointer.search_sync("TimeoutError")
        assert len(results) == 1
        assert "TimeoutError" in results[0].error

    async def test_search_async(self, checkpointer):
        """Async search works via the ABC method."""
        await checkpointer.create_run("wf-1")
        await checkpointer.save_step(_make_step(node_name="embed", index=0))

        results = await checkpointer.search("embed")
        assert len(results) == 1

    async def test_fts_consistent_after_multiple_saves(self, checkpointer):
        """FTS index stays consistent when steps are saved across supersteps."""
        await checkpointer.create_run("wf-1")
        # Simulate a cyclic graph: same node appears in multiple supersteps
        await checkpointer.save_step(_make_step(node_name="generate", superstep=0, index=0))
        await checkpointer.save_step(_make_step(node_name="evaluate", superstep=0, index=1))
        await checkpointer.save_step(_make_step(node_name="generate", superstep=1, index=2))
        await checkpointer.save_step(_make_step(node_name="evaluate", superstep=1, index=3))

        results = checkpointer.search_sync("generate")
        assert len(results) == 2
        assert all(r.node_name == "generate" for r in results)

        results = checkpointer.search_sync("evaluate")
        assert len(results) == 2


class TestMigration:
    def test_fresh_db_gets_v2_schema(self, tmp_path):
        """A new database gets v2 schema automatically."""
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
        assert version == 2
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
        assert version == 2
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
