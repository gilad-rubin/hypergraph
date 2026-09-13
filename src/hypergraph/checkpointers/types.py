"""Checkpointer types for run persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from hypergraph._utils import format_duration_ms, plural
from hypergraph.exceptions import HostError

if TYPE_CHECKING:
    from collections.abc import Iterable


def _utcnow() -> datetime:
    """UTC-aware datetime (avoids deprecated utcnow)."""
    return datetime.now(timezone.utc)


class StepStatus(Enum):
    """Status of a single step execution."""

    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


# === Durable node addressing ===
#
# One canonical address names one node-boundary occurrence inside one run:
# ``<run_id>:<superstep>:<node_name>``. It is the same tuple ``steps`` is
# unique on, so a boundary and its StepRecord always agree. Every durable
# record that must name "this node, this occurrence" uses it — pending node
# boundaries today, pause slots next.
#
# Nesting: a nested graph runs as its own child run, so its boundaries carry
# the child ``run_id``. The parent's own boundary for the delegating
# ``GraphNode`` keeps the parent-facing address, exactly like the parent's
# StepRecord for that node.
#
# The last two segments stay unambiguous: node names are Python identifiers
# and the superstep is digits, while run ids may contain ``/`` (nested
# children) and ``:`` (Batch children are ``<batch_workflow_id>:<item_key>``).
# A reader therefore splits from the RIGHT — but no parser ships until
# something under ``src/`` actually needs to read an address back.

NODE_ADDRESS_SEPARATOR = ":"


def node_address(run_id: str, superstep: int, node_name: str) -> str:
    """Canonical durable address of one node-boundary occurrence.

    A loop's second visit to the same node lands on a later superstep and
    therefore gets a different address — occurrences never collide.
    """
    return f"{run_id}{NODE_ADDRESS_SEPARATOR}{superstep}{NODE_ADDRESS_SEPARATOR}{node_name}"


class BoundaryState(Enum):
    """Recovery classification of one node boundary — never a guess.

    ``COMMITTED`` — a StepRecord exists for the address; the journal
    witnessed the outcome (completed, failed, or paused).
    ``PENDING`` — the boundary was recorded as runnable and nothing marked it
    settled or dispatched. It never started, so it is safe to dispatch.
    ``SETTLED_UNRECORDED`` — the node ran to completion and said so, but its
    StepRecord died with the superstep that was killed before it committed
    (StepRecords commit per superstep, the marker per node). The outcome
    value is gone; the fact that the node completed is not.
    ``UNKNOWN_EFFECT`` — reserved for declared-effect nodes (PRD 0014): the
    boundary was marked dispatched and never settled, so recovery must not
    dispatch it again automatically.
    """

    PENDING = "pending"
    COMMITTED = "committed"
    SETTLED_UNRECORDED = "settled_unrecorded"
    UNKNOWN_EFFECT = "unknown_effect"


def derive_boundary_state(
    step_status: StepStatus | None,
    dispatched_at: datetime | None,
    settled_at: datetime | None = None,
) -> BoundaryState:
    """Classify one boundary by joining recorded intent with the journal.

    The single definition of the cascade — every backend calls this instead
    of re-deriving it, so a fifth state means editing exactly one place.

    A StepRecord of any status is a witnessed settlement. Without one, a
    settlement mark still says the node ran to completion
    (``SETTLED_UNRECORDED``); a dispatch mark alone says only that a provider
    was called and the outcome is unknown (``UNKNOWN_EFFECT``, PRD 0014);
    neither mark is safe, untouched, pending work.

    Settlement outranks dispatch deliberately: a dispatched boundary that
    later settled is not an unknown effect — the node returned, so the effect
    is known to have landed. Only its recorded value was lost.
    """
    if step_status is not None:
        return BoundaryState.COMMITTED
    if settled_at is not None:
        return BoundaryState.SETTLED_UNRECORDED
    if dispatched_at is not None:
        return BoundaryState.UNKNOWN_EFFECT
    return BoundaryState.PENDING


@dataclass(frozen=True)
class PendingNode:
    """Durable *intent*: one runnable node boundary in one superstep.

    Written for every runnable sibling before the first sibling of that
    superstep dispatches, so process death between siblings cannot forget the
    unfinished ones. A pending record never claims a node ran — StepRecords
    remain the sole execution journal.

    ``dispatched_at`` is the declared-effect seam (PRD 0014) and stays
    ``None`` on the boundary-record write path: only effect reservation, made
    before a provider call, may mark a boundary dispatched.

    ``settled_at`` is the per-node settlement marker: the runner sets it the
    moment a node's result is in hand, before that result is folded into
    shared state and long before the superstep's StepRecords commit. It is
    the only thing that survives a kill inside a superstep, and it still
    claims nothing about the node's OUTPUT — only that the node completed.
    """

    run_id: str
    superstep: int
    node_name: str
    node_type: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    dispatched_at: datetime | None = None
    settled_at: datetime | None = None

    @property
    def address(self) -> str:
        """Canonical durable address of this boundary."""
        return node_address(self.run_id, self.superstep, self.node_name)

    def __repr__(self) -> str:
        return f"PendingNode {self.address}"


@dataclass(frozen=True)
class NodeBoundary:
    """Recovery view of one node boundary: intent joined with the journal.

    ``state`` is derived, never stored: a boundary is ``COMMITTED`` exactly
    when its StepRecord exists, and ``SETTLED_UNRECORDED`` when the runner
    marked the node complete but no StepRecord followed. ``dispatched_at``
    stays ``None`` until declared-effect reservation (PRD 0014) sets it.
    """

    run_id: str
    superstep: int
    node_name: str
    state: BoundaryState
    node_type: str | None = None
    created_at: datetime | None = None
    dispatched_at: datetime | None = None
    settled_at: datetime | None = None
    step_status: StepStatus | None = None

    @property
    def address(self) -> str:
        """Canonical durable address of this boundary."""
        return node_address(self.run_id, self.superstep, self.node_name)

    def __repr__(self) -> str:
        parts = [f"NodeBoundary {self.address}", self.state.value]
        if self.step_status is not None:
            parts.append(f"step: {self.step_status.value}")
        return " | ".join(parts)


@dataclass(frozen=True)
class RunTotals:
    """The three run-level counters a status transition may carry.

    They always travel together — a status write that knows the duration
    knows the node and error counts too — so they travel as one value
    instead of as three parallel keyword arguments through every write path.

    ``None`` on a field means "leave the stored value alone", exactly the
    per-field semantics ``update_run_status`` has always had.
    """

    duration_ms: float | None = None
    node_count: int | None = None
    error_count: int | None = None


#: The "record nothing new" totals — a status transition that carries no counters.
NO_RUN_TOTALS = RunTotals()


@dataclass(frozen=True)
class PauseSlot:
    """Durable record of ONE interrupt occurrence (PRD 0010).

    Before this record existed, pause truth died with the process: the
    question and its answer port lived only on the in-memory ``RunResult``.
    The slot and the run's transition to ``PAUSED`` are one commit, and the
    paused StepRecord is never written after them, so a committed ``PAUSED``
    run is never missing its question (see ``record_pause``).

    ``pause_id`` is the node address of the occurrence
    (``<run_id>:<superstep>:<node_name>``) — a loop's second visit lands on a
    later superstep and therefore owns a different id.

    ``node_name`` is the **parent-facing** node: for a nested interrupt it is
    the delegating ``GraphNode`` in this run, exactly like the paused
    StepRecord. ``node_path`` keeps the full ``graphnode/inner`` display path,
    and the child run records its own slot under the child workflow id.

    ``question`` is a JSON-safe projection (prompt / options / evidence /
    answer-type name) — the live handler payload never enters the journal.
    ``answer_schema`` is the graph-derived answer contract as JSON Schema,
    describing the JSON *form* of the declared ``answer_type`` — a settled
    answer is durable resume input, so it is always JSON-safe. An empty
    schema means nothing was declared; a schema carrying
    ``"x-hypergraph-unrenderable"`` names a declared type the renderer could
    not express. Both constrain nothing beyond JSON-safety (see
    ``checkpointers/_answer_schema.py``).

    ``answer`` is the settled value — the durable resume input for
    ``response_key``. It is meaningful only once ``settled_at`` is set.
    """

    run_id: str
    superstep: int
    node_name: str
    response_key: str
    question: dict[str, Any] = field(default_factory=dict)
    answer_schema: dict[str, Any] = field(default_factory=dict)
    options: tuple[str, ...] | None = None
    node_path: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    settled_at: datetime | None = None
    answer: Any = None

    @property
    def pause_id(self) -> str:
        """Canonical durable id of this interrupt occurrence."""
        return node_address(self.run_id, self.superstep, self.node_name)

    @property
    def is_open(self) -> bool:
        """Whether this occurrence is still waiting for an answer."""
        return self.settled_at is None

    def __repr__(self) -> str:
        state = "open" if self.is_open else "settled"
        return f"PauseSlot {self.pause_id} | {state} | answers {self.response_key!r}"

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict with only primitive types."""
        return {
            "pause_id": self.pause_id,
            "run_id": self.run_id,
            "superstep": self.superstep,
            "node_name": self.node_name,
            "node_path": self.node_path,
            "response_key": self.response_key,
            "question": self.question,
            "answer_schema": self.answer_schema,
            "options": None if self.options is None else list(self.options),
            "created_at": self.created_at.isoformat(),
            "settled_at": self.settled_at.isoformat() if self.settled_at else None,
            "answer": self.answer,
        }


