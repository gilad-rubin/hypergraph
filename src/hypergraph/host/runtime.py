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
from hypergraph.host.host import GraphBuilder, Host, _normalize_event_processors

if TYPE_CHECKING:
    from collections.abc import Sequence

    from hypergraph.events.processor import EventProcessor
    from hypergraph.graph import Graph


class HostRuntime:
    """A lazy process-level Run Home, incremental Host, and worker.

    Nothing is opened at construction. Accessing :attr:`client` or serving a
    first graph opens the Home. Unbound graphs are served with an
    :class:`~hypergraph.AsyncRunner`; an explicit graph runner is preserved.

    Args:
        path: Filesystem path for the Run Home, or ``":memory:"``.
        deployment_version: Version pinned into Definitions and submissions.
        event_processors: Processors this process adds to **every** durable
            Run its worker executes. The seam an embedding application uses
            to observe durable execution: the runtime constructs the runner
            for an unbound graph, so an application could otherwise reach no
            runner at all — and a graph that DOES carry its own runner is
            covered too, because the processors are added by the Host at
            execution rather than baked into one runner. Same contract as
            :func:`~hypergraph.serve`: shared across concurrent Runs,
            best-effort dispatch, and omitting it changes nothing.
        worker_id: The stable name this runtime's worker claims and
            registers under. Default: a fresh per-process name, which is
            right for a throwaway process and wrong for a supervised one —
            a restart under a NEW name cannot reclaim its own outstanding
            claims at once and waits out their lease instead. A deployment
            that is restarted by a supervisor should name itself after the
            DEPLOYMENT rather than the process.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        deployment_version: str = "",
        event_processors: Sequence[EventProcessor] | None = None,
        worker_id: str = "",
    ) -> None:
        self._path = Path(path)
        self._deployment_version = deployment_version
        self._event_processors = _normalize_event_processors(event_processors, caller="HostRuntime()")
        self._home: RunHome | None = None
        self._host: Host | None = None
        self._worker: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closing = False
        self._close_pending = False
        self._worker_close: asyncio.Task[BaseException | None] | None = None
        self._home_close: asyncio.Task[None] | None = None
        runtime_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        if worker_id is not None and not isinstance(worker_id, str):
            raise TypeError(f"HostRuntime(worker_id=) must be a string naming this worker, got {type(worker_id).__name__}.")
        self._worker_id = worker_id or f"host-runtime-{runtime_id}"
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

    async def serving_builder(self, key: str, builder: GraphBuilder) -> Host:
        """Register a CONSTRUCTOR and ensure the worker is ready.

        The builder mirror of :meth:`serving`: it registers the code by
        address rather than by instance, so this process can drain work
        another one configured. Nothing is built here — a builder is called
        when a submission that names it is claimed.

        The call returns only after this worker has published ``key`` to the
        Run Home. Another process may therefore submit that address as soon
        as this method returns without racing worker startup.

        Repeated calls for the same key replace the registration, which is
        safe because the built Definition is always verified against the
        submission's pinned identity before it executes.

        Returns:
            The runtime's stable Host, for ``submit`` and ``submit_batch``.
        """
        async with self._lock:
            self._require_not_closing()
            self._raise_worker_failure()
            host = self._ensure_host()
            host.serve_builder(key, builder)
            self._ensure_worker(host)
            await self._wait_until_builder_is_served(host, key)
            return host

    async def registering(self, graph: Graph) -> Host:
        """Register ``graph`` for SUBMISSION and reading, and start no worker.

        The client mirror of :meth:`serving`. Submission is graph-first — the
        pinned identity is the Graph object's own name plus structural hash —
        so a process that wants to submit work somebody ELSE executes still
        has to hold the Definition. Before leases that cost it nothing,
        because one Home admitted one worker and a second process was refused
        by name. Now it would cost a rival worker: :meth:`serving` would make
        this process an executor of everything it submits, which is rarely
        what a notebook beside a running deployment means.

        So the two halves of ``serving()`` are separable, and which one a
        process wants is a real choice: register here to submit and watch,
        call :meth:`serving` to also execute. Calling ``serving()`` later
        arms the worker over the same registrations — that is how a process
        decides to run the work itself after finding out nobody else will.

        Returns:
            The runtime's stable Host, for ``submit`` and ``submit_batch``.
        """
        from hypergraph.graph import Graph
        from hypergraph.runners.async_ import AsyncRunner

        if not isinstance(graph, Graph):
            raise TypeError(f"HostRuntime.registering() expects a Graph, got {type(graph).__name__}.")
        async with self._lock:
            self._require_not_closing()
            self._raise_worker_failure()
            registered = graph if graph.bound_runner is not None else graph.with_runner(AsyncRunner())
            host = self._ensure_host()
            host.add_definition(registered)
            return host

    async def registering_builder(self, key: str, builder: GraphBuilder) -> Host:
        """Register a CONSTRUCTOR for submission, and start no worker.

        The builder mirror of :meth:`registering`, and what a process uses to
        say "I can rebuild this" without also saying "I will run it".
        """
        async with self._lock:
            self._require_not_closing()
            self._raise_worker_failure()
            host = self._ensure_host()
            host.serve_builder(key, builder)
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
                    # A worker that ended CANCELLED is a clean close outcome:
                    # submitted work is durable, and close() exists to wind the
                    # worker down — a cancellation racing the cooperative
                    # shutdown (an event-loop teardown, a task-group exit) must
                    # not turn an orderly close into a failure. Cancellation
                    # BETWEEN operations stays loud via _raise_worker_failure.
                    if isinstance(error, asyncio.CancelledError):
                        error = None

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
            self._host = Host(
                home=home,
                definitions={},
                deployment_version=self._deployment_version,
                bus=bus,
                event_processors=self._event_processors,
            )
        return self._host

    def _open_home(self) -> RunHome:
        if self._home is None:
            if str(self._path) != ":memory:":
                self._path.parent.mkdir(parents=True, exist_ok=True)
            self._home = RunHome.open(self._path)
        return self._home

    def _ensure_worker(self, host: Host) -> None:
        if self._worker is None:
            host._worker_starting()
            self._worker = asyncio.create_task(host.work_forever(self._worker_id), name=self._task_name)

    async def _wait_until_builder_is_served(self, host: Host, key: str) -> None:
        worker = self._worker
        if worker is None:
            raise RuntimeError("HostRuntime worker was not started.")
        ready = asyncio.create_task(host._wait_until_builder_is_served(key))
        try:
            done, _pending = await asyncio.wait((ready, worker), return_when=asyncio.FIRST_COMPLETED)
            if ready in done:
                await ready
                self._raise_worker_failure()
                if worker.done():
                    raise RuntimeError(f"HostRuntime worker stopped after publishing builder {key!r}.")
                return
            self._raise_worker_failure()
            raise RuntimeError(f"HostRuntime worker stopped before publishing builder {key!r}.")
        finally:
            if not ready.done():
                ready.cancel()
                await asyncio.gather(ready, return_exceptions=True)

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
