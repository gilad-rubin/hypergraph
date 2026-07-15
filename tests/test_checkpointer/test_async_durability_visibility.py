"""Async checkpoint durability is best-effort but visible (decision D1, issue #126).

With the default ``durability="async"``, background step-save failures must not
fail the run — but they must surface on ``result.checkpoint_ok`` /
``result.checkpoint_errors``. With ``durability="sync"``, the same failure
fails the run (pinned existing behavior, sync AND async runner flavors).
"""

from __future__ import annotations

import asyncio

import pytest

from hypergraph import AsyncRunner, Graph, RunStatus, SyncRunner, interrupt, node
from hypergraph.checkpointers import MemoryCheckpointer, SqliteCheckpointer
from hypergraph.checkpointers.base import CheckpointPolicy
from hypergraph.checkpointers.types import StepRecord
from hypergraph.exceptions import IncompatibleRunnerError
from hypergraph.runners._shared.node_context import NodeContext
from tests._interrupt_questions import StringQuestion


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


class ChildOnlyFailingSaveCheckpointer(MemoryCheckpointer):
    """Fail step writes below the parent workflow, but persist parent steps."""

    async def save_step(self, record: StepRecord) -> None:
        if "/" in record.run_id:
            raise RuntimeError(f"disk full for {record.run_id}")
        await super().save_step(record)


class CheckpointForwardingAsyncRunner:
    """Non-template runner wrapper that explicitly accepts the private sink."""

    _accepts_checkpoint_error_sink = True

    def __init__(self, checkpointer: MemoryCheckpointer):
        self._runner = AsyncRunner(checkpointer=checkpointer)

    @property
    def capabilities(self):
        return self._runner.capabilities

    @property
    def supported_node_types(self):
        return self._runner.supported_node_types

    async def run(self, *args, _checkpoint_error_sink=None, **kwargs):
        return await self._runner.run(
            *args,
            _checkpoint_error_sink=_checkpoint_error_sink,
            **kwargs,
        )

    async def map(self, *args, _checkpoint_error_sink=None, **kwargs):
        return await self._runner.map(
            *args,
            _checkpoint_error_sink=_checkpoint_error_sink,
            **kwargs,
        )


class NonParticipatingCheckpointAsyncRunner:
    """Checkpoint-capable async wrapper that does not accept sink forwarding."""

    def __init__(self, checkpointer: MemoryCheckpointer):
        self._runner = AsyncRunner(checkpointer=checkpointer)
        self.run_calls = 0

    @property
    def capabilities(self):
        return self._runner.capabilities

    @property
    def supported_node_types(self):
        return self._runner.supported_node_types

    async def run(self, *args, **kwargs):
        self.run_calls += 1
        return await self._runner.run(*args, **kwargs)

    async def map(self, *args, **kwargs):
        return await self._runner.map(*args, **kwargs)


class NonCheckpointingAsyncRunner:
    """Non-template async wrapper with no durability evidence to forward."""

    def __init__(self):
        self._runner = AsyncRunner()

    @property
    def capabilities(self):
        return self._runner.capabilities

    @property
    def supported_node_types(self):
        return self._runner.supported_node_types

    async def run(self, *args, **kwargs):
        return await self._runner.run(*args, **kwargs)

    async def map(self, *args, **kwargs):
        return await self._runner.map(*args, **kwargs)


def _assert_nested_checkpoint_failures(result, *run_ids: str) -> None:
    expected = {f"RuntimeError('disk full for {run_id}')" for run_id in run_ids}
    assert result.checkpoint_ok is False
    assert set(result.checkpoint_errors) == expected
    assert len(result.checkpoint_errors) == len(expected)
    assert all(isinstance(error, str) for error in result.checkpoint_errors)


