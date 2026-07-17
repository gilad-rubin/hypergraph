"""Process-local handles for background runner execution."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from typing import Any, Generic, TypeVar

from hypergraph.events.processor import AsyncEventProcessor
from hypergraph.events.types import Event, StreamingChunkEvent
from hypergraph.exceptions import _failure_evidence_context
from hypergraph.runners._shared.results import MapResult, RunResult
from hypergraph.runners._shared.stop import StopSignal, _WorkflowReservation

_T = TypeVar("_T")


def _raise_result_failure(result: object) -> None:
    if isinstance(result, RunResult):
        run_results = (result,)
    elif isinstance(result, MapResult):
        run_results = result.results  # type: ignore[assignment]
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


class AsyncRunEventHandle(AsyncEventProcessor):
    """Iterate one live async run's events inside an async context manager.

    Lifecycle events are retained. Streaming chunks are preview events: when
    the bounded buffer is full, the oldest queued chunk is discarded first.
    """

    def __init__(
        self,
        operation: Callable[[], Awaitable[RunResult]],
        live_tasks: set[asyncio.Task[Any]],
        *,
        buffer_size: int,
    ) -> None:
        if buffer_size < 1:
            raise ValueError("buffer_size must be at least 1")
        self._operation = operation
        self._live_tasks = live_tasks
        self._buffer_size = buffer_size
        self._events: deque[Event] = deque()
        self._available = asyncio.Event()
        self._space_available = asyncio.Event()
        self._task: asyncio.Task[RunResult] | None = None
        self._entered = False
        self._stream_closed = False
        self._dropped_chunks = 0

    @property
    def buffer_size(self) -> int:
        """Maximum number of delivered events retained for the consumer."""
        return self._buffer_size

    @property
    def buffered_event_count(self) -> int:
        """Number of events currently retained for the consumer."""
        return len(self._events)

    @property
    def dropped_chunks(self) -> int:
        """Number of preview chunks discarded because the buffer was full."""
        return self._dropped_chunks

    @property
    def done(self) -> bool:
        """Whether the underlying run task has settled."""
        return self._task is not None and self._task.done()

    async def __aenter__(self) -> AsyncRunEventHandle:
        if self._entered:
            raise RuntimeError("An AsyncRunEventHandle can only be entered once")
        self._entered = True
        task = asyncio.get_running_loop().create_task(self._operation())
        self._task = task
        self._live_tasks.add(task)

        def _observe_completion(completed: asyncio.Task[Any]) -> None:
            self._live_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(_observe_completion)
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._stream_closed = True
        self._available.set()
        return False

    def __aiter__(self) -> AsyncRunEventHandle:
        return self

    async def __anext__(self) -> Event:
        if not self._entered:
            raise RuntimeError("Use AsyncRunner.iter() with 'async with' before iterating events")
        while not self._events:
            if self._stream_closed:
                raise StopAsyncIteration
            self._available.clear()
            await self._available.wait()
        event = self._events.popleft()
        self._space_available.set()
        return event

    def on_event(self, event: Event) -> None:
        """Receive synchronous preview chunks from ``NodeContext.stream()``."""
        if not isinstance(event, StreamingChunkEvent):
            raise RuntimeError(f"Synchronous event delivery is only supported for preview chunks, got {type(event).__name__}")
        self._enqueue_chunk(event)

    async def on_event_async(self, event: Event) -> None:
        """Receive ordered lifecycle events from the async dispatcher."""
        if isinstance(event, StreamingChunkEvent):
            self._enqueue_chunk(event)
            return
        while len(self._events) >= self._buffer_size:
            if self._evict_oldest_chunk():
                continue
            self._space_available.clear()
            await self._space_available.wait()
        self._events.append(event)
        self._available.set()

    async def shutdown_async(self) -> None:
        """Mark event production complete after runner processor shutdown."""
        self._stream_closed = True
        self._available.set()

    async def result(self, *, raise_on_failure: bool = True) -> RunResult:
        """Wait for the run and return its result, raising failures by default."""
        if self._task is None:
            raise RuntimeError("Use AsyncRunner.iter() with 'async with' before retrieving its result")
        result = await asyncio.shield(self._task)
        if raise_on_failure:
            _raise_result_failure(result)
        return result

    def _enqueue_chunk(self, event: StreamingChunkEvent) -> None:
        if self._stream_closed:
            return
        if len(self._events) >= self._buffer_size and not self._evict_oldest_chunk():
            self._dropped_chunks += 1
            return
        self._events.append(event)
        self._available.set()

    def _evict_oldest_chunk(self) -> bool:
        for index, queued_event in enumerate(self._events):
            if isinstance(queued_event, StreamingChunkEvent):
                del self._events[index]
                self._dropped_chunks += 1
                return True
        return False


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