class PauseSettlementError(HostError, RuntimeError):
    """Base class for refusals to settle a durable pause occurrence.

    Every subclass is raised BEFORE any write: a refused answer never
    consumes the occurrence, so the slot the caller observed stays exactly
    as it was.

    These refusals reach callers through ``RunHomeClient.answer``, so they
    are ``HostError``\\ s: one ``except HostError`` around client calls
    catches every durable-host refusal. ``RuntimeError`` is kept in the
    bases so existing ``except RuntimeError`` clauses still match.
    """


class AnswerRejectedError(PauseSettlementError):
    """The answer did not name an answerable occurrence, or failed its schema.

    Raised for a missing/unknown ``pause_id``, a run with no durable pause, a
    run that is no longer paused, and a value that fails the slot's
    ``answer_schema``. The current slot stays open — the caller may correct
    the value and answer the same occurrence again.
    """

    def __init__(self, run_id: str, message: str, *, pause_id: str | None = None, issues: tuple[str, ...] = ()) -> None:
        self.run_id = run_id
        self.pause_id = pause_id
        self.issues = issues
        super().__init__(message)


class PauseAlreadySettledError(PauseSettlementError):
    """This exact occurrence was already answered; the first value wins.

    A durable pause is settled once. The second caller learns that the
    decision is already made rather than silently overwriting it.
    """

    def __init__(self, run_id: str, pause_id: str, message: str) -> None:
        self.run_id = run_id
        self.pause_id = pause_id
        super().__init__(message)


