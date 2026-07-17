"""Contract tests for cooperative async per-attempt timeout (#231).

Assertion map (ticket red-green items + locked #187 precedence):
    1  cancellation settles before timeout escapes
       test_attempt_timeout_waits_for_cleanup_and_records_truth
    2  retry never overlaps settling work
       test_timeout_retry_starts_only_after_prior_attempt_settles
    3  unsupported work is rejected before execution
       test_sync_runner_rejects_timeout_before_execution
       test_sync_runner_rejects_async_timeout_before_execution
       test_async_runner_rejects_sync_callable_timeout_before_execution
       test_async_runner_rejects_sync_generator_timeout_before_execution
       test_delegated_runner_rejects_timeout_before_execution
       test_daft_runner_rejects_timeout_before_execution
    4  suppressed cancellation returns a witnessed value
       test_suppressed_cancellation_accepts_late_success_with_evidence
    5  cleanup exception replaces timeout
       test_cleanup_exception_is_preserved_exactly
       test_cleanup_exception_retry_eligibility_uses_its_own_type
    6  no wall-clock assertions
       Event/barrier ordering only; tiny deadlines merely trigger cancellation
    7  series deadline during active work
       test_retry_window_expiry_during_active_work_has_distinct_error
    8  async generators are supported; sync generators are rejected
       test_async_generator_timeout_waits_for_cleanup
    9  external cancellation remains BaseException control flow
       test_external_cancellation_is_not_converted_to_timeout

The tests deliberately assert public runner behavior and public checkpoint
evidence. ``asyncio.wait_for(..., timeout=5)`` calls are deadlock guards, not
elapsed-time assertions.
"""

from __future__ import annotations

import asyncio
import math

import pytest

from hypergraph import (
    AsyncRunner,
    AttemptTimeoutError,
    DaftRunner,
    Graph,
    IncompatibleRunnerError,
    RetryPolicy,
    RetryWindowExpiredError,
    SyncRunner,
    node,
)
from hypergraph.checkpointers import AttemptStatus, MemoryCheckpointer, SqliteCheckpointer, StepStatus

TIMEOUT = 0.01


async def _closed_attempts(checkpointer, workflow_id: str, node_name: str):
    steps = [step for step in await checkpointer.get_steps(workflow_id) if step.node_name == node_name]
    assert len(steps) == 1, f"expected one logical step for {node_name}, got {len(steps)}"
    step = steps[0]
    assert step.attempt_series_id is not None
    series = await checkpointer.get_attempt_series(step.attempt_series_id)
    records = await checkpointer.get_attempt_records(step.attempt_series_id)
    assert series is not None and not series.is_open
    return step, records


def _assert_actionable_timeout_rejection(error: IncompatibleRunnerError, node_name: str) -> None:
    message = str(error)
    assert error.node_name == node_name
    assert "did not run" in message
    assert "make the node async" in message
    assert "cancellation-aware I/O" in message
    assert "client library's own timeout" in message


async def test_attempt_timeout_waits_for_cleanup_and_records_truth(tmp_path) -> None:
    cleanup_completed = asyncio.Event()
    never_finishes = asyncio.Event()
    checkpointer = SqliteCheckpointer(str(tmp_path / "timeout.db"))

    @node(output_name="response", timeout=TIMEOUT)
    async def call_model(prompt: str) -> str:
        try:
            await never_finishes.wait()
        finally:
            cleanup_completed.set()

    try:
        with pytest.raises(AttemptTimeoutError):
            await AsyncRunner(checkpointer=checkpointer).run(
                Graph([call_model]),
                {"prompt": "hello"},
                workflow_id="wf-timeout",
            )

        assert cleanup_completed.is_set(), "timeout must not escape before cancellation cleanup settles"
        step, records = await _closed_attempts(checkpointer, "wf-timeout", "call_model")
        assert step.status is StepStatus.FAILED
        assert len(records) == 1
        assert records[0].status is AttemptStatus.TIMED_OUT
        assert records[0].deadline_elapsed is True
        assert records[0].cancellation_requested is True
        assert not hasattr(records[0], "work_stopped")
    finally:
        await checkpointer.close()


async def test_timeout_retry_starts_only_after_prior_attempt_settles() -> None:
    checkpointer = MemoryCheckpointer()
    cleanup_started = asyncio.Event()
    allow_cleanup_to_finish = asyncio.Event()
    order: list[str] = []
    calls = 0

    @node(
        output_name="response",
        timeout=TIMEOUT,
        retry=RetryPolicy(
            max_attempts=2,
            retry_on=(AttemptTimeoutError,),
            initial_delay=0.001,
            jitter="none",
        ),
    )
    async def call_model(prompt: str) -> str:
        nonlocal calls
        calls += 1
        order.append(f"attempt-{calls}-started")
        if calls == 2:
            return "recovered"
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            order.append("cleanup-started")
            cleanup_started.set()
            await allow_cleanup_to_finish.wait()
            order.append("cleanup-finished")
            raise

    task = asyncio.create_task(
        AsyncRunner(checkpointer=checkpointer).run(
            Graph([call_model]),
            {"prompt": "hello"},
            workflow_id="wf-retry-timeout",
        )
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=5)
    assert order == ["attempt-1-started", "cleanup-started"]
    allow_cleanup_to_finish.set()
    result = await asyncio.wait_for(task, timeout=5)

    assert result["response"] == "recovered"
    assert order == [
        "attempt-1-started",
        "cleanup-started",
        "cleanup-finished",
        "attempt-2-started",
    ]
    step, records = await _closed_attempts(checkpointer, "wf-retry-timeout", "call_model")
    assert step.status is StepStatus.COMPLETED
    assert [record.status for record in records] == [AttemptStatus.TIMED_OUT, AttemptStatus.SUCCEEDED]
    assert records[0].deadline_elapsed is True
    assert records[0].cancellation_requested is True
    assert records[1].deadline_elapsed is False
    assert records[1].cancellation_requested is False


