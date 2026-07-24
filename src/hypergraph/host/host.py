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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from hypergraph.host._bus import _BusEventProcessor, _PreviewBus, _register_bus
from hypergraph.host.batch import BatchTolerance, validate_batch_items
from hypergraph.host.client import RunHomeClient
from hypergraph.host.definition import DefinitionId
from hypergraph.host.errors import ForkCompatibilityError, HostError
from hypergraph.host.fingerprint import batch_fingerprint, start_fingerprint
from hypergraph.host.home import RunHome
from hypergraph.host.refs import BatchRef, BatchSubmitReceipt, RunRef, SubmitReceipt
from hypergraph.host.views import TERMINAL_WORKFLOW_STATUSES
from hypergraph.host.worker import _drain, _WorkerLock

if TYPE_CHECKING:
    from hypergraph.graph import Graph
    from hypergraph.runners.base import BaseRunner


@dataclass(frozen=True)
class _Definition:
    """One served Definition: graph plus its Home-bound runner clone."""

    name: str
    graph: Graph
    runner: BaseRunner
    version: str
    struct_hash: str

    @property
    def definition_id(self) -> DefinitionId:
        """The complete pinned identity of this served Definition."""
        return DefinitionId(self.name, self.version, self.struct_hash)


def _normalize_start_at(start_at: datetime | str | None) -> str | None:
    """Normalize start_at to a UTC ISO string safe for lexicographic comparison.

    Claim eligibility compares ``start_at <= now`` lexicographically, so every
    stored value shares one shape: UTC ``+00:00`` ISO. Naive inputs are read
    as UTC; offset inputs are converted. Normalizing here (before the start
    fingerprint is computed) keeps equivalent inputs deduping identically.
    """
    if start_at is None:
        return None
    if isinstance(start_at, datetime):
        parsed = start_at
    elif isinstance(start_at, str):
        text = start_at.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"start_at must be an ISO 8601 timestamp, got {start_at!r}.") from None
    else:
        raise TypeError(f"start_at must be a datetime, an ISO string, or None; got {type(start_at).__name__}.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _validate_recovery_cap(recovery_cap: int) -> None:
    if isinstance(recovery_cap, bool) or not isinstance(recovery_cap, int) or recovery_cap < 0:
        raise ValueError(f"recovery_cap must be an int >= 0 (the progressless re-adoption budget), got {recovery_cap!r}.")


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
        accepts: tuple[DefinitionId, ...] = (),
    ) -> None:
        self._home = home
        self._definitions = definitions
        self._deployment_version = deployment_version
        self._accepts = accepts
        # Served identities: each Definition's exact pinned identity plus any
        # explicitly accepted prior identities (ADR 0007).
        self._served_identities: frozenset[DefinitionId] = frozenset(definition.definition_id for definition in definitions.values()) | frozenset(
            accepts
        )
        self._bus = bus
        self._client = RunHomeClient(home, _bus=bus)
        self._stop_event: asyncio.Event | None = None
        self._worker_loop: asyncio.AbstractEventLoop | None = None
        self._shutdown_requested = False
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
        recovery_cap: int = 3,
    ) -> SubmitReceipt:
        """Accept one run into the Run Home BEFORE any execution.

        The submission row and its first durable update commit in one
        transaction before this method returns — process loss afterwards
        cannot erase durable intent. Each submission pins the complete
        ``DefinitionId`` and a start fingerprint (identity + normalized
        inputs + ``start_at``): resubmitting a fingerprint-identical
        nonterminal ``workflow_id`` returns the existing receipt with
        ``duplicate=True``; reusing a terminal one raises
        ``AlreadyTerminalError``; a fingerprint mismatch on the same id
        raises ``WorkflowIdConflictError``.

        Args:
            definition_name: A Definition named in ``serve()``.
            inputs: JSON-serializable graph inputs.
            workflow_id: Optional explicit id; one is generated when omitted.
            start_at: Optional delayed start (datetime or ISO string).
            source_ref: Optional caller provenance marker.
            recovery_cap: Recovery brake budget — how many progressless
                crash re-adoptions park this run as recovery-exhausted
                instead of resuming it (0 brakes on the first re-adoption).
                Not part of the dedup fingerprint.
        """
        _validate_recovery_cap(recovery_cap)
        definition = self._require_definition(definition_name)
        inputs_json = self._serialize_inputs(inputs)
        start_at_iso = _normalize_start_at(start_at)
        workflow_id = workflow_id or f"{definition_name}-{uuid.uuid4().hex[:12]}"
        created, _row = await self._home._submit(
            workflow_id,
            definition.name,
            definition.version,
            definition.struct_hash,
            inputs_json,
            start_at_iso,
            source_ref,
            fingerprint=start_fingerprint(definition.definition_id, inputs_json, start_at_iso),
            recovery_cap=recovery_cap,
        )
        return self._receipt(workflow_id, created)

    def submit_sync(
        self,
        definition_name: str,
        inputs: dict[str, Any],
        *,
        workflow_id: str | None = None,
        start_at: datetime | str | None = None,
        source_ref: str | None = None,
        recovery_cap: int = 3,
    ) -> SubmitReceipt:
        """Sync mirror of ``submit``."""
        _validate_recovery_cap(recovery_cap)
        definition = self._require_definition(definition_name)
        inputs_json = self._serialize_inputs(inputs)
        start_at_iso = _normalize_start_at(start_at)
        workflow_id = workflow_id or f"{definition_name}-{uuid.uuid4().hex[:12]}"
        created, _row = self._home._submit_sync(
            workflow_id,
            definition.name,
            definition.version,
            definition.struct_hash,
            inputs_json,
            start_at_iso,
            source_ref,
            fingerprint=start_fingerprint(definition.definition_id, inputs_json, start_at_iso),
            recovery_cap=recovery_cap,
        )
        return self._receipt(workflow_id, created)

    def _prepare_batch(
        self,
        definition_name: str,
        items: Mapping[str, Mapping[str, Any]],
        *,
        workflow_id: str,
        tolerance: BatchTolerance | None,
        start_at: datetime | str | None,
    ) -> tuple[_Definition, list[tuple[str, str]], str | None, str | None, str]:
        """Validate a Batch submission and compute its start fingerprint."""
        if not isinstance(workflow_id, str) or not workflow_id:
            raise ValueError(f"submit_batch() requires a non-empty workflow_id string, got {workflow_id!r}.")
        if tolerance is not None and not isinstance(tolerance, BatchTolerance):
            raise TypeError(f"submit_batch() tolerance must be a BatchTolerance or None, got {type(tolerance).__name__}.")
        definition = self._require_definition(definition_name)
        pairs = validate_batch_items(items)
        start_at_iso = _normalize_start_at(start_at)
        tolerance_json = json.dumps(tolerance.to_dict()) if tolerance is not None else None
        fingerprint = batch_fingerprint(
            definition.definition_id,
            {key: json.loads(inputs_json) for key, inputs_json in pairs},
            tolerance,
            start_at_iso,
        )
        return definition, pairs, start_at_iso, tolerance_json, fingerprint

    async def submit_batch(
        self,
        definition_name: str,
        items: Mapping[str, Mapping[str, Any]],
        *,
        workflow_id: str,
        tolerance: BatchTolerance | None = None,
        start_at: datetime | str | None = None,
        source_ref: str | None = None,
    ) -> BatchSubmitReceipt:
        """Accept an immutable Batch into the Run Home BEFORE any execution.

        ONE transaction persists the immutable manifest (Definition
        identity, item keys with pinned inputs, tolerance declaration,
        start intent), one child submission per unique item key (child
        workflow id ``<workflow_id>:<item_key>``), and the ``manifest``
        batch update at ``bseq=1`` — a partial Batch can never appear
        accepted. Children are ordinary submissions: they flow through the
        existing claim/execute/stop/recovery machinery unchanged, each
        carrying its pinned per-item inputs.

        Dedup mirrors ``submit``: a fingerprint-identical (Definition
        identity + manifest + tolerance + ``start_at``) nonterminal
        resubmission returns the existing receipt with ``duplicate=True``
        naming the same ``BatchRef``; a fingerprint mismatch on the same id
        raises ``WorkflowIdConflictError``; a fully settled Batch raises
        ``AlreadyTerminalError``. Run and Batch workflow ids share one
        namespace: reusing an id owned by a plain Run submission is a
        conflict too.

        Args:
            definition_name: A Definition named in ``serve()``.
            items: Mapping of unique, non-empty logical item keys to
                JSON-serializable per-item inputs. Duplicate keys and an
                empty manifest are ``ValueError``; the mapping order is the
                manifest order used for keyed outcomes.
            workflow_id: Required explicit Batch id (dedup identity).
            tolerance: Optional ``BatchTolerance`` pinned into the manifest
                (part of the dedup fingerprint; trip semantics land with
                the tolerance ticket).
            start_at: Optional delayed start (datetime or ISO string),
                applied to every child.
            source_ref: Optional caller provenance marker.
        """
        definition, pairs, start_at_iso, tolerance_json, fingerprint = self._prepare_batch(
            definition_name,
            items,
            workflow_id=workflow_id,
            tolerance=tolerance,
            start_at=start_at,
        )
        batch_id = f"b-{uuid.uuid4().hex[:12]}"
        created, row = await self._home._submit_batch(
            batch_id,
            workflow_id,
            definition.name,
            definition.version,
            definition.struct_hash,
            pairs,
            tolerance_json,
            start_at_iso,
            source_ref,
            fingerprint=fingerprint,
        )
        return BatchSubmitReceipt(
            batch_ref=BatchRef(home=self._home.uri, batch_id=row["batch_id"]),
            workflow_id=workflow_id,
            duplicate=not created,
        )

    def submit_batch_sync(
        self,
        definition_name: str,
        items: Mapping[str, Mapping[str, Any]],
        *,
        workflow_id: str,
        tolerance: BatchTolerance | None = None,
        start_at: datetime | str | None = None,
        source_ref: str | None = None,
    ) -> BatchSubmitReceipt:
        """Sync mirror of ``submit_batch``."""
        definition, pairs, start_at_iso, tolerance_json, fingerprint = self._prepare_batch(
            definition_name,
            items,
            workflow_id=workflow_id,
            tolerance=tolerance,
            start_at=start_at,
        )
        batch_id = f"b-{uuid.uuid4().hex[:12]}"
        created, row = self._home._submit_batch_sync(
            batch_id,
            workflow_id,
            definition.name,
            definition.version,
            definition.struct_hash,
            pairs,
            tolerance_json,
            start_at_iso,
            source_ref,
            fingerprint=fingerprint,
        )
        return BatchSubmitReceipt(
            batch_ref=BatchRef(home=self._home.uri, batch_id=row["batch_id"]),
            workflow_id=workflow_id,
            duplicate=not created,
        )

    async def fork(self, ref: RunRef, *, into: str, reason: str) -> SubmitReceipt:
        """Migrate an existing run to a served Definition via explicit fork.

        The new submission pins the TARGET Definition identity, keeps the
        source inputs, and records ``forked_from`` lineage plus the human
        ``reason``. Compatibility is checked at fork time: the target's
        ``structural_hash`` must equal the source submission's pinned hash
        (history seeding requires restorable checkpoints), else
        ``ForkCompatibilityError``. Fork needs loaded Definition code, so it
        lives on the Host — unlike ``client.rerun()``.

        Args:
            ref: The source run's inert address.
            into: Name of a Definition served by this host.
            reason: Non-empty migration reason, stored on the submission.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"fork() requires a non-empty reason string naming why the migration happens, got {reason!r}.")
        target = self._require_definition(into)
        submission = await self._home._get_submission(ref.run_id)
        if submission is None:
            raise HostError(f"Cannot fork {ref.run_id!r}: no such run in this Run Home.")
        source_id = DefinitionId(submission["definition_name"], submission["def_version"], submission["def_struct_hash"])
        if target.struct_hash != source_id.structural_hash:
            raise ForkCompatibilityError(source_id, target.definition_id)
        workflow_id = f"{ref.run_id}-fork-{uuid.uuid4().hex[:6]}"
        inputs_json = submission["inputs_json"]
        created, _row = await self._home._submit(
            workflow_id,
            target.name,
            target.version,
            target.struct_hash,
            inputs_json,
            None,
            None,
            fingerprint=start_fingerprint(target.definition_id, inputs_json, None),
            forked_from=ref.run_id,
            fork_reason=reason,
        )
        return self._receipt(workflow_id, created)

    def fork_sync(self, ref: RunRef, *, into: str, reason: str) -> SubmitReceipt:
        """Sync mirror of ``fork``."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"fork() requires a non-empty reason string naming why the migration happens, got {reason!r}.")
        target = self._require_definition(into)
        submission = self._home._get_submission_sync(ref.run_id)
        if submission is None:
            raise HostError(f"Cannot fork {ref.run_id!r}: no such run in this Run Home.")
        source_id = DefinitionId(submission["definition_name"], submission["def_version"], submission["def_struct_hash"])
        if target.struct_hash != source_id.structural_hash:
            raise ForkCompatibilityError(source_id, target.definition_id)
        workflow_id = f"{ref.run_id}-fork-{uuid.uuid4().hex[:6]}"
        inputs_json = submission["inputs_json"]
        created, _row = self._home._submit_sync(
            workflow_id,
            target.name,
            target.version,
            target.struct_hash,
            inputs_json,
            None,
            None,
            fingerprint=start_fingerprint(target.definition_id, inputs_json, None),
            forked_from=ref.run_id,
            fork_reason=reason,
        )
        return self._receipt(workflow_id, created)

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

    def _receipt(self, workflow_id: str, created: bool) -> SubmitReceipt:
        return SubmitReceipt(
            run_ref=RunRef(home=self._home.uri, run_id=workflow_id),
            workflow_id=workflow_id,
            duplicate=not created,
        )

    def shutdown(self) -> None:
        """Signal the worker loop to stop claiming and drain (bounded).

        Safe to call before the worker's first loop iteration: when no
        worker loop is live, the request is remembered and consumed by the
        next ``work_forever`` startup, so a shutdown racing worker startup
        is never lost (and never leaks into a later worker run).
        """
        stop_event = self._stop_event
        loop = self._worker_loop
        if stop_event is None or loop is None:
            self._shutdown_requested = True
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
        if self._shutdown_requested:
            # A shutdown that raced worker startup is honored here, then
            # consumed: later work_forever() runs on this Host start fresh.
            stop_event.set()
            self._shutdown_requested = False
        tasks: dict[str, asyncio.Task] = {}
        try:
            await self._home._restart_scan()
            try:
                while not stop_event.is_set():
                    claimed = await self._home._claim_eligible(
                        datetime.now(timezone.utc).isoformat(),
                        served=self._served_identities,
                    )
                    for row in claimed:
                        task = asyncio.create_task(self._execute_submission(row))
                        task.add_done_callback(self._record_task_exception)
                        tasks[row["workflow_id"]] = task
                    tasks = {workflow_id: task for workflow_id, task in tasks.items() if not task.done()}
                    await self._process_stop_commands(set(tasks))
                    await asyncio.sleep(0 if claimed else poll_interval)
            finally:
                await _drain(set(tasks.values()), drain_timeout)
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

    async def _process_stop_commands(self, executing: set[str]) -> None:
        """Observe unapplied durable stop commands once per loop iteration.

        A command is marked applied only when the observation is final:
        terminal runs (or finished submissions) are applied with no effect,
        and executing runs receive ``runner.stop(workflow_id, info=...)``
        once the run is actually registered with the runner. Everything
        else stays unapplied for a later cycle — a stop targeting a run
        that never started is handled by the pre-run gate in
        ``_execute_submission``; a stop targeting a crashed-but-resumable
        run lands once its resumed execution registers.
        """
        commands = await self._home._unapplied_stop_commands()
        if not commands:
            return
        applied: list[int] = []
        for command_id, workflow_id, info in commands:
            submission = await self._home._get_submission(workflow_id)
            run = await self._home.get_run_async(workflow_id)
            if run is not None and run.status in TERMINAL_WORKFLOW_STATUSES:
                applied.append(command_id)
                continue
            if submission is not None and submission["state"] == "finished":
                applied.append(command_id)
                continue
            if workflow_id not in executing or submission is None:
                continue
            definition = self._definitions.get(submission["definition_name"])
            if definition is None:
                continue
            if not definition.runner.has_active_run(workflow_id):
                # Claimed and executing, but the runner has not registered
                # the workflow yet — stop() would be a no-op. Retry next
                # cycle; the pre-run gate covers the never-started case.
                continue
            definition.runner.stop(workflow_id, info=info)
            applied.append(command_id)
        await self._home._apply_stop_commands(applied)

    async def _execute_submission(self, row: dict[str, Any]) -> None:
        """Execute one claimed submission through its Definition's runner."""
        definition = self._definitions.get(row["definition_name"])
        if definition is None:
            # Not served by this worker; leave claimed for the restart scan.
            return
        workflow_id = row["workflow_id"]
        if await self._home._apply_stop_never_started(workflow_id):
            # A stop landed before first execution: the command is applied
            # and the submission finished without inventing a runs row.
            return
        inputs: dict[str, Any] | None = json.loads(row["inputs_json"])
        processors = [_BusEventProcessor(self._bus, workflow_id)]
        run_fn = definition.runner.run
        run_kwargs: dict[str, Any] = {
            "workflow_id": workflow_id,
            "event_processors": processors,
            "error_handling": "continue",
        }
        existing_run = await self._home.get_run_async(workflow_id)
        if existing_run is not None and await self._home._reset_unstarted_run(workflow_id):
            # Killed after claim but before the first committed step: the
            # empty runs row carried no history (and blocked both resume
            # and fresh input delivery), so it was deleted. Start fresh
            # from the pinned inputs; lineage, if any, is re-applied from
            # the submission below.
            existing_run = None
        if existing_run is not None:
            # Crash resume: the runs row already exists. Checkpoint state
            # supplies the consumed inputs — passing them again raises
            # InputOverrideRequiresForkError — and lineage was recorded by
            # the first attempt, so retry/fork kwargs stay off too.
            inputs = None
        elif row["retry_of"]:
            # Rerun: explicit workflow_id + retry_from is a legal runner
            # combination (validate_lineage_request only forbids
            # fork_from+retry_from or checkpoint combos). retry_workflow
            # keeps the explicit id and derives the same retry_index the
            # client used (both are COUNT(runs.retry_of=source)+1), and the
            # runs row records retry_of/retry_index with completed-step
            # checkpoint reuse. A source that never executed has no runs
            # row — retry_workflow would raise "Unknown source", so fall
            # back to a plain run; lineage stays recorded on the submission.
            if await self._home.get_run_async(row["retry_of"]) is not None:
                run_kwargs["retry_from"] = row["retry_of"]
        elif row["forked_from"] and await self._home.get_run_async(row["forked_from"]) is not None:
            # Fork: same legal-combination reasoning; fork_workflow keeps
            # the explicit id and the runs row records forked_from lineage,
            # seeded from the source's recorded history.
            run_kwargs["fork_from"] = row["forked_from"]
        if asyncio.iscoroutinefunction(run_fn):
            await run_fn(definition.graph, inputs, **run_kwargs)
        else:
            await asyncio.to_thread(run_fn, definition.graph, inputs, **run_kwargs)
        # Mark finished only after the run settled: a cancelled or crashed
        # execution leaves the submission claimed for the restart scan.
        await self._home._finish_submission(workflow_id)


