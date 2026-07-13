"""Process-local handles for background runner execution."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from typing import Any, Generic, TypeVar

from hypergraph.exceptions import _failure_evidence_context
from hypergraph.runners._shared.results import RunResult
from hypergraph.runners._shared.stop import StopSignal

_T = TypeVar("_T")


def _raise_run_result_failure(result: object) -> None:
    if isinstance(result, RunResult) and result.failed:
        error = result.error
        assert error is not None, "FAILED status requires an error"
        with _failure_evidence_context(error, result.node_failures):
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
            _raise_run_result_failure(result)
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
                _raise_run_result_failure(result)
            return result
        finally:
            if threading.current_thread() is not self._thread:
                self._thread.join()
