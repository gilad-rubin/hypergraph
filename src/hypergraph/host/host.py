"""Host and serve() — the durable host's ownership seam.

The Host owns new work (``submit``, ``submit_batch``) and worker lifecycle
(``work_forever``, bounded drain, lock release). It exposes the one
``RunHomeClient`` as ``host.client`` and adds no pass-through verb copies.
Direct runner execution (Tier 0) is unchanged: the host clones each
Definition's runner onto the Home's checkpointer and never mutates the
supplied runner.

**Submission is graph-first.** Every new-work verb takes the served
``Graph`` object and resolves its pinned Definition internally; a caller
never types a Definition-name string. There are exactly two new-work verbs
— ``submit`` (one durable Run) and ``submit_batch`` (an immutable set of
independent durable Runs) — and deliberately no ``host.map``: a Durable
Batch returns a durable receipt, not an immediate ``MapResult``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from hypergraph.host._batch_store import BatchAcceptance, DefinitionPin
from hypergraph.host._bus import _BusEventProcessor, _PreviewBus, _register_bus
from hypergraph.host.batch import BatchTolerance, MapMode, _item_key, expand_batch_items, freeze_batch_items
from hypergraph.host.client import RunHomeClient
from hypergraph.host.definition import DefinitionId
from hypergraph.host.errors import ForkCompatibilityError, HostError, UnservedGraphError
from hypergraph.host.fingerprint import batch_fingerprint, start_fingerprint
from hypergraph.host.home import RunHome, _normalize_utc_iso
from hypergraph.host.refs import BatchRef, BatchSubmitReceipt, RunRef, SubmitReceipt
from hypergraph.host.views import SUBMISSION_STATE_FINISHED, SUBMISSION_STATE_PAUSED, TERMINAL_WORKFLOW_STATUSES
from hypergraph.host.worker import _drain, _WorkerLock

if TYPE_CHECKING:
    from pydantic import BaseModel

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
    """Normalize start_at through the Home's one store-time normalizer.

    Claim eligibility compares ``start_at <= now`` lexicographically against
    the same due predicate scheduled pause answers use, so both verbs
    normalize identically (see ``home._normalize_utc_iso``). Normalizing here
    — before the start fingerprint is computed — keeps equivalent inputs
    deduping identically.
    """
    return _normalize_utc_iso(start_at, field="start_at")


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
        graph: Graph,
        values: dict[str, Any],
        *,
        workflow_id: str | None = None,
        start_at: datetime | str | None = None,
        source_ref: str | None = None,
        recovery_cap: int = 3,
    ) -> SubmitReceipt:
        """Accept ONE durable Run into the Run Home BEFORE any execution.

        The call reads like ``runner.run(graph, values)`` and returns a
        durable receipt instead of a result::

            receipt = await host.submit(ingestion_graph, {"work_item_id": "work-a81f43c129"})
            receipt.run_ref     # inert, serializable — safe to store

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
            graph: A Graph this host serves. Its Definition is resolved from
                the object's own pinned identity — name plus
                ``structural_hash`` — so no call site types a Definition
                name. An unserved Graph raises ``UnservedGraphError``
                immediately: a submission must never name code no worker can
                execute.
            values: JSON-serializable graph inputs.
            workflow_id: Optional explicit id; one is generated when omitted.
            start_at: Optional delayed start (datetime or ISO string).
            source_ref: Optional caller provenance marker.
            recovery_cap: Recovery brake budget — how many progressless
                crash re-adoptions park this run as recovery-exhausted
                instead of resuming it (0 brakes on the first re-adoption).
                Not part of the dedup fingerprint.
        """
        definition, inputs_json, start_at_iso, workflow_id = self._prepare_run(
            graph, values, workflow_id=workflow_id, start_at=start_at, recovery_cap=recovery_cap
        )
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
        graph: Graph,
        values: dict[str, Any],
        *,
        workflow_id: str | None = None,
        start_at: datetime | str | None = None,
        source_ref: str | None = None,
        recovery_cap: int = 3,
    ) -> SubmitReceipt:
        """Sync mirror of ``submit``."""
        definition, inputs_json, start_at_iso, workflow_id = self._prepare_run(
            graph, values, workflow_id=workflow_id, start_at=start_at, recovery_cap=recovery_cap
        )
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

    def _prepare_run(
        self,
        graph: Graph,
        values: dict[str, Any],
        *,
        workflow_id: str | None,
        start_at: datetime | str | None,
        recovery_cap: int,
    ) -> tuple[_Definition, str, str | None, str]:
        """Validate one Run submission and normalize its stored fields."""
        _validate_recovery_cap(recovery_cap)
        definition = self._require_definition(graph)
        inputs_json = self._serialize_inputs(values)
        start_at_iso = _normalize_start_at(start_at)
        return definition, inputs_json, start_at_iso, workflow_id or f"{definition.name}-{uuid.uuid4().hex[:12]}"

    def _prepare_batch(
        self,
        graph: Graph,
        values: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        map_over: str | Sequence[str] | None,
        map_mode: MapMode,
        identity: str,
        schema: type[BaseModel] | None,
        exclusive_by: str | None,
        admission_units: str | None,
        workflow_id: str,
        tolerance: BatchTolerance | None,
        start_at: datetime | str | None,
        recovery_cap: int,
    ) -> tuple[_Definition, list[tuple[str, str]], str | None, str | None, str]:
        """Expand and freeze a Batch submission, then fingerprint it.

        Expansion runs HERE — before the acceptance transaction — so the
        transaction only ever writes the frozen manifest (PRD 0019: the
        accepted manifest is all-or-nothing, and mutating the caller's
        collection afterwards cannot change durable intent).
        """
        if not isinstance(workflow_id, str) or not workflow_id:
            raise ValueError(f"submit_batch() requires a non-empty workflow_id string, got {workflow_id!r}.")
        if tolerance is not None and not isinstance(tolerance, BatchTolerance):
            raise TypeError(f"submit_batch() tolerance must be a BatchTolerance or None, got {type(tolerance).__name__}.")
        _validate_recovery_cap(recovery_cap)
        definition = self._require_definition(graph)
        if exclusive_by is not None and (not isinstance(exclusive_by, str) or not exclusive_by):
            raise ValueError(f"submit_batch() exclusive_by must name a graph port, got {exclusive_by!r}.")
        if admission_units is not None and (not isinstance(admission_units, str) or not admission_units):
            raise ValueError(f"submit_batch() admission_units must name an item field, got {admission_units!r}.")
        if isinstance(values, Mapping):
            if map_over is None:
                raise TypeError("submit_batch() mapping-form values require map_over; typed item sequences do not.")
            pairs = expand_batch_items(
                values,
                map_over=map_over,
                map_mode=map_mode,
                identity=identity,
                graph=graph,
                schema=schema,
            )
        else:
            if map_over is not None:
                raise TypeError(
                    f"submit_batch() runner-shaped values must be a Mapping when map_over is present; got "
                    f"{type(values).__name__} values ({values!r}).\n\nHow to fix:\n  Remove map_over and pass a sequence "
                    "of per-item mappings, or transpose the values into a mapping of input collections."
                )
            pairs = freeze_batch_items(values, identity=identity, graph=graph, schema=schema)
        if exclusive_by is not None:
            for index, (_, inputs_json) in enumerate(pairs):
                inputs = json.loads(inputs_json)
                if exclusive_by not in inputs:
                    raise ValueError(
                        f"submit_batch() item {index} has no initial value for exclusive_by={exclusive_by!r}. "
                        "The durable lock must have a key before the Run is admitted."
                    )
                _item_key(inputs[exclusive_by], identity=exclusive_by, index=index)
        if admission_units is not None:
            for index, (_, inputs_json) in enumerate(pairs):
                inputs = json.loads(inputs_json)
                value = inputs.get(admission_units)
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError(f"submit_batch() item {index} admission_units={admission_units!r} must be an int >= 1; got {value!r}.")
        start_at_iso = _normalize_start_at(start_at)
        tolerance_json = json.dumps(tolerance.to_dict()) if tolerance is not None else None
        fingerprint = batch_fingerprint(
            definition.definition_id,
            {key: json.loads(inputs_json) for key, inputs_json in pairs},
            tolerance,
            start_at_iso,
            exclusive_by,
            admission_units,
        )
        return definition, pairs, start_at_iso, tolerance_json, fingerprint

    async def submit_batch(
        self,
        graph: Graph,
        values: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        identity: str,
        map_over: str | Sequence[str] | None = None,
        map_mode: MapMode = "zip",
        schema: type[BaseModel] | None = None,
        exclusive_by: str | None = None,
        admission_units: str | None = None,
        workflow_id: str,
        tolerance: BatchTolerance | None = None,
        start_at: datetime | str | None = None,
        source_ref: str | None = None,
        recovery_cap: int = 3,
    ) -> BatchSubmitReceipt:
        """Accept an immutable Batch of independent durable Runs.

        The call reads like ``runner.map(graph, values, map_over=...)`` and
        returns a durable receipt instead of a ``MapResult``::

            receipt = await host.submit_batch(
                ingestion_graph,
                {"work_item_id": work_item_ids},
                map_over="work_item_id",
                identity="work_item_id",
                workflow_id=batch_id,
            )

        Only the **input-expansion** vocabulary is borrowed from
        ``runner.map``. Map execution controls are deliberately absent
        because their durable equivalents already exist: active-Run
        admission owns concurrency (``home.max_active_runs``), Batch
        ``tolerance`` owns failure policy, and ``watch`` owns incremental
        observation.

        ONE transaction persists the immutable manifest (Definition
        identity, item keys with pinned inputs, tolerance declaration,
        start intent), one child submission per item key (child workflow id
        ``<workflow_id>:<item_key>``), and the ``manifest`` batch update at
        ``bseq=1`` — a partial Batch can never appear accepted. Children are
        ordinary submissions and independent Runs, never one parent graph
        Run: they flow through the existing claim/execute/pause/stop/
        recovery machinery unchanged, each carrying its pinned per-item
        inputs.

        Dedup mirrors ``submit``: a fingerprint-identical (Definition
        identity + manifest + tolerance + ``start_at``) nonterminal
        resubmission returns the existing receipt with ``duplicate=True``
        naming the same ``BatchRef``; a fingerprint mismatch on the same id
        raises ``WorkflowIdConflictError``; a fully settled Batch raises
        ``AlreadyTerminalError``. Run and Batch workflow ids share one
        namespace: reusing an id owned by a plain Run submission is a
        conflict too.

        Args:
            graph: A Graph this host serves (see ``submit``). An unserved
                Graph raises ``UnservedGraphError``.
            values: Runner-shaped inputs. Inputs named in ``map_over`` hold
                per-item collections; every other input is broadcast to
                every child verbatim.
            map_over: Input name (or names) to expand, exactly as
                ``runner.map`` reads it.
            map_mode: ``"zip"`` (parallel iteration, the default) or
                ``"product"`` (cartesian), exactly as ``runner.map`` reads
                it.
            identity: Required. Names one expanded input whose per-item
                JSON-safe scalar value becomes the logical item key. Missing,
                empty, non-scalar, and duplicate keys are refused before
                acceptance (``ItemKeyError``) — a generated map index is
                never durable identity.
            workflow_id: Required explicit Batch id (dedup identity).
            tolerance: Optional ``BatchTolerance`` pinned into the manifest
                (part of the dedup fingerprint). Once failure-equivalent
                children strictly exceed either threshold the Batch trips:
                new child admission closes, claimed children settle, and
                every remaining item becomes explicitly unstarted.
            start_at: Optional delayed start (datetime or ISO string),
                applied to every child.
            source_ref: Optional caller provenance marker.
            recovery_cap: Recovery brake budget applied to every child.
        """
        definition, pairs, start_at_iso, tolerance_json, fingerprint = self._prepare_batch(
            graph,
            values,
            map_over=map_over,
            map_mode=map_mode,
            identity=identity,
            schema=schema,
            exclusive_by=exclusive_by,
            admission_units=admission_units,
            workflow_id=workflow_id,
            tolerance=tolerance,
            start_at=start_at,
            recovery_cap=recovery_cap,
        )
        request = BatchAcceptance(
            batch_id=f"b-{uuid.uuid4().hex[:12]}",
            workflow_id=workflow_id,
            definition=DefinitionPin(definition.name, definition.version, definition.struct_hash),
            items=tuple(pairs),
            fingerprint=fingerprint,
            tolerance_json=tolerance_json,
            start_at=start_at_iso,
            source_ref=source_ref,
            recovery_cap=recovery_cap,
            exclusive_by=exclusive_by,
            admission_units=admission_units,
        )
        created, row = await self._home._submit_batch(request)
        return BatchSubmitReceipt(
            batch_ref=BatchRef(home=self._home.uri, batch_id=row["batch_id"]),
            workflow_id=workflow_id,
            duplicate=not created,
        )

    def submit_batch_sync(
        self,
        graph: Graph,
        values: Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        identity: str,
        map_over: str | Sequence[str] | None = None,
        map_mode: MapMode = "zip",
        schema: type[BaseModel] | None = None,
        exclusive_by: str | None = None,
        admission_units: str | None = None,
        workflow_id: str,
        tolerance: BatchTolerance | None = None,
        start_at: datetime | str | None = None,
        source_ref: str | None = None,
        recovery_cap: int = 3,
    ) -> BatchSubmitReceipt:
        """Sync mirror of ``submit_batch``."""
        definition, pairs, start_at_iso, tolerance_json, fingerprint = self._prepare_batch(
            graph,
            values,
            map_over=map_over,
            map_mode=map_mode,
            identity=identity,
            schema=schema,
            exclusive_by=exclusive_by,
            admission_units=admission_units,
            workflow_id=workflow_id,
            tolerance=tolerance,
            start_at=start_at,
            recovery_cap=recovery_cap,
        )
        request = BatchAcceptance(
            batch_id=f"b-{uuid.uuid4().hex[:12]}",
            workflow_id=workflow_id,
            definition=DefinitionPin(definition.name, definition.version, definition.struct_hash),
            items=tuple(pairs),
            fingerprint=fingerprint,
            tolerance_json=tolerance_json,
            start_at=start_at_iso,
            source_ref=source_ref,
            recovery_cap=recovery_cap,
            exclusive_by=exclusive_by,
            admission_units=admission_units,
        )
        created, row = self._home._submit_batch_sync(request)
        return BatchSubmitReceipt(
            batch_ref=BatchRef(home=self._home.uri, batch_id=row["batch_id"]),
            workflow_id=workflow_id,
            duplicate=not created,
        )

    async def fork(self, ref: RunRef, *, into: Graph, reason: str, source_ref: str | None = None) -> SubmitReceipt:
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
            into: The target Graph, served by this host. Like ``submit``,
                migration names loaded code — never a Definition-name
                string.
            reason: Non-empty migration reason, stored on the submission.
            source_ref: Opaque caller provenance recorded on the NEW
                submission, exactly as ``submit`` and ``stop`` record it
                (US58) — a migration is an authenticated product action too,
                and lineage alone cannot say who asked for it. Audit only:
                never authentication, never a fingerprint input, and never
                part of dedup.
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
            source_ref,
            fingerprint=start_fingerprint(target.definition_id, inputs_json, None),
            forked_from=ref.run_id,
            fork_reason=reason,
        )
        return self._receipt(workflow_id, created)

    def fork_sync(self, ref: RunRef, *, into: Graph, reason: str, source_ref: str | None = None) -> SubmitReceipt:
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
            source_ref,
            fingerprint=start_fingerprint(target.definition_id, inputs_json, None),
            forked_from=ref.run_id,
            fork_reason=reason,
        )
        return self._receipt(workflow_id, created)

    def _require_definition(self, graph: Graph) -> _Definition:
        """Resolve the served Definition a Graph object names, or refuse.

        THE graph-first resolution point, shared by ``submit``,
        ``submit_batch``, and ``fork``. Identity is the Graph's own pinned
        pair — name plus ``structural_hash`` — never object identity, so
        the served object and an equivalent rebuild (or the pre-
        ``with_runner`` original, which keeps both) resolve alike, while a
        graph whose topology drifted is refused rather than silently
        submitted against a Definition it no longer matches.
        """
        from hypergraph.graph import Graph as _Graph

        if not isinstance(graph, _Graph):
            served = sorted(self._definitions)
            raise TypeError(
                f"Durable submission is graph-first: it expects the served Graph object, got "
                f"{type(graph).__name__} ({graph!r}). This host serves: {served}.\n\n"
                "How to fix: pass the Graph you passed to serve(...) — "
                "host.submit(graph, values). A Definition-name string is not a selector: the pinned "
                "identity is the Graph's own name plus structural_hash, which only the object carries."
            )
        definition = self._definitions.get(graph.name or "")
        if definition is None or definition.struct_hash != graph.structural_hash:
            raise UnservedGraphError(
                graph.name or "",
                graph.structural_hash,
                {name: served.struct_hash for name, served in self._definitions.items()},
            )
        return definition

    @staticmethod
    def _serialize_inputs(values: dict[str, Any]) -> str:
        if not isinstance(values, dict):
            raise TypeError(
                f"submit() values must be a dict of graph input names to values, got {type(values).__name__} ({values!r}).\n\n"
                "How to fix: pass the graph's boundary inputs by name — "
                "host.submit(graph, {'work_item_id': 'w1'}). A positional or sequence value cannot be "
                "matched to an input name."
            )
        return json.dumps(values)

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

        Each pass takes one ``now`` **from the store's clock** and scans
        every due row with it: submissions whose ``start_at`` has arrived,
        then scheduled pause answers whose ``due_at`` has arrived. There is
        one due-row scanner, not a timer per feature — and one clock, so a
        worker whose process clock drifts never claims early or fires late.
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
                    # ONE store-authoritative `now` per pass drives every due
                    # row: delayed starts (`start_at`) and scheduled pause
                    # answers (`due_at`) share this scan rather than owning
                    # separate timers (PRD 0017 / ADR 0008). It comes from the
                    # STORE, so two workers on one Home agree on which rows
                    # are due however their process clocks differ.
                    now_iso = await self._home._store_now()
                    claimed = await self._home._claim_eligible(now_iso, served=self._served_identities)
                    for row in claimed:
                        task = asyncio.create_task(self._execute_submission(row))
                        task.add_done_callback(self._record_task_exception)
                        tasks[row["workflow_id"]] = task
                    tasks = {workflow_id: task for workflow_id, task in tasks.items() if not task.done()}
                    await self._home._settle_due_answers(now_iso)
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
        a run parked on a durable pause is stopped where it sits, and
        executing runs receive ``runner.stop(workflow_id, info=...)`` once
        the run is actually registered with the runner. Everything else
        stays unapplied for a later cycle — a stop targeting a run that
        never started is handled by the pre-run gate in
        ``_execute_submission``; a stop targeting a crashed-but-resumable
        run lands once that run executes again. Neither is terminal, so the
        durable intent waits rather than being marked applied against a run
        that never saw it.
        """
        commands = await self._home._unapplied_stop_commands()
        if not commands:
            return
        applied = [command_id for command_id, workflow_id, info in commands if await self._apply_one_stop(workflow_id, info, executing)]
        await self._home._apply_stop_commands(applied)

    async def _apply_one_stop(self, workflow_id: str, info: Any, executing: set[str]) -> bool:
        """Deliver one durable stop; True once the observation is final."""
        submission = await self._home._get_submission(workflow_id)
        run = await self._home.get_run_async(workflow_id)
        if run is not None and run.status in TERMINAL_WORKFLOW_STATUSES:
            return True
        if submission is not None and submission["state"] == SUBMISSION_STATE_FINISHED:
            return True
        if submission is not None and submission["state"] == SUBMISSION_STATE_PAUSED:
            # Parked on a person, so there is no live execution to cooperate
            # with — and none will exist unless somebody answers. Waiting
            # here meant a cancelled review item stayed open forever, so the
            # Home settles the parked run instead (atomically, and losing to
            # an answer that commits first).
            return await self._home._apply_stop_to_paused(workflow_id)
        if workflow_id not in executing or submission is None:
            return False
        definition = self._definitions.get(submission["definition_name"])
        if definition is None or not definition.runner.has_active_run(workflow_id):
            # Claimed and executing, but the runner has not registered the
            # workflow yet — stop() would be a no-op. Retry next cycle; the
            # pre-run gate covers the never-started case.
            return False
        definition.runner.stop(workflow_id, info=info)
        return True

    async def _resume_values(self, workflow_id: str) -> dict[str, Any] | None:
        """The settled answer port to resume an answered pause with, or None.

        A worker resumes an answered child with **only** ``{response_key:
        answer}`` read from the persisted slot. It never resupplies the
        submission's pinned start inputs: those were consumed on the first
        attempt and live in checkpoint state, so replaying them would be an
        input override (``InputOverrideRequiresForkError``) rather than a
        resume. The answer port is the one payload strict checkpoint resume
        accepts on the same workflow id.

        None means "resume with nothing" — an unanswered parked run
        re-adopted by the restart scan simply replays to its interrupt and
        parks again on the same occurrence.
        """
        slot = await self._home.get_pause_slot(workflow_id)
        if slot is None or slot.settled_at is None:
            return None
        return {slot.response_key: slot.answer}

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
            # Resume: the runs row already exists. Checkpoint state supplies
            # the consumed inputs — passing them again raises
            # InputOverrideRequiresForkError — and lineage was recorded by
            # the first attempt, so retry/fork kwargs stay off too. The one
            # payload a resume may carry is a settled answer port.
            inputs = await self._resume_values(workflow_id)
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
            cancellation, cancellation_token = self._home._register_sync_wait_cancellation()
            try:
                await asyncio.to_thread(run_fn, definition.graph, inputs, **run_kwargs)
            except asyncio.CancelledError:
                # ``to_thread`` cancellation cannot kill the worker thread.
                # Fence only an exclusion waiter; ordinary sync-node crash
                # semantics remain at-least-once and are re-adopted.
                cancellation.set()
                raise
            finally:
                self._home._clear_sync_wait_cancellation(cancellation_token)
        # Release the claim only after the run came back: a cancelled or
        # crashed execution leaves the submission claimed for the restart
        # scan. The release settles THIS claim (`row["claim_seq"]`) or
        # nothing — while this attempt was unwinding, an answer plus a
        # re-claim may already have handed the submission to a newer one.
        await self._home._release_submission(workflow_id, row["claim_seq"])