def test_sync_runner_rejects_timeout_before_execution() -> None:
    calls: list[str] = []

    @node(output_name="response", timeout=1)
    def charge_card(prompt: str) -> str:
        calls.append(prompt)
        return "charged"

    with pytest.raises(IncompatibleRunnerError) as exc_info:
        SyncRunner().run(Graph([charge_card]), {"prompt": "hello"})

    _assert_actionable_timeout_rejection(exc_info.value, "charge_card")
    assert calls == []


def test_sync_runner_rejects_async_timeout_before_execution() -> None:
    calls: list[str] = []

    @node(output_name="response", timeout=1)
    async def call_model(prompt: str) -> str:
        calls.append(prompt)
        return "ok"

    with pytest.raises(IncompatibleRunnerError) as exc_info:
        SyncRunner().run(Graph([call_model]), {"prompt": "hello"})

    _assert_actionable_timeout_rejection(exc_info.value, "call_model")
    assert calls == []


async def test_async_runner_rejects_sync_callable_timeout_before_execution() -> None:
    calls: list[str] = []

    @node(output_name="response", timeout=1)
    def charge_card(prompt: str) -> str:
        calls.append(prompt)
        return "charged"

    with pytest.raises(IncompatibleRunnerError) as exc_info:
        await AsyncRunner().run(Graph([charge_card]), {"prompt": "hello"})

    _assert_actionable_timeout_rejection(exc_info.value, "charge_card")
    assert calls == []


async def test_async_runner_rejects_sync_generator_timeout_before_execution() -> None:
    calls: list[str] = []

    @node(output_name="chunks", timeout=1)
    def stream_chunks(prompt: str):
        calls.append(prompt)
        yield "chunk"

    with pytest.raises(IncompatibleRunnerError) as exc_info:
        await AsyncRunner().run(Graph([stream_chunks]), {"prompt": "hello"})

    _assert_actionable_timeout_rejection(exc_info.value, "stream_chunks")
    assert calls == []


async def test_delegated_runner_rejects_timeout_before_execution() -> None:
    calls: list[str] = []

    @node(output_name="response", timeout=1)
    async def call_model(prompt: str) -> str:
        calls.append(prompt)
        return "ok"

    delegated = Graph([call_model], name="delegated").as_node().with_runner(SyncRunner())
    with pytest.raises(IncompatibleRunnerError) as exc_info:
        await AsyncRunner().run(Graph([delegated]), {"prompt": "hello"})

    _assert_actionable_timeout_rejection(exc_info.value, "call_model")
    assert calls == []


def test_daft_runner_rejects_timeout_before_execution() -> None:
    calls: list[str] = []

    @node(output_name="response", timeout=1)
    async def call_model(prompt: str) -> str:
        calls.append(prompt)
        return "ok"

    # Compatibility validation runs before Daft plan construction. Bypassing
    # optional-dependency initialization keeps this contract test available in
    # the core test environment while still exercising DaftRunner.run().
    runner = DaftRunner.__new__(DaftRunner)
    runner._cache = None
    with pytest.raises(IncompatibleRunnerError) as exc_info:
        runner.run(Graph([call_model]), {"prompt": "hello"})

    _assert_actionable_timeout_rejection(exc_info.value, "call_model")
    assert calls == []


async def test_suppressed_cancellation_accepts_late_success_with_evidence() -> None:
    checkpointer = MemoryCheckpointer()
    cleanup_completed = asyncio.Event()

    @node(output_name="response", timeout=TIMEOUT)
    async def call_model(prompt: str) -> str:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_completed.set()
            return f"late:{prompt}"

    result = await AsyncRunner(checkpointer=checkpointer).run(
        Graph([call_model]),
        {"prompt": "hello"},
        workflow_id="wf-late-success",
    )

    assert result["response"] == "late:hello", "a witnessed value must never be discarded"
    assert cleanup_completed.is_set()
    step, records = await _closed_attempts(checkpointer, "wf-late-success", "call_model")
    assert step.status is StepStatus.COMPLETED
    assert [record.status for record in records] == [AttemptStatus.SUCCEEDED]
    assert records[0].deadline_elapsed is True
    assert records[0].cancellation_requested is True


