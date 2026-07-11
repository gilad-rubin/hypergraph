"""Regression tests: a duplicate start must NEVER corrupt the running row.

Bug: starting a second run with a workflow_id that is still executing raises
``WorkflowAlreadyRunningError`` at pre-flight (the rejected call never owns
the run row), but the generic error handler in the run template caught it and
called ``update_run_status(..., FAILED, ...)`` — marking the ORIGINAL,
still-running workflow's persisted row FAILED (issue #127 evidence).
"""

import asyncio

import pytest

from hypergraph import AsyncRunner, Graph, RunStatus, SyncRunner, WorkflowAlreadyRunningError, node
from hypergraph.checkpointers import MemoryCheckpointer, SqliteCheckpointer, WorkflowStatus


class TestAsyncDuplicateStartStatus:
    @pytest.mark.asyncio
    async def test_duplicate_start_never_marks_running_row_failed(self):
        """B4.1: run 1 held open; run 2 raises; run 1's row stays ACTIVE and
        finishes COMPLETED — never FAILED."""
        entered = asyncio.Event()
        release = asyncio.Event()

        @node(output_name="result")
        async def hold() -> str:
            entered.set()
            await release.wait()
            return "done"

        graph = Graph([hold])
        checkpointer = MemoryCheckpointer()
        runner = AsyncRunner(checkpointer=checkpointer)

        run1 = asyncio.create_task(runner.run(graph, workflow_id="wf-dup"))
        await entered.wait()

        # Duplicate start is rejected at pre-flight.
        with pytest.raises(WorkflowAlreadyRunningError):
            await runner.run(graph, workflow_id="wf-dup")

        # The rejected call never owned the row — run 1 is still ACTIVE.
        row = await checkpointer.get_run_async("wf-dup")
        assert row is not None
        assert row.status == WorkflowStatus.ACTIVE

        release.set()
        result = await run1
        assert result.status == RunStatus.COMPLETED

        row = await checkpointer.get_run_async("wf-dup")
        assert row.status == WorkflowStatus.COMPLETED


class TestSyncDuplicateStartStatus:
    def test_duplicate_start_never_marks_running_row_failed(self, tmp_path):
        """Parity: the sync template shares the handler bug. A same-thread
        duplicate start (from inside a node) must not mark the row FAILED."""
        checkpointer = SqliteCheckpointer(str(tmp_path / "dup.db"))
        runner = SyncRunner(checkpointer=checkpointer)
        graph_box: dict[str, Graph] = {}
        observed_mid_flight: list[WorkflowStatus] = []

        @node(output_name="result")
        def duplicate_start() -> str:
            # Same runner, same workflow_id, while this run is executing.
            try:
                runner.run(graph_box["g"], workflow_id="wf-dup")
            except WorkflowAlreadyRunningError:
                row = checkpointer.get_run("wf-dup")
                assert row is not None
                observed_mid_flight.append(row.status)
            return "done"

        graph_box["g"] = Graph([duplicate_start])

        result = runner.run(graph_box["g"], workflow_id="wf-dup")
        assert result.status == RunStatus.COMPLETED

        # The rejected inner call must not have touched the row's status.
        assert observed_mid_flight == [WorkflowStatus.ACTIVE]

        row = checkpointer.get_run("wf-dup")
        assert row.status == WorkflowStatus.COMPLETED
        checkpointer._sync_db().close()
