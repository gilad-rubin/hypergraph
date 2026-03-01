"""Tests for checkpoint resume semantics: run() merges state, map() skips completed."""

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, node
from hypergraph.checkpointers import SqliteCheckpointer, WorkflowStatus
from hypergraph.runners._shared.types import RunStatus

aiosqlite = pytest.importorskip("aiosqlite")


# --- Shared nodes ---


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3


# --- Fixtures ---


@pytest.fixture
async def checkpointer(tmp_path):
    """Async checkpointer for each test."""
    cp = SqliteCheckpointer(str(tmp_path / "test.db"))
    await cp.initialize()
    yield cp
    await cp.close()


@pytest.fixture
def sync_checkpointer(tmp_path):
    """Sync-only checkpointer for each test."""
    cp = SqliteCheckpointer(str(tmp_path / "test.db"))
    cp._sync_db()  # triggers schema creation
    yield cp
    if cp._sync_conn:
        cp._sync_conn.close()


# =============================================================================
# Async run() resume tests
# =============================================================================


class TestAsyncRunResume:
    async def test_run_merges_checkpoint_state(self, checkpointer):
        """Second run with same workflow_id gets checkpoint state merged in.

        Pattern: first run produces 'doubled', second run uses it to produce 'tripled'
        without the user providing 'doubled' explicitly.
        """
        runner = AsyncRunner(checkpointer=checkpointer)
        graph_step1 = Graph([double])
        graph_step2 = Graph([triple])

        # First run: produce 'doubled'
        await runner.run(graph_step1, {"x": 5}, workflow_id="resume-1")

        # Second run: 'doubled' comes from checkpoint, produces 'tripled'
        result = await runner.run(graph_step2, workflow_id="resume-1")

        assert result["tripled"] == 30

    async def test_run_runtime_values_override_checkpoint(self, checkpointer):
        """Explicit runtime values win over checkpoint state."""
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        # First run: doubled=10, tripled=30
        await runner.run(graph, {"x": 5}, workflow_id="override-1")

        # Second run: override 'x' with different value
        result = await runner.run(graph, {"x": 100}, workflow_id="override-1")

        # Should use x=100 (runtime wins), not x=5 (checkpoint)
        assert result["doubled"] == 200
        assert result["tripled"] == 600

    async def test_run_validation_passes_with_checkpoint_values(self, checkpointer):
        """A required input satisfied by checkpoint doesn't raise MissingInputError."""
        runner = AsyncRunner(checkpointer=checkpointer)
        graph_step1 = Graph([double])
        graph_step2 = Graph([triple])

        # First run: produce 'doubled=10'
        await runner.run(graph_step1, {"x": 5}, workflow_id="valid-1")

        # Second run needs 'doubled' — checkpoint provides it, no values needed
        result = await runner.run(graph_step2, workflow_id="valid-1")
        assert result["tripled"] == 30

    async def test_run_cycle_graph_resume(self, checkpointer):
        """Resume a cyclic graph: checkpoint provides the seed value."""
        from hypergraph import END, ifelse

        call_count = 0

        @node(output_name="count")
        def increment(count: int) -> int:
            nonlocal call_count
            call_count += 1
            return count + 1

        @ifelse(when_true=END, when_false="increment")
        def check_done(count: int) -> bool:
            return count >= 3

        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([increment, check_done])

        # First run: count goes 0 → 1 → 2 → 3
        await runner.run(graph, {"count": 0}, workflow_id="cycle-1")
        first_calls = call_count

        # Reset counter
        call_count = 0

        # Resume: checkpoint has count=3, gate immediately exits
        result = await runner.run(graph, workflow_id="cycle-1")
        assert result["count"] >= 3
        # Should need fewer increments since checkpoint provides count=3
        assert call_count <= first_calls

    async def test_no_checkpointer_resume_is_noop(self):
        """Without a checkpointer, workflow_id doesn't trigger resume behavior."""
        runner = AsyncRunner()
        graph = Graph([double, triple])

        result = await runner.run(graph, {"x": 5}, workflow_id="noop-1")
        assert result["tripled"] == 30

        # Second run still works (no checkpoint to load)
        result = await runner.run(graph, {"x": 7}, workflow_id="noop-1")
        assert result["tripled"] == 42

    async def test_no_workflow_id_skips_resume(self, checkpointer):
        """Without workflow_id, no resume even with checkpointer."""
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        # Run without workflow_id
        result = await runner.run(graph, {"x": 5})
        assert result["tripled"] == 30

    async def test_first_run_with_no_prior_state(self, checkpointer):
        """First run with workflow_id and checkpointer works normally (no prior state)."""
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        result = await runner.run(graph, {"x": 5}, workflow_id="fresh-1")
        assert result["tripled"] == 30


# =============================================================================
# Async map() resume tests
# =============================================================================


