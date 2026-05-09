"""Background handle types for start_run() and start_map()."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import replace
from typing import Any

from hypergraph.runners._shared.inspect import FailureCase, LiveInspectState, MapInspectWidget, RunView
from hypergraph.runners._shared.types import MapResult, RunResult, RunStatus


def _raise_failed_run(result: RunResult, *, raise_on_failure: bool) -> RunResult:
    if raise_on_failure and result.status == RunStatus.FAILED and result.error is not None:
        raise result.error
    return result


def _raise_failed_map(result: MapResult, *, raise_on_failure: bool) -> MapResult:
    if raise_on_failure:
        for item in result.results:
            if item.status == RunStatus.FAILED and item.error is not None:
                raise item.error
    return result


class SyncRunHandle:
    """Handle for a background sync start_run() call."""

    def __init__(
        self,
        future: Future[RunResult],
        *,
        stop_callback: Any | None = None,
        live_state: LiveInspectState | None = None,
    ) -> None:
        self._future = future
        self._stop_callback = stop_callback
        self._live_state = live_state

    @property
    def done(self) -> bool:
        return self._future.done()

    @property
    def status(self) -> RunStatus | str:
        if not self._future.done():
            return "running"
        return self._future.result().status

    @property
    def failure(self) -> FailureCase | None:
        if not self._future.done():
            return None
        return self._future.result().failure

    def stop(self, *, info: Any = None) -> None:
        if self._stop_callback is None:
            raise RuntimeError("This run cannot be stopped yet without workflow-backed control.")
        self._stop_callback(info=info)

    def wait(self) -> None:
        self._future.result()

    def result(self, *, raise_on_failure: bool = True) -> RunResult:
        return _raise_failed_run(self._future.result(), raise_on_failure=raise_on_failure)

    def view(self) -> RunView:
        if not self._future.done():
            if self._live_state is None:
                raise RuntimeError("Run is still in progress. Wait for result() before accessing the terminal view.")
            return self._live_state.view()
        return self.result(raise_on_failure=False).view()

    def inspect(self) -> RunView:
        return self.view()


class AsyncRunHandle:
    """Handle for a background async start_run() call."""

    def __init__(
        self,
        task: asyncio.Task[RunResult],
        *,
        stop_callback: Any | None = None,
        live_state: LiveInspectState | None = None,
    ) -> None:
        self._task = task
        self._stop_callback = stop_callback
        self._live_state = live_state

    @property
    def done(self) -> bool:
        return self._task.done()

    @property
    def status(self) -> RunStatus | str:
        if not self._task.done():
            return "running"
        return self._task.result().status

    @property
    def failure(self) -> FailureCase | None:
        if not self._task.done():
            return None
        return self._task.result().failure

    def stop(self, *, info: Any = None) -> None:
        if self._stop_callback is None:
            raise RuntimeError("This run cannot be stopped yet without workflow-backed control.")
        self._stop_callback(info=info)

    async def wait(self) -> None:
        await self._task

    async def result(self, *, raise_on_failure: bool = True) -> RunResult:
        result = await self._task
        return _raise_failed_run(result, raise_on_failure=raise_on_failure)

    def view(self) -> RunView:
        if not self._task.done():
            if self._live_state is None:
                raise RuntimeError("Run is still in progress. Await result() or wait() before accessing the terminal view.")
            return self._live_state.view()
        return self._task.result().view()

    def inspect(self) -> RunView:
        return self.view()


class SyncMapHandle:
    """Handle for a background sync start_map() call."""

    def __init__(
        self,
        future: Future[MapResult],
        *,
        stop_callback: Any | None = None,
        inspect_widget: MapInspectWidget | None = None,
    ) -> None:
        self._future = future
        self._stop_callback = stop_callback
        self._inspect_widget = inspect_widget

    @property
    def done(self) -> bool:
        return self._future.done()

    @property
    def status(self) -> RunStatus | str:
        if not self._future.done():
            return "running"
        return self._future.result().status

    @property
    def failures(self) -> list[FailureCase]:
        if not self._future.done():
            return []
        cases: list[FailureCase] = []
        for idx, result in enumerate(self._future.result().results):
            if result.failure is not None:
                cases.append(replace(result.failure, item_index=idx))
        return cases

    def stop(self, *, info: Any = None) -> None:
        if self._stop_callback is None:
            raise RuntimeError("This map cannot be stopped yet without workflow-backed control.")
        self._stop_callback(info=info)

    def wait(self) -> None:
        self._future.result()

    def result(self, *, raise_on_failure: bool = True) -> MapResult:
        result = self._future.result()
        if self._inspect_widget is not None:
            self._inspect_widget.refresh_result(result)
        return _raise_failed_map(result, raise_on_failure=raise_on_failure)

    def view(self) -> tuple[RunView, ...]:
        result = self.result(raise_on_failure=False)
        return tuple(item.inspect() for item in result.results)

    def inspect(self) -> tuple[RunView, ...]:
        return self.view()


class AsyncMapHandle:
    """Handle for a background async start_map() call."""

    def __init__(
        self,
        task: asyncio.Task[MapResult],
        *,
        stop_callback: Any | None = None,
        inspect_widget: MapInspectWidget | None = None,
    ) -> None:
        self._task = task
        self._stop_callback = stop_callback
        self._inspect_widget = inspect_widget

    @property
    def done(self) -> bool:
        return self._task.done()

    @property
    def status(self) -> RunStatus | str:
        if not self._task.done():
            return "running"
        return self._task.result().status

    @property
    def failures(self) -> list[FailureCase]:
        if not self._task.done():
            return []
        cases: list[FailureCase] = []
        for idx, result in enumerate(self._task.result().results):
            if result.failure is not None:
                cases.append(replace(result.failure, item_index=idx))
        return cases

    def stop(self, *, info: Any = None) -> None:
        if self._stop_callback is None:
            raise RuntimeError("This map cannot be stopped yet without workflow-backed control.")
        self._stop_callback(info=info)

    async def wait(self) -> None:
        await self._task

    async def result(self, *, raise_on_failure: bool = True) -> MapResult:
        result = await self._task
        if self._inspect_widget is not None:
            self._inspect_widget.refresh_result(result)
        return _raise_failed_map(result, raise_on_failure=raise_on_failure)

    def view(self) -> tuple[RunView, ...]:
        if not self._task.done():
            raise RuntimeError("Map is still in progress. Await result() or wait() before accessing the terminal view.")
        return tuple(item.inspect() for item in self._task.result().results)

    def inspect(self) -> tuple[RunView, ...]:
        return self.view()
