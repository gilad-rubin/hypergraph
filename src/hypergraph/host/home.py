"""RunHome — the SQLite Run Home for the durable host (Tier 1).

A RunHome IS the existing SQLite checkpointer plus coordination tables
(schema v6): durable submissions, the per-Run durable update sequence, the
host command channel, and the Home-scoped coordination settings every
process that opens the store agrees on (``max_active_runs``). Steps stay the
sole execution journal; host coordination facts never enter
``RunStatus``/``WorkflowStatus``.

Home-bound runners persist every mutation immediately: the Home forces
checkpoint durability ``"sync"`` and rejects ``"exit"`` policies.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from hypergraph.checkpointers.base import CheckpointPolicy, _check_settlement

# host/ is the same persistence subsystem as checkpointers/ (a RunHome IS a
# SqliteCheckpointer), so reaching its private column list here is deliberate.
from hypergraph.checkpointers.sqlite import _RUNS_COLS, SqliteCheckpointer, _run_status_update
from hypergraph.checkpointers.types import (
    NO_RUN_TOTALS,
    AnswerRejectedError,
    PauseAlreadySettledError,
    StalePauseError,
    WorkflowStatus,
)
from hypergraph.host.batch import BatchTolerance, tolerance_trips
from hypergraph.host.definition import DefinitionId
from hypergraph.host.errors import AlreadyTerminalError, HostError, WorkflowIdConflictError
from hypergraph.host.fingerprint import batch_mismatch_aspect, canonical_json, fingerprint_mismatch_aspect, start_fingerprint
from hypergraph.host.views import (
    BATCH_OUTCOME_RECOVERY_EXHAUSTED,
    SUBMISSION_STATE_EXHAUSTED,
    SUBMISSION_STATE_FINISHED,
    SUBMISSION_STATE_PAUSED,
    TERMINAL_STATUS_VALUES,
    is_child_settled,
)

if TYPE_CHECKING:
    from hypergraph.checkpointers.types import Checkpoint, PauseSlot, Run

logger = logging.getLogger("hypergraph.host")

_SUBMISSION_COLS = (
    "workflow_id, definition_name, def_version, def_struct_hash, inputs_json, "
    "start_at, state, recovery_attempts, recovery_cap, source_ref, created_at, claimed_at, finished_at, "
    "fingerprint, compat_state, retry_of, retry_index, forked_from, fork_reason, last_progress_step_count, batch_id, item_key"
)
_SUBMISSION_PLACEHOLDERS = ", ".join("?" for _ in _SUBMISSION_COLS.split(", "))
_QUALIFIED_SUBMISSION_COLS = ", ".join(f"s.{name}" for name in _SUBMISSION_COLS.split(", "))
_QUALIFIED_RUN_COLS = ", ".join(f"r.{name}" for name in _RUNS_COLS.split(", "))
_TERMINAL_STATUS_VALUES = tuple(sorted(TERMINAL_STATUS_VALUES))
_PROGRESS_STATUSES = frozenset({"paused", *_TERMINAL_STATUS_VALUES})
_CLAIM_BATCH = 16
# THE active-Run count for host work admission: claimed submissions, the
# ones this worker owns end to end. Both the claim gate and the view read
# this same definition so "holds a slot" never means two different things.
_ACTIVE_RUN_COUNT_SQL = "SELECT COUNT(*) FROM host_submissions WHERE state = 'claimed'"
# The active-Run cap is a Home-scoped fact in the store, not a per-instance
# Python attribute: the worker enforcing it and the operator tuning or reading
# it hold different RunHome objects (often in different processes), and a
# process-local cap would make them disagree about the same queue.
_MAX_ACTIVE_RUNS_KEY = "max_active_runs"
_SELECT_SETTING_SQL = "SELECT value FROM host_settings WHERE key = ?"
_UPSERT_SETTING_SQL = (
    "INSERT INTO host_settings (key, value, updated_at) VALUES (?, ?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
)

_BATCH_COLS = (
    "batch_id, workflow_id, definition_name, def_version, def_struct_hash, items_json, "
    "tolerance_json, start_at, fingerprint, source_ref, created_at, retry_of"
)
_BATCH_PLACEHOLDERS = ", ".join("?" for _ in _BATCH_COLS.split(", "))

# Batch update kinds that are matched (not just written) in SQL.
_TRIP_UPDATE_KIND = "tolerance_tripped"
_UNSTARTED_UPDATE_KIND = "child_unstarted"
_SETTLED_UPDATE_KIND = "child_settled"
# The two nonterminal child facts. Unlike the settled/unstarted pair these
# are NOT once-per-item: a loop that pauses again is a new occurrence and
# earns its own pair, which is why both are deduped by ``pause_id`` rather
# than by item key (see ``_last_occurrence_fact``).
_PAUSED_UPDATE_KIND = "child_paused"
_RUNNABLE_UPDATE_KIND = "child_runnable"

# The run's current pause occurrence, for the paths that must decide
# "is this run's open question already answered?" inside their own
# transaction (release-after-pause and worker resume both hinge on it).
_CURRENT_SLOT_SQL = "SELECT pause_id, settled_at, response_key, answer FROM pause_slots WHERE run_id = ? ORDER BY rowid DESC LIMIT 1"

# Failure equivalence (PRD 0019): a child counts toward tolerance when its
# runs row failed or its submission is recovery-exhausted. Paused, queued,
# delayed, admission-limited, and unstarted children never count — and
# neither do partial or stopped runs.
_COUNT_FAILURE_EQUIVALENT = (
    "SELECT COUNT(*) FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id "
    f"WHERE s.batch_id = ? AND (r.status = '{WorkflowStatus.FAILED.value}' OR s.state = '{SUBMISSION_STATE_EXHAUSTED}')"
)
_SELECT_TRIPPED = f"SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind = '{_TRIP_UPDATE_KIND}' LIMIT 1"

# The execution journal's own claim on a workflow_id. Every acceptance path
# checks it AFTER the host rows, so a runs row that answers here belongs to
# Tier-0 work no host submission or Batch owns (see _raise_on_tier0_reuse).
_SELECT_RUN_STATUS = "SELECT status FROM runs WHERE id = ?"

# Rerun ordinal allocation. Both counts run INSIDE the transaction that
# inserts the new submission or Batch, over rows that exist at ACCEPTANCE
# time — never over runs rows, which only appear once an earlier rerun has
# executed and would hand two pending reruns the same id.
_COUNT_ACCEPTED_RETRIES = "SELECT COUNT(*) FROM host_submissions WHERE retry_of = ?"
_COUNT_ACCEPTED_BATCH_RETRIES = "SELECT COUNT(*) FROM host_batches WHERE retry_of = ?"


class _Unset:
    """Sentinel for 'argument omitted', which ``None`` cannot express here.

    ``max_active_runs=None`` is a real value (unlimited), so ``open()`` needs a
    third state to mean "adopt whatever the store already holds".
    """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


_UNSET = _Unset()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_utc_iso(value: datetime | str | None, *, field: str) -> str | None:
    """Normalize a caller timestamp to a UTC ISO string safe for `<=` compares.

    Every due row in this store is decided by a lexicographic comparison
    against one store-authoritative ``now`` (see ``_due_clause``), so every
    stored value must share one shape: UTC ``+00:00`` ISO. Naive inputs are
    read as UTC; offset inputs are converted. Delayed starts (``start_at``)
    and scheduled pause answers (``due_at``) normalize through this one
    function so a timestamp cannot mean two different instants depending on
    which verb accepted it.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(
                f"{field} must be an ISO 8601 timestamp, got {value!r}.\n\nHow to fix:\n  Pass a datetime or an ISO 8601 string (e.g. '2026-07-25T09:00:00+00:00')."
            ) from None
    else:
        raise TypeError(
            f"{field} must be a datetime, an ISO string, or None; got {type(value).__name__}.\n\n"
            f"How to fix:\n  Pass a timezone-aware datetime (naive is read as UTC) or an ISO 8601 string (e.g. '2026-07-25T09:00:00+00:00'), or None for no {field}."
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


# The store's own clock, rendered in the one UTC ISO shape `_normalize_utc_iso`
# produces, so a store `now` and a caller timestamp compare as plain strings.
# `%f` is SQLite's `SS.SSS`; the literal `000` pads it to Python's six
# fractional digits. `'now'` is UTC in SQLite.
_STORE_NOW_SQL = "SELECT strftime('%Y-%m-%dT%H:%M:%f000+00:00', 'now')"


def _due_clause(column: str, *, null_is_due: bool) -> str:
    """THE due predicate for store-authoritative time, as a SQL fragment.

    One definition of "this row's time has arrived", shared by delayed
    starts (``host_submissions.start_at``) and scheduled pause answers
    (``host_commands.due_at``) — PRD 0017: "Scheduled answers share a
    due-row scanner with delayed starts". The comparison always runs inside
    the store transaction against a ``now`` the caller supplies; that ``now``
    comes from the store's own clock (``RunHome._store_now``) unless a test
    drives time explicitly, so two workers with skewed process clocks decide
    due-ness against ONE clock.

    ``null_is_due`` says what a NULL time means for THIS column, because the
    two columns share a string and not a NULL semantic:

    - ``start_at`` is NULL when no delay was requested, which means "start
      now" (``null_is_due=True``).
    - ``due_at`` is NULL only on a ``stop`` row or a malformed scheduled
      answer — never a timer a caller armed, since ``schedule_answer``
      requires a due time. A NULL there must be inert, never instantly due
      (``null_is_due=False``); SQL's ``NULL <= ?`` is not true, so the row
      simply never matches.
    """
    if null_is_due:
        return f"({column} IS NULL OR {column} <= ?)"
    return f"({column} <= ?)"


# The closed host_commands verb vocabulary (ADR 0008 / PRD 0017). Two verbs,
# both framework-chosen: there is deliberately no caller-chosen verb, no
# recurrence, and no cron surface — a generic scheduled-command framework is
# explicitly out of scope. Every statement that writes or matches a verb
# binds one of these, so a third verb cannot appear as a bare SQL literal.
STOP_VERB = "stop"
SCHEDULE_ANSWER_VERB = "schedule_answer"
HOST_COMMAND_VERBS: frozenset[str] = frozenset({STOP_VERB, SCHEDULE_ANSWER_VERB})

# What firing a scheduled answer produced, recorded on its command row and on
# its durable ``command`` run update for audit. Not a status vocabulary: it
# never enters WorkflowStatus and never decides claim eligibility. A fired row
# is never deleted — the audit trail is the point, so a voided timer stays
# queryable with the reason it lost.
ScheduledAnswerOutcome = Literal["settled", "already_settled", "superseded", "rejected"]

SCHEDULED_ANSWER_SETTLED: ScheduledAnswerOutcome = "settled"
SCHEDULED_ANSWER_ALREADY_SETTLED: ScheduledAnswerOutcome = "already_settled"
SCHEDULED_ANSWER_SUPERSEDED: ScheduledAnswerOutcome = "superseded"
SCHEDULED_ANSWER_REJECTED: ScheduledAnswerOutcome = "rejected"
# The same closed vocabulary as stored strings, for the store's own values.
SCHEDULED_ANSWER_OUTCOMES: frozenset[str] = frozenset(
    {SCHEDULED_ANSWER_SETTLED, SCHEDULED_ANSWER_ALREADY_SETTLED, SCHEDULED_ANSWER_SUPERSEDED, SCHEDULED_ANSWER_REJECTED}
)


@dataclass(frozen=True)
class _ScheduledAnswerRow:
    """One scheduled answer normalized into its stored ``host_commands`` shape.

    Named because the write path passes it whole between a pure normalizer
    and two mirrors; an anonymous tuple made callers rebind ``source_ref`` to
    itself just to keep positions straight.
    """

    run_id: str
    payload: str
    due_at: str
    source_ref: str | None
    created_at: str


@dataclass(frozen=True)
class _DueScheduledAnswer:
    """One due timer, read from its command row for firing.

    Carries everything the fire path both applies and reports: the value it
    would answer with, and the provenance (``due_at``, ``source_ref``) its
    durable outcome fact republishes so a detached ``watch`` consumer learns
    the timer's fate without reading the store.
    """

    command_id: int
    run_id: str
    pause_id: str | None
    value: Any
    due_at: str | None
    source_ref: str | None


def _scheduled_answer_fact(
    *,
    pause_id: str | None,
    due_at: str | None,
    source_ref: str | None,
    outcome: ScheduledAnswerOutcome | None,
) -> dict[str, Any]:
    """The durable ``command`` payload for one scheduled answer, arming or fired.

    Arming and settling emit the SAME shape so a detached ``watch`` consumer
    never has to infer which it is: ``outcome`` is None while the timer is
    armed and one of ``SCHEDULED_ANSWER_OUTCOMES`` once it fired. PRD 0018
    A9 — every Run mutation receives one monotonic durable sequence — and a
    fired or voided timer IS a recorded state change, so the fate must be
    readable from the stream alone.
    """
    return {"verb": SCHEDULE_ANSWER_VERB, "pause_id": pause_id, "due_at": due_at, "source_ref": source_ref, "outcome": outcome}


def _cap_from_row(row: tuple[Any, ...] | None) -> int | None:
    """The stored active-Run cap, or None when uncapped.

    A missing row and a NULL value both mean unlimited, so callers never have
    to distinguish "never configured" from "explicitly unlimited".
    """
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _slots_left(cap_row: tuple[Any, ...] | None, count_row: tuple[Any, ...] | None) -> int | None:
    """Free slots under the stored cap; None when uncapped.

    THE definition of "a free slot" for host work admission, shared by the
    claim gate and the view, sync and async. A restart that re-adopted more
    claimed Runs than a lowered cap yields a negative budget, which simply
    claims nothing new — admission delays work, it never cancels it.
    """
    cap = _cap_from_row(cap_row)
    if cap is None:
        return None
    return cap - (int(count_row[0]) if count_row else 0)


def _row_to_submission(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(_SUBMISSION_COLS.split(", "), row, strict=True))


def _row_to_batch(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(_BATCH_COLS.split(", "), row, strict=True))


def _items_map(items: list[tuple[str, str]]) -> dict[str, Any]:
    """The manifest as ``{item_key: inputs}``, in expansion order."""
    return {key: json.loads(inputs_json) for key, inputs_json in items}


def _manifest_payload(
    batch_id: str,
    workflow_id: str,
    *,
    definition_name: str,
    def_version: str,
    def_struct_hash: str,
    items: list[tuple[str, str]],
    tolerance_json: str | None,
    start_at: str | None,
    source_ref: str | None,
    batch_retry_of: str | None,
) -> dict[str, Any]:
    """The ``manifest`` Batch fact at ``bseq=1``. Shared by both mirrors."""
    return {
        "batch_id": batch_id,
        "workflow_id": workflow_id,
        "definition_id": {
            "name": definition_name,
            "deployment_version": def_version,
            "structural_hash": def_struct_hash,
        },
        "item_keys": [key for key, _ in items],
        "tolerance": json.loads(tolerance_json) if tolerance_json is not None else None,
        "start_at": start_at,
        "source_ref": source_ref,
        "retry_of": batch_retry_of,
    }


def _child_submission_row(
    child_workflow_id: str,
    inputs_json: str,
    item_key: str,
    *,
    batch_id: str,
    definition_name: str,
    def_version: str,
    def_struct_hash: str,
    start_at: str | None,
    source_ref: str | None,
    recovery_cap: int,
    child_source: str | None,
    child_retry_index: int | None,
    now: str,
) -> tuple[Any, ...]:
    """One child submission row — ordinary pending work with Batch membership."""
    return (
        child_workflow_id,
        definition_name,
        def_version,
        def_struct_hash,
        inputs_json,
        start_at,
        "pending",
        0,
        recovery_cap,
        source_ref,
        now,
        None,
        None,
        start_fingerprint(DefinitionId(definition_name, def_version, def_struct_hash), inputs_json, start_at),
        "compatible",
        child_source,
        child_retry_index,
        None,
        None,
        0,
        batch_id,
        item_key,
    )


def _children_settled_rows(rows: list[tuple[Any, ...]]) -> bool:
    """True when every (submission_state, run_status) pair is settled.

    A child is settled when its run reached a terminal status, its
    submission finished (terminal run, stop-before-start, or a tolerance
    trip that closed admission before it ever ran), or its submission is
    recovery-exhausted (parked; v1 treats parked work as settled).

    A tolerance trip needs no special case here: it marks every remaining
    item's submission finished in the tripping transaction, so those items
    are settled-and-unstarted by the same rule stop-before-start already
    uses.
    """
    return all(is_child_settled(sub_state, run_status) for sub_state, run_status in rows)


def _is_committed_progress(kind: str, payload: dict[str, Any]) -> bool:
    """True when a run mutation is NEW committed progress (resets the brake).

    Only step saves, durable pauses, and terminal transitions count. A status
    flip to ``active`` at claim/re-adoption (``run_started``/``status``) is
    recovery bookkeeping, not progress, and must NOT reset the counter.
    Journal-skipped steps during recovery are not re-saved, so resume alone
    never touches the counter either.
    """
    if kind == "step":
        return True
    return kind == "status" and payload.get("status") in _PROGRESS_STATUSES


def _retry_workflow_id(source: str, ordinal: int) -> str:
    """THE rerun id derivation, for Runs and Batches alike: ``<source>-retry-N``.

    ``ordinal`` is allocated in the acceptance transaction, so the id and
    the ordinal persisted alongside it are one value, decided once.
    """
    return f"{source}-retry-{ordinal}"


def _next_ordinal(count_row: tuple[Any, ...] | None) -> int:
    """The next retry ordinal from an in-transaction COUNT row."""
    return int((count_row[0] if count_row else 0) or 0) + 1


def _require_retry_source(retry_of: str | None) -> str:
    """Guard the "mint me an id" contract: only a rerun may omit one."""
    if retry_of is None:
        raise ValueError(
            "A submission with no workflow_id must name the run it repeats.\n\n"
            "How to fix: pass an explicit workflow_id, or pass retry_of=<source workflow_id> so the "
            "acceptance transaction can mint '<source>-retry-N'."
        )
    return retry_of


def _raise_on_tier0_reuse(status: str | None, *, workflow_id: str, item_key: str | None = None) -> None:
    """Refuse a workflow_id the execution journal already owns (US11).

    THE third owner of the workflow_id namespace. A ``runs`` row with no
    ``host_submissions`` and no ``host_batches`` row is Tier-0 work —
    executed straight against this store as a plain checkpointer. Callers
    reach here only after the host-row checks, which already resolve
    host-owned reuse (use-existing dedup, terminal conflict, fingerprint
    conflict); this one fires when no host row exists at all.

    The host cannot adopt such a run: it holds no pinned Definition
    identity and no start fingerprint for it, so there is nothing to
    compare and nothing to dedupe against. Both cases are conflicts, and
    the existing typed errors already carry the distinction — terminal
    Tier-0 history is ``AlreadyTerminalError`` (completed history never
    changes identity), a still-running one is ``WorkflowIdConflictError``
    (the id is taken by live work).

    ``status`` is the stored ``runs.status`` value, or None when no runs
    row exists (the accept-it case). ``item_key`` names the manifest item
    when the refused id is a generated Batch child id.
    """
    if status is None:
        return
    subject = f"The child workflow id for item {item_key!r} ({workflow_id!r})" if item_key else f"workflow_id {workflow_id!r}"
    pick_new = (
        "choose a new Batch workflow_id or item key (a child id is always '<batch workflow_id>:<item key>')"
        if item_key
        else "choose a new workflow_id"
    )
    if status in TERMINAL_STATUS_VALUES:
        raise AlreadyTerminalError(
            workflow_id,
            f"{subject} is already terminal in this Run Home: a run with that id settled ({status}) with no host "
            "submission behind it — it was executed directly against this store as a checkpointer.\n\n"
            f"How to fix: {pick_new}. Completed history never changes identity, and the host cannot take over an id "
            "it did not submit.",
        )
    raise WorkflowIdConflictError(
        workflow_id,
        "a run this host never submitted owns this workflow_id",
        f"{subject} is already in use in this Run Home by a {status!r} run this host never submitted — it was "
        "executed directly against this store, so there is no pinned Definition identity or start fingerprint to "
        "compare against and nothing to dedupe into.\n\n"
        f"How to fix: {pick_new}, or wait for that run to settle and pick a fresh id either way. Host submissions, "
        "Batches, and host-less runs share one workflow_id namespace.",
    )


def _raise_on_conflicting_reuse(
    existing: dict[str, Any],
    *,
    workflow_id: str,
    fingerprint: str,
    definition_name: str,
    def_version: str,
    def_struct_hash: str,
    inputs_json: str,
    start_at: str | None,
) -> None:
    """Apply the dedup/conflict contract to an existing submission row.

    Terminal reuse wins first (completed history never changes identity): a
    finished submission is terminal even when no runs row exists (stopped
    before first execution). Then fingerprint mismatch is a distinct typed
    conflict; an identical fingerprint falls through to use-existing dedup.
    Caller holds the write transaction and rolls back on raise.
    """
    if existing["state"] == "finished":
        raise AlreadyTerminalError(workflow_id)
    if existing["fingerprint"] != fingerprint:
        aspect = fingerprint_mismatch_aspect(
            existing,
            definition_name=definition_name,
            def_version=def_version,
            def_struct_hash=def_struct_hash,
            inputs_json=inputs_json,
            start_at=start_at,
        )
        raise WorkflowIdConflictError(workflow_id, aspect)


class RunHome(SqliteCheckpointer):
    """SQLite Run Home: checkpointer plus durable-host coordination tables.

    Open with ``RunHome.open("file:./runs.db")`` (or ``":memory:"`` for
    tests). A RunHome works everywhere a ``SqliteCheckpointer`` does —
    direct runner execution against it is unchanged Tier-0 behavior.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        policy: CheckpointPolicy | None = None,
        serializer: Any = None,
        max_active_runs: int | None | _Unset = _UNSET,
    ):
        if policy is not None and policy.durability == "exit":
            raise ValueError(
                'RunHome rejects durability="exit": host runs must persist every mutation immediately, not buffer checkpoint writes to run exit.'
            )
        effective_policy = CheckpointPolicy(
            durability="sync",
            retention=policy.retention if policy is not None else "full",
            window=policy.window if policy is not None else None,
            ttl=policy.ttl if policy is not None else None,
        )
        super().__init__(path, policy=effective_policy, serializer=serializer)
        if not isinstance(max_active_runs, _Unset):
            # Explicit argument writes through; omitting it adopts whatever the
            # store already holds (see the `max_active_runs` property).
            self.max_active_runs = max_active_runs

    @classmethod
    def open(
        cls,
        uri: str | Path,
        *,
        policy: CheckpointPolicy | None = None,
        serializer: Any = None,
        max_active_runs: int | None | _Unset = _UNSET,
    ) -> RunHome:
        """Open (or create) a Run Home at ``uri``.

        Args:
            uri: ``"file:./path.db"``, a plain filesystem path, or
                ``":memory:"`` for an isolated in-memory Home.
            policy: Optional retention/ttl settings. Durability is forced to
                ``"sync"``; ``"exit"`` durability is rejected.
            serializer: Optional value serializer (default: JSON).
            max_active_runs: Host work admission — how many Runs a worker
                executes at once. The cap lives in the store, so **passing it
                writes through** (including an explicit ``None`` for
                unlimited) and **omitting it adopts the stored value**. A
                store that was never configured is unlimited. Tunable later
                via the ``max_active_runs`` attribute.
        """
        return cls(uri, policy=policy, serializer=serializer, max_active_runs=max_active_runs)

    @property
    def uri(self) -> str:
        """The Home URI string as passed to ``open()`` (used in refs)."""
        return self._path

    @property
    def max_active_runs(self) -> int | None:
        """Host work admission: concurrent Runs a worker may execute.

        The cap is a **Home-scoped fact in the store**, not a per-instance
        attribute: the worker that enforces it and the operator process that
        tunes or reads it hold different ``RunHome`` objects. Assigning
        writes through immediately, so a worker already polling this Home
        honors the new value on its next claim scan — no restart, no shared
        Python object. Reading returns what the store holds.

        Lowering it never rejects or cancels anything: over-limit Runs stay
        pending and wait in claim order as
        ``WaitingCondition.ADMISSION_LIMITED``. Raising it lets the oldest
        waiting Run in first.

        This counts *Runs a worker executes*, so it is not the same control
        as a ``ProcessLocalLimiter`` (``provider_limit``), which counts
        concurrent calls to an external provider. A claimed Run parked on a
        provider permit is still executing and still holds its slot here.
        None means unlimited (no Run ever reports ADMISSION_LIMITED).
        """
        with self._sync_lock:
            row = self._sync_db().execute(_SELECT_SETTING_SQL, (_MAX_ACTIVE_RUNS_KEY,)).fetchone()
        return _cap_from_row(row)

    @max_active_runs.setter
    def max_active_runs(self, value: int | None) -> None:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise ValueError(
                f"max_active_runs must be an int >= 1 concurrent Runs, or None for unlimited; got {value!r}.\n\n"
                "How to fix:\n"
                "  home.max_active_runs = 4     # this Home admits 4 Runs at once\n"
                "  home.max_active_runs = None  # unlimited (the default)"
            )
        with self._sync_lock:
            db = self._sync_db()
            db.execute(_UPSERT_SETTING_SQL, (_MAX_ACTIVE_RUNS_KEY, None if value is None else str(value), _now_iso()))
            db.commit()

    # === Store-authoritative time ===

    async def _store_now(self) -> str:
        """The STORE's clock as a UTC ISO string — never the worker's.

        ADR 0008 decides applicability when "store-authoritative time passes
        ``due_at``", and PRD 0017 says store time controls claim
        eligibility. A worker reading its own process clock would make a
        skewed machine claim early or fire late, and two workers on one Home
        would disagree about which rows are due. Reading the timestamp from
        the store gives every worker on this Home ONE clock, whatever their
        process clocks say.

        Callers that need one instant for several scans take it once and
        pass it down (``Host.work_forever``); the due-row scans default to
        it when no ``now`` is supplied, and accept an explicit one so tests
        drive time deterministically instead of sleeping.
        """
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(_STORE_NOW_SQL)
            row = await cursor.fetchone()
        return str(row[0])

    # === Host work admission ===

    async def _admission_is_full(self) -> bool:
        """True when the active-Run cap leaves no slot for new work.

        A claimed submission is one a worker owns end to end — starting,
        executing, parked on a provider permit, or settling — so it holds a
        slot. Pending (queued, scheduled, version-incompatible), exhausted,
        and finished submissions are not claimed and hold none.
        """
        await self._ensure_db()
        async with self._txn_lock():
            free = await self._free_admission_slots()
        return free is not None and free <= 0

    def _admission_is_full_sync(self) -> bool:
        """Sync mirror of ``_admission_is_full``."""
        with self._sync_lock:
            db = self._sync_db()
            cap_row = db.execute(_SELECT_SETTING_SQL, (_MAX_ACTIVE_RUNS_KEY,)).fetchone()
            if _cap_from_row(cap_row) is None:  # uncapped is never full
                return False
            free = _slots_left(cap_row, db.execute(_ACTIVE_RUN_COUNT_SQL).fetchone())
        return free is not None and free <= 0

    async def _free_admission_slots(self) -> int | None:
        """Slots left under the active-Run cap; None when uncapped.

        Both the cap and the count are read inside the caller's transaction,
        so a claim decision uses the numbers that transaction commits
        against — including a cap another process wrote a moment ago.
        """
        cursor = await self._db.execute(_SELECT_SETTING_SQL, (_MAX_ACTIVE_RUNS_KEY,))
        cap_row = await cursor.fetchone()
        if _cap_from_row(cap_row) is None:
            return None
        cursor = await self._db.execute(_ACTIVE_RUN_COUNT_SQL)
        return _slots_left(cap_row, await cursor.fetchone())

    # === run_updates appends (same-transaction as run mutations) ===

    def _append_run_update_sync(self, db: Any, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        """Append one run_updates row; caller holds the write transaction.

        Single INSERT...SELECT so the seq allocation and the insert are one
        statement (no read-then-write race).
        """
        db.execute(
            "INSERT INTO run_updates (run_id, seq, kind, payload, created_at) "
            "SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ? FROM run_updates WHERE run_id = ?",
            (run_id, kind, json.dumps(payload), _now_iso(), run_id),
        )

    async def _append_run_update(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        """Append one run_updates row; caller holds the write transaction."""
        await self._db.execute(
            "INSERT INTO run_updates (run_id, seq, kind, payload, created_at) "
            "SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ? FROM run_updates WHERE run_id = ?",
            (run_id, kind, json.dumps(payload), _now_iso(), run_id),
        )

    def _reset_recovery_attempts_sync(self, db: Any, run_id: str) -> None:
        """Reset the recovery brake on NEW committed progress (same transaction)."""
        db.execute(
            "UPDATE host_submissions SET recovery_attempts = 0 WHERE workflow_id = ? AND recovery_attempts > 0",
            (run_id,),
        )

    def _after_run_mutation_sync(self, db: Any, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        self._append_run_update_sync(db, run_id, kind, payload)
        if _is_committed_progress(kind, payload):
            self._reset_recovery_attempts_sync(db, run_id)
        if kind == "status" and payload.get("status") in _TERMINAL_STATUS_VALUES:
            self._append_child_settled_sync(db, run_id, payload["status"])
        elif kind == "status" and payload.get("status") == WorkflowStatus.PAUSED.value:
            self._append_child_paused_sync(db, run_id, payload.get("pause_id"))

    async def _after_run_mutation(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        await self._append_run_update(run_id, kind, payload)
        if _is_committed_progress(kind, payload):
            await self._db.execute(
                "UPDATE host_submissions SET recovery_attempts = 0 WHERE workflow_id = ? AND recovery_attempts > 0",
                (run_id,),
            )
        if kind == "status" and payload.get("status") in _TERMINAL_STATUS_VALUES:
            await self._append_child_settled(run_id, payload["status"])
        elif kind == "status" and payload.get("status") == WorkflowStatus.PAUSED.value:
            await self._append_child_paused(run_id, payload.get("pause_id"))

    # === batch_updates appends (same-transaction as Batch facts) ===

    def _append_batch_update_sync(self, db: Any, batch_id: str, kind: str, payload: dict[str, Any], item_key: str | None = None) -> None:
        """Append one batch_updates row; caller holds the write transaction.

        Single INSERT...SELECT so the bseq allocation and the insert are one
        statement (no read-then-write race) — same discipline as run_updates.
        """
        db.execute(
            "INSERT INTO batch_updates (batch_id, bseq, kind, item_key, payload, created_at) "
            "SELECT ?, COALESCE(MAX(bseq), 0) + 1, ?, ?, ?, ? FROM batch_updates WHERE batch_id = ?",
            (batch_id, kind, item_key, json.dumps(payload), _now_iso(), batch_id),
        )

    async def _append_batch_update(self, batch_id: str, kind: str, payload: dict[str, Any], item_key: str | None = None) -> None:
        """Append one batch_updates row; caller holds the write transaction."""
        await self._db.execute(
            "INSERT INTO batch_updates (batch_id, bseq, kind, item_key, payload, created_at) "
            "SELECT ?, COALESCE(MAX(bseq), 0) + 1, ?, ?, ?, ? FROM batch_updates WHERE batch_id = ?",
            (batch_id, kind, item_key, json.dumps(payload), _now_iso(), batch_id),
        )

    def _append_child_settled_sync(self, db: Any, run_id: str, status: str) -> None:
        """Mirror a child settling for good onto its Batch, same transaction.

        ``status`` is a terminal ``WorkflowStatus`` value, or
        ``BATCH_OUTCOME_RECOVERY_EXHAUSTED`` when the recovery brake parked
        the child — the same string ``BatchView.outcomes`` reports, so a
        stream consumer reconstructs the view's outcome verbatim.

        No-op for runs without Batch membership. Idempotent per child: a
        repeated settle write (e.g. a resume path) never duplicates the
        child_settled fact, so the per-Batch sequence stays gap-free without
        backpressuring the child's commit path.
        """
        row = db.execute(
            "SELECT batch_id, item_key FROM host_submissions WHERE workflow_id = ?",
            (run_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return
        batch_id, item_key = row
        exists = db.execute(
            "SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind = ? AND item_key = ? LIMIT 1",
            (batch_id, _SETTLED_UPDATE_KIND, item_key),
        ).fetchone()
        if exists is not None:
            return
        self._append_batch_update_sync(
            db,
            batch_id,
            _SETTLED_UPDATE_KIND,
            {"item_key": item_key, "workflow_id": run_id, "status": status},
            item_key=item_key,
        )
        self._maybe_trip_tolerance_sync(db, batch_id)

    async def _append_child_settled(self, run_id: str, status: str) -> None:
        """Async mirror of ``_append_child_settled_sync``."""
        cursor = await self._db.execute(
            "SELECT batch_id, item_key FROM host_submissions WHERE workflow_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            return
        batch_id, item_key = row
        exists_cursor = await self._db.execute(
            "SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind = ? AND item_key = ? LIMIT 1",
            (batch_id, _SETTLED_UPDATE_KIND, item_key),
        )
        if await exists_cursor.fetchone() is not None:
            return
        await self._append_batch_update(
            batch_id,
            _SETTLED_UPDATE_KIND,
            {"item_key": item_key, "workflow_id": run_id, "status": status},
            item_key=item_key,
        )
        await self._maybe_trip_tolerance(batch_id)

    # === Occurrence-scoped Batch facts (child_paused / child_runnable) ===
    #
    # A paused child and its re-admission are the two nonterminal facts a
    # detached operator surface needs: "this item is waiting on me" and
    # "my answer put it back in the queue". Both commit in the SAME
    # transaction as the state change that causes them — the pause commit
    # (``record_pause``) and the answer settlement (``settle_pause``) — so
    # a reader can never see a paused child the Batch stream never
    # mentioned, or an answered child that looks permanently parked.
    #
    # Neither is an ACCOUNTING fact: they never settle a manifest item, and
    # a loop that pauses again earns a fresh pair. That is why they dedupe
    # on the occurrence (``pause_id``) rather than once per item key the way
    # ``child_settled``/``child_unstarted`` do.

    @staticmethod
    def _occurrence_fact(batch_id: str, item_key: str, run_id: str, home_uri: str, pause_id: str | None) -> dict[str, Any]:
        """The shared payload for both occurrence facts.

        Carries the logical item key AND the child's inert Run address (PRD
        0017 US38), so a consumer never parses ``<batch>:<item>`` child
        workflow-id syntax to act on the item.
        """
        return {
            "batch_id": batch_id,
            "item_key": item_key,
            "workflow_id": run_id,
            "run_ref": {"home": home_uri, "run_id": run_id},
            "pause_id": pause_id,
        }

    @staticmethod
    def _is_repeat_occurrence(row: tuple[Any, ...] | None, pause_id: str | None) -> bool:
        """True when the last fact of this kind already named this occurrence.

        A resumed run that replays its interrupt re-commits the identical
        ``pause_id`` (the slot insert is a no-op by ``ON CONFLICT``), and a
        settlement path may be reached twice for one answer. Comparing the
        LAST fact's occurrence suppresses both without suppressing the next
        turn of a loop, whose ``pause_id`` differs by superstep.
        """
        return row is not None and json.loads(row[0]).get("pause_id") == pause_id

    def _append_child_paused_sync(self, db: Any, run_id: str, pause_id: str | None) -> None:
        """Mirror a child parking on a human onto its Batch, same transaction."""
        membership = db.execute("SELECT batch_id, item_key FROM host_submissions WHERE workflow_id = ?", (run_id,)).fetchone()
        if membership is None or membership[0] is None:
            return
        batch_id, item_key = membership
        last = db.execute(
            "SELECT payload FROM batch_updates WHERE batch_id = ? AND kind = ? AND item_key = ? ORDER BY bseq DESC LIMIT 1",
            (batch_id, _PAUSED_UPDATE_KIND, item_key),
        ).fetchone()
        if self._is_repeat_occurrence(last, pause_id):
            return
        self._append_batch_update_sync(
            db,
            batch_id,
            _PAUSED_UPDATE_KIND,
            self._occurrence_fact(batch_id, item_key, run_id, self.uri, pause_id),
            item_key=item_key,
        )

    async def _append_child_paused(self, run_id: str, pause_id: str | None) -> None:
        """Async mirror of ``_append_child_paused_sync``."""
        membership = await self._batch_membership(run_id)
        if membership is None:
            return
        batch_id, item_key = membership
        cursor = await self._db.execute(
            "SELECT payload FROM batch_updates WHERE batch_id = ? AND kind = ? AND item_key = ? ORDER BY bseq DESC LIMIT 1",
            (batch_id, _PAUSED_UPDATE_KIND, item_key),
        )
        if self._is_repeat_occurrence(await cursor.fetchone(), pause_id):
            return
        await self._append_batch_update(
            batch_id,
            _PAUSED_UPDATE_KIND,
            self._occurrence_fact(batch_id, item_key, run_id, self.uri, pause_id),
            item_key=item_key,
        )

    async def _append_child_runnable(self, run_id: str, pause_id: str | None) -> None:
        """Mirror an answered child re-entering claim order, same transaction."""
        membership = await self._batch_membership(run_id)
        if membership is None:
            return
        batch_id, item_key = membership
        cursor = await self._db.execute(
            "SELECT payload FROM batch_updates WHERE batch_id = ? AND kind = ? AND item_key = ? ORDER BY bseq DESC LIMIT 1",
            (batch_id, _RUNNABLE_UPDATE_KIND, item_key),
        )
        if self._is_repeat_occurrence(await cursor.fetchone(), pause_id):
            return
        await self._append_batch_update(
            batch_id,
            _RUNNABLE_UPDATE_KIND,
            self._occurrence_fact(batch_id, item_key, run_id, self.uri, pause_id),
            item_key=item_key,
        )

    def _append_child_runnable_sync(self, db: Any, run_id: str, pause_id: str | None) -> None:
        """Sync mirror of ``_append_child_runnable``."""
        membership = db.execute("SELECT batch_id, item_key FROM host_submissions WHERE workflow_id = ?", (run_id,)).fetchone()
        if membership is None or membership[0] is None:
            return
        batch_id, item_key = membership
        last = db.execute(
            "SELECT payload FROM batch_updates WHERE batch_id = ? AND kind = ? AND item_key = ? ORDER BY bseq DESC LIMIT 1",
            (batch_id, _RUNNABLE_UPDATE_KIND, item_key),
        ).fetchone()
        if self._is_repeat_occurrence(last, pause_id):
            return
        self._append_batch_update_sync(
            db,
            batch_id,
            _RUNNABLE_UPDATE_KIND,
            self._occurrence_fact(batch_id, item_key, run_id, self.uri, pause_id),
            item_key=item_key,
        )

    async def _batch_membership(self, run_id: str) -> tuple[str, str] | None:
        """``(batch_id, item_key)`` for a Batch child, else None; caller holds the txn."""
        cursor = await self._db.execute("SELECT batch_id, item_key FROM host_submissions WHERE workflow_id = ?", (run_id,))
        row = await cursor.fetchone()
        return None if row is None or row[0] is None else (str(row[0]), str(row[1]))

    async def _append_child_unstarted(self, run_id: str) -> None:
        """Record that a Batch item ended unstarted, in the caller's transaction.

        THE unstarted-child fact (PRD 0019 A9). Every path that settles a
        child WITHOUT ever executing it — admission refusing a child of an
        already-tripped Batch, and a stop that landed before first
        execution — appends this row in the SAME transaction as the state
        flip that causes it, so a detached ``watch(batch_ref)`` accounts
        the item from the durable sequence alone. Items the trip fact
        itself named in ``unstarted_items`` never reach here: their flip
        happened inside the trip.

        No-op for runs without Batch membership. Idempotent per item, like
        ``_append_child_settled``: the flip is already once-only (it moves
        the row out of 'pending'/'claimed'), and the guard keeps a replayed
        path from double-accounting the item even so.

        Async-only on purpose: both callers are worker paths
        (``_claim_eligible``, ``_apply_stop_never_started``) that have no
        sync mirror, so a ``_sync`` twin here would be dead code.
        """
        cursor = await self._db.execute(
            "SELECT batch_id, item_key FROM host_submissions WHERE workflow_id = ?",
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            return
        batch_id, item_key = row
        exists_cursor = await self._db.execute(
            "SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind = ? AND item_key = ? LIMIT 1",
            (batch_id, _UNSTARTED_UPDATE_KIND, item_key),
        )
        if await exists_cursor.fetchone() is not None:
            return
        await self._append_batch_update(
            batch_id,
            _UNSTARTED_UPDATE_KIND,
            {"item_key": item_key, "workflow_id": run_id},
            item_key=item_key,
        )

    # === Pinned tolerance (ticket 06) ===

    def _maybe_trip_tolerance_sync(self, db: Any, batch_id: str) -> None:
        """Evaluate the pinned tolerance and trip the Batch, same transaction.

        Called immediately after a child fact that can change failure
        truth, INSIDE the caller's transaction (PRD 0019 A9): the child's
        terminal record and the trip land together, so the per-Batch
        sequence stays gap-free and a reader never sees a Batch that should
        have tripped but has not.

        Tripping does three things, all durable at once:

        1. Appends the ``tolerance_tripped`` fact at the next ``bseq``.
        2. Marks every still-pending child finished — those items are
           explicitly unstarted, never fabricated failures (ADR 0004) —
           and names them in the trip fact's ``unstarted_items``.
        3. Leaves claimed children alone: already-claimed work settles. A
           claimed child that never settles (a crash returns it to
           'pending') is refused at ``_claim_eligible``, which appends its
           own ``child_unstarted`` fact — so every manifest item is
           accounted by the durable sequence, not just by the view.

        Idempotent per Batch (a Batch trips exactly once) and a no-op when
        no tolerance was pinned. The Batch keeps no status of its own: a
        trip is a Batch fact, never a ``WorkflowStatus``.
        """
        batch_row = db.execute("SELECT items_json, tolerance_json FROM host_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if batch_row is None or batch_row[1] is None:
            return
        if db.execute(_SELECT_TRIPPED, (batch_id,)).fetchone() is not None:
            return
        (failure_count,) = db.execute(_COUNT_FAILURE_EQUIVALENT, (batch_id,)).fetchone()
        payload = self._trip_payload(batch_row[0], batch_row[1], int(failure_count))
        if payload is None:
            return
        unstarted = [
            str(row[0])
            for row in db.execute(
                "SELECT item_key FROM host_submissions WHERE batch_id = ? AND state = 'pending' ORDER BY rowid",
                (batch_id,),
            ).fetchall()
        ]
        db.execute(
            "UPDATE host_submissions SET state = 'finished', finished_at = ? WHERE batch_id = ? AND state = 'pending'",
            (_now_iso(), batch_id),
        )
        self._append_batch_update_sync(db, batch_id, _TRIP_UPDATE_KIND, {**payload, "unstarted_items": unstarted})

    async def _maybe_trip_tolerance(self, batch_id: str) -> None:
        """Async mirror of ``_maybe_trip_tolerance_sync``; caller holds the transaction."""
        batch_cursor = await self._db.execute("SELECT items_json, tolerance_json FROM host_batches WHERE batch_id = ?", (batch_id,))
        batch_row = await batch_cursor.fetchone()
        if batch_row is None or batch_row[1] is None:
            return
        tripped_cursor = await self._db.execute(_SELECT_TRIPPED, (batch_id,))
        if await tripped_cursor.fetchone() is not None:
            return
        count_cursor = await self._db.execute(_COUNT_FAILURE_EQUIVALENT, (batch_id,))
        (failure_count,) = await count_cursor.fetchone()
        payload = self._trip_payload(batch_row[0], batch_row[1], int(failure_count))
        if payload is None:
            return
        pending_cursor = await self._db.execute(
            "SELECT item_key FROM host_submissions WHERE batch_id = ? AND state = 'pending' ORDER BY rowid",
            (batch_id,),
        )
        unstarted = [str(row[0]) for row in await pending_cursor.fetchall()]
        await self._db.execute(
            "UPDATE host_submissions SET state = 'finished', finished_at = ? WHERE batch_id = ? AND state = 'pending'",
            (_now_iso(), batch_id),
        )
        await self._append_batch_update(batch_id, _TRIP_UPDATE_KIND, {**payload, "unstarted_items": unstarted})

    @staticmethod
    def _trip_payload(items_json: str, tolerance_json: str, failure_count: int) -> dict[str, Any] | None:
        """Return the trip fact payload, or None when the Batch has not tripped.

        The percentage denominator is the pinned manifest length, read from
        the immutable manifest row — never a live count of remaining work.
        """
        tolerance = BatchTolerance.from_dict(json.loads(tolerance_json))
        total_items = len(json.loads(items_json))
        if not tolerance_trips(tolerance, failure_count=failure_count, total_items=total_items):
            return None
        return {
            "failed": failure_count,
            "total_items": total_items,
            "max_failed": tolerance.max_failed,
            "max_failed_percent": tolerance.max_failed_percent,
        }

    def _batch_tripped_sync(self, batch_id: str) -> bool:
        """True when a durable ``tolerance_tripped`` fact exists for this Batch."""
        with self._sync_lock:
            db = self._sync_db()
            return db.execute(_SELECT_TRIPPED, (batch_id,)).fetchone() is not None

    async def _batch_tripped(self, batch_id: str) -> bool:
        """Async mirror of ``_batch_tripped_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(_SELECT_TRIPPED, (batch_id,))
            return await cursor.fetchone() is not None

    # === Submissions ===

    def _submit_sync(
        self,
        workflow_id: str | None,
        definition_name: str,
        def_version: str,
        def_struct_hash: str,
        inputs_json: str,
        start_at: str | None,
        source_ref: str | None,
        *,
        fingerprint: str,
        retry_of: str | None = None,
        forked_from: str | None = None,
        fork_reason: str | None = None,
        recovery_cap: int = 3,
        batch_id: str | None = None,
        item_key: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Insert one submission plus its 'submitted' update, atomically.

        ``workflow_id=None`` means "mint the next rerun of ``retry_of``":
        the ordinal is allocated from accepted submissions INSIDE this
        transaction, the id is derived from it, and the ordinal is stored
        on the row. Allocating it outside handed two reruns requested
        before either executed the same ``<source>-retry-1``, and the
        second silently deduped into the first.

        Returns ``(created, row)``. When a submission already exists for
        ``workflow_id`` nothing is written: a fingerprint-identical
        nonterminal row returns ``(False, existing)`` (use-existing dedup),
        terminal reuse raises ``AlreadyTerminalError``, and a fingerprint
        mismatch raises ``WorkflowIdConflictError``. Reusing an id owned by
        a Batch is likewise a conflict (AlreadyTerminalError once that
        Batch is settled) — run and Batch workflow ids share one namespace,
        and so does the execution journal: an id already owned by a
        host-less (Tier-0) runs row is refused last, once the host rows have
        had their say (``_raise_on_tier0_reuse``).
        """
        with self._sync_lock:
            db = self._sync_db()
            try:
                db.execute("BEGIN IMMEDIATE")
                retry_index: int | None = None
                if workflow_id is None:
                    retry_index = _next_ordinal(db.execute(_COUNT_ACCEPTED_RETRIES, (_require_retry_source(retry_of),)).fetchone())
                    workflow_id = _retry_workflow_id(str(retry_of), retry_index)
                existing_row = db.execute(
                    f"SELECT {_SUBMISSION_COLS} FROM host_submissions WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                if existing_row is not None:
                    _raise_on_conflicting_reuse(
                        _row_to_submission(existing_row),
                        workflow_id=workflow_id,
                        fingerprint=fingerprint,
                        definition_name=definition_name,
                        def_version=def_version,
                        def_struct_hash=def_struct_hash,
                        inputs_json=inputs_json,
                        start_at=start_at,
                    )
                    db.rollback()
                    return False, _row_to_submission(existing_row)
                batch_row = db.execute(
                    f"SELECT {_BATCH_COLS} FROM host_batches WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                if batch_row is not None:
                    if _children_settled_rows(
                        db.execute(
                            "SELECT s.state, r.status FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                            (batch_row[0],),
                        ).fetchall()
                    ):
                        raise AlreadyTerminalError(workflow_id)
                    raise WorkflowIdConflictError(workflow_id, "an existing Batch owns this workflow_id")
                run_row = db.execute(_SELECT_RUN_STATUS, (workflow_id,)).fetchone()
                if run_row is not None:
                    _raise_on_tier0_reuse(str(run_row[0]), workflow_id=workflow_id)
                now = _now_iso()
                db.execute(
                    f"INSERT INTO host_submissions ({_SUBMISSION_COLS}) VALUES ({_SUBMISSION_PLACEHOLDERS})",
                    (
                        workflow_id,
                        definition_name,
                        def_version,
                        def_struct_hash,
                        inputs_json,
                        start_at,
                        "pending",
                        0,
                        recovery_cap,
                        source_ref,
                        now,
                        None,
                        None,
                        fingerprint,
                        "compatible",
                        retry_of,
                        retry_index,
                        forked_from,
                        fork_reason,
                        0,
                        batch_id,
                        item_key,
                    ),
                )
                self._append_run_update_sync(
                    db,
                    workflow_id,
                    "submitted",
                    {"definition_name": definition_name, "workflow_id": workflow_id},
                )
                db.commit()
            except BaseException:
                self._rollback_sync(db)
                raise
        row = self._get_submission_sync(workflow_id)
        assert row is not None
        return True, row

    async def _submit(
        self,
        workflow_id: str | None,
        definition_name: str,
        def_version: str,
        def_struct_hash: str,
        inputs_json: str,
        start_at: str | None,
        source_ref: str | None,
        *,
        fingerprint: str,
        retry_of: str | None = None,
        forked_from: str | None = None,
        fork_reason: str | None = None,
        recovery_cap: int = 3,
        batch_id: str | None = None,
        item_key: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Async mirror of ``_submit_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                retry_index: int | None = None
                if workflow_id is None:
                    retry_cursor = await self._db.execute(_COUNT_ACCEPTED_RETRIES, (_require_retry_source(retry_of),))
                    retry_index = _next_ordinal(await retry_cursor.fetchone())
                    workflow_id = _retry_workflow_id(str(retry_of), retry_index)
                cursor = await self._db.execute(
                    f"SELECT {_SUBMISSION_COLS} FROM host_submissions WHERE workflow_id = ?",
                    (workflow_id,),
                )
                existing_row = await cursor.fetchone()
                if existing_row is not None:
                    _raise_on_conflicting_reuse(
                        _row_to_submission(existing_row),
                        workflow_id=workflow_id,
                        fingerprint=fingerprint,
                        definition_name=definition_name,
                        def_version=def_version,
                        def_struct_hash=def_struct_hash,
                        inputs_json=inputs_json,
                        start_at=start_at,
                    )
                    await self._db.rollback()
                    return False, _row_to_submission(existing_row)
                batch_cursor = await self._db.execute(
                    f"SELECT {_BATCH_COLS} FROM host_batches WHERE workflow_id = ?",
                    (workflow_id,),
                )
                batch_row = await batch_cursor.fetchone()
                if batch_row is not None:
                    children_cursor = await self._db.execute(
                        "SELECT s.state, r.status FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                        (batch_row[0],),
                    )
                    if _children_settled_rows(await children_cursor.fetchall()):
                        raise AlreadyTerminalError(workflow_id)
                    raise WorkflowIdConflictError(workflow_id, "an existing Batch owns this workflow_id")
                run_cursor = await self._db.execute(_SELECT_RUN_STATUS, (workflow_id,))
                run_row = await run_cursor.fetchone()
                if run_row is not None:
                    _raise_on_tier0_reuse(str(run_row[0]), workflow_id=workflow_id)
                now = _now_iso()
                await self._db.execute(
                    f"INSERT INTO host_submissions ({_SUBMISSION_COLS}) VALUES ({_SUBMISSION_PLACEHOLDERS})",
                    (
                        workflow_id,
                        definition_name,
                        def_version,
                        def_struct_hash,
                        inputs_json,
                        start_at,
                        "pending",
                        0,
                        recovery_cap,
                        source_ref,
                        now,
                        None,
                        None,
                        fingerprint,
                        "compatible",
                        retry_of,
                        retry_index,
                        forked_from,
                        fork_reason,
                        0,
                        batch_id,
                        item_key,
                    ),
                )
                await self._append_run_update(
                    workflow_id,
                    "submitted",
                    {"definition_name": definition_name, "workflow_id": workflow_id},
                )
                await self._db.commit()
            except BaseException:
                await self._rollback_async()
                raise
        row = await self._get_submission(workflow_id)
        assert row is not None
        return True, row

    def _get_submission_sync(self, workflow_id: str) -> dict[str, Any] | None:
        with self._sync_lock:
            db = self._sync_db()
            row = db.execute(
                f"SELECT {_SUBMISSION_COLS} FROM host_submissions WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            return _row_to_submission(row) if row is not None else None

    async def _get_submission(self, workflow_id: str) -> dict[str, Any] | None:
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(
                f"SELECT {_SUBMISSION_COLS} FROM host_submissions WHERE workflow_id = ?",
                (workflow_id,),
            )
            row = await cursor.fetchone()
            return _row_to_submission(row) if row is not None else None

    # === Rerun lineage: the accepted ordinal is the runner's retry_index ===
    #
    # The checkpointer derives retry_index from a live COUNT of runs rows,
    # which depends on how many earlier reruns happen to have executed. A
    # host rerun already decided its ordinal at acceptance and put it in the
    # workflow id, so the Home answers from the submission instead: the id
    # and runs.retry_index are then one value, whatever order reruns run in.

    _ACCEPTED_RETRY_INDEX_SQL = "SELECT retry_index FROM host_submissions WHERE workflow_id = ? AND retry_of = ?"

    def retry_workflow(self, source_run_id: str, *, workflow_id: str | None = None, superstep: int | None = None) -> tuple[str, Checkpoint]:
        """``SqliteCheckpointer.retry_workflow`` with the ACCEPTED ordinal."""
        new_workflow_id, checkpoint = super().retry_workflow(source_run_id, workflow_id=workflow_id, superstep=superstep)
        with self._sync_lock:
            db = self._sync_db()
            row = db.execute(self._ACCEPTED_RETRY_INDEX_SQL, (new_workflow_id, source_run_id)).fetchone()
        if row is not None and row[0] is not None:
            checkpoint.retry_index = int(row[0])
        return new_workflow_id, checkpoint

    async def retry_workflow_async(
        self, source_run_id: str, *, workflow_id: str | None = None, superstep: int | None = None
    ) -> tuple[str, Checkpoint]:
        """Async mirror of ``retry_workflow``."""
        new_workflow_id, checkpoint = await super().retry_workflow_async(source_run_id, workflow_id=workflow_id, superstep=superstep)
        async with self._txn_lock():
            cursor = await self._db.execute(self._ACCEPTED_RETRY_INDEX_SQL, (new_workflow_id, source_run_id))
            row = await cursor.fetchone()
        if row is not None and row[0] is not None:
            checkpoint.retry_index = int(row[0])
        return new_workflow_id, checkpoint

    # === Batches (ticket 05) ===

    def _submit_batch_sync(
        self,
        batch_id: str,
        workflow_id: str | None,
        definition_name: str,
        def_version: str,
        def_struct_hash: str,
        items: list[tuple[str, str]],
        tolerance_json: str | None,
        start_at: str | None,
        source_ref: str | None,
        *,
        fingerprint: str,
        recovery_cap: int = 3,
        batch_retry_of: str | None = None,
        child_retry_of: dict[str, str] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Accept one Batch atomically: manifest + child submissions + bseq 1.

        ONE transaction persists the host_batches manifest row, one child
        submission (with its own 'submitted' run update) per item key, and
        the 'manifest' batch_updates row at bseq 1 — a partial Batch can
        never appear accepted. ``items`` is a manifest-ordered list of
        ``(item_key, inputs_json)`` pairs.

        ``batch_retry_of`` records Batch lineage when an item-scoped rerun
        mints this manifest from a source Batch (the source batch id);
        ``child_retry_of`` maps item key -> source child workflow id so each
        new child records ``retry_of`` against its source child. Both are
        None for ordinary submissions.

        ``workflow_id=None`` means "mint the next rerun of
        ``batch_retry_of``": the Batch ordinal is allocated from accepted
        Batches INSIDE this transaction and the id derived from it, so
        concurrent rerun callers can never mint the same
        ``<source>-retry-N`` and dedupe into one Batch. Each rerun child
        gets its own accepted ordinal the same way.

        Returns ``(created, batch_row)``. Dedup mirrors the Run rules: a
        fingerprint-identical nonterminal resubmission returns
        ``(False, existing)`` (use-existing), a settled Batch raises
        ``AlreadyTerminalError``, and a fingerprint mismatch raises
        ``WorkflowIdConflictError``. Run and Batch workflow ids share one
        namespace: reusing an id owned by a plain Run submission is a
        conflict (AlreadyTerminalError once that Run is finished), and so
        is a child workflow id owned by unrelated existing work. The
        execution journal owns ids too: the Batch id and every generated
        child id are checked against host-less (Tier-0) runs rows as well
        (``_raise_on_tier0_reuse``), so a child can never be minted onto
        somebody else's history.
        """
        with self._sync_lock:
            db = self._sync_db()
            try:
                db.execute("BEGIN IMMEDIATE")
                if workflow_id is None:
                    source_row = db.execute(
                        "SELECT workflow_id FROM host_batches WHERE batch_id = ?",
                        (_require_retry_source(batch_retry_of),),
                    ).fetchone()
                    workflow_id = _retry_workflow_id(
                        str(source_row[0]),
                        _next_ordinal(db.execute(_COUNT_ACCEPTED_BATCH_RETRIES, (batch_retry_of,)).fetchone()),
                    )
                existing = self._batch_dedup_sync(
                    db,
                    workflow_id,
                    definition_name=definition_name,
                    def_version=def_version,
                    def_struct_hash=def_struct_hash,
                    items=items,
                    tolerance_json=tolerance_json,
                    start_at=start_at,
                    fingerprint=fingerprint,
                )
                if existing is not None:
                    db.rollback()
                    return False, existing
                child_ids = self._reserve_child_ids_sync(db, workflow_id, items)
                now = _now_iso()
                db.execute(
                    f"INSERT INTO host_batches ({_BATCH_COLS}) VALUES ({_BATCH_PLACEHOLDERS})",
                    (
                        batch_id,
                        workflow_id,
                        definition_name,
                        def_version,
                        def_struct_hash,
                        json.dumps(_items_map(items)),
                        tolerance_json,
                        start_at,
                        fingerprint,
                        source_ref,
                        now,
                        batch_retry_of,
                    ),
                )
                self._insert_children_sync(
                    db,
                    child_ids,
                    batch_id=batch_id,
                    definition_name=definition_name,
                    def_version=def_version,
                    def_struct_hash=def_struct_hash,
                    start_at=start_at,
                    source_ref=source_ref,
                    recovery_cap=recovery_cap,
                    child_retry_of=child_retry_of,
                    now=now,
                )
                self._append_batch_update_sync(
                    db,
                    batch_id,
                    "manifest",
                    _manifest_payload(
                        batch_id,
                        workflow_id,
                        definition_name=definition_name,
                        def_version=def_version,
                        def_struct_hash=def_struct_hash,
                        items=items,
                        tolerance_json=tolerance_json,
                        start_at=start_at,
                        source_ref=source_ref,
                        batch_retry_of=batch_retry_of,
                    ),
                )
                db.commit()
            except BaseException:
                self._rollback_sync(db)
                raise
        row = self._get_batch_sync(batch_id)
        assert row is not None
        return True, row

    def _batch_dedup_sync(
        self,
        db: Any,
        workflow_id: str,
        *,
        definition_name: str,
        def_version: str,
        def_struct_hash: str,
        items: list[tuple[str, str]],
        tolerance_json: str | None,
        start_at: str | None,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        """Resolve ``workflow_id`` reuse: use-existing row, a raise, or None.

        Returns the existing Batch row when this is a fingerprint-identical
        nonterminal resubmission (use-existing) and ``None`` when the id is
        free to accept. Everything else raises — a settled Batch, a
        fingerprint mismatch, an id owned by a plain Run submission, or one
        owned by a host-less (Tier-0) run.
        """
        existing_batch_row = db.execute(
            f"SELECT {_BATCH_COLS} FROM host_batches WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        if existing_batch_row is not None:
            existing = _row_to_batch(existing_batch_row)
            settled = _children_settled_rows(
                db.execute(
                    "SELECT s.state, r.status FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                    (existing["batch_id"],),
                ).fetchall()
            )
            if settled:
                raise AlreadyTerminalError(workflow_id)
            if existing["fingerprint"] != fingerprint:
                raise WorkflowIdConflictError(
                    workflow_id,
                    batch_mismatch_aspect(
                        existing,
                        definition_name=definition_name,
                        def_version=def_version,
                        def_struct_hash=def_struct_hash,
                        items_canonical=canonical_json(_items_map(items)),
                        tolerance_json=tolerance_json,
                        start_at=start_at,
                    ),
                )
            return existing
        submission_row = db.execute("SELECT state FROM host_submissions WHERE workflow_id = ?", (workflow_id,)).fetchone()
        if submission_row is not None:
            if submission_row[0] == "finished":
                raise AlreadyTerminalError(workflow_id)
            raise WorkflowIdConflictError(workflow_id, "an existing Run owns this workflow_id")
        run_row = db.execute(_SELECT_RUN_STATUS, (workflow_id,)).fetchone()
        if run_row is not None:
            _raise_on_tier0_reuse(str(run_row[0]), workflow_id=workflow_id)
        return None

    def _reserve_child_ids_sync(self, db: Any, workflow_id: str, items: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
        """Derive every child workflow id and refuse one already owned.

        Returns ``(item_key, child_workflow_id, inputs_json)`` triples in
        manifest order.
        """
        child_ids = [(key, f"{workflow_id}:{key}", inputs_json) for key, inputs_json in items]
        for key, child_workflow_id, _inputs_json in child_ids:
            collision = db.execute(
                "SELECT 1 FROM host_submissions WHERE workflow_id = ? UNION SELECT 1 FROM host_batches WHERE workflow_id = ?",
                (child_workflow_id, child_workflow_id),
            ).fetchone()
            if collision is not None:
                raise WorkflowIdConflictError(
                    child_workflow_id,
                    f"the child workflow id for item {key!r} collides with existing work in this Run Home",
                )
            child_run_row = db.execute(_SELECT_RUN_STATUS, (child_workflow_id,)).fetchone()
            if child_run_row is not None:
                _raise_on_tier0_reuse(str(child_run_row[0]), workflow_id=child_workflow_id, item_key=key)
        return child_ids

    def _insert_children_sync(
        self,
        db: Any,
        child_ids: list[tuple[str, str, str]],
        *,
        batch_id: str,
        definition_name: str,
        def_version: str,
        def_struct_hash: str,
        start_at: str | None,
        source_ref: str | None,
        recovery_cap: int,
        child_retry_of: dict[str, str] | None,
        now: str,
    ) -> None:
        """Insert one pending child submission per item, each with its own fact."""
        for key, child_workflow_id, inputs_json in child_ids:
            child_source = (child_retry_of or {}).get(key)
            child_retry_index = None if child_source is None else _next_ordinal(db.execute(_COUNT_ACCEPTED_RETRIES, (child_source,)).fetchone())
            db.execute(
                f"INSERT INTO host_submissions ({_SUBMISSION_COLS}) VALUES ({_SUBMISSION_PLACEHOLDERS})",
                _child_submission_row(
                    child_workflow_id,
                    inputs_json,
                    key,
                    batch_id=batch_id,
                    definition_name=definition_name,
                    def_version=def_version,
                    def_struct_hash=def_struct_hash,
                    start_at=start_at,
                    source_ref=source_ref,
                    recovery_cap=recovery_cap,
                    child_source=child_source,
                    child_retry_index=child_retry_index,
                    now=now,
                ),
            )
            self._append_run_update_sync(
                db,
                child_workflow_id,
                "submitted",
                {"definition_name": definition_name, "workflow_id": child_workflow_id, "batch_id": batch_id, "item_key": key},
            )

    async def _submit_batch(
        self,
        batch_id: str,
        workflow_id: str | None,
        definition_name: str,
        def_version: str,
        def_struct_hash: str,
        items: list[tuple[str, str]],
        tolerance_json: str | None,
        start_at: str | None,
        source_ref: str | None,
        *,
        fingerprint: str,
        recovery_cap: int = 3,
        batch_retry_of: str | None = None,
        child_retry_of: dict[str, str] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Async mirror of ``_submit_batch_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                if workflow_id is None:
                    source_cursor = await self._db.execute(
                        "SELECT workflow_id FROM host_batches WHERE batch_id = ?",
                        (_require_retry_source(batch_retry_of),),
                    )
                    source_row = await source_cursor.fetchone()
                    ordinal_cursor = await self._db.execute(_COUNT_ACCEPTED_BATCH_RETRIES, (batch_retry_of,))
                    workflow_id = _retry_workflow_id(str(source_row[0]), _next_ordinal(await ordinal_cursor.fetchone()))
                existing = await self._batch_dedup(
                    workflow_id,
                    definition_name=definition_name,
                    def_version=def_version,
                    def_struct_hash=def_struct_hash,
                    items=items,
                    tolerance_json=tolerance_json,
                    start_at=start_at,
                    fingerprint=fingerprint,
                )
                if existing is not None:
                    await self._db.rollback()
                    return False, existing
                child_ids = await self._reserve_child_ids(workflow_id, items)
                now = _now_iso()
                await self._db.execute(
                    f"INSERT INTO host_batches ({_BATCH_COLS}) VALUES ({_BATCH_PLACEHOLDERS})",
                    (
                        batch_id,
                        workflow_id,
                        definition_name,
                        def_version,
                        def_struct_hash,
                        json.dumps(_items_map(items)),
                        tolerance_json,
                        start_at,
                        fingerprint,
                        source_ref,
                        now,
                        batch_retry_of,
                    ),
                )
                await self._insert_children(
                    child_ids,
                    batch_id=batch_id,
                    definition_name=definition_name,
                    def_version=def_version,
                    def_struct_hash=def_struct_hash,
                    start_at=start_at,
                    source_ref=source_ref,
                    recovery_cap=recovery_cap,
                    child_retry_of=child_retry_of,
                    now=now,
                )
                await self._append_batch_update(
                    batch_id,
                    "manifest",
                    _manifest_payload(
                        batch_id,
                        workflow_id,
                        definition_name=definition_name,
                        def_version=def_version,
                        def_struct_hash=def_struct_hash,
                        items=items,
                        tolerance_json=tolerance_json,
                        start_at=start_at,
                        source_ref=source_ref,
                        batch_retry_of=batch_retry_of,
                    ),
                )
                await self._db.commit()
            except BaseException:
                await self._rollback_async()
                raise
        row = await self._get_batch(batch_id)
        assert row is not None
        return True, row

    async def _batch_dedup(
        self,
        workflow_id: str,
        *,
        definition_name: str,
        def_version: str,
        def_struct_hash: str,
        items: list[tuple[str, str]],
        tolerance_json: str | None,
        start_at: str | None,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        """Async mirror of ``_batch_dedup_sync``."""
        batch_cursor = await self._db.execute(
            f"SELECT {_BATCH_COLS} FROM host_batches WHERE workflow_id = ?",
            (workflow_id,),
        )
        existing_batch_row = await batch_cursor.fetchone()
        if existing_batch_row is not None:
            existing = _row_to_batch(existing_batch_row)
            settled_cursor = await self._db.execute(
                "SELECT s.state, r.status FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                (existing["batch_id"],),
            )
            if _children_settled_rows(await settled_cursor.fetchall()):
                raise AlreadyTerminalError(workflow_id)
            if existing["fingerprint"] != fingerprint:
                raise WorkflowIdConflictError(
                    workflow_id,
                    batch_mismatch_aspect(
                        existing,
                        definition_name=definition_name,
                        def_version=def_version,
                        def_struct_hash=def_struct_hash,
                        items_canonical=canonical_json(_items_map(items)),
                        tolerance_json=tolerance_json,
                        start_at=start_at,
                    ),
                )
            return existing
        submission_cursor = await self._db.execute("SELECT state FROM host_submissions WHERE workflow_id = ?", (workflow_id,))
        submission_row = await submission_cursor.fetchone()
        if submission_row is not None:
            if submission_row[0] == "finished":
                raise AlreadyTerminalError(workflow_id)
            raise WorkflowIdConflictError(workflow_id, "an existing Run owns this workflow_id")
        run_cursor = await self._db.execute(_SELECT_RUN_STATUS, (workflow_id,))
        run_row = await run_cursor.fetchone()
        if run_row is not None:
            _raise_on_tier0_reuse(str(run_row[0]), workflow_id=workflow_id)
        return None

    async def _reserve_child_ids(self, workflow_id: str, items: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
        """Async mirror of ``_reserve_child_ids_sync``."""
        child_ids = [(key, f"{workflow_id}:{key}", inputs_json) for key, inputs_json in items]
        for key, child_workflow_id, _inputs_json in child_ids:
            collision_cursor = await self._db.execute(
                "SELECT 1 FROM host_submissions WHERE workflow_id = ? UNION SELECT 1 FROM host_batches WHERE workflow_id = ?",
                (child_workflow_id, child_workflow_id),
            )
            if await collision_cursor.fetchone() is not None:
                raise WorkflowIdConflictError(
                    child_workflow_id,
                    f"the child workflow id for item {key!r} collides with existing work in this Run Home",
                )
            child_run_cursor = await self._db.execute(_SELECT_RUN_STATUS, (child_workflow_id,))
            child_run_row = await child_run_cursor.fetchone()
            if child_run_row is not None:
                _raise_on_tier0_reuse(str(child_run_row[0]), workflow_id=child_workflow_id, item_key=key)
        return child_ids

    async def _insert_children(
        self,
        child_ids: list[tuple[str, str, str]],
        *,
        batch_id: str,
        definition_name: str,
        def_version: str,
        def_struct_hash: str,
        start_at: str | None,
        source_ref: str | None,
        recovery_cap: int,
        child_retry_of: dict[str, str] | None,
        now: str,
    ) -> None:
        """Async mirror of ``_insert_children_sync``."""
        for key, child_workflow_id, inputs_json in child_ids:
            child_source = (child_retry_of or {}).get(key)
            child_retry_index = None
            if child_source is not None:
                child_ordinal_cursor = await self._db.execute(_COUNT_ACCEPTED_RETRIES, (child_source,))
                child_retry_index = _next_ordinal(await child_ordinal_cursor.fetchone())
            await self._db.execute(
                f"INSERT INTO host_submissions ({_SUBMISSION_COLS}) VALUES ({_SUBMISSION_PLACEHOLDERS})",
                _child_submission_row(
                    child_workflow_id,
                    inputs_json,
                    key,
                    batch_id=batch_id,
                    definition_name=definition_name,
                    def_version=def_version,
                    def_struct_hash=def_struct_hash,
                    start_at=start_at,
                    source_ref=source_ref,
                    recovery_cap=recovery_cap,
                    child_source=child_source,
                    child_retry_index=child_retry_index,
                    now=now,
                ),
            )
            await self._append_run_update(
                child_workflow_id,
                "submitted",
                {"definition_name": definition_name, "workflow_id": child_workflow_id, "batch_id": batch_id, "item_key": key},
            )

    def _get_batch_sync(self, batch_id: str) -> dict[str, Any] | None:
        with self._sync_lock:
            db = self._sync_db()
            row = db.execute(
                f"SELECT {_BATCH_COLS} FROM host_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            return _row_to_batch(row) if row is not None else None

    async def _get_batch(self, batch_id: str) -> dict[str, Any] | None:
        """Async mirror of ``_get_batch_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(
                f"SELECT {_BATCH_COLS} FROM host_batches WHERE batch_id = ?",
                (batch_id,),
            )
            row = await cursor.fetchone()
            return _row_to_batch(row) if row is not None else None

    def _batch_child_rows_sync(self, batch_id: str) -> dict[str, tuple[dict[str, Any], Run | None]]:
        """Batch children joined with their runs rows, keyed by item key."""
        with self._sync_lock:
            db = self._sync_db()
            rows: dict[str, tuple[dict[str, Any], Run | None]] = {}
            cursor = db.execute(
                f"SELECT {_QUALIFIED_SUBMISSION_COLS}, {_QUALIFIED_RUN_COLS} FROM host_submissions s "
                "LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                (batch_id,),
            )
            sub_count = len(_SUBMISSION_COLS.split(", "))
            for row in cursor.fetchall():
                submission = _row_to_submission(row[:sub_count])
                run = self._row_to_run(row[sub_count:]) if row[sub_count] is not None else None
                rows[submission["item_key"]] = (submission, run)
            return rows

    async def _batch_child_rows(self, batch_id: str) -> dict[str, tuple[dict[str, Any], Run | None]]:
        """Async mirror of ``_batch_child_rows_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            rows: dict[str, tuple[dict[str, Any], Run | None]] = {}
            cursor = await self._db.execute(
                f"SELECT {_QUALIFIED_SUBMISSION_COLS}, {_QUALIFIED_RUN_COLS} FROM host_submissions s "
                "LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                (batch_id,),
            )
            sub_count = len(_SUBMISSION_COLS.split(", "))
            for row in await cursor.fetchall():
                submission = _row_to_submission(row[:sub_count])
                run = self._row_to_run(row[sub_count:]) if row[sub_count] is not None else None
                rows[submission["item_key"]] = (submission, run)
            return rows

    def _read_batch_updates_sync(self, batch_id: str, after_bseq: int = 0) -> list[tuple[int, str, str, str]]:
        """Read batch_updates rows with bseq > after_bseq, in bseq order."""
        with self._sync_lock:
            db = self._sync_db()
            cursor = db.execute(
                "SELECT bseq, kind, payload, created_at FROM batch_updates WHERE batch_id = ? AND bseq > ? ORDER BY bseq",
                (batch_id, after_bseq),
            )
            return [(int(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in cursor.fetchall()]

    async def _read_batch_updates(self, batch_id: str, after_bseq: int = 0) -> list[tuple[int, str, str, str]]:
        """Async mirror of ``_read_batch_updates_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(
                "SELECT bseq, kind, payload, created_at FROM batch_updates WHERE batch_id = ? AND bseq > ? ORDER BY bseq",
                (batch_id, after_bseq),
            )
            rows = await cursor.fetchall()
            return [(int(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows]

    async def _all_children_settled(self, batch_id: str) -> bool:
        """True when every manifest child is accounted for good.

        THE watch-termination question for a Batch, and deliberately not
        "has this Batch been stopped": a durable ``stopped`` fact is a
        control fact appended BEFORE the child stop commands it writes, and
        each of those commits its own ``child_unstarted`` fact later. Ending
        the stream on ``stopped`` dropped exactly those facts (PRD 0019 A9).
        Settled here means terminal, unstarted (finished with no runs row),
        or recovery-exhausted — the same rule ``BatchView.settled`` uses,
        and every path that reaches it commits the item's Batch fact in the
        same transaction as the state flip, so settled implies delivered.
        """
        await self._ensure_db()
        async with self._txn_lock():
            children_cursor = await self._db.execute(
                "SELECT s.state, r.status FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                (batch_id,),
            )
            return _children_settled_rows(await children_cursor.fetchall())

    def _write_batch_stop_sync(self, batch_id: str, info: Any, source_ref: str | None = None) -> bool:
        """Record a durable Batch stop, atomically, in ONE transaction.

        Appends the 'stopped' batch_updates row and writes a durable stop
        command (with its 'command' run update) for every child that has
        not settled yet — pending children then finish via the existing
        stop-before-start gate (no runs row invented, and the gate appends
        each one's ``child_unstarted`` Batch fact, since the 'stopped' row
        names no items) and executing children receive the stop on the
        worker's next scan. Children with an unapplied stop already keep
        their first stop (first stop wins).

        Returns True when the Batch stop was newly written; False when the
        Batch was already stopped (duplicate). Raises ``HostError`` for an
        unknown batch and ``AlreadyTerminalError`` when every child is
        already settled.
        """
        with self._sync_lock:
            db = self._sync_db()
            try:
                db.execute("BEGIN IMMEDIATE")
                batch_row = db.execute(
                    f"SELECT {_BATCH_COLS} FROM host_batches WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()
                if batch_row is None:
                    raise HostError(f"Cannot stop batch {batch_id!r}: no such batch in this Run Home.")
                batch = _row_to_batch(batch_row)
                child_rows = db.execute(
                    "SELECT s.workflow_id, s.state, r.status FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                    (batch_id,),
                ).fetchall()
                if _children_settled_rows([(state, status) for _wf, state, status in child_rows]):
                    raise AlreadyTerminalError(batch["workflow_id"])
                stopped_row = db.execute(
                    "SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind = 'stopped' LIMIT 1",
                    (batch_id,),
                ).fetchone()
                if stopped_row is not None:
                    db.rollback()
                    return False
                now = _now_iso()
                self._append_batch_update_sync(
                    db,
                    batch_id,
                    "stopped",
                    {"verb": STOP_VERB, "info": info, "source_ref": source_ref},
                )
                for child_workflow_id, _state, _status in child_rows:
                    if is_child_settled(_state, _status):
                        continue
                    existing = db.execute(
                        "SELECT id FROM host_commands WHERE run_id = ? AND verb = ? AND applied_at IS NULL LIMIT 1",
                        (child_workflow_id, STOP_VERB),
                    ).fetchone()
                    if existing is not None:
                        continue
                    db.execute(
                        "INSERT INTO host_commands (run_id, verb, payload, source_ref, created_at) VALUES (?, ?, ?, ?, ?)",
                        (child_workflow_id, STOP_VERB, json.dumps({"info": info}), source_ref, now),
                    )
                    self._append_run_update_sync(db, child_workflow_id, "command", {"verb": STOP_VERB, "info": info, "source_ref": source_ref})
                db.commit()
                return True
            except BaseException:
                self._rollback_sync(db)
                raise

    async def _write_batch_stop(self, batch_id: str, info: Any, source_ref: str | None = None) -> bool:
        """Async mirror of ``_write_batch_stop_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                batch_cursor = await self._db.execute(
                    f"SELECT {_BATCH_COLS} FROM host_batches WHERE batch_id = ?",
                    (batch_id,),
                )
                batch_row = await batch_cursor.fetchone()
                if batch_row is None:
                    raise HostError(f"Cannot stop batch {batch_id!r}: no such batch in this Run Home.")
                batch = _row_to_batch(batch_row)
                children_cursor = await self._db.execute(
                    "SELECT s.workflow_id, s.state, r.status FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?",
                    (batch_id,),
                )
                child_rows = await children_cursor.fetchall()
                if _children_settled_rows([(state, status) for _wf, state, status in child_rows]):
                    raise AlreadyTerminalError(batch["workflow_id"])
                stopped_cursor = await self._db.execute(
                    "SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind = 'stopped' LIMIT 1",
                    (batch_id,),
                )
                if await stopped_cursor.fetchone() is not None:
                    await self._db.rollback()
                    return False
                now = _now_iso()
                await self._append_batch_update(
                    batch_id,
                    "stopped",
                    {"verb": STOP_VERB, "info": info, "source_ref": source_ref},
                )
                for child_workflow_id, _state, _status in child_rows:
                    if is_child_settled(_state, _status):
                        continue
                    existing_cursor = await self._db.execute(
                        "SELECT id FROM host_commands WHERE run_id = ? AND verb = ? AND applied_at IS NULL LIMIT 1",
                        (child_workflow_id, STOP_VERB),
                    )
                    if await existing_cursor.fetchone() is not None:
                        continue
                    await self._db.execute(
                        "INSERT INTO host_commands (run_id, verb, payload, source_ref, created_at) VALUES (?, ?, ?, ?, ?)",
                        (child_workflow_id, STOP_VERB, json.dumps({"info": info}), source_ref, now),
                    )
                    await self._append_run_update(child_workflow_id, "command", {"verb": STOP_VERB, "info": info, "source_ref": source_ref})
                await self._db.commit()
                return True
            except BaseException:
                await self._rollback_async()
                raise

    # === Answered pauses re-enter claim order (PRD 0017 US10/US36) ===

    async def _readmit_answered_pause(self, run_id: str, pause_id: str | None) -> bool:
        """Return an answered parked submission to claim order; caller holds the txn.

        THE paused→runnable transition, and deliberately a plain flip back
        to ``pending``: an answered child is ordinary queued work, subject
        to the same Definition-compatibility, delayed-start, admission-cap,
        stop, recovery-brake, and worker-lock rules as every other
        submission. Answering never jumps the queue.

        The compare-and-set on ``state = 'paused'`` is what makes the whole
        thing safe. It flips exactly once, so a re-fired timer or a replayed
        settlement cannot re-admit twice; and it flips NOTHING while the
        worker still holds the claim — a run answered in the window between
        its pause commit and ``_release_submission`` is re-admitted by that
        release instead, which reads the same settled slot. Between them the
        child becomes runnable exactly once, whichever side commits first.

        Returns True when this call performed the transition.
        """
        result = await self._db.execute(
            "UPDATE host_submissions SET state = 'pending', claimed_at = NULL, finished_at = NULL WHERE workflow_id = ? AND state = ?",
            (run_id, SUBMISSION_STATE_PAUSED),
        )
        if result.rowcount != 1:
            return False
        await self._append_child_runnable(run_id, pause_id)
        return True

    def _readmit_answered_pause_sync(self, db: Any, run_id: str, pause_id: str | None) -> bool:
        """Sync mirror of ``_readmit_answered_pause``."""
        result = db.execute(
            "UPDATE host_submissions SET state = 'pending', claimed_at = NULL, finished_at = NULL WHERE workflow_id = ? AND state = ?",
            (run_id, SUBMISSION_STATE_PAUSED),
        )
        if result.rowcount != 1:
            return False
        self._append_child_runnable_sync(db, run_id, pause_id)
        return True

    async def settle_pause(self, run_id: str, *, pause_id: str | None = None, value: Any) -> PauseSlot:
        """Settle one occurrence AND re-admit the run, in ONE transaction.

        This is the Run Home's extension of the checkpointer's settlement:
        the schema check, the compare-and-set on ``settled_at IS NULL``, the
        persisted answer, the durable ``answer`` run update, the
        ``child_runnable`` Batch update, and the submission's paused→pending
        transition all commit together. No observer can see an answered
        child that is permanently unclaimable, and process loss immediately
        after ``answer()`` returns cannot lose either half.

        Every refusal still comes from the shared cascade
        (``base._check_settlement``) and is raised before any write, so a
        rejected value leaves both the occurrence and the parked submission
        exactly as they were.
        """
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                slot = await self._settle_pause_in_txn(run_id, pause_id=pause_id, value=value)
                await self._readmit_answered_pause(run_id, slot.pause_id)
                await self._db.commit()
                return slot
            except BaseException:
                await self._rollback_async()
                raise

    def settle_pause_sync(self, run_id: str, *, pause_id: str | None = None, value: Any) -> PauseSlot:
        """Sync mirror of ``settle_pause``."""
        with self._sync_lock:
            db = self._sync_db()
            try:
                db.execute("BEGIN IMMEDIATE")
                slot = self._settle_pause_in_txn_sync(db, run_id, pause_id=pause_id, value=value)
                self._readmit_answered_pause_sync(db, run_id, slot.pause_id)
                db.commit()
                return slot
            except BaseException:
                self._rollback_sync(db)
                raise

    async def _claim_eligible(
        self, now_iso: str | None = None, *, served: Collection[DefinitionId], limit: int = _CLAIM_BATCH
    ) -> list[dict[str, Any]]:
        """CAS-claim eligible pending submissions (state -> 'claimed').

        Eligible means ``state='pending'``, ``compat_state='compatible'``,
        and ``start_at`` absent or past. ``now_iso`` defaults to the STORE's
        clock (``_store_now``), so eligibility never depends on a worker
        process's own clock; a caller that scans several due sets in one
        pass passes one instant down, and tests drive time explicitly. A
        submission is claimed only when its pinned Definition identity is in
        ``served`` — the exact served identities plus any ``accepts=``
        declarations. An unserved submission is marked
        ``compat_state='incompatible'`` (idempotent): later scans skip it so
        incompatible rows never starve claimable ones, and clients see
        ``WaitingCondition.VERSION_INCOMPATIBLE``.

        This is also the single admission choke point a tolerance trip
        closes: a child of a tripped Batch is never claimed. It is finished
        without executing instead, so a child that a crash returned to
        'pending' after its Batch tripped becomes an explicit unstarted
        item rather than sitting claimable forever — and a durable
        ``child_unstarted`` Batch fact commits with that flip, so the
        per-Batch sequence accounts the item too (PRD 0019 A9).

        The trip closes admission for work that has not STARTED. A child
        with a runs row already began — a crash returned it to pending, or a
        human answered its pause — and calling that item "unstarted" would
        be a plain falsehood: it has committed steps, and its submission
        would read settled while its bucket read queued. Such a child is
        claimed and allowed to reach its own terminal outcome, which is the
        same courtesy the trip extends to already-claimed children.

        It is also where host work admission applies: at most the Home's
        stored ``max_active_runs`` submissions are claimed at once, counting
        the claims already outstanding. Over-limit work is left pending and
        waits in claim order (oldest ``created_at`` first, ``rowid``
        breaking ties) — it is never rejected or cancelled, and it is
        reported as
        ``WaitingCondition.ADMISSION_LIMITED``. A full cap never starves the
        non-claiming dispositions: tripped-Batch children and
        version-incompatible rows are still settled on this scan.
        """
        served_set = frozenset(served)
        now_iso = await self._store_now() if now_iso is None else now_iso
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                cursor = await self._db.execute(
                    f"SELECT {_SUBMISSION_COLS} FROM host_submissions "
                    f"WHERE state = 'pending' AND compat_state = 'compatible' AND {_due_clause('start_at', null_is_due=True)} "
                    # rowid breaks created_at ties so claim order is TOTAL:
                    # two submissions accepted inside the same microsecond
                    # would otherwise be ordered arbitrarily, and "over-limit
                    # work waits in claim order" would be undefined for them.
                    "ORDER BY created_at, rowid LIMIT ?",
                    (now_iso, limit),
                )
                submissions = [_row_to_submission(row) for row in await cursor.fetchall()]
                free_slots = await self._free_admission_slots()
                tripped = await self._tripped_batch_ids({s["batch_id"] for s in submissions if s["batch_id"] is not None})
                claimed: list[dict[str, Any]] = []
                for submission in submissions:
                    if submission["batch_id"] in tripped and not await self._has_run_row(submission["workflow_id"]):
                        result = await self._db.execute(
                            "UPDATE host_submissions SET state = 'finished', finished_at = ? WHERE workflow_id = ? AND state = 'pending'",
                            (now_iso, submission["workflow_id"]),
                        )
                        if result.rowcount == 1:
                            # A9: this item ends unstarted AFTER the trip
                            # fact already listed its unstarted items, so it
                            # gets its own durable row in the SAME
                            # transaction as the state flip. Without it a
                            # detached watch() would never learn the item's
                            # outcome and the stream could not reconstruct
                            # the view.
                            await self._append_child_unstarted(submission["workflow_id"])
                        continue
                    identity = DefinitionId(
                        submission["definition_name"],
                        submission["def_version"],
                        submission["def_struct_hash"],
                    )
                    if identity not in served_set:
                        # Refuse loudly and durably: this worker cannot serve
                        # the pinned identity; a new worker/version
                        # re-evaluates via the restart scan.
                        logger.warning(
                            "Worker cannot serve submission %s: pinned identity %s is not served by this host; "
                            "marking it version-incompatible (it stays parked until a serving worker or explicit migration).",
                            submission["workflow_id"],
                            identity.to_dict(),
                        )
                        await self._db.execute(
                            "UPDATE host_submissions SET compat_state = 'incompatible' WHERE workflow_id = ? AND state = 'pending'",
                            (submission["workflow_id"],),
                        )
                        continue
                    if free_slots is not None and free_slots <= 0:
                        # Over the active-Run cap: leave it pending. Claim
                        # order is the scan order, so the oldest waiting
                        # submission takes the next freed slot.
                        continue
                    result = await self._db.execute(
                        "UPDATE host_submissions SET state = 'claimed', claimed_at = ? WHERE workflow_id = ? AND state = 'pending'",
                        (now_iso, submission["workflow_id"]),
                    )
                    if result.rowcount == 1:
                        claimed.append(submission)
                        if free_slots is not None:
                            free_slots -= 1
                await self._db.commit()
                return claimed
            except BaseException:
                await self._rollback_async()
                raise

    async def _has_run_row(self, workflow_id: str) -> bool:
        """Whether this submission ever started executing; caller holds the txn."""
        cursor = await self._db.execute("SELECT 1 FROM runs WHERE id = ?", (workflow_id,))
        return await cursor.fetchone() is not None

    async def _tripped_batch_ids(self, batch_ids: Collection[str]) -> frozenset[str]:
        """Which of ``batch_ids`` have a durable trip; caller holds the transaction."""
        ids = list(batch_ids)
        if not ids:
            return frozenset()
        placeholders = ", ".join("?" for _ in ids)
        cursor = await self._db.execute(
            f"SELECT DISTINCT batch_id FROM batch_updates WHERE kind = '{_TRIP_UPDATE_KIND}' AND batch_id IN ({placeholders})",
            ids,
        )
        return frozenset(str(row[0]) for row in await cursor.fetchall())

    async def _release_submission(self, workflow_id: str) -> None:
        """Release a claimed submission the worker is done executing.

        THE outcome branch a worker takes when ``runner.run`` returns. It
        reads the run's own committed status rather than trusting the
        caller, so the durable submission state can never disagree with the
        journal:

        - a run that came back ``PAUSED`` parked on a durable interrupt.
          The worker is finished with it (it stops holding an active-Run
          slot) but the RUN is not: a human answer is outstanding. It gets
          ``paused`` and NO ``finished_at`` — writing 'finished' here made
          the same child ``active`` to the bucket ladder and settled to
          ``is_child_settled``, so ``watch(batch_ref)`` ended while a
          decision was still open, and a detached ``stop`` of the parked
          run was refused as terminal.
        - a run that came back ``PAUSED`` whose occurrence is ALREADY
          settled is not parked at all: somebody answered it in the window
          between the pause commit and this release. Parking it would strand
          an answered run forever, because ``settle_pause``'s re-admission
          compare-and-set found the submission still ``claimed`` and flipped
          nothing. It goes straight back to ``pending`` — runnable, with its
          ``child_runnable`` Batch fact — so the two paths together admit
          the child exactly once whichever commits first.
        - every other outcome is settled work: ``finished`` with its
          timestamp.

        A ``paused`` submission stays parked and unclaimable until its
        occurrence is answered; ``settle_pause`` is what re-admits it.
        """
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                cursor = await self._db.execute(_SELECT_RUN_STATUS, (workflow_id,))
                run_row = await cursor.fetchone()
                paused = run_row is not None and str(run_row[0]) == WorkflowStatus.PAUSED.value
                answered = await self._settled_slot(workflow_id) if paused else None
                if answered is not None:
                    await self._db.execute(
                        "UPDATE host_submissions SET state = 'pending', claimed_at = NULL, finished_at = NULL WHERE workflow_id = ?",
                        (workflow_id,),
                    )
                    await self._append_child_runnable(workflow_id, answered)
                else:
                    state = SUBMISSION_STATE_PAUSED if paused else SUBMISSION_STATE_FINISHED
                    await self._db.execute(
                        "UPDATE host_submissions SET state = ?, finished_at = ? WHERE workflow_id = ?",
                        (state, None if paused else _now_iso(), workflow_id),
                    )
                await self._db.commit()
            except BaseException:
                await self._rollback_async()
                raise

    async def _settled_slot(self, run_id: str) -> str | None:
        """The current occurrence's ``pause_id`` when it is already answered.

        None means "no occurrence, or the question is still open". Caller
        holds the transaction, so the answer this reads is the one the
        caller's decision commits against.
        """
        cursor = await self._db.execute(_CURRENT_SLOT_SQL, (run_id,))
        row = await cursor.fetchone()
        return None if row is None or row[1] is None else str(row[0])

    async def _reset_unstarted_run(self, run_id: str) -> bool:
        """Delete a runs row that carries zero execution history.

        A claimed run killed before its first committed step leaves an
        empty ``active`` runs row that can neither resume (checkpoint state
        has no seed inputs) nor restart (input override is forbidden).
        Deleting the history-less row lets the worker start fresh from the
        submission's pinned inputs; the submission (and its recorded
        lineage) stays the source of truth. Any committed step or
        attempt-ledger row — or a non-active status — keeps the runs row;
        that work resumes through the normal crash-recovery path instead.

        Returns True when the row was deleted. The deletion and its durable
        ``run_reset`` update commit in one transaction.
        """
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                result = await self._db.execute(
                    "DELETE FROM runs WHERE id = ? AND status = 'active' "
                    "AND NOT EXISTS (SELECT 1 FROM steps WHERE run_id = ?) "
                    "AND NOT EXISTS (SELECT 1 FROM attempt_series WHERE run_id = ?)",
                    (run_id, run_id, run_id),
                )
                if result.rowcount == 1:
                    await self._append_run_update(
                        run_id,
                        "run_reset",
                        {"reason": "claimed run lost before its first committed step; starting fresh from pinned inputs"},
                    )
                await self._db.commit()
                return result.rowcount == 1
            except BaseException:
                await self._rollback_async()
                raise

    async def _restart_scan(self) -> None:
        """Re-adopt unfinished claimed submissions on worker startup.

        Claimed submissions whose run settled terminally are marked
        finished. Every other claimed submission is a recovery attempt and
        gets ``recovery_attempts += 1``: when the incremented count reaches
        the submission's ``recovery_cap`` it is parked as 'exhausted' (a
        durable ``recovery_exhausted`` run update — and, for a Batch child,
        a ``child_settled`` Batch fact — is appended in the same
        transaction) so a crash-looping run is braked instead of resumed
        forever; otherwise it returns to 'pending' with the incremented
        count. The counter is NOT evaluated against step history here —
        NEW committed progress (step saves, durable pauses, terminal
        transitions) resets it to 0 at commit time via
        ``_after_run_mutation``, so a run killed WITH committed steps still
        shows the incremented attempt count after re-adoption (prototype
        Scenario 3). Pending submissions also reset to
        ``compat_state='compatible'`` so a new worker/deployment
        re-evaluates version compatibility from scratch.
        """
        await self._ensure_db()
        now = _now_iso()
        terminal_placeholders = ", ".join("?" for _ in _TERMINAL_STATUS_VALUES)
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                await self._db.execute(
                    f"UPDATE host_submissions SET state = 'finished', finished_at = ? "
                    f"WHERE state = 'claimed' AND workflow_id IN "
                    f"(SELECT id FROM runs WHERE status IN ({terminal_placeholders}))",
                    (now, *_TERMINAL_STATUS_VALUES),
                )
                cursor = await self._db.execute(
                    f"SELECT {_SUBMISSION_COLS} FROM host_submissions WHERE state = 'claimed'",
                )
                for row in await cursor.fetchall():
                    submission = _row_to_submission(row)
                    workflow_id = submission["workflow_id"]
                    attempts = submission["recovery_attempts"] + 1
                    if attempts >= submission["recovery_cap"]:
                        # Recovery brake: the run was re-adopted without new
                        # committed progress too many times. Park it
                        # exhausted; client.rerun() revives.
                        await self._db.execute(
                            "UPDATE host_submissions SET state = 'exhausted', recovery_attempts = ? WHERE workflow_id = ?",
                            (attempts, workflow_id),
                        )
                        await self._append_run_update(
                            workflow_id,
                            "recovery_exhausted",
                            {"recovery_attempts": attempts, "recovery_cap": submission["recovery_cap"]},
                        )
                        # A parked child is a settled, failure-equivalent
                        # child OUTCOME (PRD 0019), so it is Batch truth:
                        # its Batch learns it as a child_settled fact whose
                        # status is the same "recovery_exhausted" string
                        # BatchView.outcomes reports, in this same
                        # transaction. That fact can also trip the Batch,
                        # like any other child fact. Without it the view
                        # counted the parked child and the durable stream
                        # stayed silent, so a detached watch() under-
                        # accounted the manifest (A9).
                        await self._append_child_settled(workflow_id, BATCH_OUTCOME_RECOVERY_EXHAUSTED)
                        continue
                    await self._db.execute(
                        "UPDATE host_submissions SET state = 'pending', claimed_at = NULL, recovery_attempts = ? WHERE workflow_id = ?",
                        (attempts, workflow_id),
                    )
                # A new worker (possibly a new deployment) re-evaluates
                # version compatibility from scratch.
                await self._db.execute(
                    "UPDATE host_submissions SET compat_state = 'compatible' WHERE state = 'pending'",
                )
                await self._db.commit()
            except BaseException:
                await self._rollback_async()
                raise

    # === host_commands (durable stop channel) ===
    #
    # host_commands carries exactly the two verbs in HOST_COMMAND_VERBS:
    # STOP_VERB (below) and SCHEDULE_ANSWER_VERB (further down). Both are
    # host-owned — a caller never names a verb — and every statement binds
    # one of the constants rather than writing a bare literal, so the set of
    # verbs the Home can write stays closed and greppable. Neither dedup nor
    # eligibility ever reads source_ref.

    def _write_stop_command_sync(self, workflow_id: str, info: Any, source_ref: str | None = None) -> bool:
        """Record a durable stop command plus its 'command' update, atomically.

        Returns True when a new command row was written; False when an
        unapplied stop already exists (the first stop owns its ``info`` and
        nothing new is written). Raises ``HostError`` for an unknown run and
        ``AlreadyTerminalError`` when the run is already terminal (or its
        submission already finished) at write time. A ``paused`` submission
        is neither: the run parked on a durable interrupt and is still
        stoppable, so the command is accepted and waits, exactly like a stop
        aimed at a crashed-but-resumable run. ``source_ref`` is an opaque
        caller provenance marker (ADR 0005 A11) stored on the command row;
        it never affects dedup.
        """
        with self._sync_lock:
            db = self._sync_db()
            try:
                db.execute("BEGIN IMMEDIATE")
                submission_row = db.execute(
                    "SELECT state FROM host_submissions WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                run_row = db.execute(_SELECT_RUN_STATUS, (workflow_id,)).fetchone()
                if submission_row is None and run_row is None:
                    raise HostError(f"Cannot stop {workflow_id!r}: no such run in this Run Home.")
                if run_row is not None and run_row[0] in _TERMINAL_STATUS_VALUES:
                    raise AlreadyTerminalError(workflow_id)
                if submission_row is not None and submission_row[0] == "finished":
                    raise AlreadyTerminalError(workflow_id)
                existing = db.execute(
                    "SELECT id FROM host_commands WHERE run_id = ? AND verb = ? AND applied_at IS NULL LIMIT 1",
                    (workflow_id, STOP_VERB),
                ).fetchone()
                if existing is not None:
                    db.rollback()
                    return False
                db.execute(
                    "INSERT INTO host_commands (run_id, verb, payload, source_ref, created_at) VALUES (?, ?, ?, ?, ?)",
                    (workflow_id, STOP_VERB, json.dumps({"info": info}), source_ref, _now_iso()),
                )
                self._append_run_update_sync(db, workflow_id, "command", {"verb": STOP_VERB, "info": info, "source_ref": source_ref})
                db.commit()
                return True
            except BaseException:
                self._rollback_sync(db)
                raise

    async def _write_stop_command(self, workflow_id: str, info: Any, source_ref: str | None = None) -> bool:
        """Async mirror of ``_write_stop_command_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                submission_cursor = await self._db.execute(
                    "SELECT state FROM host_submissions WHERE workflow_id = ?",
                    (workflow_id,),
                )
                submission_row = await submission_cursor.fetchone()
                run_cursor = await self._db.execute(_SELECT_RUN_STATUS, (workflow_id,))
                run_row = await run_cursor.fetchone()
                if submission_row is None and run_row is None:
                    raise HostError(f"Cannot stop {workflow_id!r}: no such run in this Run Home.")
                if run_row is not None and run_row[0] in _TERMINAL_STATUS_VALUES:
                    raise AlreadyTerminalError(workflow_id)
                if submission_row is not None and submission_row[0] == "finished":
                    raise AlreadyTerminalError(workflow_id)
                existing_cursor = await self._db.execute(
                    "SELECT id FROM host_commands WHERE run_id = ? AND verb = ? AND applied_at IS NULL LIMIT 1",
                    (workflow_id, STOP_VERB),
                )
                if await existing_cursor.fetchone() is not None:
                    await self._db.rollback()
                    return False
                await self._db.execute(
                    "INSERT INTO host_commands (run_id, verb, payload, source_ref, created_at) VALUES (?, ?, ?, ?, ?)",
                    (workflow_id, STOP_VERB, json.dumps({"info": info}), source_ref, _now_iso()),
                )
                await self._append_run_update(workflow_id, "command", {"verb": STOP_VERB, "info": info, "source_ref": source_ref})
                await self._db.commit()
                return True
            except BaseException:
                await self._rollback_async()
                raise

    # === host_commands (scheduled pause answers — ADR 0008) ===

    def _scheduled_answer_row(
        self,
        workflow_id: str,
        pause_id: str | None,
        value: Any,
        due_at: datetime | str,
        source_ref: str | None,
    ) -> _ScheduledAnswerRow:
        """Normalize one scheduled answer into its stored row shape.

        Raises before any store work: a scheduled answer with no due time is
        just an answer, and ``client.answer`` already applies those. The
        stored ``due_at`` is therefore never NULL, which is why the due
        predicate treats a NULL one as inert rather than instantly due.
        """
        due_at_iso = _normalize_utc_iso(due_at, field="due_at")
        if due_at_iso is None:
            raise ValueError(
                "schedule_answer() requires a due_at time.\n\nHow to fix:\n  Pass due_at=<datetime or ISO string> for when the answer should apply, or call client.answer(...) to answer the pause now."
            )
        return _ScheduledAnswerRow(
            run_id=workflow_id,
            payload=json.dumps({"pause_id": pause_id, "value": value}),
            due_at=due_at_iso,
            source_ref=source_ref,
            created_at=_now_iso(),
        )

    def _write_scheduled_answer_sync(
        self,
        workflow_id: str,
        *,
        pause_id: str | None,
        value: Any,
        due_at: datetime | str,
        source_ref: str | None = None,
    ) -> bool:
        """Record one scheduled answer for the named pause occurrence, atomically.

        Admission runs the SAME refusal cascade a human answer gets
        (``base._check_settlement``): an unnamed, unknown, superseded,
        already-settled, or schema-failing scheduled answer is refused before
        any write, so an unarmable timer is never accepted and later
        discovered dead. The value is validated twice on purpose — here, so
        the caller learns immediately, and again at fire time, because the
        slot is what settlement compares against.

        Returns True when a new command row was written; False when an
        unapplied scheduled answer already exists for this occurrence — one
        scheduled answer per pause, first one wins, exactly like ``stop``.
        ``source_ref`` is opaque audit provenance and is read by neither
        check.
        """
        row = self._scheduled_answer_row(workflow_id, pause_id, value, due_at, source_ref)
        with self._sync_lock:
            db = self._sync_db()
            try:
                db.execute("BEGIN IMMEDIATE")
                current, known, run_status = self._read_settlement_inputs_sync(db, row.run_id)
                slot = _check_settlement(
                    run_id=row.run_id,
                    pause_id=pause_id,
                    current=current,
                    known_pause_ids=known,
                    run_status=run_status,
                    value=value,
                )
                existing = db.execute(
                    "SELECT id FROM host_commands WHERE run_id = ? AND verb = ? AND pause_id = ? AND applied_at IS NULL LIMIT 1",
                    (row.run_id, SCHEDULE_ANSWER_VERB, slot.pause_id),
                ).fetchone()
                if existing is not None:
                    db.rollback()
                    return False
                db.execute(
                    "INSERT INTO host_commands (run_id, verb, payload, pause_id, due_at, source_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (row.run_id, SCHEDULE_ANSWER_VERB, row.payload, slot.pause_id, row.due_at, row.source_ref, row.created_at),
                )
                self._append_run_update_sync(
                    db,
                    row.run_id,
                    "command",
                    _scheduled_answer_fact(pause_id=slot.pause_id, due_at=row.due_at, source_ref=row.source_ref, outcome=None),
                )
                db.commit()
                return True
            except BaseException:
                self._rollback_sync(db)
                raise

    async def _write_scheduled_answer(
        self,
        workflow_id: str,
        *,
        pause_id: str | None,
        value: Any,
        due_at: datetime | str,
        source_ref: str | None = None,
    ) -> bool:
        """Async mirror of ``_write_scheduled_answer_sync``."""
        row = self._scheduled_answer_row(workflow_id, pause_id, value, due_at, source_ref)
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                current, known, run_status = await self._read_settlement_inputs(row.run_id)
                slot = _check_settlement(
                    run_id=row.run_id,
                    pause_id=pause_id,
                    current=current,
                    known_pause_ids=known,
                    run_status=run_status,
                    value=value,
                )
                existing_cursor = await self._db.execute(
                    "SELECT id FROM host_commands WHERE run_id = ? AND verb = ? AND pause_id = ? AND applied_at IS NULL LIMIT 1",
                    (row.run_id, SCHEDULE_ANSWER_VERB, slot.pause_id),
                )
                if await existing_cursor.fetchone() is not None:
                    await self._db.rollback()
                    return False
                await self._db.execute(
                    "INSERT INTO host_commands (run_id, verb, payload, pause_id, due_at, source_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (row.run_id, SCHEDULE_ANSWER_VERB, row.payload, slot.pause_id, row.due_at, row.source_ref, row.created_at),
                )
                await self._append_run_update(
                    row.run_id,
                    "command",
                    _scheduled_answer_fact(pause_id=slot.pause_id, due_at=row.due_at, source_ref=row.source_ref, outcome=None),
                )
                await self._db.commit()
                return True
            except BaseException:
                await self._rollback_async()
                raise

    async def _due_scheduled_answers(self, now_iso: str) -> list[_DueScheduledAnswer]:
        """Scheduled answers whose due time has arrived, oldest command first.

        Shares ``_due_clause`` — and the caller's single store-authoritative
        ``now`` — with the delayed-start filter in ``_claim_eligible``: one
        due-row scan pass decides both (PRD 0017).
        """
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(
                "SELECT id, run_id, pause_id, payload, due_at, source_ref FROM host_commands "
                f"WHERE verb = ? AND applied_at IS NULL AND {_due_clause('due_at', null_is_due=False)} ORDER BY id",
                (SCHEDULE_ANSWER_VERB, now_iso),
            )
            rows = await cursor.fetchall()
        return [
            _DueScheduledAnswer(
                command_id=int(row[0]),
                run_id=str(row[1]),
                pause_id=row[2],
                value=json.loads(row[3]).get("value"),
                due_at=row[4],
                source_ref=row[5],
            )
            for row in rows
        ]

    async def _settle_due_answers(self, now_iso: str | None = None) -> list[tuple[int, ScheduledAnswerOutcome]]:
        """Fire every due scheduled answer and record what it produced.

        ``now_iso`` defaults to the STORE's clock (``_store_now``); a caller
        that scans several due sets in one pass passes one instant down, and
        tests drive time explicitly.

        Returns ``(command_id, outcome)`` for the timers THIS scan fired —
        never a timer another worker fired first, whose outcome is that
        worker's fact to record. Async only, like every other worker-side
        scan on this Home; scheduling itself has a sync mirror because
        callers are not workers.
        """
        now_iso = await self._store_now() if now_iso is None else now_iso
        results: list[tuple[int, ScheduledAnswerOutcome]] = []
        for due in await self._due_scheduled_answers(now_iso):
            outcome = await self._fire_scheduled_answer(due)
            if outcome is not None:
                results.append((due.command_id, outcome))
        return results

    async def _fire_scheduled_answer(self, due: _DueScheduledAnswer) -> ScheduledAnswerOutcome | None:
        """Settle one due timer and record what THAT attempt produced, atomically.

        The settlement attempt and the ``outcome`` it earns are ONE
        transaction, so the recorded fact is always the truth about this
        command row. Splitting them would let the audit trail lie in two
        ordinary situations: a crash between settling and marking would
        re-fire the timer, which then meets its own settled pause and
        records ``already_settled`` for a timer that actually settled; and a
        second worker scanning the same due row could overwrite the winner's
        fact with its own loser's refusal. Neither is a double settlement —
        the compare-and-set never allowed that — but both are false audit.

        The timer is claimed inside the transaction (``applied_at IS NULL``)
        BEFORE it settles: ``BEGIN IMMEDIATE`` serializes writers, so a
        worker that lost the claim returns None and records nothing rather
        than relabelling the winner's row.

        Settlement itself goes through ``_settle_pause_in_txn`` — THE
        settlement body a human answer takes — so an answer-versus-timer
        race is decided by the same compare-and-set on ``settled_at IS
        NULL``, in commit order, with no preference rule and no second lock.
        A timer that lost, or whose pause was superseded by a later
        occurrence, simply carries the refusal the shared cascade raised.

        "Inapplicable" means **refused at fire time and recorded**, never
        deleted: the row keeps its pause id, due time, value, and
        ``source_ref`` and gains an ``outcome``, and the same fate commits as
        a durable ``command`` run update (PRD 0018 A9) so a detached
        ``watch`` consumer sees it too. ADR 0008's "the loser receives a
        truthful rejection" has no live caller on the timer side — that row
        and that update ARE how the rejection is delivered.
        """
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                claim = await self._db.execute(
                    "SELECT 1 FROM host_commands WHERE id = ? AND applied_at IS NULL",
                    (due.command_id,),
                )
                if await claim.fetchone() is None:
                    # Another worker fired it between the scan and here; its
                    # outcome is that worker's recorded fact, not ours.
                    await self._db.rollback()
                    return None
                outcome: ScheduledAnswerOutcome
                try:
                    slot = await self._settle_pause_in_txn(due.run_id, pause_id=due.pause_id, value=due.value)
                    # A timer's answer re-admits exactly like a human's: same
                    # transaction, same compare-and-set, same Batch fact. A
                    # settled-but-parked child would otherwise be the one
                    # thing ADR 0008 rules out — a decision nothing acts on.
                    await self._readmit_answered_pause(due.run_id, slot.pause_id)
                    outcome = SCHEDULED_ANSWER_SETTLED
                except StalePauseError:
                    outcome = SCHEDULED_ANSWER_SUPERSEDED
                except PauseAlreadySettledError:
                    outcome = SCHEDULED_ANSWER_ALREADY_SETTLED
                except AnswerRejectedError:
                    outcome = SCHEDULED_ANSWER_REJECTED
                # Every refusal is raised before any write, so a refused
                # attempt has left this transaction clean to keep writing in.
                await self._db.execute(
                    "UPDATE host_commands SET applied_at = ?, outcome = ? WHERE id = ?",
                    (_now_iso(), outcome, due.command_id),
                )
                await self._append_run_update(
                    due.run_id,
                    "command",
                    _scheduled_answer_fact(pause_id=due.pause_id, due_at=due.due_at, source_ref=due.source_ref, outcome=outcome),
                )
                await self._db.commit()
                return outcome
            except BaseException:
                await self._rollback_async()
                raise

    async def _unapplied_stop_commands(self) -> list[tuple[int, str, Any]]:
        """Read unapplied stop commands as (command_id, workflow_id, info)."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(
                "SELECT id, run_id, payload FROM host_commands WHERE verb = ? AND applied_at IS NULL ORDER BY id",
                (STOP_VERB,),
            )
            rows = await cursor.fetchall()
            return [(int(row[0]), str(row[1]), json.loads(row[2]).get("info")) for row in rows]

    async def _apply_stop_commands(self, command_ids: Collection[int]) -> None:
        """Mark stop commands applied: the worker observed and acted on them."""
        ids = list(command_ids)
        if not ids:
            return
        placeholders = ", ".join("?" for _ in ids)
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                await self._db.execute(
                    f"UPDATE host_commands SET applied_at = ? WHERE id IN ({placeholders}) AND applied_at IS NULL",
                    (_now_iso(), *ids),
                )
                await self._db.commit()
            except BaseException:
                await self._rollback_async()
                raise

    async def _apply_stop_to_paused(self, workflow_id: str) -> bool:
        """Apply a stop to a run parked on a durable pause, atomically.

        A cooperative stop is normally delivered to a LIVE execution — the
        worker calls ``runner.stop(workflow_id)`` and the run winds itself
        down. A parked run has no live execution to cooperate: it is waiting
        on a person, and it re-executes only if somebody answers. Leaving the
        command unapplied therefore meant "stop" never stopped a paused
        child at all — the one case an operator most wants it for
        (cancelling an upload that is sitting in review).

        So the Host settles it here instead: the run's terminal ``STOPPED``
        transition, its durable run update, its ``child_settled`` Batch fact
        (and any tolerance trip that follows), and the submission's flip to
        ``finished`` all commit together.

        The compare-and-set on ``state = 'paused'`` is what keeps this
        honest against an answer racing it. If the answer commits first the
        submission is already ``pending``, this matches nothing, and the stop
        stays unapplied to be delivered to the resumed execution — an
        ordinary cooperative stop. If this commits first the run is terminal,
        and the answer is refused by the shared settlement cascade ("is
        stopped, not paused"). Commit order is the only winner selection.

        Returns True when this call stopped a parked run.
        """
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                cursor = await self._db.execute(
                    "SELECT 1 FROM host_submissions s JOIN runs r ON r.id = s.workflow_id WHERE s.workflow_id = ? AND s.state = ? AND r.status = ?",
                    (workflow_id, SUBMISSION_STATE_PAUSED, WorkflowStatus.PAUSED.value),
                )
                if await cursor.fetchone() is None:
                    await self._db.rollback()
                    return False
                sql, params = _run_status_update(WorkflowStatus.STOPPED, NO_RUN_TOTALS)
                await self._db.execute(sql, [*params, workflow_id])
                await self._db.execute(
                    "UPDATE host_submissions SET state = ?, finished_at = ? WHERE workflow_id = ? AND state = ?",
                    (SUBMISSION_STATE_FINISHED, _now_iso(), workflow_id, SUBMISSION_STATE_PAUSED),
                )
                # Emits the run update, the child_settled Batch fact, and any
                # tolerance trip — the same fan-out every terminal transition
                # gets, in this transaction.
                await self._after_run_mutation(workflow_id, "status", {"status": WorkflowStatus.STOPPED.value})
                await self._db.commit()
                return True
            except BaseException:
                await self._rollback_async()
                raise

    async def _apply_stop_never_started(self, workflow_id: str) -> bool:
        """Pre-run gate for a stop that arrived before first execution.

        Returns True when an unapplied stop exists AND no runs row exists:
        the command is marked applied and the submission finished (the run
        never executes and no runs row is invented). Returns False when no
        stop is pending or a runs row already exists (a resume proceeds and
        the periodic scan delivers the stop to the live execution).

        A Batch child stopped this way is an explicit unstarted item, so
        the flip commits with its own ``child_unstarted`` Batch fact (PRD
        0019 A9). The ``stopped`` fact carries only the verb and info — it
        never names which items ended unstarted — so without this row a
        detached ``watch(batch_ref)`` could not account those items from
        the stream alone.
        """
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                stop_cursor = await self._db.execute(
                    "SELECT id FROM host_commands WHERE run_id = ? AND verb = ? AND applied_at IS NULL LIMIT 1",
                    (workflow_id, STOP_VERB),
                )
                if await stop_cursor.fetchone() is None:
                    await self._db.commit()
                    return False
                run_cursor = await self._db.execute("SELECT 1 FROM runs WHERE id = ?", (workflow_id,))
                if await run_cursor.fetchone() is not None:
                    await self._db.commit()
                    return False
                now = _now_iso()
                await self._db.execute(
                    "UPDATE host_commands SET applied_at = ? WHERE run_id = ? AND verb = ? AND applied_at IS NULL",
                    (now, workflow_id, STOP_VERB),
                )
                result = await self._db.execute(
                    "UPDATE host_submissions SET state = 'finished', finished_at = ? WHERE workflow_id = ? AND state IN ('pending', 'claimed')",
                    (now, workflow_id),
                )
                if result.rowcount == 1:
                    # Only a real transition emits, so a Batch item is never
                    # accounted twice by the stream.
                    await self._append_child_unstarted(workflow_id)
                await self._db.commit()
                return True
            except BaseException:
                await self._rollback_async()
                raise

    # === listing (client.list) ===

    def _list_run_rows_sync(self) -> list[tuple[dict[str, Any] | None, Run | None]]:
        """All submissions with their runs row, plus bare Tier-0 runs."""
        with self._sync_lock:
            db = self._sync_db()
            rows: list[tuple[dict[str, Any] | None, Run | None]] = []
            cursor = db.execute(
                f"SELECT {_QUALIFIED_SUBMISSION_COLS}, {_QUALIFIED_RUN_COLS} FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id"
            )
            sub_count = len(_SUBMISSION_COLS.split(", "))
            for row in cursor.fetchall():
                submission = _row_to_submission(row[:sub_count])
                run = self._row_to_run(row[sub_count:]) if row[sub_count] is not None else None
                rows.append((submission, run))
            cursor = db.execute(f"SELECT {_RUNS_COLS} FROM runs WHERE id NOT IN (SELECT workflow_id FROM host_submissions)")
            for row in cursor.fetchall():
                rows.append((None, self._row_to_run(row)))
            return rows

    async def _list_run_rows(self) -> list[tuple[dict[str, Any] | None, Run | None]]:
        """Async mirror of ``_list_run_rows_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            rows: list[tuple[dict[str, Any] | None, Run | None]] = []
            cursor = await self._db.execute(
                f"SELECT {_QUALIFIED_SUBMISSION_COLS}, {_QUALIFIED_RUN_COLS} FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id"
            )
            sub_count = len(_SUBMISSION_COLS.split(", "))
            for row in await cursor.fetchall():
                submission = _row_to_submission(row[:sub_count])
                run = self._row_to_run(row[sub_count:]) if row[sub_count] is not None else None
                rows.append((submission, run))
            cursor = await self._db.execute(f"SELECT {_RUNS_COLS} FROM runs WHERE id NOT IN (SELECT workflow_id FROM host_submissions)")
            for row in await cursor.fetchall():
                rows.append((None, self._row_to_run(row)))
            return rows

    # === run_updates reads (watch replay) ===

    def _read_run_updates_sync(self, run_id: str, after_seq: int = 0) -> list[tuple[int, str, str, str]]:
        """Read run_updates rows with seq > after_seq, in seq order."""
        with self._sync_lock:
            db = self._sync_db()
            cursor = db.execute(
                "SELECT seq, kind, payload, created_at FROM run_updates WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            )
            return [(int(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in cursor.fetchall()]

    async def _read_run_updates(self, run_id: str, after_seq: int = 0) -> list[tuple[int, str, str, str]]:
        """Async mirror of ``_read_run_updates_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(
                "SELECT seq, kind, payload, created_at FROM run_updates WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            )
            rows = await cursor.fetchall()
            return [(int(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows]