class TestAsyncDurabilityBestEffort:
    async def test_background_handle_preserves_best_effort_failure_evidence(self):
        checkpointer = FailingSaveCheckpointer()
        checkpointer.policy = CheckpointPolicy(durability="async")
        runner = AsyncRunner(checkpointer=checkpointer)

        handle = runner.start_run(
            Graph([double]),
            {"x": 5},
            workflow_id="wf-background-best-effort",
        )
        result = await handle.result()

        assert result.completed
        assert result["doubled"] == 10
        assert checkpointer.save_attempts > 0
        assert result.checkpoint_ok is False
        assert result.checkpoint_errors
        assert any("disk full" in error for error in result.checkpoint_errors)

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


class TestNestedAsyncDurabilityBestEffort:
    async def test_deeply_nested_graph_completion_surfaces_each_child_failure_once(self):
        checkpointer = ChildOnlyFailingSaveCheckpointer()
        runner = AsyncRunner(checkpointer=checkpointer)
        leaf = Graph([double], name="leaf")
        middle = Graph([leaf.as_node()], name="middle")
        outer = Graph([middle.as_node()], name="outer")

        result = await runner.run(outer, {"x": 5}, workflow_id="wf-nested")

        assert result.completed
        assert result["doubled"] == 10
        _assert_nested_checkpoint_failures(
            result,
            "wf-nested/middle",
            "wf-nested/middle/leaf",
        )

    async def test_mapped_graph_completion_surfaces_child_failures(self):
        checkpointer = ChildOnlyFailingSaveCheckpointer()
        runner = AsyncRunner(checkpointer=checkpointer)
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node().map_over("x")], name="outer")

        result = await runner.run(outer, {"x": [2, 3]}, workflow_id="wf-mapped")

        assert result.completed
        assert result["doubled"] == [4, 6]
        _assert_nested_checkpoint_failures(
            result,
            "wf-mapped/inner/0",
            "wf-mapped/inner/1",
        )

    async def test_nested_pause_surfaces_child_failure(self):
        @interrupt(answer_name="decision")
        def approval(draft: str) -> StringQuestion:
            return StringQuestion(prompt="Approve?", evidence=(draft,))

        checkpointer = ChildOnlyFailingSaveCheckpointer()
        runner = AsyncRunner(checkpointer=checkpointer)
        inner = Graph([approval], name="inner")
        outer = Graph([inner.as_node()], name="outer")

        result = await runner.run(outer, {"draft": "review me"}, workflow_id="wf-paused")

        assert result.status == RunStatus.PAUSED
        _assert_nested_checkpoint_failures(result, "wf-paused/inner")

    async def test_nested_cooperative_stop_surfaces_child_failure(self):
        started = asyncio.Event()

        @node(output_name="partial")
        async def stream(ctx: NodeContext) -> str:
            started.set()
            while not ctx.stop_requested:
                await asyncio.sleep(0)
            return "saved partial"

        checkpointer = ChildOnlyFailingSaveCheckpointer()
        runner = AsyncRunner(checkpointer=checkpointer)
        inner = Graph([stream], name="inner")
        outer = Graph([inner.as_node()], name="outer")

        async def stop_child() -> None:
            await started.wait()
            runner.stop("wf-stopped")

        stop_task = asyncio.create_task(stop_child())
        result = await runner.run(outer, workflow_id="wf-stopped")
        await stop_task

        assert result.status == RunStatus.STOPPED
        assert result["partial"] == "saved partial"
        _assert_nested_checkpoint_failures(result, "wf-stopped/inner")

    async def test_outer_continue_failure_surfaces_child_checkpoint_failure(self):
        @node(output_name="never")
        def fail_inside_child(x: int) -> int:
            raise ValueError(f"bad child input: {x}")

        checkpointer = ChildOnlyFailingSaveCheckpointer()
        runner = AsyncRunner(checkpointer=checkpointer)
        inner = Graph([fail_inside_child], name="inner")
        outer = Graph([inner.as_node()], name="outer")

        result = await runner.run(
            outer,
            {"x": 5},
            workflow_id="wf-failed",
            error_handling="continue",
        )

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, ValueError)
        _assert_nested_checkpoint_failures(result, "wf-failed/inner")

    async def test_healthy_nested_run_keeps_checkpoint_evidence_clean(self):
        runner = AsyncRunner(checkpointer=MemoryCheckpointer())
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node()], name="outer")

        result = await runner.run(outer, {"x": 5}, workflow_id="wf-healthy-nested")

        assert result.completed
        assert result.checkpoint_ok is True
        assert result.checkpoint_errors == ()