class StalePauseError(PauseSettlementError):
    """The named occurrence has been superseded by a later pause.

    A loop that pauses twice produces two occurrences. An answer armed
    against the first one must never settle the second — it is a different
    question.
    """

    def __init__(self, run_id: str, pause_id: str, current_pause_id: str, message: str) -> None:
        self.run_id = run_id
        self.pause_id = pause_id
        self.current_pause_id = current_pause_id
        super().__init__(message)


#: Fact kinds the FRAMEWORK writes to a run's durable log, and to the Batch
#: stream a consumer usually reads beside it. A node's own ``ctx.record``
#: refuses these, so one flat vocabulary stays unambiguous: a consumer that
#: switches on ``kind`` can trust that ``step`` or ``child_settled`` means
#: what the framework says it means and was never authored by user code.
#:
#: Adding a framework kind means adding it here — ``tests/test_host``
#: asserts the host's real vocabularies are covered.
RESERVED_FACT_KINDS: frozenset[str] = frozenset(
    {
        # run_updates (host/views.py RunUpdate)
        "submitted",
        "run_started",
        "step",
        "status",
        "command",
        "answer",
        "recovery_exhausted",
        "dead_lettered",
        "run_reset",
        # batch_updates (host/_batch_store.py)
        "manifest",
        "child_settled",
        "child_paused",
        "child_runnable",
        "tolerance_tripped",
        "child_unstarted",
        "child_abandoned",
    }
)


