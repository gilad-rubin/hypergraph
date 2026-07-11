"""Async checkpoint durability is best-effort but visible (decision D1, issue #126).

With the default ``durability="async"``, background step-save failures must not
fail the run — but they must surface on ``result.checkpoint_ok`` /
``result.checkpoint_errors``. With ``durability="sync"``, the same failure
fails the run (pinned existing behavior, sync AND async runner flavors).
"""

from __future__ import annotations

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, node
from hypergraph.checkpointers import MemoryCheckpointer, SqliteCheckpointer
from hypergraph.checkpointers.base import CheckpointPolicy
from hypergraph.checkpointers.types import StepRecord


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


class FailingSaveCheckpointer(MemoryCheckpointer):
    """MemoryCheckpointer whose step writes always fail."""

    def __init__(self):
        super().__init__()
        self.save_attempts = 0

    async def save_step(self, record: StepRecord) -> None:
        self.save_attempts += 1
        raise RuntimeError("disk full")


class TestAsyncDurabilityBestEffort:
    async def test_async_durability_failure_completes_but_flags_checkpoint(self):
        checkpointer = FailingSaveCheckpointer()
        checkpointer.policy = CheckpointPolicy(durability="async")
        runner = AsyncRunner(checkpointer=checkpointer)

        result = await runner.run(Graph([double]), {"x": 5}, workflow_id="wf-best-effort")

        assert result.completed
        assert result["doubled"] == 10
        assert checkpointer.save_attempts > 0
        assert result.checkpoint_ok is False
        assert result.checkpoint_errors
        assert any("disk full" in err for err in result.checkpoint_errors)
        # String reprs only — no live exception objects across the boundary.
        assert all(isinstance(err, str) for err in result.checkpoint_errors)

    async def test_healthy_checkpointer_reports_checkpoint_ok(self):
        checkpointer = MemoryCheckpointer()
        runner = AsyncRunner(checkpointer=checkpointer)

        result = await runner.run(Graph([double]), {"x": 5}, workflow_id="wf-healthy")

        assert result.completed
        assert result.checkpoint_ok is True
        assert result.checkpoint_errors == ()

    async def test_no_checkpointer_defaults_checkpoint_ok(self):
        result = await AsyncRunner().run(Graph([double]), {"x": 5})
        assert result.checkpoint_ok is True
        assert result.checkpoint_errors == ()


class TestSyncDurabilityFailsTheRun:
    async def test_async_runner_sync_durability_failure_fails_run(self):
        """Pinned: durability='sync' step-save failures propagate and fail the run."""
        checkpointer = FailingSaveCheckpointer()
        checkpointer.policy = CheckpointPolicy(durability="sync", retention="full")
        runner = AsyncRunner(checkpointer=checkpointer)

        with pytest.raises(RuntimeError, match="disk full"):
            await runner.run(Graph([double]), {"x": 5}, workflow_id="wf-sync-durability")

    def test_sync_runner_save_failure_fails_run(self, tmp_path):
        """Pinned parity: SyncRunner writes steps synchronously, so a save
        failure always fails the run — there is no best-effort mode."""

        class FailingSyncCheckpointer(SqliteCheckpointer):
            def save_step_sync(self, record: StepRecord) -> None:
                raise RuntimeError("disk full")

        checkpointer = FailingSyncCheckpointer(str(tmp_path / "cp.db"))
        runner = SyncRunner(checkpointer=checkpointer)

        with pytest.raises(RuntimeError, match="disk full"):
            runner.run(Graph([double]), {"x": 5}, workflow_id="wf-sync-runner")