def _definition_from(graph: Graph, *, home: RunHome, deployment_version: str, taken: Collection[str]) -> _Definition:
    """Validate one served graph and bind its runner to the Run Home.

    ``taken`` is the set of Definition names already claimed in this
    ``serve()`` call — two graphs of the same name would make Definition
    identity ambiguous at submission time.
    """
    from hypergraph.graph import Graph as GraphType
    from hypergraph.runners.base import BaseRunner

    if not isinstance(graph, GraphType):
        raise TypeError(f"serve() expects Graph instances, got {type(graph).__name__}.")
    if not graph.name:
        raise ValueError("serve() requires every graph to have a name. Pass name=... to Graph(...) and retry.")
    if graph.name in taken:
        raise ValueError(f"Duplicate definition name {graph.name!r} in serve().")
    runner = graph.bound_runner
    if runner is None:
        raise ValueError(f"Graph {graph.name!r} has no bound runner. Call graph.with_runner(runner) before serve().")
    if not isinstance(runner, BaseRunner):
        raise TypeError(f"Graph {graph.name!r} carries {type(runner).__name__}, not a BaseRunner. Use graph.with_runner(SyncRunner()/AsyncRunner()).")
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
    return _Definition(
        name=graph.name,
        graph=graph,
        runner=bound_runner,
        version=deployment_version,
        struct_hash=graph.structural_hash,
    )


def _validate_accepts(accepts: tuple[DefinitionId, ...], definitions: dict[str, _Definition]) -> None:
    """Refuse an ``accepts=`` declaration this deployment could never drain."""
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
    if not isinstance(home, RunHome):
        raise TypeError(f"serve() requires home=RunHome.open(...), got {type(home).__name__}.")
    if not graphs:
        raise ValueError("serve() requires at least one graph.")
    for entry in accepts:
        if not isinstance(entry, DefinitionId):
            raise TypeError(f"serve() accepts= entries must be DefinitionId instances, got {type(entry).__name__}.")

    definitions: dict[str, _Definition] = {}
    for graph in graphs:
        definition = _definition_from(graph, home=home, deployment_version=deployment_version, taken=definitions)
        definitions[definition.name] = definition
    _validate_accepts(accepts, definitions)

    bus = _PreviewBus()
    _register_bus(home.uri, bus)
    return Host(home=home, definitions=definitions, deployment_version=deployment_version, bus=bus, accepts=tuple(accepts))