def serve(*graphs: Graph, home: RunHome, deployment_version: str = "", accepts: tuple[DefinitionId, ...] = ()) -> Host:
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
        accepts: Complete prior ``DefinitionId`` identities this deployment
            declares it can drain (ADR 0007). The worker claims a parked
            submission only when its pinned identity matches a served
            Definition exactly or one of these declarations. Every entry is
            validated structurally at serve() time: it must name a Definition
            this host serves and its ``structural_hash`` must equal the
            served Definition's hash — anything else is a ``ValueError``
            (an undrainable declaration would park submissions forever).
    """
    from hypergraph.graph import Graph
    from hypergraph.runners.base import BaseRunner

    if not isinstance(home, RunHome):
        raise TypeError(f"serve() requires home=RunHome.open(...), got {type(home).__name__}.")
    if not graphs:
        raise ValueError("serve() requires at least one graph.")
    for entry in accepts:
        if not isinstance(entry, DefinitionId):
            raise TypeError(f"serve() accepts= entries must be DefinitionId instances, got {type(entry).__name__}.")

    definitions: dict[str, _Definition] = {}
    for graph in graphs:
        if not isinstance(graph, Graph):
            raise TypeError(f"serve() expects Graph instances, got {type(graph).__name__}.")
        if not graph.name:
            raise ValueError("serve() requires every graph to have a name. Pass name=... to Graph(...) and retry.")
        if graph.name in definitions:
            raise ValueError(f"Duplicate definition name {graph.name!r} in serve().")
        runner = graph.bound_runner
        if runner is None:
            raise ValueError(f"Graph {graph.name!r} has no bound runner. Call graph.with_runner(runner) before serve().")
        if not isinstance(runner, BaseRunner):
            raise TypeError(
                f"Graph {graph.name!r} carries {type(runner).__name__}, not a BaseRunner. Use graph.with_runner(SyncRunner()/AsyncRunner())."
            )
        capabilities = runner.capabilities
        if not capabilities.supports_checkpointing and not capabilities.supports_events:
            # Statically incapable runners (e.g. DaftRunner) fail loudly at
            # construction. An unbound SyncRunner/AsyncRunner also reports
            # supports_checkpointing=False (it flips True once bound), so the
            # capability alone is not decisive — with_checkpointer() has the
            # final say on whether a binding seam exists at all.
            raise ValueError(
                f"{type(runner).__name__} cannot serve durable runs: it has no checkpointer/event support. Bind a SyncRunner or AsyncRunner instead."
            )
        try:
            bound_runner = runner.with_checkpointer(home)
        except TypeError as exc:
            raise ValueError(
                f"{type(runner).__name__} cannot serve durable runs: it has no checkpointer/event support. Bind a SyncRunner or AsyncRunner instead."
            ) from exc
        definitions[graph.name] = _Definition(
            name=graph.name,
            graph=graph,
            runner=bound_runner,
            version=deployment_version,
            struct_hash=graph.structural_hash,
        )

    for entry in accepts:
        served = definitions.get(entry.name)
        if served is None:
            raise ValueError(
                f"serve() accepts= entry {entry.to_dict()!r} names Definition {entry.name!r}, which this host does not serve. "
                "An accepts declaration must name a served Definition so its parked submissions can actually drain."
            )
        if entry.structural_hash != served.struct_hash:
            raise ValueError(
                f"serve() accepts= entry {entry.to_dict()!r} pins structural_hash {entry.structural_hash!r}, but the served "
                f"Definition {entry.name!r} has hash {served.struct_hash!r}. accepts= requires structural compatibility (ADR 0007)."
            )

    bus = _PreviewBus()
    _register_bus(home.uri, bus)
    return Host(home=home, definitions=definitions, deployment_version=deployment_version, bus=bus, accepts=tuple(accepts))