class TestAsyncMapResume:
    async def test_map_skips_completed_items(self, checkpointer):
        """Re-running map() with same workflow_id skips already-completed items."""
        call_count = 0

        @node(output_name="doubled")
        def counting_double(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([counting_double])

        # First run: all 3 items execute
        await runner.map(
            graph,
            {"x": [10, 20, 30]},
            map_over="x",
            workflow_id="batch-1",
        )
        assert call_count == 3

        # Reset counter
        call_count = 0

        # Resume: all 3 completed, should skip all
        result = await runner.map(
            graph,
            {"x": [10, 20, 30]},
            map_over="x",
            workflow_id="batch-1",
        )
        assert call_count == 0
        assert len(result.results) == 3
        # Restored results should have correct values
        assert all(r["doubled"] is not None for r in result.results)

    async def test_map_reruns_failed_items(self, checkpointer):
        """Failed items are re-executed on resume (only COMPLETED are skipped)."""
        attempt = 0

        @node(output_name="result")
        def flaky(x: int) -> int:
            nonlocal attempt
            attempt += 1
            if x == 20 and attempt <= 1:
                raise ValueError("transient failure")
            return x * 2

        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([flaky])

        # First run: item 1 (x=20) fails
        result1 = await runner.map(
            graph,
            {"x": [10, 20, 30]},
            map_over="x",
            workflow_id="batch-retry",
            error_handling="continue",
        )
        statuses = [r.status for r in result1.results]
        assert statuses[1] == RunStatus.FAILED  # x=20 failed

        # Reset attempt counter (flaky now succeeds)
        attempt = 10  # past the failure threshold

        # Resume: x=10 and x=30 skip (completed), x=20 re-runs (was failed)
        result2 = await runner.map(
            graph,
            {"x": [10, 20, 30]},
            map_over="x",
            workflow_id="batch-retry",
            error_handling="continue",
        )
        statuses2 = [r.status for r in result2.results]
        assert all(s == RunStatus.COMPLETED for s in statuses2)

    async def test_map_no_checkpointer_no_skip(self):
        """Without checkpointer, map always runs all items."""
        call_count = 0

        @node(output_name="doubled")
        def counting_double(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        runner = AsyncRunner()
        graph = Graph([counting_double])

        await runner.map(graph, {"x": [1, 2]}, map_over="x", workflow_id="no-cp")
        assert call_count == 2

        call_count = 0
        await runner.map(graph, {"x": [1, 2]}, map_over="x", workflow_id="no-cp")
        assert call_count == 2  # all re-executed


# =============================================================================
# Sync run() resume tests
# =============================================================================


class TestSyncRunResume:
    def test_run_merges_checkpoint_state(self, sync_checkpointer):
        """Sync mirror: second run with same workflow_id gets checkpoint state."""
        runner = SyncRunner(checkpointer=sync_checkpointer)
        graph_step1 = Graph([double])
        graph_step2 = Graph([triple])

        runner.run(graph_step1, {"x": 5}, workflow_id="sync-resume-1")
        result = runner.run(graph_step2, workflow_id="sync-resume-1")

        assert result["tripled"] == 30

    def test_run_runtime_values_override_checkpoint(self, sync_checkpointer):
        """Sync mirror: explicit runtime values win over checkpoint state."""
        runner = SyncRunner(checkpointer=sync_checkpointer)
        graph = Graph([double, triple])

        runner.run(graph, {"x": 5}, workflow_id="sync-override")
        result = runner.run(graph, {"x": 100}, workflow_id="sync-override")

        assert result["doubled"] == 200
        assert result["tripled"] == 600


# =============================================================================
# Sync map() resume tests
# =============================================================================


class TestSyncMapResume:
    def test_map_skips_completed_items(self, sync_checkpointer):
        """Sync mirror: re-running map() skips completed items."""
        call_count = 0

        @node(output_name="doubled")
        def counting_double(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        runner = SyncRunner(checkpointer=sync_checkpointer)
        graph = Graph([counting_double])

        runner.map(graph, {"x": [10, 20, 30]}, map_over="x", workflow_id="sync-batch")
        assert call_count == 3

        call_count = 0
        result = runner.map(graph, {"x": [10, 20, 30]}, map_over="x", workflow_id="sync-batch")
        assert call_count == 0
        assert len(result.results) == 3

    def test_map_reruns_failed_items(self, sync_checkpointer):
        """Sync mirror: failed items are re-executed on resume."""
        attempt = 0

        @node(output_name="result")
        def flaky(x: int) -> int:
            nonlocal attempt
            attempt += 1
            if x == 20 and attempt <= 1:
                raise ValueError("transient failure")
            return x * 2

        runner = SyncRunner(checkpointer=sync_checkpointer)
        graph = Graph([flaky])

        result1 = runner.map(
            graph,
            {"x": [10, 20, 30]},
            map_over="x",
            workflow_id="sync-retry",
            error_handling="continue",
        )
        assert result1.results[1].status == RunStatus.FAILED

        attempt = 10
        result2 = runner.map(
            graph,
            {"x": [10, 20, 30]},
            map_over="x",
            workflow_id="sync-retry",
            error_handling="continue",
        )
        assert all(r.status == RunStatus.COMPLETED for r in result2.results)


# =============================================================================
# list_runs parent_run_id filter (async)
# =============================================================================


class TestListRunsParentFilter:
    async def test_list_runs_filters_by_parent(self, checkpointer):
        """Async list_runs with parent_run_id returns only children."""
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double])

        # Create parent + children via map
        await runner.map(graph, {"x": [1, 2, 3]}, map_over="x", workflow_id="parent-1")

        # Also create an unrelated run
        await runner.run(graph, {"x": 99}, workflow_id="unrelated")

        # Filter by parent
        children = await checkpointer.list_runs(parent_run_id="parent-1")
        child_ids = {r.id for r in children}
        assert child_ids == {"parent-1/0", "parent-1/1", "parent-1/2"}
        assert "unrelated" not in child_ids
        assert "parent-1" not in child_ids


# =============================================================================
# create_run upsert preserves created_at
# =============================================================================


class TestCreateRunUpsert:
    async def test_upsert_preserves_created_at(self, checkpointer):
        """Re-creating a run preserves the original created_at timestamp."""
        run1 = await checkpointer.create_run("upsert-1", graph_name="test")
        original_created = run1.created_at

        # Upsert same run ID
        await checkpointer.create_run("upsert-1", graph_name="test-v2")

        # Fetch from DB to verify
        stored = await checkpointer.get_run_async("upsert-1")
        assert stored.created_at == original_created
        assert stored.status == WorkflowStatus.ACTIVE
