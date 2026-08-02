"""Read models for the durable host.

Views report persisted facts only. ``waiting`` names a coordination
condition — it is never a ``WorkflowStatus`` and never enters the run row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from hypergraph.checkpointers.types import WorkflowStatus
from hypergraph.host.definition import DefinitionId
from hypergraph.host.refs import BatchRef, RunRef

# Terminal run statuses (mirrors the checkpointer's completed_at semantics).
TERMINAL_WORKFLOW_STATUSES: frozenset[WorkflowStatus] = frozenset(
    {
        WorkflowStatus.COMPLETED,
        WorkflowStatus.FAILED,
        WorkflowStatus.PARTIAL,
        WorkflowStatus.STOPPED,
    }
)
# The same vocabulary as stored strings, for the store's own row values.
TERMINAL_STATUS_VALUES: frozenset[str] = frozenset(status.value for status in TERMINAL_WORKFLOW_STATUSES)

# Submission states in which the host will never touch a submission again:
# 'finished' (terminal run, stop-before-start, or a tolerance trip that
# closed admission) and 'exhausted' (parked by the recovery brake).
SUBMISSION_STATE_FINISHED = "finished"
SUBMISSION_STATE_EXHAUSTED = "exhausted"
# A run the worker released because it PAUSED on a durable interrupt. The
# worker is done with it (it holds no active-Run slot) but the run itself
# is not: a human answer is outstanding and the runs row is nonterminal.
# Deliberately NOT settled — writing 'finished' here would make the same
# child 'active' to the bucket ladder and settled to `is_child_settled`,
# ending `watch()` while a decision was still open.
SUBMISSION_STATE_PAUSED = "paused"
SETTLED_SUBMISSION_STATES: frozenset[str] = frozenset({SUBMISSION_STATE_FINISHED, SUBMISSION_STATE_EXHAUSTED})


def is_child_settled(submission_state: str | None, run_status: str | None) -> bool:
    """True when a Batch child can never change outcome again.

    THE settled-child rule. Every caller — the rerun gate, ``BatchView``,
    Batch stop, and Batch-owned workflow-id reuse — routes through this one
    predicate so a child is never settled for one and in flight for another.
    Both arguments are stored row values (``host_submissions.state`` and
    ``runs.status``), not enums, because the store is what they compare.

    A ``paused`` submission is deliberately NOT settled: its run parked on
    a durable interrupt and can still change outcome once answered.

    Args:
        submission_state: The child's ``host_submissions.state``, or None
            when no submission row exists.
        run_status: The child's ``runs.status`` value, or None when the
            child has no runs row yet.

    Returns:
        True when the child reached a terminal run status or its submission
        is settled (finished or recovery-exhausted).
    """
    return run_status in TERMINAL_STATUS_VALUES or submission_state in SETTLED_SUBMISSION_STATES


def is_child_resting(submission_state: str | None, run_status: str | None) -> bool:
    """True when a child can no longer move WITHOUT a person.

    THE resting rule, and deliberately weaker than settled: a child parked
    on a durable interrupt will never advance on its own, so a caller
    waiting for work to stop moving must count it as arrived. Otherwise one
    human gate holds every wait loop open forever — which is exactly what a
    770-document re-ingest hit (issue #386), and why "settled" and "resting"
    are two predicates rather than one.

    Resting is NOT settled: a parked child can still change outcome once
    somebody answers. Nothing derives an outcome from this predicate; it
    answers only "is anything still running or queued?".
    """
    return is_child_settled(submission_state, run_status) or submission_state == SUBMISSION_STATE_PAUSED


class WaitingCondition(Enum):
    """Closed typed vocabulary naming why a Run waits.

    A waiting condition is a coordination fact, never a ``WorkflowStatus``.
    ``None`` on ``RunView.waiting`` means the Run is executing or terminal.

    ``ADMISSION_LIMITED`` names HOST work admission only — the Run Home's
    ``max_active_runs`` cap on Runs one worker executes at once. A Run
    parked on an injected provider permit is executing, holds its Host
    slot, and reports ``None``: provider-resource admission is a different
    control and never appears in this vocabulary.
    """

    QUEUED = "queued"  # eligible, awaiting claim
    SCHEDULED = "scheduled"  # future start_at
    PAUSED = "paused"  # durable pause slot open
    VERSION_INCOMPATIBLE = "version_incompatible"  # no serving worker
    ADMISSION_LIMITED = "admission_limited"  # over the active-Run cap
    RECOVERY_EXHAUSTED = "recovery_exhausted"  # pinned recovery cap hit


@dataclass(frozen=True)
class RunUpdate:
    """One update observed through ``RunHomeClient.watch``.

    Attributes:
        cursor: Reconnectable cursor string (``"seq:N"``). Only durable
            updates advance it; live previews repeat the last durable cursor.
        durable: True for committed Run Home facts; False for best-effort
            live previews. Callers must only store cursors from durable
            updates.
        kind: Fact kind — ``submitted``, ``run_started``, ``step``,
            ``status``, ``command``, ``answer``, ``recovery_exhausted``,
            ``run_reset`` — or an event class name for previews. A
            ``status`` fact for a pause also carries the ``pause_id`` it
            committed with.
        payload: JSON-safe fact payload. A ``command`` fact names its
            ``verb`` (``stop`` or ``schedule_answer``) and carries the
            accepting caller's opaque ``source_ref`` — audit provenance
            only, never authentication and never part of dedup. A
            ``schedule_answer`` fact also carries its ``pause_id``,
            ``due_at``, and an ``outcome``: None while the timer is armed,
            and one of ``settled``/``already_settled``/``superseded``/
            ``rejected`` on the second fact that commits when it fires or is
            voided. A fired timer is a recorded state change, so the stream
            alone tells a detached ``watch`` consumer the timer's fate — no
            store query needed.
        timestamp: ISO timestamp of the fact (or of preview observation).
    """

    cursor: str
    durable: bool
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""


@dataclass(frozen=True)
class RunView:
    """Persisted facts about one Run, plus why it waits.

    Attributes:
        run_ref: Inert address of the run.
        workflow_id: The run's workflow id (same as ``run_ref.run_id``).
        definition_name: Definition the run was submitted against.
        status: The run's ``WorkflowStatus``, or None while no runs row
            exists yet (submission still pending).
        waiting: Typed waiting condition, or None: ``QUEUED`` (accepted,
            execution not started), ``SCHEDULED`` (future ``start_at``),
            ``PAUSED`` (runs row paused), ``VERSION_INCOMPATIBLE`` (no
            serving worker claims the pinned identity),
            ``ADMISSION_LIMITED`` (due and claimable, but the Home's
            stored ``max_active_runs`` has no free slot), and
            ``RECOVERY_EXHAUSTED`` (the pinned recovery cap tripped).
            Never a WorkflowStatus.
        definition_id: The pinned Definition identity from the submission,
            or reconstructed from the runs row for host-less (Tier 0) runs.
            None only when neither exists.
        retry_of: Source workflow id when this run is a rerun (repetition),
            else None. Lineage never merges: when ``retry_of`` is set,
            ``forked_from`` is None.
        forked_from: Source workflow id when this run is a fork (migration),
            else None. Taken from the runs row when present, else the
            submission's recorded lineage.
        created_at: When the runs row was created, or None before execution
            starts.
        completed_at: When the run reached a terminal status, else None.
    """

    run_ref: RunRef
    workflow_id: str
    definition_name: str
    status: WorkflowStatus | None
    waiting: WaitingCondition | None
    definition_id: DefinitionId | None
    retry_of: str | None
    forked_from: str | None
    created_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True)
class RunQuery:
    """Typed filter for ``RunHomeClient.list``.

    Every field is a typed value — never a free string — so queries branch
    on the same closed vocabulary views report. Omitted fields match
    everything.

    Attributes:
        definition: Restrict to one Definition name.
        status: Restrict to one ``WorkflowStatus`` (runs row required).
        waiting: Restrict to one ``WaitingCondition`` (the same typed
            waiting computation ``RunView.waiting`` uses).
        older_than: Restrict to work created at least this long ago
            (aged-unclaimed and backlog queries).
        limit: Maximum views returned, newest first. Defaults to 100.
        batch: Restrict to children of one Batch — a ``BatchRef`` or a bare
            batch id string. Runs without Batch membership never match.
    """

    definition: str | None = None
    status: WorkflowStatus | None = None
    waiting: WaitingCondition | None = None
    older_than: timedelta | None = None
    limit: int = 100
    batch: BatchRef | str | None = None


# THE Batch-level outcome name for a child parked by the recovery brake.
# It is not a WorkflowStatus — the child's run never reached one — so the
# view and the durable stream must agree on one string: BatchView.outcomes
# reports it and the child_settled fact carries it as its status.
BATCH_OUTCOME_RECOVERY_EXHAUSTED = "recovery_exhausted"

# THE Batch-level outcome for a child a tolerance trip closed admission on
# AFTER it had started. Terminal and settled like the brake outcome above,
# but it names the tolerance decision, not the recovery brake — and it is
# emphatically not "unstarted": this child committed steps.
BATCH_OUTCOME_ABANDONED = "abandoned"

# Closed bucket vocabulary for BatchView.counts. Every manifest item is
# accounted in exactly one bucket; terminal buckets are WorkflowStatus
# values so child outcomes share the Run vocabulary.
#
# ``paused`` and ``active`` are disjoint on purpose: a child parked on a
# human decision is NOT running. Counting it as active told an operator
# "N items are being worked on" when the true answer was "N items are
# waiting for you", and made human response time look like throughput.
#
# ``unstarted`` and ``abandoned`` are disjoint for the same kind of reason.
# A tolerance trip closes admission on every pending child at once, but an
# item that never began is safe to rerun from scratch, while one that began
# may have landed side effects already. One bucket for both would tell an
# operator nothing happened when something did.
BATCH_COUNT_KEYS: tuple[str, ...] = (
    "completed",
    "failed",
    "partial",
    "stopped",
    "active",
    "paused",
    "queued",
    "recovery_exhausted",
    "unstarted",
    "abandoned",
)


@dataclass(frozen=True)
class BatchItemView:
    """One logical Batch item: its address, its truth, and whether it ran.

    The unit an item-scoped operator surface acts on. It carries the
    child's inert ``RunRef`` so ``client.get``/``answer``/``get_run_slot``/
    ``watch``/``stop``/``rerun`` all work per item without anyone deriving
    a ``<batch workflow id>:<item key>`` string — child id syntax is Run
    Home implementation detail, not a consumer contract.

    Attributes:
        item_key: The logical item key from the immutable manifest.
        run_ref: Inert address of this item's independent child Run.
        workflow_id: The child's workflow id (same as ``run_ref.run_id``).
        status: The child's ``WorkflowStatus``, or None while it has no
            runs row (never started).
        waiting: Typed waiting condition, or None — the same closed
            ``WaitingCondition`` vocabulary ``RunView.waiting`` reports, so
            an item and its Run never disagree about why it waits.
        outcome: The item's settled outcome string — a terminal status,
            ``"recovery_exhausted"`` for a child the recovery brake parked,
            or ``"abandoned"`` for a started child a tolerance trip closed
            admission on — and None while it can still change. Exactly the
            value ``BatchView.outcomes`` reports for this key.
        started: Whether this child ever began executing (it has a runs
            row). False distinguishes "requested but never admitted" from
            "ran and produced nothing".

    It deliberately carries no graph output values: results are Run truth,
    read through the child's own ``RunRef``.
    """

    item_key: str
    run_ref: RunRef
    workflow_id: str
    status: WorkflowStatus | None
    waiting: WaitingCondition | None
    outcome: str | None
    started: bool

    def __repr__(self) -> str:
        return " | ".join([f"BatchItem: {self.item_key}", item_condition(self), self.workflow_id])

    def _repr_html_(self) -> str | None:
        from hypergraph._repr import plain_reprs
        from hypergraph.host.presenters import render_batch_item_html

        if plain_reprs():
            return None
        return render_batch_item_html(self)


def item_condition(item: BatchItemView) -> str:
    """One phrase answering "where is this item?", for both reprs.

    The fields are deliberately not equal in weight, so reading them in
    priority order is what makes a one-line repr honest:

    1. A settled ``outcome`` is final — nothing else can still change.
    2. A ``waiting`` condition is why an unsettled item is not progressing,
       which is the thing an operator can actually act on.
    3. A bare ``status`` means it is executing right now.
    4. None of the three means the item has no runs row and nothing is
       waiting on it — it was accepted and settled without ever executing.
       ``BatchView`` calls that ``unstarted``, and so does this, because an
       item and the Batch that holds it must not name one state two ways.
    """
    if item.outcome is not None:
        return item.outcome
    if item.waiting is not None:
        return f"waiting: {item.waiting.value}"
    if item.status is not None:
        return item.status.value
    return "unstarted"


@dataclass(frozen=True)
class BatchView:
    """Persisted facts about one Batch, keyed by logical item key.

    Attributes:
        batch_ref: Inert address of the Batch.
        workflow_id: The Batch's caller-chosen workflow id.
        definition_id: The pinned Definition identity from the manifest.
        counts: Children per state bucket over the closed
            ``BATCH_COUNT_KEYS`` vocabulary (all keys always present):
            terminal buckets (``completed``/``failed``/``partial``/
            ``stopped``) from the child runs row; ``active`` for a child a
            worker has claimed and is executing; ``paused`` for a child
            parked on a human answer (never ``active`` — it holds no
            active-Run slot); ``queued`` for a child awaiting claim,
            including one whose answer just made it runnable again;
            ``recovery_exhausted`` for a parked child; ``unstarted`` for a
            child that finished without ever executing (stop-before-start,
            or closed admission it never reached); and ``abandoned`` for one
            that HAD started when closed admission settled it.
            Every manifest item is accounted exactly once.
        items: Logical item key → ``BatchItemView``, in manifest order.
            The item-scoped surface: each entry carries the child's inert
            ``RunRef`` and current truth.
        outcomes: Logical item key → outcome, in manifest order: the
            terminal status string for settled children,
            ``"recovery_exhausted"`` for parked children, ``"abandoned"``
            for a started child a tolerance trip closed admission on, None
            while a child is in flight, and None for unstarted items
            (Hypergraph never fabricates results for items that never ran).
        unstarted_items: Manifest keys whose child never executed, in
            manifest order — requested but never admitted. Safe to rerun
            from scratch: nothing ran, so nothing landed.
        abandoned_items: Manifest keys whose child HAD started when a
            tolerance trip closed admission, in manifest order. These
            committed steps and may have landed side effects, so they are
            the items an operator has to reconcile before rerunning —
            which is exactly why they are not called unstarted.
        settled: True when no child is active, paused, or queued (terminal,
            unstarted, abandoned, and recovery-exhausted children are
            settled). A paused child is deliberately NOT settled: its
            question is open and its outcome can still change.
        tolerance_tripped: True when failure-equivalent children strictly
            exceeded a pinned tolerance, closing new child admission. A
            trip is a Batch fact, never a ``WorkflowStatus``: the Batch
            stays truthfully partial — mixed outcomes with the remaining
            items explicitly unstarted, never a failed or stopped Batch.
        retry_of: Source ``batch_id`` when this Batch was minted by
            ``client.rerun(batch_ref, ...)``, else None. The source Batch
            is never mutated; lineage points backwards only.
    """

    batch_ref: BatchRef
    workflow_id: str
    definition_id: DefinitionId
    counts: dict[str, int]
    items: dict[str, BatchItemView]
    outcomes: dict[str, str | None]
    unstarted_items: tuple[str, ...]
    abandoned_items: tuple[str, ...]
    settled: bool
    tolerance_tripped: bool
    retry_of: str | None

    @property
    def resting(self) -> bool:
        """True when nothing is running or queued — only terminal or parked.

        The predicate a caller polls when a Batch may legitimately stop
        moving without being finished: every item is accounted for good, or
        parked on a person. ``settled`` is the stricter question — it also
        requires that no question is open.

        Derived from the closed ``BATCH_COUNT_KEYS`` vocabulary, which
        accounts every manifest item exactly once, so this can never
        disagree with ``counts``.
        """
        return self.counts["active"] == 0 and self.counts["queued"] == 0


@dataclass(frozen=True)
class BatchUpdate:
    """One update observed through ``RunHomeClient.watch(batch_ref)``.

    Attributes:
        cursor: Reconnectable cursor string (``"bseq:N"``). Only durable
            updates advance it; live previews repeat the last durable cursor.
        durable: True for committed Run Home facts; False for best-effort
            live previews fanned in from child runs (same process only).
            Callers must only store cursors from durable updates.
        kind: Fact kind — ``manifest`` (bseq 1, the accepted start intent),
            ``child_paused`` (a child committed a durable pause and is
            waiting on a human), ``child_runnable`` (that occurrence was
            answered and the child re-entered claim order),
            ``child_settled`` (a child settled for good: a terminal run
            transition, or the recovery brake parking it), ``tolerance_tripped``
            (a pinned tolerance was strictly exceeded, committed in that
            same transaction at the next ``bseq``), ``child_unstarted`` (an
            item that ended unstarted without the trip fact naming it — a
            stopped Batch's child that never executed, or a child a crash
            returned to pending after the trip), ``child_abandoned`` (the
            other half of that split: a child that HAD started when closed
            admission settled it), ``stopped`` (the durable Batch stop) —
            or an event class name for previews. Every one of these commits
            in the same transaction as the child or Batch state change that
            causes it.
        payload: JSON-safe fact payload. ``child_settled`` carries
            ``item_key``, ``workflow_id``, and ``status`` — a terminal
            ``WorkflowStatus`` value, or ``"recovery_exhausted"`` for a
            parked child, exactly the string ``BatchView.outcomes`` reports;
            ``child_paused`` and ``child_runnable`` carry ``item_key``,
            ``workflow_id``, an inert ``run_ref`` dict, and the ``pause_id``
            of the occurrence, so a consumer addresses the item without
            parsing child workflow-id syntax and can tell one loop turn from
            the next; ``tolerance_tripped`` carries ``failed``,
            ``total_items``, the pinned ``max_failed``/``max_failed_percent``,
            and both the ``unstarted_items`` and ``abandoned_items``
            admission closed; ``child_unstarted`` and ``child_abandoned``
            each carry ``item_key`` and ``workflow_id``. Between them, the
            durable stream accounts every manifest item exactly once — a
            detached ``watch`` never needs the view to learn an outcome.
            ``child_paused``/``child_runnable`` are state facts, not
            accounting facts: they never settle an item, and a loop that
            pauses again earns a fresh pair under a new ``pause_id``.
        timestamp: ISO timestamp of the fact (or of preview observation).
    """

    cursor: str
    durable: bool
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""


@dataclass(frozen=True)
class RunFailure:
    """Why a settled Run failed, from durable evidence only.

    ``error`` is the privacy-safe projection the step persisted — an
    exception type name, a stable ``HG_*`` diagnostic code, and static
    wording. It is never raw exception message text: Hypergraph does not
    persist that (see the privacy boundary in
    ``docs/06-api-reference/errors.md``). The real message, type and
    traceback are exported to the OpenTelemetry trace instead, so a failure
    is debugged there and merely *identified* here.

    Attributes:
        error: Privacy-safe error projection from the first failed step.
        node_name: Node that raised it, when a step recorded one.
        superstep: Superstep the failure happened in, when recorded.
    """

    error: str
    node_name: str | None = None
    superstep: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict of primitives."""
        return {"error": self.error, "node_name": self.node_name, "superstep": self.superstep}


@dataclass(frozen=True)
class RunOutcome:
    """What a Run durably produced — outputs or failure — with no graph code.

    Read with ``RunHomeClient.result(run_ref)``. The host worker keeps no
    ``RunResult``, so this is reconstructed from the same checkpointer rows
    that back resume: the run's steps folded in execution order.

    Four states a caller must be able to tell apart, and how each reads:

    ==============================  ========  =========  ==========  =======
    situation                       result()  ``started``  ``settled``  ``outputs``
    ==============================  ========  =========  ==========  =======
    never submitted here            ``None``  --         --          --
    submitted, still in flight      outcome   any        False       ``None``
    stopped/closed before starting  outcome   False      True        ``None``
    ran and produced nothing        outcome   True       True        ``{}``
    ==============================  ========  =========  ==========  =======

    So ``outputs is None`` never means "produced nothing" — that is ``{}``.

    Two honest caveats:

    * ``outputs`` is the run's FOLDED STEP OUTPUTS, not a projection
      narrowed to the graph's declared outputs. Narrowing needs the
      ``Graph`` object, and this client is graph-free by contract, so it
      reports every value the run's steps produced rather than guessing.
    * The values are JSON-safe because the Run Home's checkpointer defaults
      to ``JsonSerializer``, which round-trips through ``json``. A Home
      explicitly configured with ``PickleSerializer`` returns whatever it
      stored, and ``to_dict()`` falls back to ``repr`` for anything that
      will not serialize.

    Attributes:
        run_ref: Inert address of the Run.
        workflow_id: The Run's workflow id (same as ``run_ref.run_id``).
        status: The run's ``WorkflowStatus``, or None while it has no runs
            row (accepted but never started).
        settled: True when the Run can never change outcome again. A paused
            Run is NOT settled — an outstanding answer can still change it.
        started: Whether the Run ever began executing (it has a runs row).
            False separates "requested but never admitted" from "ran and
            produced nothing".
        outputs: Folded step outputs once settled and started, else None.
        failure: Durable failure evidence when the Run failed, else None.
    """

    run_ref: RunRef
    workflow_id: str
    status: WorkflowStatus | None
    settled: bool
    started: bool
    outputs: dict[str, Any] | None = None
    failure: RunFailure | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict of primitives, for transport straight to an API."""
        return {
            "workflow_id": self.workflow_id,
            "status": self.status.value if self.status is not None else None,
            "settled": self.settled,
            "started": self.started,
            "outputs": None if self.outputs is None else json.loads(json.dumps(self.outputs, default=repr)),
            "failure": self.failure.to_dict() if self.failure is not None else None,
        }

    def __repr__(self) -> str:
        status = self.status.value if self.status is not None else ("unstarted" if self.settled else "pending")
        parts = [f"RunOutcome: {self.workflow_id}", status]
        if self.outputs is not None:
            parts.append(f"{len(self.outputs)} values")
        if self.failure is not None:
            parts.append(f"error: {self.failure.error[:60]}")
        return " | ".join(parts)


@dataclass(frozen=True)
class BatchOutcome:
    """Every child's :class:`RunOutcome`, keyed by logical item key.

    Read with ``RunHomeClient.result(batch_ref)``. One read per Batch, not
    one per child: the children's outputs and failures are folded in a
    bounded number of statements, so a 100+ item Batch stays a handful of
    queries.

    An item whose child never executed maps to ``None`` — Hypergraph never
    fabricates a result for work that did not run. That is deliberately
    distinct from a child that ran and produced nothing, which maps to a
    ``RunOutcome`` with ``outputs == {}``.

    Attributes:
        batch_ref: Inert address of the Batch.
        workflow_id: The Batch's caller-chosen workflow id.
        settled: True when no child is active, paused, or queued.
        items: Logical item key → ``RunOutcome``, or None for a child that
            never started, in immutable manifest order.
    """

    batch_ref: BatchRef
    workflow_id: str
    settled: bool
    items: dict[str, RunOutcome | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe dict of primitives, for transport straight to an API."""
        return {
            "workflow_id": self.workflow_id,
            "settled": self.settled,
            "items": {key: None if outcome is None else outcome.to_dict() for key, outcome in self.items.items()},
        }

    def __repr__(self) -> str:
        started = sum(1 for outcome in self.items.values() if outcome is not None)
        return " | ".join(
            [
                f"BatchOutcome: {self.workflow_id}",
                "settled" if self.settled else "in flight",
                f"{started}/{len(self.items)} items ran",
            ]
        )
