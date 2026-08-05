"""Host and serve() — the durable host's ownership seam.

The Host owns new work (``submit``, ``submit_batch``) and worker lifecycle
(``work_forever``, bounded drain, lease surrender). It exposes the one
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

**Two registries, one door each.** ``serve()`` registers Definition
INSTANCES: graph objects this process holds in memory. ``serve_builder()``
registers CONSTRUCTORS: a key, and a function that rebuilds a graph from
arguments. A submission may carry that key plus its arguments
(``builder=(key, args)``), which makes the work DATA — any process that
registered the same key can rebuild the Definition and execute it, without
having been the one that configured it. The pinned ``DefinitionId`` is
unchanged and still decides what actually runs: a builder whose output does
not match it is refused, never substituted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from hypergraph.host._batch_store import BatchAcceptance, DefinitionPin
from hypergraph.host._bus import _BusEventProcessor, _PreviewBus, _register_bus
from hypergraph.host.batch import BatchTolerance, MapMode, expand_batch_items, freeze_batch_items
from hypergraph.host.client import RunHomeClient
from hypergraph.host.definition import DefinitionId
from hypergraph.host.errors import (
    BuilderIdentityError,
    ForkCompatibilityError,
    HostError,
    NoServingWorkerError,
    UnservedGraphError,
)
from hypergraph.host.fingerprint import batch_fingerprint, canonical_json, start_fingerprint
from hypergraph.host.home import (
    LEASE_RENEWAL_FRACTION,
    LEASE_TTL_SECONDS,
    WORKER_PULSE_TTL_SECONDS,
    RunHome,
    WorkerCoverage,
    _normalize_utc_iso,
)
from hypergraph.host.refs import BatchRef, BatchSubmitReceipt, RunRef, SubmitReceipt
from hypergraph.host.views import (
    DEAD_LETTER_BUILDER_FAILED,
    DEAD_LETTER_BUILDER_IDENTITY_MISMATCH,
    SUBMISSION_STATE_PAUSED,
    TERMINAL_WORKFLOW_STATUSES,
    is_child_settled,
)
from hypergraph.host.worker import _drain

if TYPE_CHECKING:
    from hypergraph.events.processor import EventProcessor
    from hypergraph.graph import Graph
    from hypergraph.runners.base import BaseRunner

logger = logging.getLogger("hypergraph.host")

#: A registered constructor: arguments in, a servable named Graph out. The
#: arguments arrive on the submission row as JSON, so they must be
#: JSON-serializable — that is what makes the work data rather than a closure.
GraphBuilder = Callable[[Mapping[str, Any]], "Graph"]


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


@dataclass(frozen=True)
class _BuilderAddress:
    """One submission's recorded constructor address: key plus arguments.

    ``args_json`` is canonical JSON so two callers who spelled the same
    arguments in a different key order produce one stored value and share one
    memoized Definition — the same canonicalization start fingerprints use.
    """

    key: str
    args_json: str


def _normalize_builder(builder: tuple[str, Mapping[str, Any]] | None) -> _BuilderAddress | None:
    """Validate a ``builder=`` argument into the pair a row stores."""
    if builder is None:
        return None
    if not isinstance(builder, tuple) or len(builder) != 2:
        raise TypeError(
            f"builder= expects a (key, args) pair naming a registered constructor and its arguments, got {builder!r}.\n\n"
            'How to fix: pass builder=("my.builder.key", {"corpus": "protocols"}) — the key a worker '
            "registered with host.serve_builder(), and the JSON-serializable arguments it takes."
        )
    key, args = builder
    if not isinstance(key, str) or not key:
        raise ValueError(f"builder= requires a non-empty string key naming a registered constructor, got {key!r}.")
    if not isinstance(args, Mapping):
        raise TypeError(f"builder= arguments must be a Mapping of JSON-serializable values, got {type(args).__name__} ({args!r}).")
    return _BuilderAddress(key, canonical_json(dict(args)))


def _validate_builder_key(key: str) -> None:
    if not isinstance(key, str) or not key:
        raise ValueError(f"serve_builder() requires a non-empty string key naming the constructor, got {key!r}.")


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


def heartbeat_tick(lease_ttl: float) -> float:
    """How often a live worker writes, in seconds.

    One write says two things — this worker is alive (``host_workers``) and
    so are its claims (``lease_until``) — so its cadence has to satisfy the
    SHORTER of the two windows. Pacing it on ``lease_ttl`` alone would let a
    long lease starve the registry: a worker with a one-hour TTL would write
    every twenty minutes, its ``host_workers`` row would go stale after
    ninety seconds, and every submit would be told nothing alive can serve
    its work — while that worker's claims were perfectly fresh.

    A third of the window is Kafka's ``heartbeat.interval.ms`` rule: three
    consecutive missed writes before anything is declared gone. Writing
    earlier than a lease strictly needs is free; letting a registration
    expire is not.
    """
    return min(lease_ttl, WORKER_PULSE_TTL_SECONDS) * LEASE_RENEWAL_FRACTION


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
        event_processors: Sequence[EventProcessor] = (),
        builders: Mapping[str, GraphBuilder] | None = None,
    ) -> None:
        self._home = home
        self._definitions = definitions
        self._deployment_version = deployment_version
        self._accepts = accepts
        # The constructor registry, and its memo. The memo is keyed by
        # (key, canonical args) rather than by Definition name because that
        # is the pair a submission carries: two rows with the same address
        # build once, and a builder whose arguments differ builds again.
        self._builders: dict[str, GraphBuilder] = dict(builders or {})
        self._builder_definitions: dict[tuple[str, str], _Definition] = {}
        # Deployment-wide processors: every durable Run this worker executes
        # gets them, whichever Definition it belongs to and whichever runner
        # that Definition carries.
        self._event_processors: tuple[EventProcessor, ...] = tuple(event_processors)
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
        # What this worker last published to host_workers, and when. Both
        # decide when the next pulse is due: the clock keeps the row fresh,
        # and the published set republishes immediately when a Definition or
        # a builder is added mid-flight.
        self._published: tuple[frozenset[DefinitionId], frozenset[str]] | None = None
        self._pulsed_at: float | None = None
        self.worker_errors: list[BaseException] = []

    @property
    def client(self) -> RunHomeClient:
        """The one RunHomeClient for this Home (no verb copies on Host)."""
        return self._client

    @property
    def builder_keys(self) -> frozenset[str]:
        """Constructor keys this host registered (see ``serve_builder``)."""
        return frozenset(self._builders)

    async def live_coverage(self) -> WorkerCoverage:
        """What every worker with a fresh pulse on this Home can execute.

        A pure read of the ``host_workers`` registry — it grants nothing and
        fences nothing, the claim CAS and ``claim_seq`` remain the only
        authority. What it answers is "would this work run if I submitted it
        and did nothing else?", which is the question a process has to settle
        BEFORE deciding whether to become a worker itself.

        ``submit`` already asks it on the caller's behalf and refuses an
        address nobody answers to (``NoServingWorkerError``), so most callers
        never need this. It is for the process that must arrange its own
        execution FIRST — attaching an event processor to the runner that will
        run the work, say, which is only worth doing where the work will run.
        """
        return await self._home._live_worker_coverage()

    def live_coverage_sync(self) -> WorkerCoverage:
        """Sync mirror of :meth:`live_coverage`."""
        return self._home._live_worker_coverage_sync()

    def serve_builder(self, key: str, builder: GraphBuilder) -> None:
        """Register a CONSTRUCTOR under ``key``, beside the served instances.

        ``serve()`` answers "which graphs do I hold?"; this answers "which
        graphs can I build?"::

            host.serve_builder("review.retrieval", lambda args: review_graph(**args))
            await host.submit(graph, values, builder=("review.retrieval", {"corpus": "protocols"}))

        A submission carrying that pair records the address of the code
        rather than assuming the executor already holds it, so a notebook can
        configure work a server drains — the case where the configuring
        process is not, and never will be, the executing one.

        The builder is called with the recorded arguments and must return a
        named ``Graph`` with a bound runner, exactly like a ``serve()``
        argument. Its output is then checked against the submission's pinned
        ``DefinitionId``, so registering a builder grants no authority to
        change what a submission runs: a drifted builder is refused
        (``BuilderIdentityError`` at submit, a ``builder_identity_mismatch``
        dead letter at claim), never silently substituted.

        Re-registering a key REPLACES it, and is safe precisely because of
        that check — a replacement can only ever build the identity a
        submission already pinned, or fail to. Definitions the old builder
        produced stay served; only the memo for that key is dropped.

        Args:
            key: Stable address of the constructor. It travels on the
                submission row, so it must mean the same thing in every
                process that registers it — a dotted product name
                (``"panda.review.retrieval"``) rather than anything derived
                from local state.
            builder: ``builder(args) -> Graph``. ``args`` is the mapping the
                submission recorded, decoded from JSON.

        Raises:
            ValueError: If ``key`` is empty or not a string.
            TypeError: If ``builder`` is not callable.
        """
        _validate_builder_key(key)
        if not callable(builder):
            raise TypeError(f"serve_builder({key!r}, ...) expects a callable taking the recorded arguments, got {type(builder).__name__}.")
        self._builders[key] = builder
        self._builder_definitions = {memo: definition for memo, definition in self._builder_definitions.items() if memo[0] != key}

    def add_definition(self, graph: Graph) -> None:
        """Add one Definition without restarting this Host's worker.

        Re-adding the same Definition identity is a no-op. A served name
        cannot be replaced with a different structural identity; replacement
        remains an explicit worker restart so in-flight work never changes
        code underneath its pinned Definition.

        Args:
            graph: A named graph with a runner bound via ``with_runner()``.

        Raises:
            ValueError: If the graph is invalid for durable serving or tries
                to replace a served Definition.
        """
        definition = _definition_from(graph, home=self._home, deployment_version=self._deployment_version, taken=())
        existing = self._definitions.get(definition.name)
        if existing is not None:
            if existing.struct_hash == definition.struct_hash:
                return
            raise ValueError(
                f"Definition {definition.name!r} is already served with structural hash {existing.struct_hash!r}; "
                f"it cannot be replaced in-place by {definition.struct_hash!r}.\n\n"
                "How to fix: close this Host and create a new one to replace a Definition, or give the new graph a distinct name."
            )
        self._definitions[definition.name] = definition
        self._served_identities = self._served_identities | {definition.definition_id}

    async def submit(
        self,
        graph: Graph,
        values: dict[str, Any],
        *,
        workflow_id: str | None = None,
        start_at: datetime | str | None = None,
        source_ref: str | None = None,
        recovery_cap: int = 3,
        builder: tuple[str, Mapping[str, Any]] | None = None,
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
            builder: Optional ``(key, args)`` constructor address recorded on
                the row, so a worker that does not hold this Definition in
                memory can rebuild it — see ``serve_builder``. ``key`` must
                be registered here or by a live worker, else
                ``NoServingWorkerError``; the built Definition must match the
                pinned identity, else ``BuilderIdentityError``. Not part of
                the dedup fingerprint: a duplicate resubmission returns the
                stored row and never rewrites the address it was accepted
                with.
        """
        address = _normalize_builder(builder)
        definition, inputs_json, start_at_iso, workflow_id = self._prepare_run(
            graph, values, workflow_id=workflow_id, start_at=start_at, recovery_cap=recovery_cap, builder=address
        )
        await self._require_executor(address)
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
            builder_key=None if address is None else address.key,
            builder_args_json=None if address is None else address.args_json,
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
        builder: tuple[str, Mapping[str, Any]] | None = None,
    ) -> SubmitReceipt:
        """Sync mirror of ``submit``."""
        address = _normalize_builder(builder)
        definition, inputs_json, start_at_iso, workflow_id = self._prepare_run(
            graph, values, workflow_id=workflow_id, start_at=start_at, recovery_cap=recovery_cap, builder=address
        )
        self._require_executor_sync(address)
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
            builder_key=None if address is None else address.key,
            builder_args_json=None if address is None else address.args_json,
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
        builder: _BuilderAddress | None = None,
    ) -> tuple[_Definition, str, str | None, str]:
        """Validate one Run submission and normalize its stored fields."""
        _validate_recovery_cap(recovery_cap)
        definition = self._require_definition(graph, builder)
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
        admission_cost: str | None,
        workflow_id: str,
        tolerance: BatchTolerance | None,
        start_at: datetime | str | None,
        recovery_cap: int,
        builder: _BuilderAddress | None = None,
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
        definition = self._require_definition(graph, builder)
        if admission_cost is not None and (not isinstance(admission_cost, str) or not admission_cost):
            raise ValueError(f"submit_batch() admission_cost must name an item field, got {admission_cost!r}.")
        if isinstance(values, Mapping):
            if map_over is None:
                raise TypeError("submit_batch() mapping-form values require map_over; typed item sequences do not.")
            pairs = expand_batch_items(
                values,
                map_over=map_over,
                map_mode=map_mode,
                identity=identity,
                graph=graph,
                schema=None,
            )
        else:
            if map_over is not None:
                raise TypeError(
                    f"submit_batch() runner-shaped values must be a Mapping when map_over is present; got "
                    f"{type(values).__name__} values ({values!r}).\n\nHow to fix:\n  Remove map_over and pass a sequence "
                    "of per-item mappings, or transpose the values into a mapping of input collections."
                )
            pairs = freeze_batch_items(values, identity=identity, graph=graph, schema=None)
        if admission_cost is not None:
            for index, (_, inputs_json) in enumerate(pairs):
                inputs = json.loads(inputs_json)
                value = inputs.get(admission_cost)
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError(f"submit_batch() item {index} admission_cost={admission_cost!r} must be an int >= 1; got {value!r}.")
        start_at_iso = _normalize_start_at(start_at)
        tolerance_json = json.dumps(tolerance.to_dict()) if tolerance is not None else None
        fingerprint = batch_fingerprint(
            definition.definition_id,
            {key: json.loads(inputs_json) for key, inputs_json in pairs},
            tolerance,
            start_at_iso,
            admission_cost,
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
        admission_cost: str | None = None,
        workflow_id: str,
        tolerance: BatchTolerance | None = None,
        start_at: datetime | str | None = None,
        source_ref: str | None = None,
        recovery_cap: int = 3,
        builder: tuple[str, Mapping[str, Any]] | None = None,
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
            identity: Required. Names one expanded field whose per-item
                JSON-safe scalar value becomes the logical item key. The
                field may be manifest-only: if it is not a graph boundary
                input, it keys the Batch but is not passed to child Runs.
                Missing, empty, non-scalar, and duplicate keys are refused
                before acceptance (``ItemKeyError``) — a generated map
                index is never durable identity.
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
        address = _normalize_builder(builder)
        definition, pairs, start_at_iso, tolerance_json, fingerprint = self._prepare_batch(
            graph,
            values,
            map_over=map_over,
            map_mode=map_mode,
            identity=identity,
            admission_cost=admission_cost,
            workflow_id=workflow_id,
            tolerance=tolerance,
            start_at=start_at,
            recovery_cap=recovery_cap,
            builder=address,
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
            admission_cost=admission_cost,
            child_admission_costs={key: 1 if admission_cost is None else int(json.loads(inputs_json)[admission_cost]) for key, inputs_json in pairs},
            builder_key=None if address is None else address.key,
            builder_args_json=None if address is None else address.args_json,
        )
        await self._require_executor(address)
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
        admission_cost: str | None = None,
        workflow_id: str,
        tolerance: BatchTolerance | None = None,
        start_at: datetime | str | None = None,
        source_ref: str | None = None,
        recovery_cap: int = 3,
        builder: tuple[str, Mapping[str, Any]] | None = None,
    ) -> BatchSubmitReceipt:
        """Sync mirror of ``submit_batch``."""
        address = _normalize_builder(builder)
        definition, pairs, start_at_iso, tolerance_json, fingerprint = self._prepare_batch(
            graph,
            values,
            map_over=map_over,
            map_mode=map_mode,
            identity=identity,
            admission_cost=admission_cost,
            workflow_id=workflow_id,
            tolerance=tolerance,
            start_at=start_at,
            recovery_cap=recovery_cap,
            builder=address,
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
            admission_cost=admission_cost,
            child_admission_costs={key: 1 if admission_cost is None else int(json.loads(inputs_json)[admission_cost]) for key, inputs_json in pairs},
            builder_key=None if address is None else address.key,
            builder_args_json=None if address is None else address.args_json,
        )
        self._require_executor_sync(address)
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

    def _require_definition(self, graph: Graph, builder: _BuilderAddress | None = None) -> _Definition:
        """Resolve the served Definition a Graph object names, or refuse.

        THE graph-first resolution point, shared by ``submit``,
        ``submit_batch``, and ``fork``. Identity is the Graph's own pinned
        pair — name plus ``structural_hash`` — never object identity, so
        the served object and an equivalent rebuild (or the pre-
        ``with_runner`` original, which keeps both) resolve alike, while a
        graph whose topology drifted is refused rather than silently
        submitted against a Definition it no longer matches.

        A ``builder`` address widens where the Definition may COME from,
        never what it has to be: a graph this host does not serve is built
        from the registered constructor and admitted only if the result has
        the identity the caller passed. So a process holding only builders
        can submit, and a builder that drifted still cannot change what runs.
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
        if definition is not None and definition.struct_hash == graph.structural_hash:
            return definition
        if builder is not None and builder.key in self._builders:
            built = self._build_definition(builder)
            pinned = DefinitionId(graph.name or "", self._deployment_version, graph.structural_hash)
            if built.definition_id != pinned:
                raise BuilderIdentityError(builder.key, pinned, built.definition_id)
            return built
        raise UnservedGraphError(
            graph.name or "",
            graph.structural_hash,
            {name: served.struct_hash for name, served in self._definitions.items()},
        )

    def _build_definition(self, builder: _BuilderAddress) -> _Definition:
        """Build (once) the Definition one constructor address names.

        Memoized on the address, which is what makes a Batch of hundreds of
        children cost ONE construction: every child row carries the same
        ``(key, args)`` pair, and building a configured graph is rarely
        cheap. The built Definition is also added to the served instances, so
        everything downstream — stop delivery, fork, a second submission —
        finds it by name exactly as if it had been passed to ``serve()``.
        """
        memo = (builder.key, builder.args_json)
        cached = self._builder_definitions.get(memo)
        if cached is not None:
            return cached
        graph = self._builders[builder.key](json.loads(builder.args_json))
        definition = _definition_from(graph, home=self._home, deployment_version=self._deployment_version, taken=())
        served = self._definitions.get(definition.name)
        if served is None or served.struct_hash != definition.struct_hash:
            self.add_definition(graph)
            definition = self._definitions[definition.name]
        else:
            definition = served
        self._builder_definitions[memo] = definition
        return definition

    async def _require_executor(self, builder: _BuilderAddress | None) -> None:
        """Refuse a submission naming a constructor nothing can call.

        The identity half of this question is already answered:
        ``_require_definition`` only returns for a Definition THIS host
        serves, and a host that serves it may itself become the worker. The
        open half is the builder address, because recording one means "some
        other process will rebuild this" — and an address nothing registers
        is the failure the whole builder registry exists to prevent, caught
        here rather than as a queue of rows with no executor.
        """
        if builder is None or builder.key in self._builders:
            return
        coverage = await self._home._live_worker_coverage()
        if builder.key not in coverage.builders:
            raise NoServingWorkerError(builder.key, registered=self._builders, workers=coverage.worker_ids)

    def _require_executor_sync(self, builder: _BuilderAddress | None) -> None:
        """Sync mirror of ``_require_executor``."""
        if builder is None or builder.key in self._builders:
            return
        coverage = self._home._live_worker_coverage_sync()
        if builder.key not in coverage.builders:
            raise NoServingWorkerError(builder.key, registered=self._builders, workers=coverage.worker_ids)

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

    async def work_forever(
        self,
        worker_id: str,
        *,
        poll_interval: float = 0.05,
        drain_timeout: float = 30.0,
        lease_ttl: float = LEASE_TTL_SECONDS,
    ) -> None:
        """Run the worker loop: claim, execute, repeat, with bounded drain.

        **Several workers may share one Run Home.** Each claim is an atomic
        compare-and-set that also takes a time-bounded LEASE, so two workers
        can never hold one submission, and a worker that stops renewing has
        its claims adopted by whoever is still polling. The exclusive
        ``flock`` this used to take is gone: it made "is this half-finished
        Run dead, or is another worker holding it?" unaskable rather than
        answered, at the cost of forbidding a notebook or a maintenance
        script from ever executing work beside the server.

        Startup adopts this ``worker_id``'s own outstanding claims
        immediately — this process IS that worker and is executing nothing —
        so a supervised restart resumes as promptly as it did under the
        lock. Every later pass adopts only claims whose lease has EXPIRED,
        and never its own. On ``shutdown()`` or cancellation the loop stops
        claiming, awaits active runs up to ``drain_timeout``, cancels the
        rest, surrenders whatever leases it still holds so another worker
        can pick that work up at once rather than after the TTL, withdraws
        its registration, and returns (or re-raises the cancellation)
        cleanly.

        Each pass takes one ``now`` **from the store's clock** and scans
        every due row with it: expired leases, then submissions whose
        ``start_at`` has arrived, then scheduled pause answers whose
        ``due_at`` has arrived. There is one due-row scanner, not a timer
        per feature — and one clock, so workers whose process clocks drift
        still agree on which claims have run out.

        Startup also REGISTERS this worker in ``host_workers`` — its served
        identities and its builder keys — and the loop keeps that row's
        pulse fresh, on the same tick that renews its leases. The
        registration grants nothing: it is how a submitting process learns
        whether anybody could execute its work, how the claim scan tells
        "another worker's work" apart from "work nothing alive can run"
        instead of parking both alike, and what re-opens the
        version-incompatible park when a worker able to drain it arrives. A
        clean exit withdraws the row; a killed worker's row simply goes
        stale.

        Args:
            worker_id: This worker's stable name. It is written onto every
                claim it takes, so a restart under the same name reclaims
                its own work at once; two live workers must not share one.
            poll_interval: Idle sleep between claim scans.
            drain_timeout: How long ``shutdown()`` awaits in-flight runs
                before cancelling them.
            lease_ttl: Seconds a claim stays this worker's before anybody
                may adopt it, renewed at a third of that while the worker
                lives. Lower it only for tests that must watch adoption
                happen; a production value below the longest single Run's
                event-loop stall invites duplicate execution (the claim
                fence makes that wasteful, not unsafe).
        """
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError("work_forever() requires a non-empty worker_id string.")
        if lease_ttl <= 0:
            raise ValueError(f"work_forever() lease_ttl must be a positive number of seconds, got {lease_ttl!r}.")
        stop_event = asyncio.Event()
        self._stop_event = stop_event
        self._worker_loop = asyncio.get_running_loop()
        if self._shutdown_requested:
            # A shutdown that raced worker startup is honored here, then
            # consumed: later work_forever() runs on this Host start fresh.
            stop_event.set()
            self._shutdown_requested = False
        tasks: dict[str, asyncio.Task] = {}
        self._published = None
        self._pulsed_at = None
        try:
            await self._home._reclaim_expired(worker_id=worker_id, adopt_own=True)
            try:
                while not stop_event.is_set():
                    # ONE store-authoritative `now` per pass drives every due
                    # row: expired leases, delayed starts (`start_at`), and
                    # scheduled pause answers (`due_at`) share this scan
                    # rather than owning separate timers (PRD 0017 / ADR
                    # 0008). It comes from the STORE, so workers on one Home
                    # agree on which rows are due however their process
                    # clocks differ.
                    now_iso = await self._home._store_now()
                    # Renew before reclaiming, so this worker's own claims are
                    # provably fresh before it judges anybody else's.
                    await self._pulse(worker_id, now_iso, lease_ttl)
                    await self._home._reclaim_expired(now_iso, worker_id=worker_id)
                    claimed = await self._home._claim_eligible(
                        now_iso,
                        served=self._served_identities,
                        builders=frozenset(self._builders),
                        worker_id=worker_id,
                        lease_ttl=lease_ttl,
                    )
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
                await self._release_leases(worker_id)
                await self._withdraw(worker_id)
        finally:
            self._stop_event = None
            self._worker_loop = None
            self._published = None
            self._pulsed_at = None

    async def _pulse(self, worker_id: str, now_iso: str, lease_ttl: float) -> None:
        """Say this worker is alive — and so are its claims — on one tick.

        Two triggers, because the registration row answers two questions.
        The CLOCK keeps "is this worker alive?" true, at
        ``LEASE_RENEWAL_FRACTION`` of the lease TTL: three writes per window,
        Kafka's ``heartbeat.interval.ms`` rule. The published SET keeps "what
        can it execute?" true the moment ``add_definition`` or
        ``serve_builder`` widens it — waiting a tick there would let a submit
        refuse work this worker had just learned to do, and would leave a
        version-incompatible row parked against a worker that can now drain
        it.

        The LEASE renewal rides the same clock trigger rather than the poll
        pass. The two facts have the same shape and the same kind of
        deadline, so writing them on different cadences would buy nothing
        and cost a write transaction every 50 ms on a Home that may now
        carry several writers. ``heartbeat_tick`` is where the one cadence
        that satisfies both windows is decided.
        """
        current = (self._served_identities, frozenset(self._builders))
        elapsed = None if self._pulsed_at is None else asyncio.get_running_loop().time() - self._pulsed_at
        due = elapsed is None or elapsed >= heartbeat_tick(lease_ttl)
        if self._published == current and not due:
            return
        await self._home._pulse_worker(worker_id, served=current[0], builders=current[1])
        if due:
            await self._home._renew_leases(worker_id, now_iso, lease_ttl=lease_ttl)
            self._pulsed_at = asyncio.get_running_loop().time()
        self._published = current

    async def _release_leases(self, worker_id: str) -> None:
        """Surrender this worker's claims on a clean exit; never fail shutdown.

        A stopping worker knows it is gone, so it says so instead of making
        the rest of the deployment wait out the TTL. Like ``_withdraw``, a
        failure here costs only promptness — the lease expires on its own —
        so it is logged rather than raised over a drain that just succeeded.
        """
        try:
            await self._home._expire_leases(worker_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Worker %s could not surrender its leases; they expire on their own.", worker_id, exc_info=True)

    async def _withdraw(self, worker_id: str) -> None:
        """Withdraw the registration; cleanup never fails a shutdown.

        A row this call could not delete is not a correctness problem — the
        freshness window retires it — so anything short of the caller's own
        cancellation is logged rather than raised over the drain that just
        completed successfully.
        """
        try:
            await self._home._retire_worker(worker_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Worker %s could not withdraw its registration; its row expires on its own.", worker_id, exc_info=True)

    def _record_task_exception(self, task: asyncio.Task) -> None:
        """Retrieve a finished execution task's exception for observability.

        A failed execution leaves its submission claimed. Its lease stops
        being renewed when this worker stops, and the reclaim scan re-adopts
        it — this worker's own at its next startup, anybody's once expired.
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
        if submission is not None and is_child_settled(submission["state"], None):
            # Settled without a terminal runs row: stopped before it started,
            # closed by a tolerance trip, braked by the recovery cap, or
            # retired as a dead letter. The stop lands with no effect, and
            # that observation is FINAL — leaving it unapplied would keep a
            # durable command outstanding forever against work that can never
            # move again.
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
        re-adopted by the reclaim scan simply replays to its interrupt and
        parks again on the same occurrence.
        """
        slot = await self._home.get_pause_slot(workflow_id)
        if slot is None or slot.settled_at is None:
            return None
        return {slot.response_key: slot.answer}

    async def _definition_for_claim(self, row: dict[str, Any]) -> _Definition | None:
        """THE Definition a claimed row is allowed to execute, or None.

        Which registry claimed the row decides how it resolves, and the two
        rules are deliberately not the same.

        A row claimed by SERVED IDENTITY resolves by name, as it always has:
        the identity is one this host declared — exactly, or through an
        ``accepts=`` migration declaration — so the named Definition is by
        construction the one entitled to execute it (``_validate_accepts``
        pins that equivalence at ``serve()`` time).

        A row claimed by BUILDER KEY carries an identity this host never
        declared, so a name lookup could hand it a same-named Definition
        nobody pinned it to — and it would run, resuming checkpoints against
        foreign topology. That path therefore builds, and admits the result
        only if it equals the pinned identity. A builder that drifted or
        raised retires the submission as a dead letter with the reason:
        neither is something a retry fixes, and neither may be papered over
        by executing something else.

        None means "leave this claim alone": no builder covers the row, which
        can only happen if the registry shrank between the claim and here.
        The reclaim scan returns it to pending once this claim's lease runs
        out, and the next claim scan decides its disposition against fresh
        registry truth.
        """
        pinned = DefinitionId(row["definition_name"], row["def_version"], row["def_struct_hash"])
        if pinned in self._served_identities:
            served = self._definitions.get(pinned.name)
            if served is not None:
                return served
        builder_key = row["builder_key"]
        if builder_key is None or builder_key not in self._builders:
            return None
        address = _BuilderAddress(builder_key, row["builder_args_json"] or "{}")
        try:
            built = self._build_definition(address)
        except Exception as error:
            logger.warning("Builder %r failed to rebuild submission %s.", builder_key, row["workflow_id"], exc_info=True)
            await self._home._dead_letter(
                row["workflow_id"],
                DEAD_LETTER_BUILDER_FAILED,
                claim_seq=row["claim_seq"],
                detail={"error": type(error).__name__},
            )
            return None
        if built.definition_id != pinned:
            await self._home._dead_letter(
                row["workflow_id"],
                DEAD_LETTER_BUILDER_IDENTITY_MISMATCH,
                claim_seq=row["claim_seq"],
                detail={"built": built.definition_id.to_dict()},
            )
            return None
        return built

    async def _execute_submission(self, row: dict[str, Any]) -> None:
        """Execute one claimed submission through its Definition's runner."""
        definition = await self._definition_for_claim(row)
        if definition is None:
            # Not executable by this worker; either already dead-lettered
            # with a reason, or left claimed for the reclaim scan.
            return
        workflow_id = row["workflow_id"]
        if await self._home._apply_stop_never_started(workflow_id):
            # A stop landed before first execution: the command is applied
            # and the submission finished without inventing a runs row.
            return
        inputs: dict[str, Any] | None = json.loads(row["inputs_json"])
        # Deployment processors first, per the library's carried-before-call-site
        # order: they were declared once for the whole host, the bus processor is
        # made per Run. Dispatch is best-effort, so an app processor that raises
        # cannot break the preview bus or the run.
        processors = [*self._event_processors, _BusEventProcessor(self._bus, workflow_id)]
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