class ReservedFactKindError(HostError, ValueError):
    """A node tried to record a fact under a framework-owned ``kind``.

    Raised by ``NodeContext.record`` BEFORE any write, and raised wherever
    the node runs — under a durable host or in-process with no store at
    all — because a kind collision is a coding mistake, not a deployment
    difference. ``kind`` is otherwise a free string the node owns.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(
            f"record() kind {kind!r} is reserved by the framework.\n\n"
            f"How to fix: pick a name the framework does not write — {kind!r} already means something "
            "specific to every watcher of this run.\n"
            f"  Reserved: {', '.join(sorted(RESERVED_FACT_KINDS))}"
        )


class AttemptStatus(Enum):
    """Status of one callable invocation inside an attempt series.

    ``STARTED`` is a durable reservation, not an outcome. A crash-stranded
    ``STARTED`` row is settled to ``OUTCOME_UNKNOWN`` on resume — never
    invented as cancelled or never-run, because external side effects may
    have completed.
    """

    STARTED = "started"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SUCCEEDED = "succeeded"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"


#: Attempt statuses that settle an attempt (everything except the reservation).
TERMINAL_ATTEMPT_STATUSES = frozenset(
    {
        AttemptStatus.FAILED,
        AttemptStatus.TIMED_OUT,
        AttemptStatus.SUCCEEDED,
        AttemptStatus.CANCELLED,
        AttemptStatus.OUTCOME_UNKNOWN,
    }
)


class AttemptLedgerError(RuntimeError):
    """A durable attempt-ledger invariant was violated.

    Raised for fingerprint mismatches, exhausted budgets, elapsed deadlines,
    double-open series, and invalid close linkage. Reservation/outcome
    persistence failures propagate as their original exception instead.
    """


@dataclass(frozen=True)
class AttemptError:
    """Privacy-safe durable error projection: type name, no message text.

    Attempt records are durable content, so the projection follows the #233
    privacy boundary: exception type identity only — no args tuple, no stack
    trace, no ``repr`` of user values, and no ``str(exc)`` message text
    (raw messages can embed secrets). The ``message`` field is retained for
    schema stability and stays empty for framework-created records; the exact
    exception object remains available on local surfaces at failure time.
    """

    type_name: str
    message: str = ""

    @classmethod
    def from_exception(cls, error: BaseException) -> AttemptError:
        """Project an exception into its privacy-safe durable form."""
        error_type = type(error)
        if error_type.__module__ in ("builtins", "__main__"):
            type_name = error_type.__qualname__
        else:
            type_name = f"{error_type.__module__}.{error_type.__qualname__}"
        return cls(type_name=type_name, message="")


@dataclass(frozen=True)
class AttemptSeries:
    """Durable retry budget for one logical node execution.

    The series id is stable across scheduler/superstep drift: a resumed node
    that lands on a different superstep continues the SAME series. A series
    is open while ``closed_at`` is None; open series are never pruned.
    ``committed_superstep`` is set when the series closes with its linked
    :class:`StepRecord`.
    """

    id: str
    run_id: str
    node_name: str
    policy_fingerprint: str
    max_attempts: int
    opened_at: datetime
    deadline_at: datetime | None = None
    committed_superstep: int | None = None
    closed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def __repr__(self) -> str:
        state = "open" if self.is_open else "closed"
        return f"AttemptSeries {self.id} | {self.run_id}/{self.node_name} | {state} | max_attempts={self.max_attempts}"


@dataclass(frozen=True)
class AttemptRecord:
    """One durable callable invocation (or reservation) within a series.

    ``attempt_number`` is one-based. ``retry_not_before`` and
    ``sampled_delay`` persist a once-sampled backoff decision as data so a
    restart neither redraws jitter nor restarts the full delay.

    ``deadline_elapsed`` and ``cancellation_requested`` are independent,
    witnessed facts. The settled ``status`` supplies the third fact; no field
    claims that arbitrary user work or external side effects stopped.
    """

    series_id: str
    attempt_number: int
    scheduled_superstep: int
    status: AttemptStatus
    started_at: datetime
    completed_at: datetime | None = None
    error: AttemptError | None = None
    retry_not_before: datetime | None = None
    sampled_delay: float | None = None
    deadline_elapsed: bool = False
    cancellation_requested: bool = False

    def __repr__(self) -> str:
        parts = [f"Attempt #{self.attempt_number}", self.status.value, f"superstep {self.scheduled_superstep}"]
        if self.error is not None:
            parts.append(f"error: {self.error.type_name}")
        return " | ".join(parts)


class WorkflowStatus(Enum):
    """Status of a run (kept as WorkflowStatus to avoid collision with runners.RunStatus)."""

    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"
    PARTIAL = "partial"
    COMPLETED = "completed"
    FAILED = "failed"


def fold_producers(
    folded: Iterable[tuple[str, StepStatus, tuple[str, ...] | None]],
    *,
    carrier_node_name: str,
) -> tuple[str, ...] | None:
    """THE rule for a retention carrier's producer provenance (#277).

    Both checkpointer backends call this instead of deriving it themselves,
    so memory and SQLite can never disagree about what a baseline claims.

    ``folded`` is the step rows this compaction pass is about to delete, in
    fold order, as ``(node_name, status, folded_producers)``. A COMPLETED row
    contributes its own node name — the only status that carries values, and
    the only one that is completion evidence. A PAUSED or FAILED row is an
    attempt, not a completion, and contributes nothing.

    Fold order is the caller's, and the two backends reach it differently:
    memory sorts with ``_step_sort_key`` while SQLite orders in SQL by
    ``_STEP_TIME_ORDER``. Both are (completed-or-created time, created time,
    row identity), so they agree except on rows sharing a timestamp, where the
    tiebreak is the step index versus the row id. Read the result as a SET of
    producers; the order is for a human reading a carrier, not a contract.

    A previous carrier being re-folded contributes the producers IT recorded,
    never its own carrier name, so provenance survives repeated compaction.
    If that carrier has none (``None`` — written before this field existed),
    the whole result is ``None``: partial provenance would read as "these and
    no others", which is exactly the false evidence this field exists to
    prevent.

    Returns:
        Distinct producer names in fold order, or ``None`` when a folded
        legacy carrier makes the provenance incomplete.
    """
    producers: list[str] = []
    seen: set[str] = set()
    for node_name, status, carried in folded:
        if node_name == carrier_node_name:
            if carried is None:
                return None
            names: tuple[str, ...] = carried
        elif status is StepStatus.COMPLETED:
            names = (node_name,)
        else:
            continue
        for producer in names:
            if producer not in seen:
                seen.add(producer)
                producers.append(producer)
    return tuple(producers)


@dataclass(frozen=True)
class StepRecord:
    """Single atomic record of a node execution.

    Contains both metadata and output values. The checkpointer
    saves each StepRecord atomically — either all data is saved
    or nothing.

    ``folded_producers`` is meaningful only on a retention-compaction carrier
    (see :func:`fold_producers`): it names the nodes whose completed step
    records the carrier folded, so a reader can tell "this value came from
    that node" apart from "some node produced a value of the same name".
    ``None`` on every ordinary step, and on a carrier written before the
    field existed — legacy provenance is absent, not empty.
    """

    run_id: str
    superstep: int
    node_name: str
    index: int
    status: StepStatus
    input_versions: dict[str, int]
    values: dict[str, Any] | None = None
    duration_ms: float = 0.0
    cached: bool = False
    decision: str | list[str] | None = None
    error: str | None = None
    node_type: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    completed_at: datetime | None = None
    child_run_id: str | None = None
    partial: bool = False
    attempt_series_id: str | None = None
    folded_producers: tuple[str, ...] | None = None

    def __repr__(self) -> str:
        status = "cached" if self.cached else self.status.value
        parts = [f"Step [{self.index}] {self.node_name}", status]
        if self.duration_ms > 0:
            parts.append(format_duration_ms(self.duration_ms))
        parts.append(f"superstep {self.superstep}")
        if self.error:
            parts.append(f"error: {self.error[:60]}")
        return " | ".join(parts)

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import plain_reprs
        from hypergraph.checkpointers.presenters import render_step_record_html

        if plain_reprs():
            return None
        return render_step_record_html(self)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict with only primitive types."""
        return {
            "run_id": self.run_id,
            "superstep": self.superstep,
            "node_name": self.node_name,
            "index": self.index,
            "status": self.status.value,
            "input_versions": self.input_versions,
            "values": self.values,
            "duration_ms": self.duration_ms,
            "cached": self.cached,
            "decision": self.decision,
            "error": self.error,
            "node_type": self.node_type,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "child_run_id": self.child_run_id,
            "partial": self.partial,
            "attempt_series_id": self.attempt_series_id,
            "folded_producers": None if self.folded_producers is None else list(self.folded_producers),
        }


