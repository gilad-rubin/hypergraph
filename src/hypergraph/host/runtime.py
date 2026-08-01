"""Process-level ownership for one lazy Run Home, Host, and worker."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from hypergraph.host._bus import _PreviewBus, _register_bus
from hypergraph.host.client import RunHomeClient
from hypergraph.host.home import RunHome
from hypergraph.host.host import Host

if TYPE_CHECKING:
    from hypergraph.graph import Graph


class HostRuntime:
    """A lazy process-level Run Home, incremental Host, and worker.

    Nothing is opened at construction. Accessing :attr:`client` or serving a
    first graph opens the Home. Unbound graphs are served with an
    :class:`~hypergraph.AsyncRunner`; an explicit graph runner is preserved.

    Args:
        path: Filesystem path for the Run Home, or ``":memory:"``.
        deployment_version: Version pinned into Definitions and submissions.
    """

    def __init__(self, path: str | Path, *, deployment_version: str = "") -> None:
        self._path = Path(path)
        self._deployment_version = deployment_version
        self._home: RunHome | None = None
        self._host: Host | None = None
        self._worker: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closing = False
        self._close_pending = False
        self._worker_close: asyncio.Task[BaseException | None] | None = None
        self._home_close: asyncio.Task[None] | None = None
        runtime_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self._worker_id = f"host-runtime-{runtime_id}"
        self._task_name = f"hypergraph-host-{runtime_id}"

    @property
    def client(self) -> RunHomeClient:
        """The client for this runtime's Run Home, opened on first access."""
        self._require_not_closing()
        self._raise_worker_failure()
        return self._ensure_host().client

    async def serving(self, graph: Graph) -> Host:
        """Serve ``graph`` incrementally and ensure the worker is running.

        Repeated calls for the same Definition identity are idempotent. A new
        Definition is added to the live Host, so active work and the worker
        task are left in place.

        Returns:
            The runtime's stable Host, for ``submit`` and ``submit_batch``.
        """
        from hypergraph.graph import Graph
        from hypergraph.runners.async_ import AsyncRunner

        if not isinstance(graph, Graph):
            raise TypeError(f"HostRuntime.serving() expects a Graph, got {type(graph).__name__}.")
        async with self._lock:
            self._require_not_closing()
            self._raise_worker_failure()
            served_graph = graph if graph.bound_runner is not None else graph.with_runner(AsyncRunner())
            host = self._ensure_host()
            host.add_definition(served_graph)
            self._ensure_worker(host)
            return host

    async def close(self) -> None:
        """Drain the worker and close the Home; submitted work stays durable."""
        async with self._lock:
            self._closing = True
            self._close_pending = True
            try:
                worker = self._worker
                error = None
                if worker is not None:
                    if not worker.done() and self._host is not None:
                        self._host.shutdown()
                    if self._worker_close is None:
                        self._worker_close = asyncio.create_task(self._worker_outcome(worker))
                    # The observer converts an independently cancelled worker
                    # into a returned error. Therefore CancelledError here can
                    # only belong to this close() caller; shielding leaves the
                    # drain alive for a later close() to join.
                    error = await asyncio.shield(self._worker_close)

                home = self._home
                if home is not None:
                    if self._home_close is None:
                        self._home_close = asyncio.create_task(home.close())
                    try:
                        await asyncio.shield(self._home_close)
                    except asyncio.CancelledError:
                        # The private close task is never cancelled by the
                        # runtime; shield makes this the caller's cancellation.
                        raise
                    except BaseException:
                        self._home_close = None
                        raise

                self._worker = None
                self._host = None
                self._home = None
                self._worker_close = None
                self._home_close = None
                self._close_pending = False

                if error is not None:
                    raise RuntimeError("HostRuntime worker stopped unexpectedly.") from error
            finally:
                self._closing = False

    @staticmethod
    async def _worker_outcome(worker: asyncio.Task[None]) -> BaseException | None:
        try:
            await worker
        except BaseException as error:
            return error
        return None

    def _require_not_closing(self) -> None:
        if self._closing or self._close_pending:
            raise RuntimeError("HostRuntime is closing; await close() before using it again.")

    def _ensure_host(self) -> Host:
        if self._host is None:
            home = self._open_home()
            bus = _PreviewBus()
            _register_bus(home.uri, bus)
            self._host = Host(home=home, definitions={}, deployment_version=self._deployment_version, bus=bus)
        return self._host

    def _open_home(self) -> RunHome:
        if self._home is None:
            if str(self._path) != ":memory:":
                self._path.parent.mkdir(parents=True, exist_ok=True)
            self._home = RunHome.open(self._path)
        return self._home

    def _ensure_worker(self, host: Host) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(host.work_forever(self._worker_id), name=self._task_name)

    def _worker_failure(self) -> BaseException | None:
        worker = self._worker
        if worker is None or not worker.done():
            return None
        self._worker = None
        if worker.cancelled():
            return asyncio.CancelledError()
        return worker.exception()

    def _raise_worker_failure(self) -> None:
        error = self._worker_failure()
        if error is not None:
            raise RuntimeError("HostRuntime worker stopped unexpectedly.") from error
