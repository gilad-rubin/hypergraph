"""Tests for checkpoint resume semantics: run() merges state, map() skips completed."""

import pytest
import pytest_asyncio

from hypergraph import AsyncRunner, Graph, SyncRunner, node
from hypergraph.checkpointers import SqliteCheckpointer, WorkflowStatus
from hypergraph.events import EventProcessor
from hypergraph.events.types import NodeStartEvent, RunEndEvent, RunStartEvent
from hypergraph.exceptions import (
    GraphChangedError,
    InputOverrideRequiresForkError,
    MissingInputError,
    WorkflowAlreadyCompletedError,
)
from hypergraph.runners._shared.results import RunStatus

aiosqlite = pytest.importorskip("aiosqlite")


class EventCollector(EventProcessor):
    """Collect execution events without adding behavior to the runner."""

    def __init__(self):
        self.events: list[object] = []

    def on_event(self, event):
        self.events.append(event)

    def of_type(self, event_type):
        return [event for event in self.events if isinstance(event, event_type)]


# --- Shared nodes ---


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3


# --- Fixtures ---


@pytest_asyncio.fixture
async def checkpointer(tmp_path):
    """Checkpointer for each test."""
    cp = SqliteCheckpointer(str(tmp_path / "test.db"))
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
        """Changing graph under same workflow_id requires explicit fork."""
        runner = AsyncRunner(checkpointer=checkpointer)
        graph_step1 = Graph([double])
        graph_step2 = Graph([triple])

        await runner.run(graph_step1, {"x": 5}, workflow_id="resume-1")

        with pytest.raises(GraphChangedError):
            await runner.run(graph_step2, workflow_id="resume-1")

        checkpoint = checkpointer.checkpoint("resume-1")
        result = await runner.run(graph_step2, checkpoint=checkpoint, workflow_id="resume-1-fork")
        assert result["tripled"] == 30

    async def test_run_runtime_values_override_checkpoint(self, checkpointer):
        """Values on same workflow_id are disallowed; fork to branch."""
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        await runner.run(graph, {"x": 5}, workflow_id="override-1")

        with pytest.raises(InputOverrideRequiresForkError):
            await runner.run(graph, {"x": 100}, workflow_id="override-1")

        checkpoint = checkpointer.checkpoint("override-1")
        result = await runner.run(graph, {"x": 100}, checkpoint=checkpoint, workflow_id="override-1-fork")
        assert result["doubled"] == 200
        assert result["tripled"] == 600

    async def test_run_validation_passes_with_checkpoint_values(self, checkpointer):
        """Fork validation accepts required inputs satisfied by checkpoint values."""
        runner = AsyncRunner(checkpointer=checkpointer)
        graph_step1 = Graph([double])
        graph_step2 = Graph([triple])

        await runner.run(graph_step1, {"x": 5}, workflow_id="valid-1")
        checkpoint = checkpointer.checkpoint("valid-1")
        result = await runner.run(graph_step2, checkpoint=checkpoint, workflow_id="valid-1-fork")
        assert result["tripled"] == 30

    async def test_checkpoint_resume_missing_seed_inputs_raises(self, checkpointer):
        """Checkpoint-started runs should fail clearly instead of no-op completing."""
        runner = AsyncRunner(checkpointer=checkpointer)

        @node(output_name="consumed")
        def consume_x(x: int) -> int:
            return x + 1

        await runner.run(Graph([double]), {"x": 5}, workflow_id="missing-seed-src")
        checkpoint = checkpointer.checkpoint("missing-seed-src")

        with pytest.raises(MissingInputError, match="missing required seed inputs"):
            await runner.run(
                Graph([consume_x]),
                checkpoint=checkpoint,
                workflow_id="missing-seed-fork",
            )

    async def test_checkpoint_resume_missing_seed_inputs_across_active_branches_raises(self, checkpointer):
        """Resume must not complete one branch while silently skipping another."""
        runner = AsyncRunner(checkpointer=checkpointer)

        @node(output_name="x")
        def produce_x(seed: int) -> int:
            return seed

        @node(output_name="a")
        def use_x(x: int) -> int:
            return x + 1

        @node(output_name="b")
        def use_y(y: int) -> int:
            return y + 1

        await runner.run(Graph([produce_x]), {"seed": 1}, workflow_id="missing-branch-src")
        checkpoint = checkpointer.checkpoint("missing-branch-src")

        with pytest.raises(MissingInputError, match="missing required seed inputs"):
            await runner.run(
                Graph([use_x, use_y]),
                checkpoint=checkpoint,
                workflow_id="missing-branch-fork",
            )

    async def test_override_workflow_auto_forks_existing_id(self, checkpointer):
        """override_workflow=True auto-forks instead of raising strict resume errors."""
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        await runner.run(graph, {"x": 5}, workflow_id="override-auto")

        result = await runner.run(
            graph,
            {"x": 100},
            workflow_id="override-auto",
            override_workflow=True,
        )
        assert result["doubled"] == 200
        assert result["tripled"] == 600
        assert result.workflow_id is not None
        assert result.workflow_id.startswith("override-auto-fork-")
        assert result.workflow_id != "override-auto"

        run = checkpointer.get_run(result.workflow_id)
        assert run is not None
        assert run.forked_from == "override-auto"

    async def test_fork_from_allows_fork_without_checkpoint_object(self, checkpointer):
        """fork_from forks by workflow ID directly (no explicit checkpoint handling)."""
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        await runner.run(graph, {"x": 5}, workflow_id="fork-src")
        result = await runner.run(graph, {"x": 100}, fork_from="fork-src")
        assert result["tripled"] == 600
        assert result.workflow_id is not None

        run = checkpointer.get_run(result.workflow_id)
        assert run is not None
        assert run.forked_from == "fork-src"

    async def test_retry_from_allows_retry_without_checkpoint_object(self, checkpointer):
        """retry_from retries by workflow ID directly (no explicit checkpoint handling)."""
        should_fail = True

        @node(output_name="seed")
        def seed(x: int) -> int:
            return x

        @node(output_name="out")
        def flaky(seed: int) -> int:
            nonlocal should_fail
            if should_fail:
                raise RuntimeError("transient")
            return seed * 10

        graph = Graph([seed, flaky])
        runner = AsyncRunner(checkpointer=checkpointer)
        await runner.run(graph, {"x": 5}, workflow_id="retry-src", error_handling="continue")
        should_fail = False

        retry_graph = graph.with_entrypoint("flaky")
        retried = await runner.run(retry_graph, retry_from="retry-src")
        assert retried.status == RunStatus.COMPLETED
        run = checkpointer.get_run(retried.workflow_id)
        assert run is not None
        assert run.retry_of == "retry-src"

    async def test_run_cycle_graph_resume(self, checkpointer):
        """Completed workflows are terminal (resume requires fork)."""
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
        graph = Graph([increment, check_done], entrypoint="increment")

        # First run: count goes 0 → 1 → 2 → 3
        await runner.run(graph, {"count": 0}, workflow_id="cycle-1")

        # Reset counter
        call_count = 0

        with pytest.raises(WorkflowAlreadyCompletedError):
            await runner.run(graph, workflow_id="cycle-1")
        assert call_count == 0

    async def test_no_checkpointer_resume_is_noop(self):
        """Without a checkpointer, workflow_id doesn't trigger resume behavior."""
        runner = AsyncRunner()
        graph = Graph([double, triple])

        result = await runner.run(graph, {"x": 5}, workflow_id="noop-1")
        assert result["tripled"] == 30

        # Second run still works (no checkpoint to load)
        result = await runner.run(graph, {"x": 7}, workflow_id="noop-1")
        assert result["tripled"] == 42

    async def test_fork_from_requires_checkpointer(self):
        """fork_from should fail fast when no checkpointer is configured."""
        runner = AsyncRunner()
        graph = Graph([double, triple])

        with pytest.raises(ValueError, match="require a checkpointer"):
            await runner.run(graph, {"x": 7}, fork_from="noop-1")

    async def test_no_workflow_id_skips_resume(self, checkpointer):
        """With checkpointer, missing workflow_id is auto-generated."""
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple])

        result = await runner.run(graph, {"x": 5})
        assert result["tripled"] == 30
        assert result.workflow_id is not None
        run = checkpointer.get_run(result.workflow_id)
        assert run is not None

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
        collector = EventCollector()
        result = await runner.map(
            graph,
            {"x": [10, 20, 30]},
            map_over="x",
            workflow_id="batch-1",
            event_processors=[collector],
        )
        assert call_count == 0
        assert len(result.results) == 3
        # Restored results should have correct values
        assert all(r["doubled"] is not None for r in result.results)
        assert result.restored_count == 3
        assert all(r.restored for r in result.results)
        assert all(r.log is not None for r in result.results)
        assert all(r.log.steps[0].status == "restored" for r in result.results if r.log is not None)
        assert "3 restored" in result.summary()
        assert "avg" not in result.summary()
        assert result.to_dict()["restored_count"] == 3
        assert result.log.restored_count == 3
        assert "avg" not in result.log.summary()

        starts = collector.of_type(RunStartEvent)
        ends = collector.of_type(RunEndEvent)
        assert len(starts) == 1
        assert starts[0].is_map
        assert collector.of_type(NodeStartEvent) == []
        assert len(ends) == 1
        assert ends[0].batch_completed_items == 3
        assert ends[0].batch_restored_items == 3

        parent = checkpointer.get_run("batch-1")
        assert parent is not None
        assert parent.status == WorkflowStatus.COMPLETED

    async def test_map_reruns_failed_items(self, checkpointer):
        """Failed items are re-executed on resume (only COMPLETED are skipped)."""
        should_fail = True
        calls: list[int] = []

        @node(output_name="result")
        def flaky(x: int) -> int:
            calls.append(x)
            if x == 20 and should_fail:
                raise ValueError("transient failure")
            return x * 2

        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([flaky])

        # First run: item 1 (x=20) fails
        result1 = await runner.map(
            graph,
            {"x": [10, 20]},
            map_over="x",
            workflow_id="batch-retry",
            error_handling="continue",
        )
        statuses = [r.status for r in result1.results]
        assert statuses[1] == RunStatus.FAILED  # x=20 failed
        assert all(not result.restored for result in result1.results)

        # Now make it succeed
        should_fail = False
        calls.clear()
        collector = EventCollector()

        # Resume: x=10 skips (completed), x=20 re-runs (was failed)
        result2 = await runner.map(
            graph,
            {"x": [10, 20]},
            map_over="x",
            workflow_id="batch-retry",
            error_handling="continue",
            event_processors=[collector],
        )
        statuses2 = [r.status for r in result2.results]
        assert all(s == RunStatus.COMPLETED for s in statuses2)
        assert calls == [20]
        assert [result.restored for result in result2.results] == [True, False]
        assert result2.results[0].log is not None
        assert result2.results[0].log.steps[0].status == "restored"
        assert result2.results[1].log is not None
        assert all(step.status != "restored" for step in result2.results[1].log.steps)
        assert result2.restored_count == 1
        assert "1 restored" in result2.summary()

        starts = collector.of_type(RunStartEvent)
        assert len([event for event in starts if event.is_map]) == 1
        assert len([event for event in starts if not event.is_map]) == 1
        parent_end = next(event for event in collector.of_type(RunEndEvent) if event.batch_total_items is not None)
        assert parent_end.batch_completed_items == 2
        assert parent_end.batch_restored_items == 1

        parent = checkpointer.get_run("batch-retry")
        assert parent is not None
        assert parent.status == WorkflowStatus.COMPLETED

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

    async def test_map_resume_matches_completed_items_by_input_identity(self, checkpointer):
        """Reordered inputs should restore by input identity, not list index."""
        call_count = 0

        @node(output_name="doubled")
        def counting_double(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([counting_double])

        await runner.map(graph, {"x": [10, 20, 30]}, map_over="x", workflow_id="identity-batch")
        assert call_count == 3

        call_count = 0
        result = await runner.map(
            graph,
            {"x": [30, 10, 20]},
            map_over="x",
            workflow_id="identity-batch",
        )
        assert call_count == 0
        assert [r["doubled"] for r in result.results] == [60, 20, 40]

    async def test_map_resume_reapplies_select_filter_when_restoring(self, checkpointer):
        """Restored map items should respect select filtering just like fresh runs."""
        runner = AsyncRunner(checkpointer=checkpointer)
        graph = Graph([double, triple]).select("tripled")

        await runner.map(
            graph.select("tripled"),
            {"x": [2, 3]},
            map_over="x",
            workflow_id="select-batch",
        )
        resumed = await runner.map(
            graph.select("tripled"),
            {"x": [3, 2]},
            map_over="x",
            workflow_id="select-batch",
        )

        assert [set(r.values.keys()) for r in resumed.results] == [{"tripled"}, {"tripled"}]
        assert [r["tripled"] for r in resumed.results] == [18, 12]


# =============================================================================
# Sync run() resume tests
# =============================================================================


class TestSyncRunResume:
    def test_run_merges_checkpoint_state(self, sync_checkpointer):
        """Sync mirror: graph change requires fork."""
        runner = SyncRunner(checkpointer=sync_checkpointer)
        graph_step1 = Graph([double])
        graph_step2 = Graph([triple])

        runner.run(graph_step1, {"x": 5}, workflow_id="sync-resume-1")
        with pytest.raises(GraphChangedError):
            runner.run(graph_step2, workflow_id="sync-resume-1")

        checkpoint = sync_checkpointer.checkpoint("sync-resume-1")
        result = runner.run(graph_step2, checkpoint=checkpoint, workflow_id="sync-resume-1-fork")
        assert result["tripled"] == 30

    def test_run_runtime_values_override_checkpoint(self, sync_checkpointer):
        """Sync mirror: values on same workflow_id require fork."""
        runner = SyncRunner(checkpointer=sync_checkpointer)
        graph = Graph([double, triple])

        runner.run(graph, {"x": 5}, workflow_id="sync-override")
        with pytest.raises(InputOverrideRequiresForkError):
            runner.run(graph, {"x": 100}, workflow_id="sync-override")

        checkpoint = sync_checkpointer.checkpoint("sync-override")
        result = runner.run(graph, {"x": 100}, checkpoint=checkpoint, workflow_id="sync-override-fork")
        assert result["doubled"] == 200
        assert result["tripled"] == 600

    def test_checkpoint_resume_missing_seed_inputs_raises(self, sync_checkpointer):
        """Sync mirror: checkpoint-started no-op resumes should fail clearly."""
        runner = SyncRunner(checkpointer=sync_checkpointer)

        @node(output_name="consumed")
        def consume_x(x: int) -> int:
            return x + 1

        runner.run(Graph([double]), {"x": 5}, workflow_id="sync-missing-seed-src")
        checkpoint = sync_checkpointer.checkpoint("sync-missing-seed-src")

        with pytest.raises(MissingInputError, match="missing required seed inputs"):
            runner.run(
                Graph([consume_x]),
                checkpoint=checkpoint,
                workflow_id="sync-missing-seed-fork",
            )

    def test_checkpoint_resume_missing_seed_inputs_across_active_branches_raises(self, sync_checkpointer):
        """Sync mirror: resume must validate every active branch."""
        runner = SyncRunner(checkpointer=sync_checkpointer)

        @node(output_name="x")
        def produce_x(seed: int) -> int:
            return seed

        @node(output_name="a")
        def use_x(x: int) -> int:
            return x + 1

        @node(output_name="b")
        def use_y(y: int) -> int:
            return y + 1

        runner.run(Graph([produce_x]), {"seed": 1}, workflow_id="sync-missing-branch-src")
        checkpoint = sync_checkpointer.checkpoint("sync-missing-branch-src")

        with pytest.raises(MissingInputError, match="missing required seed inputs"):
            runner.run(
                Graph([use_x, use_y]),
                checkpoint=checkpoint,
                workflow_id="sync-missing-branch-fork",
            )

    def test_override_workflow_auto_forks_existing_id(self, sync_checkpointer):
        """Sync mirror: override_workflow=True auto-forks existing workflow_id."""
        runner = SyncRunner(checkpointer=sync_checkpointer)
        graph = Graph([double, triple])

        runner.run(graph, {"x": 5}, workflow_id="sync-override-auto")

        result = runner.run(
            graph,
            {"x": 100},
            workflow_id="sync-override-auto",
            override_workflow=True,
        )
        assert result["doubled"] == 200
        assert result["tripled"] == 600
        assert result.workflow_id is not None
        assert result.workflow_id.startswith("sync-override-auto-fork-")
        assert result.workflow_id != "sync-override-auto"

        run = sync_checkpointer.get_run(result.workflow_id)
        assert run is not None
        assert run.forked_from == "sync-override-auto"

    def test_fork_from_allows_fork_without_checkpoint_object(self, sync_checkpointer):
        """Sync mirror: fork_from forks by workflow ID directly."""
        runner = SyncRunner(checkpointer=sync_checkpointer)
        graph = Graph([double, triple])

        runner.run(graph, {"x": 5}, workflow_id="sync-fork-src")
        result = runner.run(graph, {"x": 100}, fork_from="sync-fork-src")
        assert result["tripled"] == 600
        run = sync_checkpointer.get_run(result.workflow_id)
        assert run is not None
        assert run.forked_from == "sync-fork-src"

    def test_fork_from_requires_checkpointer(self):
        """Sync mirror: fork_from should fail fast without a checkpointer."""
        runner = SyncRunner()
        graph = Graph([double, triple])

        with pytest.raises(ValueError, match="require a checkpointer"):
            runner.run(graph, {"x": 7}, fork_from="noop-1")


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
        collector = EventCollector()
        result = runner.map(
            graph,
            {"x": [10, 20, 30]},
            map_over="x",
            workflow_id="sync-batch",
            event_processors=[collector],
        )
        assert call_count == 0
        assert len(result.results) == 3
        assert result.restored_count == 3
        assert all(r.restored for r in result.results)
        assert all(r.log is not None for r in result.results)
        assert all(r.log.steps[0].status == "restored" for r in result.results if r.log is not None)
        assert "3 restored" in result.summary()
        assert "avg" not in result.summary()
        assert result.to_dict()["restored_count"] == 3
        assert result.log.restored_count == 3
        assert "avg" not in result.log.summary()

        starts = collector.of_type(RunStartEvent)
        ends = collector.of_type(RunEndEvent)
        assert len(starts) == 1
        assert starts[0].is_map
        assert collector.of_type(NodeStartEvent) == []
        assert len(ends) == 1
        assert ends[0].batch_completed_items == 3
        assert ends[0].batch_restored_items == 3

        parent = sync_checkpointer.get_run("sync-batch")
        assert parent is not None
        assert parent.status == WorkflowStatus.COMPLETED

    def test_map_reruns_failed_items(self, sync_checkpointer):
        """Sync mirror: failed items are re-executed on resume."""
        should_fail = True
        calls: list[int] = []

        @node(output_name="result")
        def flaky(x: int) -> int:
            calls.append(x)
            if x == 20 and should_fail:
                raise ValueError("transient failure")
            return x * 2

        runner = SyncRunner(checkpointer=sync_checkpointer)
        graph = Graph([flaky])

        result1 = runner.map(
            graph,
            {"x": [10, 20]},
            map_over="x",
            workflow_id="sync-retry",
            error_handling="continue",
        )
        assert result1.results[1].status == RunStatus.FAILED
        assert all(not result.restored for result in result1.results)

        should_fail = False
        calls.clear()
        collector = EventCollector()
        result2 = runner.map(
            graph,
            {"x": [10, 20]},
            map_over="x",
            workflow_id="sync-retry",
            error_handling="continue",
            event_processors=[collector],
        )
        assert all(r.status == RunStatus.COMPLETED for r in result2.results)
        assert calls == [20]
        assert [result.restored for result in result2.results] == [True, False]
        assert result2.results[0].log is not None
        assert result2.results[0].log.steps[0].status == "restored"
        assert result2.results[1].log is not None
        assert all(step.status != "restored" for step in result2.results[1].log.steps)
        assert result2.restored_count == 1
        assert "1 restored" in result2.summary()

        starts = collector.of_type(RunStartEvent)
        assert len([event for event in starts if event.is_map]) == 1
        assert len([event for event in starts if not event.is_map]) == 1
        parent_end = next(event for event in collector.of_type(RunEndEvent) if event.batch_total_items is not None)
        assert parent_end.batch_completed_items == 2
        assert parent_end.batch_restored_items == 1

        parent = sync_checkpointer.get_run("sync-retry")
        assert parent is not None
        assert parent.status == WorkflowStatus.COMPLETED

    def test_map_resume_matches_completed_items_by_input_identity(self, sync_checkpointer):
        """Sync mirror: reordered inputs should restore by input identity."""
        call_count = 0

        @node(output_name="doubled")
        def counting_double(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        runner = SyncRunner(checkpointer=sync_checkpointer)
        graph = Graph([counting_double])

        runner.map(graph, {"x": [10, 20, 30]}, map_over="x", workflow_id="sync-identity")
        assert call_count == 3

        call_count = 0
        result = runner.map(graph, {"x": [30, 10, 20]}, map_over="x", workflow_id="sync-identity")
        assert call_count == 0
        assert [r["doubled"] for r in result.results] == [60, 20, 40]

    def test_map_resume_reapplies_select_filter_when_restoring(self, sync_checkpointer):
        """Sync mirror: restored map items should preserve select filtering."""
        runner = SyncRunner(checkpointer=sync_checkpointer)
        graph = Graph([double, triple]).select("tripled")

        runner.map(
            graph.select("tripled"),
            {"x": [2, 3]},
            map_over="x",
            workflow_id="sync-select-batch",
        )
        resumed = runner.map(
            graph.select("tripled"),
            {"x": [3, 2]},
            map_over="x",
            workflow_id="sync-select-batch",
        )

        assert [set(r.values.keys()) for r in resumed.results] == [{"tripled"}, {"tripled"}]
        assert [r["tripled"] for r in resumed.results] == [18, 12]


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
        children = checkpointer.runs(parent_run_id="parent-1")
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
        stored = checkpointer.get_run("upsert-1")
        assert stored.created_at == original_created
        assert stored.status == WorkflowStatus.ACTIVE