@dataclass
class Run:
    """Run metadata record.

    ``pause_slot`` carries the run's most recent durable interrupt occurrence
    and is populated by the single-run reads (``get_run_async`` /
    ``get_run``); list views leave it ``None`` — read a specific run's slot
    with ``get_pause_slot(run_id)``.
    """

    id: str
    status: WorkflowStatus
    graph_name: str | None = None
    duration_ms: float | None = None
    node_count: int = 0
    error_count: int = 0
    parent_run_id: str | None = None
    forked_from: str | None = None
    fork_superstep: int | None = None
    retry_of: str | None = None
    retry_index: int | None = None
    config: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=_utcnow)
    completed_at: datetime | None = None
    pause_slot: PauseSlot | None = None

    def __repr__(self) -> str:
        parts = [f"Run: {self.id}"]
        if self.graph_name:
            parts[0] += f" ({self.graph_name})"
        parts.append(self.status.value)
        if self.duration_ms is not None:
            parts.append(format_duration_ms(self.duration_ms))
        items = []
        if self.node_count:
            items.append(plural(self.node_count, "step"))
        if self.error_count:
            items.append(plural(self.error_count, "error"))
        if self.retry_of:
            retry_num = f"#{self.retry_index}" if self.retry_index is not None else ""
            items.append(f"retry{retry_num} of {self.retry_of}")
        elif self.forked_from:
            at = f"@{self.fork_superstep}" if self.fork_superstep is not None else ""
            items.append(f"fork of {self.forked_from}{at}")
        if items:
            parts.append(", ".join(items))
        return " | ".join(parts)

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import plain_reprs
        from hypergraph.checkpointers.presenters import render_run_html

        if plain_reprs():
            return None
        return render_run_html(self)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable dict."""
        return {
            "id": self.id,
            "status": self.status.value,
            "graph_name": self.graph_name,
            "duration_ms": self.duration_ms,
            "node_count": self.node_count,
            "error_count": self.error_count,
            "parent_run_id": self.parent_run_id,
            "forked_from": self.forked_from,
            "fork_superstep": self.fork_superstep,
            "retry_of": self.retry_of,
            "retry_index": self.retry_index,
            "config": self.config,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "pause_slot": self.pause_slot.to_dict() if self.pause_slot is not None else None,
        }


@dataclass
class Checkpoint:
    """Point-in-time snapshot for forking runs.

    Combines accumulated state and step history at a given superstep.
    """

    values: dict[str, Any]
    steps: list[StepRecord]
    source_run_id: str | None = None
    source_superstep: int | None = None
    retry_of: str | None = None
    retry_index: int | None = None

    def __repr__(self) -> str:
        origin = ""
        if self.source_run_id:
            at = f"@{self.source_superstep}" if self.source_superstep is not None else ""
            origin = f" from {self.source_run_id}{at}"
        return f"Checkpoint{origin}: {plural(len(self.values), 'value')}, {plural(len(self.steps), 'step')}"

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import plain_reprs
        from hypergraph.checkpointers.presenters import render_checkpoint_html

        if plain_reprs():
            return None
        return render_checkpoint_html(self)


class RunTable(list):
    """List[Run] with table display in notebooks and REPL.

    Extends list for full backward compatibility — all list operations
    (len, iter, indexing, slicing) work as expected.

    When created by the checkpointer widget, ``_steps_by_run`` maps
    ``run_id → StepTable`` so each run trace can inline its steps.
    """

    _steps_by_run: dict[str, StepTable]

    def __init__(self, items: Any = (), *, steps_by_run: dict[str, Any] | None = None):
        super().__init__(items)
        self._steps_by_run = steps_by_run or {}

    def __repr__(self) -> str:
        if not self:
            return "RunTable: (empty)"
        lines = [f"RunTable: {plural(len(self), 'run')}", ""]
        for run in self:
            lines.append(f"  {run!r}")
        return "\n".join(lines)

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import plain_reprs
        from hypergraph.checkpointers.presenters import render_run_table_html

        if plain_reprs():
            return None
        return render_run_table_html(self)


class StepTable(list):
    """List[StepRecord] with table display in notebooks and REPL.

    Extends list for full backward compatibility.
    """

    def __repr__(self) -> str:
        if not self:
            return "StepTable: (empty)"
        lines = [f"StepTable: {plural(len(self), 'step')}", ""]
        for step in self:
            lines.append(f"  {step!r}")
        return "\n".join(lines)

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import plain_reprs
        from hypergraph.checkpointers.presenters import render_step_table_html

        if plain_reprs():
            return None
        return render_step_table_html(self)


@dataclass(frozen=True)
class LineageRow:
    """One row in a fork lineage tree."""

    lane: str
    run: Run
    depth: int
    is_selected: bool = False


class LineageView(list):
    """Git-like lineage visualization for workflow forks.

    A lineage view is anchored at the root ancestor of a workflow and contains
    all fork descendants in tree order. Each row has a lane prefix similar to
    ``git log --graph`` output.
    """

    def __init__(
        self,
        rows: list[LineageRow],
        *,
        selected_run_id: str,
        root_run_id: str,
        steps_by_run: dict[str, StepTable] | None = None,
    ):
        super().__init__(rows)
        self.selected_run_id = selected_run_id
        self.root_run_id = root_run_id
        self.steps_by_run = steps_by_run or {}

    def __repr__(self) -> str:
        if not self:
            return "LineageView: (empty)"

        lines = [f"LineageView: {self.selected_run_id} (root={self.root_run_id})", ""]
        for row in self:
            run = row.run
            marker = " <selected>" if row.is_selected else ""
            status = run.status.value
            kind = "retry" if run.retry_of else ("fork" if run.forked_from else "root")
            summary = f"{row.lane}{run.id} [{status}] ({kind})"
            if run.forked_from:
                at = f"@{run.fork_superstep}" if run.fork_superstep is not None else ""
                summary += f" <- {run.forked_from}{at}"
            if self.steps_by_run and run.id in self.steps_by_run:
                steps = self.steps_by_run[run.id]
                cached = sum(1 for s in steps if s.cached)
                failed = sum(1 for s in steps if s.status == StepStatus.FAILED)
                summary += f" | steps={len(steps)} cached={cached} failed={failed}"
            lines.append(summary + marker)
        return "\n".join(lines)

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import plain_reprs
        from hypergraph.checkpointers.presenters import render_lineage_view_html

        if plain_reprs():
            return None
        return render_lineage_view_html(self)
