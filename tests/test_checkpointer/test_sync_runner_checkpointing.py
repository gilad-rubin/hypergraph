"""Integration tests: SyncRunner + SqliteCheckpointer end-to-end."""

import pytest

from hypergraph import Graph, SyncRunner, node
from hypergraph.checkpointers import (
    CheckpointPolicy,
    SqliteCheckpointer,
)

aiosqlite = pytest.importorskip("aiosqlite")


@pytest.fixture
def checkpointer(tmp_path):
    """Create a fresh SqliteCheckpointer for each test (sync-only)."""
    cp = SqliteCheckpointer(str(tmp_path / "test.db"))
    # Sync initialization via the _sync_db() pattern
    db = cp._sync_db()
    from hypergraph.checkpointers._migrate import ensure_schema

    ensure_schema(db)
    yield cp
    db.close()


# --- Nodes ---


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3


# --- Tests ---


class TestSyncRunnerCheckpointing:
    def test_sync_durability_persists_steps(self, checkpointer):
        """With durability='sync', steps are persisted after each superstep."""
        checkpointer.policy = CheckpointPolicy(durability="sync", retention="full")
        runner = SyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        result = runner.run(graph, {"x": 5}, workflow_id="wf-sync")

        assert result["tripled"] == 30
        assert result.workflow_id == "wf-sync"

        # Verify run was created and completed
        db = checkpointer._sync_db()
        row = db.execute("SELECT status, graph_name FROM runs WHERE id = ?", ("wf-sync",)).fetchone()
        assert row[0] == "completed"

        # Verify steps
        steps = db.execute("SELECT node_name, status FROM steps WHERE run_id = ? ORDER BY step_index", ("wf-sync",)).fetchall()
        assert len(steps) == 2
        assert steps[0] == ("double", "completed")
        assert steps[1] == ("triple", "completed")

    def test_exit_durability_flushes_at_end(self, checkpointer):
        """With durability='exit', steps are buffered and flushed after the run."""
        checkpointer.policy = CheckpointPolicy(durability="exit", retention="latest")
        runner = SyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        runner.run(graph, {"x": 2}, workflow_id="wf-exit")

        db = checkpointer._sync_db()
        steps = db.execute("SELECT node_name FROM steps WHERE run_id = ?", ("wf-exit",)).fetchall()
        assert len(steps) == 2

    def test_no_workflow_id_skips_checkpointing(self, checkpointer):
        """Without workflow_id, no checkpointing occurs."""
        runner = SyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        result = runner.run(graph, {"x": 1})
        assert result["tripled"] == 6

        db = checkpointer._sync_db()
        runs = db.execute("SELECT id FROM runs").fetchall()
        assert len(runs) == 0

    def test_no_checkpointer_runs_normally(self):
        """Runner without checkpointer still works, even with workflow_id."""
        runner = SyncRunner()
        graph = Graph([double, triple])

        result = runner.run(graph, {"x": 2}, workflow_id="wf-ignored")
        assert result["tripled"] == 12

    def test_failed_node_persists_step_record(self, checkpointer):
        """A node that raises gets a FAILED step record."""
        checkpointer.policy = CheckpointPolicy(durability="sync", retention="full")

        @node(output_name="boom")
        def explode(x: int) -> int:
            raise ValueError("kaboom")

        runner = SyncRunner(checkpointer=checkpointer)
        graph = Graph([explode])

        result = runner.run(graph, {"x": 1}, workflow_id="wf-fail", error_handling="continue")
        assert result.status.value == "failed"

        db = checkpointer._sync_db()
        run = db.execute("SELECT status FROM runs WHERE id = ?", ("wf-fail",)).fetchone()
        assert run[0] == "failed"

        step = db.execute("SELECT node_name, status, error FROM steps WHERE run_id = ?", ("wf-fail",)).fetchone()
        assert step[0] == "explode"
        assert step[1] == "failed"
        assert "kaboom" in step[2]

    def test_exit_durability_flushes_on_failure(self, checkpointer):
        """With durability='exit', buffered steps are flushed even when run fails."""
        checkpointer.policy = CheckpointPolicy(durability="exit", retention="latest")

        @node(output_name="boom")
        def boom(x: int) -> int:
            raise ValueError("exit-fail")

        runner = SyncRunner(checkpointer=checkpointer)
        graph = Graph([boom])

        runner.run(graph, {"x": 1}, workflow_id="wf-exit-fail", error_handling="continue")

        db = checkpointer._sync_db()
        steps = db.execute("SELECT status FROM steps WHERE run_id = ?", ("wf-exit-fail",)).fetchall()
        assert len(steps) == 1
        assert steps[0][0] == "failed"

    def test_protocol_mismatch_raises_clear_error(self):
        """A checkpointer without sync writes raises TypeError at run()."""

        class AsyncOnlyCheckpointer:
            """Fake checkpointer that has no sync write methods."""

            policy = CheckpointPolicy()

        runner = SyncRunner(checkpointer=AsyncOnlyCheckpointer())
        graph = Graph([double])

        with pytest.raises(TypeError, match="does not support sync writes"):
            runner.run(graph, {"x": 1}, workflow_id="wf-proto")

    def test_workflow_id_with_slash_rejected(self):
        """User-provided workflow_id containing '/' is rejected."""
        runner = SyncRunner()
        graph = Graph([double])

        with pytest.raises(ValueError, match="cannot contain '/'"):
            runner.run(graph, {"x": 1}, workflow_id="bad/id")

    def test_run_stats_populated(self, checkpointer):
        """Run record gets node_count, error_count, and duration_ms."""
        checkpointer.policy = CheckpointPolicy(durability="sync", retention="full")
        runner = SyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        runner.run(graph, {"x": 5}, workflow_id="wf-stats")

        db = checkpointer._sync_db()
        row = db.execute("SELECT node_count, error_count, duration_ms FROM runs WHERE id = ?", ("wf-stats",)).fetchone()
        assert row[0] == 2  # node_count
        assert row[1] == 0  # error_count
        assert row[2] > 0  # duration_ms

    def test_map_creates_parent_and_child_runs(self, checkpointer):
        """map() creates a parent batch run and per-item child runs."""
        checkpointer.policy = CheckpointPolicy(durability="sync", retention="full")
        runner = SyncRunner(checkpointer=checkpointer)
        graph = Graph([double])

        runner.map(graph, {"x": [1, 2, 3]}, map_over="x", workflow_id="batch-1")

        db = checkpointer._sync_db()
        runs = db.execute("SELECT id, parent_run_id FROM runs ORDER BY id").fetchall()

        run_ids = {r[0] for r in runs}
        assert "batch-1" in run_ids
        assert "batch-1/0" in run_ids
        assert "batch-1/1" in run_ids
        assert "batch-1/2" in run_ids

        # Children should link to parent
        children = {r[0]: r[1] for r in runs if r[1] is not None}
        for child_id in ["batch-1/0", "batch-1/1", "batch-1/2"]:
            assert children[child_id] == "batch-1"

    def test_cyclic_graph_persists_multiple_supersteps(self, checkpointer):
        """Cyclic nodes that re-execute get new step records per superstep."""
        from hypergraph import END, ifelse

        checkpointer.policy = CheckpointPolicy(durability="sync", retention="full")

        @node(output_name="count")
        def increment(count: int) -> int:
            return count + 1

        @ifelse(when_true=END, when_false="increment")
        def check_done(count: int) -> bool:
            return count >= 3

        runner = SyncRunner(checkpointer=checkpointer)
        graph = Graph([increment, check_done])

        runner.run(graph, {"count": 0}, workflow_id="wf-cycle")

        db = checkpointer._sync_db()
        steps = db.execute(
            "SELECT node_name, superstep FROM steps WHERE run_id = ? AND node_name = 'increment' ORDER BY superstep",
            ("wf-cycle",),
        ).fetchall()
        assert len(steps) >= 2
        supersteps = {s[1] for s in steps}
        assert len(supersteps) == len(steps), "Each re-execution should be in a distinct superstep"