async def test_cleanup_exception_is_preserved_exactly() -> None:
    checkpointer = MemoryCheckpointer()
    cleanup_error = ValueError("cleanup failed")
    calls = 0

    @node(
        output_name="response",
        timeout=TIMEOUT,
        retry=RetryPolicy(
            max_attempts=2,
            retry_on=(AttemptTimeoutError,),
            initial_delay=0.001,
            jitter="none",
        ),
    )
    async def call_model(prompt: str) -> str:
        nonlocal calls
        calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise cleanup_error from None

    with pytest.raises(ValueError) as exc_info:
        await AsyncRunner(checkpointer=checkpointer).run(
            Graph([call_model]),
            {"prompt": "hello"},
            workflow_id="wf-cleanup-error",
        )

    assert exc_info.value is cleanup_error
    assert calls == 1, "AttemptTimeoutError eligibility must not authorize a cleanup ValueError"
    step, records = await _closed_attempts(checkpointer, "wf-cleanup-error", "call_model")
    assert step.status is StepStatus.FAILED
    assert [record.status for record in records] == [AttemptStatus.FAILED]
    assert records[0].error is not None and records[0].error.type_name == "ValueError"
    assert records[0].deadline_elapsed is True
    assert records[0].cancellation_requested is True


async def test_cleanup_exception_retry_eligibility_uses_its_own_type() -> None:
    calls = 0

    @node(
        output_name="response",
        timeout=TIMEOUT,
        retry=RetryPolicy(
            max_attempts=2,
            retry_on=(ValueError,),
            initial_delay=0.001,
            jitter="none",
        ),
    )
    async def call_model(prompt: str) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            return "recovered"
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise ValueError("cleanup failed") from None

    result = await AsyncRunner().run(Graph([call_model]), {"prompt": "hello"})

    assert result["response"] == "recovered"
    assert calls == 2


async def test_retry_window_expiry_during_active_work_has_distinct_error() -> None:
    checkpointer = MemoryCheckpointer()
    cleanup_completed = asyncio.Event()

    @node(
        output_name="response",
        retry=RetryPolicy(
            max_attempts=1,
            retry_on=(RetryWindowExpiredError,),
            retry_window=TIMEOUT,
        ),
    )
    async def call_model(prompt: str) -> str:
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_completed.set()

    with pytest.raises(RetryWindowExpiredError):
        await AsyncRunner(checkpointer=checkpointer).run(
            Graph([call_model]),
            {"prompt": "hello"},
            workflow_id="wf-window-active",
        )

    assert cleanup_completed.is_set()
    step, records = await _closed_attempts(checkpointer, "wf-window-active", "call_model")
    assert step.status is StepStatus.FAILED
    assert [record.status for record in records] == [AttemptStatus.TIMED_OUT]
    assert records[0].deadline_elapsed is True
    assert records[0].cancellation_requested is True


async def test_async_generator_timeout_waits_for_cleanup() -> None:
    cleanup_completed = asyncio.Event()

    @node(output_name="chunks", timeout=TIMEOUT)
    async def stream_chunks(prompt: str):
        try:
            yield prompt
            await asyncio.Event().wait()
        finally:
            cleanup_completed.set()

    with pytest.raises(AttemptTimeoutError):
        await AsyncRunner().run(Graph([stream_chunks]), {"prompt": "hello"})

    assert cleanup_completed.is_set()


async def test_external_cancellation_is_not_converted_to_timeout() -> None:
    checkpointer = MemoryCheckpointer()
    started = asyncio.Event()
    cleanup_completed = asyncio.Event()

    @node(output_name="response", timeout=60)
    async def call_model(prompt: str) -> str:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_completed.set()

    task = asyncio.create_task(
        AsyncRunner(checkpointer=checkpointer).run(
            Graph([call_model]),
            {"prompt": "hello"},
            workflow_id="wf-external-cancel",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cleanup_completed.is_set()
    series = await checkpointer.get_open_attempt_series("wf-external-cancel", "call_model")
    assert series is not None
    records = await checkpointer.get_attempt_records(series.id)
    assert [record.status for record in records] == [AttemptStatus.STARTED]
    assert records[0].deadline_elapsed is False
    assert records[0].cancellation_requested is False


async def test_timeout_declaration_does_not_change_direct_calls() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    @node(output_name="response", timeout=TIMEOUT)
    async def call_model(prompt: str) -> str:
        started.set()
        await release.wait()
        return prompt

    task = asyncio.create_task(call_model("raw"))
    await asyncio.wait_for(started.wait(), timeout=5)
    assert not task.done(), "direct FunctionNode calls stay raw; runner policy is not applied"
    release.set()
    assert await task == "raw"


@pytest.mark.parametrize("bad_timeout", [0, -1, math.inf, -math.inf, math.nan, True])
def test_timeout_must_be_a_positive_finite_number(bad_timeout: float) -> None:
    with pytest.raises((TypeError, ValueError), match="timeout"):

        @node(timeout=bad_timeout)
        async def call_model() -> None:
            pass


def test_timeout_is_exposed_on_function_node() -> None:
    @node(timeout=1.5)
    async def call_model() -> None:
        pass

    assert call_model.timeout == 1.5
