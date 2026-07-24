"""Host and serve() — the durable host's ownership seam.

The Host owns new work (``submit``) and worker lifecycle
(``work_forever``, bounded drain, lock release). It exposes the one
``RunHomeClient`` as ``host.client`` and adds no pass-through verb copies.
Direct runner execution (Tier 0) is unchanged: the host clones each
Definition's runner onto the Home's checkpointer and never mutates the
supplied runner.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from hypergraph.host._bus import _BusEventProcessor, _PreviewBus, _register_bus
from hypergraph.host.client import RunHomeClient
from hypergraph.host.errors import AlreadyTerminalError
from hypergraph.host.home import RunHome
from hypergraph.host.refs import RunRef
from hypergraph.host.worker import _drain, _WorkerLock

if TYPE_CHECKING:
    from hypergraph.graph import Graph
    from hypergraph.runners.base import BaseRunner


@dataclass(frozen=True)
class SubmitReceipt:
    """Acknowledgement of one accepted (or deduplicated) submission.

    Attributes:
        run_ref: Inert address of the run — safe to store in a product table.
        workflow_id: The run's workflow id.
        duplicate: True when this submission matched an existing nonterminal
            workflow_id (use-existing; no new row was written).
    """

    run_ref: RunRef
    workflow_id: str
    duplicate: bool


@dataclass(frozen=True)
class _Definition:
    """One served Definition: graph plus its Home-bound runner clone."""

    name: str
    graph: Graph
    runner: BaseRunner
    version: str
    struct_hash: str


def _normalize_start_at(start_at: datetime | str | None) -> str | None:
    if start_at is None:
        return None
    if isinstance(start_at, datetime):
        return start_at.isoformat()
    if isinstance(start_at, str):
        return start_at
    raise TypeError(f"start_at must be a datetime, an ISO string, or None; got {type(start_at).__name__}.")


class Host:
    """The durable host: submissions in, worker lifecycle, one client out.

    Created by ``serve()`` — do not construct directly.
    """

    def __init__(
        self,
        *,
        home: RunHome,
        definitions: dict[str, _Definition],
        deployment_version: str,
        bus: _PreviewBus,
    ) -> None:
        self._home = home
        self._definitions = definitions
        self._deployment_version = deployment_version
        self._bus = bus
        self._client = RunHomeClient(home, _bus=bus)
        self._stop_event: asyncio.Event | None = None
        self._worker_loop: asyncio.AbstractEventLoop | None = None
        self.worker_errors: list[BaseException] = []

    @property
    def client(self) -> RunHomeClient:
        """The one RunHomeClient for this Home (no verb copies on Host)."""
        return self._client

    async def submit(
        self,
        definition_name: str,
        inputs: dict[str, Any],
        *,
        workflow_id: str | None = None,
        start_at: datetime | str | None = None,
        source_ref: str | None = None,
    ) -> SubmitReceipt:
        """Accept one run into the Run Home BEFORE any execution.

        The submission row and its first durable update commit in one
        transaction before this method returns — process loss afterwards
        cannot erase durable intent. A fingerprint-identical duplicate
        contract arrives with a later ticket; for now, resubmitting a
        nonterminal workflow_id returns the existing receipt with
        ``duplicate=True``, and reusing a terminal one raises
        ``AlreadyTerminalError``.

        Args:
            definition_name: A Definition named in ``serve()``.
            inputs: JSON-serializable graph inputs.
            workflow_id: Optional explicit id; one is generated when omitted.
            start_at: Optional delayed start (datetime or ISO string).
            source_ref: Optional caller provenance marker.
        """
        definition = self._require_definition(definition_name)
        inputs_json = self._serialize_inputs(inputs)
        workflow_id = workflow_id or f"{definition_name}-{uuid.uuid4().hex[:12]}"
        created, row = await self._home._submit(
            workflow_id,
            definition.name,
            definition.version,
            definition.struct_hash,
            inputs_json,
            _normalize_start_at(start_at),
            source_ref,
        )
        return self._receipt(workflow_id, created, row)

    def submit_sync(
        self,
        definition_name: str,
        inputs: dict[str, Any],
        *,
        workflow_id: str | None = None,
        start_at: datetime | str | None = None,
        source_ref: str | None = None,
    ) -> SubmitReceipt:
        """Sync mirror of ``submit``."""
        definition = self._require_definition(definition_name)
        inputs_json = self._serialize_inputs(inputs)
        workflow_id = workflow_id or f"{definition_name}-{uuid.uuid4().hex[:12]}"
        created, row = self._home._submit_sync(
            workflow_id,
            definition.name,
            definition.version,
            definition.struct_hash,
            inputs_json,
            _normalize_start_at(start_at),
            source_ref,
        )
        return self._receipt(workflow_id, created, row)

    def _require_definition(self, definition_name: str) -> _Definition:
        definition = self._definitions.get(definition_name)
        if definition is None:
            raise ValueError(f"Unknown definition {definition_name!r}. This host serves: {sorted(self._definitions)}.")
        return definition

    @staticmethod
    def _serialize_inputs(inputs: dict[str, Any]) -> str:
        if not isinstance(inputs, dict):
            raise TypeError(f"submit() inputs must be a dict, got {type(inputs).__name__}.")
        return json.dumps(inputs)

    def _receipt(self, workflow_id: str, created: bool, row: dict[str, Any]) -> SubmitReceipt:
        if not created and row["state"] == "finished":
            raise AlreadyTerminalError(workflow_id)
        return SubmitReceipt(
            run_ref=RunRef(home=self._home.uri, run_id=workflow_id),
            workflow_id=workflow_id,
            duplicate=not created,
        )

    def shutdown(self) -> None:
        """Signal the worker loop to stop claiming and drain (bounded)."""
        stop_event = self._stop_event
        loop = self._worker_loop
        if stop_event is None or loop is None:
            return
        loop.call_soon_threadsafe(stop_event.set)

    async def work_forever(self, worker_id: str, *, poll_interval: float = 0.05, drain_timeout: float = 30.0) -> None:
        """Run the worker loop: claim, execute, repeat, with bounded drain.

        Startup takes the OS-level exclusive worker lock — a second
        ``work_forever`` on the same Home fails loudly with
        ``WorkerLockError``. The restart scan re-adopts unfinished claimed
        submissions so work continues without resubmission. On
        ``shutdown()`` or cancellation the loop stops claiming, awaits
        active runs up to ``drain_timeout``, cancels the rest, releases the
        lock, and returns (or re-raises the cancellation) cleanly.
        """
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError("work_forever() requires a non-empty worker_id string.")
        lock = _WorkerLock.for_home(self._home)
        lock.acquire()
        stop_event = asyncio.Event()
        self._stop_event = stop_event
        self._worker_loop = asyncio.get_running_loop()
        tasks: set[asyncio.Task] = set()
        try:
            await self._home._restart_scan()
            try:
                while not stop_event.is_set():
                    claimed = await self._home._claim_eligible(datetime.now(timezone.utc).isoformat())
                    for row in claimed:
                        task = asyncio.create_task(self._execute_submission(row))
                        task.add_done_callback(self._record_task_exception)
                        tasks.add(task)
                    tasks = {task for task in tasks if not task.done()}
                    await asyncio.sleep(0 if claimed else poll_interval)
            finally:
                await _drain(tasks, drain_timeout)
        finally:
            self._stop_event = None
            self._worker_loop = None
            lock.release()

    def _record_task_exception(self, task: asyncio.Task) -> None:
        """Retrieve a finished execution task's exception for observability.

        A failed execution leaves its submission claimed; the restart scan
        re-adopts it on the next worker startup.
        """
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.worker_errors.append(error)

    async def _execute_submission(self, row: dict[str, Any]) -> None:
        """Execute one claimed submission through its Definition's runner."""
        definition = self._definitions.get(row["definition_name"])
        if definition is None:
            # Not served by this worker; leave claimed for the restart scan.
            return
        workflow_id = row["workflow_id"]
        inputs = json.loads(row["inputs_json"])
        processors = [_BusEventProcessor(self._bus, workflow_id)]
        run_fn = definition.runner.run
        run_kwargs: dict[str, Any] = {
            "workflow_id": workflow_id,
            "event_processors": processors,
            "error_handling": "continue",
        }
        if asyncio.iscoroutinefunction(run_fn):
            await run_fn(definition.graph, inputs, **run_kwargs)
        else:
            await asyncio.to_thread(run_fn, definition.graph, inputs, **run_kwargs)
        # Mark finished only after the run settled: a cancelled or crashed
        # execution leaves the submission claimed for the restart scan.
        await self._home._finish_submission(workflow_id)