class TestDelegatedRunnerDurabilityForwarding:
    async def test_noncheckpointing_async_wrapper_does_not_need_sink_capability(self):
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node(runner=NonCheckpointingAsyncRunner())])
        parent = AsyncRunner(checkpointer=MemoryCheckpointer())

        result = await parent.run(
            outer,
            {"x": 5},
            workflow_id="wf-wrapper-no-checkpointer",
        )

        assert result.completed
        assert result["doubled"] == 10
        assert result.checkpoint_ok is True

    async def test_non_template_wrapper_forwards_run_checkpoint_failure(self):
        child_checkpointer = ChildOnlyFailingSaveCheckpointer()
        child_runner = CheckpointForwardingAsyncRunner(child_checkpointer)
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node(runner=child_runner)], name="outer")
        parent = AsyncRunner(checkpointer=MemoryCheckpointer())

        result = await parent.run(outer, {"x": 5}, workflow_id="wf-wrapper-run")

        assert result.completed
        assert result["doubled"] == 10
        assert result.checkpoint_errors == ("RuntimeError('disk full for wf-wrapper-run/inner')",)

    async def test_non_template_wrapper_forwards_mapped_failures_in_item_order(self):
        @node(output_name="doubled")
        async def delayed_double(x: int) -> int:
            await asyncio.sleep((4 - x) * 0.01)
            return x * 2

        child_checkpointer = ChildOnlyFailingSaveCheckpointer()
        child_runner = CheckpointForwardingAsyncRunner(child_checkpointer)
        inner = Graph([delayed_double], name="inner")
        outer = Graph([inner.as_node(runner=child_runner).map_over("x")], name="outer")
        parent = AsyncRunner(checkpointer=MemoryCheckpointer())

        result = await parent.run(
            outer,
            {"x": [1, 2, 3]},
            workflow_id="wf-wrapper-map",
        )

        assert result.completed
        assert result["doubled"] == [2, 4, 6]
        assert result.checkpoint_errors == tuple(f"RuntimeError('disk full for wf-wrapper-map/inner/{index}')" for index in range(3))

    async def test_non_template_wrapper_forwards_checkpoint_failure_when_child_raises(self):
        @node(output_name="never")
        async def fail_child(x: int) -> int:
            await asyncio.sleep(0)
            raise ValueError(f"bad child input: {x}")

        child_checkpointer = ChildOnlyFailingSaveCheckpointer()
        child_runner = CheckpointForwardingAsyncRunner(child_checkpointer)
        inner = Graph([fail_child], name="inner")
        outer = Graph([inner.as_node(runner=child_runner)], name="outer")
        parent = AsyncRunner(checkpointer=MemoryCheckpointer())

        result = await parent.run(
            outer,
            {"x": 5},
            workflow_id="wf-wrapper-raised",
            error_handling="continue",
        )

        assert result.status == RunStatus.FAILED
        assert isinstance(result.error, ValueError)
        assert result.checkpoint_errors == ("RuntimeError('disk full for wf-wrapper-raised/inner')",)

    async def test_nonparticipating_checkpoint_async_wrapper_is_rejected_before_run(self):
        child_runner = NonParticipatingCheckpointAsyncRunner(ChildOnlyFailingSaveCheckpointer())
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node(runner=child_runner)], name="outer")
        parent = AsyncRunner(checkpointer=MemoryCheckpointer())

        with pytest.raises(IncompatibleRunnerError) as exc_info:
            await parent.run(outer, {"x": 5}, workflow_id="wf-wrapper-rejected")

        assert child_runner.run_calls == 0
        assert "checkpoint error" in str(exc_info.value).lower()
        assert "How to fix:" in str(exc_info.value)


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
