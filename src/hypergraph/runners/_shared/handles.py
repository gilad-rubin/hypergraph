"""Process-local handles for background runner execution."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from typing import Any, Generic, TypeVar

from hypergraph.exceptions import _failure_evidence_context
from hypergraph.runners._shared.results import MapResult, RunResult
from hypergraph.runners._shared.stop import StopSignal, _WorkflowReservation

_T = TypeVar("_T")


def _raise_result_failure(result: object) -> None:
    if isinstance(result, RunResult):
        run_results = (result,)
    elif isinstance(result, MapResult):
        run_results = result.results
    else:
        return

    for run_result in run_results:
        if run_result.failed:
            error = run_result.error
            assert error is not None, "FAILED status requires an error"
            with _failure_evidence_context(error, run_result.node_failures):
                raise error from None


class AsyncHandle(Generic[_T]):
    """Control and retrieve one live asynchronous background execution."""

    def __init__(
        self,
        _task: asyncio.Task[_T],
        _signal: StopSignal,
    ) -> None:
        self._task = _task
        self._signal = _signal

    @property
    def done(self) -> bool:
        """Whether the background execution has settled."""
        return self._task.done()

    def stop(self, *, info: Any = None) -> None:
        """Request cooperative stop for the live execution."""
        if not self._task.done():
            self._signal.set(info=info)

    async def result(self, *, raise_on_failure: bool = True) -> _T:
        """Wait until execution settles and return its result."""
        result = await asyncio.shield(self._task)
        if raise_on_failure:
            _raise_result_failure(result)
        return result


class SyncHandle(Generic[_T]):
    """Control and retrieve one live synchronous background execution."""

    def __init__(
        self,
        _future: Future[_T],
        _signal: StopSignal,
        _thread: threading.Thread,
    ) -> None:
        self._future = _future
        self._signal = _signal
        self._thread = _thread

    @property
    def done(self) -> bool:
        """Whether the background execution has settled."""
        return self._future.done()

    def stop(self, *, info: Any = None) -> None:
        """Request cooperative stop for the live execution."""
        if not self._future.done():
            self._signal.set(info=info)

    def result(self, *, raise_on_failure: bool = True) -> _T:
        """Block until execution settles and return its result."""
        try:
            result = self._future.result()
            if raise_on_failure:
                _raise_result_failure(result)
            return result
        finally:
            if threading.current_thread() is not self._thread:
                self._thread.join()


def _launch_sync_execution(
    operation: Callable[[], _T],
    reservation: _WorkflowReservation,
) -> SyncHandle[_T]:
    """Launch one synchronous runner operation under its parent stop signal."""
    future: Future[_T] = Future()

    def _execute() -> None:
        try:
            result = operation()
        except BaseException as error:
            reservation.release()
            future.set_exception(error)
        else:
            # Release before Future completion makes ``handle.done`` observable,
            # so immediate same-ID reuse cannot see a stale live claim.
            reservation.release()
            future.set_result(result)

    thread = threading.Thread(target=_execute, daemon=True)
    handle = SyncHandle(future, reservation.signal, thread)
    try:
        thread.start()
    except BaseException:
        reservation.release()
        raise
    return handle


def _launch_async_execution(
    loop: asyncio.AbstractEventLoop,
    operation: Callable[[], Awaitable[_T]],
    reservation: _WorkflowReservation,
    live_tasks: set[asyncio.Task[Any]],
) -> AsyncHandle[_T]:
    """Launch one asynchronous runner operation under its parent stop signal."""

    async def _execute() -> _T:
        try:
            return await operation()
        finally:
            reservation.release()

    coroutine = _execute()
    try:
        task = loop.create_task(coroutine)
    except BaseException:
        coroutine.close()
        reservation.release()
        raise

    def _observe_completion(completed: asyncio.Task[Any]) -> None:
        live_tasks.discard(completed)
        if not completed.cancelled():
            completed.exception()

    live_tasks.add(task)
    task.add_done_callback(_observe_completion)
    return AsyncHandle(task, reservation.signal)