def serve(*graphs: Graph, home: RunHome, deployment_version: str = "") -> Host:
    """Bind Definitions to a Run Home and return the Host.

    Each graph must have a name and a runner bound via
    ``graph.with_runner(runner)``. The runner is cloned onto the Home's
    checkpointer (``runner.with_checkpointer(home)``) — the supplied
    instance is never mutated. Runners without checkpointer/event support
    (e.g. ``DaftRunner``) fail here, at construction.

    Args:
        *graphs: Named graphs with bound runners (the served Definitions).
        home: The Run Home, opened with ``RunHome.open(uri)``.
        deployment_version: Version pinned into every submission's
            Definition identity.
    """
    from hypergraph.graph import Graph
    from hypergraph.runners.base import BaseRunner

    if not isinstance(home, RunHome):
        raise TypeError(f"serve() requires home=RunHome.open(...), got {type(home).__name__}.")
    if not graphs:
        raise ValueError("serve() requires at least one graph.")

    definitions: dict[str, _Definition] = {}
    for graph in graphs:
        if not isinstance(graph, Graph):
            raise TypeError(f"serve() expects Graph instances, got {type(graph).__name__}.")
        if not graph.name:
            raise ValueError("serve() requires every graph to have a name. Pass name=... to Graph(...) and retry.")
        if graph.name in definitions:
            raise ValueError(f"Duplicate definition name {graph.name!r} in serve().")
        runner = getattr(graph, "_bound_runner", None)
        if runner is None:
            raise ValueError(f"Graph {graph.name!r} has no bound runner. Call graph.with_runner(runner) before serve().")
        if not isinstance(runner, BaseRunner):
            raise TypeError(
                f"Graph {graph.name!r} carries {type(runner).__name__}, not a BaseRunner. Use graph.with_runner(SyncRunner()/AsyncRunner())."
            )
        if "_checkpointer_instance" not in runner.__dict__ or not hasattr(runner, "_create_dispatcher"):
            raise ValueError(
                f"{type(runner).__name__} cannot serve durable runs: it has no checkpointer/event support. Bind a SyncRunner or AsyncRunner instead."
            )
        bound_runner = runner.with_checkpointer(home)
        definitions[graph.name] = _Definition(
            name=graph.name,
            graph=graph,
            runner=bound_runner,
            version=deployment_version,
            struct_hash=graph.structural_hash,
        )

    bus = _PreviewBus()
    _register_bus(home.uri, bus)
    return Host(home=home, definitions=definitions, deployment_version=deployment_version, bus=bus)