def _normalize_event_processors(event_processors: Sequence[EventProcessor] | None, *, caller: str) -> tuple[EventProcessor, ...]:
    """Accept a sequence of processors; refuse a bare one loudly.

    A single processor is iterable-looking to nobody, so passing one where a
    sequence is expected would otherwise raise deep inside ``tuple()`` or,
    worse, silently succeed for a str-like object.
    """
    if event_processors is None:
        return ()
    if isinstance(event_processors, (str, bytes)) or not isinstance(event_processors, Sequence):
        raise TypeError(f"{caller} event_processors must be a sequence of EventProcessor, got {type(event_processors).__name__}. Wrap it in a list.")
    return tuple(event_processors)


def serve(
    *graphs: Graph,
    home: RunHome,
    deployment_version: str = "",
    accepts: tuple[DefinitionId, ...] = (),
    event_processors: Sequence[EventProcessor] | None = None,
    builders: Mapping[str, GraphBuilder] | None = None,
) -> Host:
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
        event_processors: Processors this deployment adds to **every**
            durable Run the worker executes, whichever Definition it belongs
            to and whichever runner that Definition carries. The seam an
            embedding application uses for process-wide observability — an
            ``OpenTelemetryProcessor``, a metrics sink — without having to
            reach a runner it did not construct. Instances are shared across
            concurrent Runs, so a processor must be safe to use that way;
            dispatch is best-effort, so one that raises is logged and cannot
            break the Run. Omitted (the default) is byte-identical to before:
            the worker passes only its own per-Run preview processor.
        builders: Constructors this deployment registers by key, the same
            registration ``Host.serve_builder`` makes one at a time. A
            builder-only deployment is legal — pass no graphs and only
            ``builders=`` for a worker that holds nothing in memory and
            rebuilds every Definition a submission addresses.
    """
    if not isinstance(home, RunHome):
        raise TypeError(f"serve() requires home=RunHome.open(...), got {type(home).__name__}.")
    if not graphs and not builders:
        raise ValueError("serve() requires at least one graph (or a builders= registry a worker can build them from).")
    for entry in accepts:
        if not isinstance(entry, DefinitionId):
            raise TypeError(f"serve() accepts= entries must be DefinitionId instances, got {type(entry).__name__}.")
    processors = _normalize_event_processors(event_processors, caller="serve()")

    definitions: dict[str, _Definition] = {}
    for graph in graphs:
        definition = _definition_from(graph, home=home, deployment_version=deployment_version, taken=definitions)
        definitions[definition.name] = definition
    _validate_accepts(accepts, definitions)

    bus = _PreviewBus()
    _register_bus(home.uri, bus)
    host = Host(
        home=home,
        definitions=definitions,
        deployment_version=deployment_version,
        bus=bus,
        accepts=tuple(accepts),
        event_processors=processors,
    )
    for key, builder in (builders or {}).items():
        host.serve_builder(key, builder)
    return host
