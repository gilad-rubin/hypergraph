"""RunHome — the SQLite Run Home for the durable host (Tier 1).

A RunHome IS the existing SQLite checkpointer plus coordination tables
(schema v7): durable submissions, the per-Run durable update sequence, the
host command channel, the worker registry, and the Home-scoped coordination
settings every process that opens the store agrees on
(``max_active_runs``). Steps stay the
sole execution journal; host coordination facts never enter
``RunStatus``/``WorkflowStatus``.

Home-bound runners persist every mutation immediately: the Home forces
checkpoint durability ``"sync"`` and rejects ``"exit"`` policies.
"""

from __future__ import annotations

import itertools
import json
import logging
import threading
from collections.abc import Collection, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hypergraph.checkpointers.base import CheckpointPolicy, _check_settlement

# host/ is the same persistence subsystem as checkpointers/ (a RunHome IS a
# SqliteCheckpointer), so reaching its private column list here is deliberate.
from hypergraph.checkpointers.sqlite import _PUBLIC_STEP_FILTER, _RUNS_COLS, SqliteCheckpointer, _run_status_update
from hypergraph.checkpointers.types import (
    NO_RUN_TOTALS,
    AnswerRejectedError,
    PauseAlreadySettledError,
    StalePauseError,
    WorkflowStatus,
)
from hypergraph.host._batch_store import (
    ABANDONED_UPDATE_KIND,
    CLOSE_PENDING_CHILDREN,
    COUNT_FAILURE_EQUIVALENT,
    INSERT_BATCH,
    INSERT_BATCH_UPDATE,
    MANIFEST_UPDATE_KIND,
    PAUSED_UPDATE_KIND,
    RUNNABLE_UPDATE_KIND,
    SELECT_ACCOUNTED,
    SELECT_BATCH_BY_ID,
    SELECT_BATCH_BY_WORKFLOW,
    SELECT_BATCH_UPDATES,
    SELECT_BATCH_WORKFLOW_ID,
    SELECT_CHILD_ID_COLLISION,
    SELECT_CHILD_SETTLEMENT,
    SELECT_LAST_OCCURRENCE,
    SELECT_MEMBERSHIP,
    SELECT_PENDING_CLOSEOUT,
    SELECT_RECENT_BATCHES,
    SELECT_RECENT_BATCHES_BY_DEFINITION,
    SELECT_STOP_TARGETS,
    SELECT_STOPPED,
    SELECT_TOLERANCE_INPUTS,
    SELECT_TRIPPED,
    SETTLED_UPDATE_KIND,
    TRIP_UPDATE_KIND,
    UNSTARTED_UPDATE_KIND,
    BatchAcceptance,
    BatchStop,
    ChildSpec,
    children_resting_rows,
    children_settled_rows,
    closeout_kind,
    is_repeat_occurrence,
    occurrence_fact,
    refuse_child_id_collision,
    refuse_run_owned_id,
    refuse_tier0_reuse,
    resolve_batch_reuse,
    resolve_batch_stop,
    row_to_batch,
    split_closeout,
    trip_payload,
)
from hypergraph.host._pause_lifecycle import (
    INSERT_COMMAND,
    INSERT_SCHEDULED_ANSWER,
    PARK_SUBMISSION_SQL,
    READMIT_ANSWERED_SQL,
    RELEASE_SUBMISSION_SQL,
    SCHEDULE_ANSWER_VERB,
    SCHEDULED_ANSWER_ALREADY_SETTLED,
    SCHEDULED_ANSWER_REJECTED,
    SCHEDULED_ANSWER_SETTLED,
    SCHEDULED_ANSWER_SUPERSEDED,
    SELECT_UNAPPLIED_COMMAND,
    SELECT_UNAPPLIED_SCHEDULED_ANSWER,
    STOP_VERB,
    DueScheduledAnswer,
    ScheduledAnswerOutcome,
    ScheduledAnswerRow,
    scheduled_answer_fact,
    scheduled_answer_row,
)
from hypergraph.host.definition import DefinitionId
from hypergraph.host.errors import AlreadyTerminalError, HostError, WorkflowIdConflictError
from hypergraph.host.fingerprint import fingerprint_mismatch_aspect
from hypergraph.host.views import (
    BATCH_OUTCOME_DEAD_LETTER,
    BATCH_OUTCOME_RECOVERY_EXHAUSTED,
    DEAD_LETTER_BUILDER_MISSING,
    DEAD_LETTER_UNSERVED_IDENTITY,
    DEAD_LETTERED_UPDATE_KIND,
    SUBMISSION_STATE_DEAD_LETTER,
    SUBMISSION_STATE_FINISHED,
    SUBMISSION_STATE_PAUSED,
    TERMINAL_STATUS_VALUES,
)

if TYPE_CHECKING:
    from hypergraph.checkpointers.types import Checkpoint, PauseSlot, Run

logger = logging.getLogger("hypergraph.host")

#: Worker-lock identity for in-memory Homes, minted once per Home. ``id()``
#: cannot serve: it is unique only among LIVE objects, so a freed Home hands
#: its id to the next allocation and two logically distinct Homes would share
#: one entry in the worker-lock registry. A token is never reused.
_memory_lock_tokens = itertools.count()
_sync_wait_cancellation: ContextVar[threading.Event | None] = ContextVar("host_sync_wait_cancellation", default=None)

_SUBMISSION_COLS = (
    "workflow_id, definition_name, def_version, def_struct_hash, inputs_json, "
    "start_at, state, recovery_attempts, recovery_cap, source_ref, created_at, claimed_at, finished_at, "
    "fingerprint, compat_state, retry_of, retry_index, forked_from, fork_reason, last_progress_step_count, batch_id, item_key, claim_seq, "
    "admission_cost, builder_key, builder_args_json"
)
# ``claimed_by`` and ``lease_until`` exist in the v7 schema and are absent
# here on purpose: nothing in this release writes or reads them, and putting
# an unwritten column in the projection would make the row dict claim
# knowledge the store does not have.
_SUBMISSION_PLACEHOLDERS = ", ".join("?" for _ in _SUBMISSION_COLS.split(", "))
_SELECT_SUBMISSION = f"SELECT {_SUBMISSION_COLS} FROM host_submissions WHERE workflow_id = ?"
_SELECT_SUBMISSION_STATE = "SELECT state FROM host_submissions WHERE workflow_id = ?"
_INSERT_SUBMISSION = f"INSERT INTO host_submissions ({_SUBMISSION_COLS}) VALUES ({_SUBMISSION_PLACEHOLDERS})"
_RESET_RECOVERY_ATTEMPTS = "UPDATE host_submissions SET recovery_attempts = 0 WHERE workflow_id = ? AND recovery_attempts > 0"
_SELECT_RUN_EXISTS = "SELECT 1 FROM runs WHERE id = ?"
#: The seq allocation and the insert are ONE statement, so two writers can
#: never read the same max and both claim it.
_INSERT_RUN_UPDATE = (
    "INSERT INTO run_updates (run_id, seq, kind, payload, created_at) SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ?, ? FROM run_updates WHERE run_id = ?"
)
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
_MAX_ADMISSION_UNITS_KEY = "max_admission_units"
_SELECT_SETTING_SQL = "SELECT value FROM host_settings WHERE key = ?"
_UPSERT_SETTING_SQL = (
    "INSERT INTO host_settings (key, value, updated_at) VALUES (?, ?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at"
)

# The run's CURRENT pause occurrence — a later pause supersedes an earlier
# one, so "current" is the newest row. The worker reads it to resume with
# the stored answer and nothing else.
_CURRENT_SLOT_SQL = "SELECT pause_id, settled_at, response_key, answer FROM pause_slots WHERE run_id = ? ORDER BY rowid DESC LIMIT 1"

# The execution journal's own claim on a workflow_id. Every acceptance path
# checks it AFTER the host rows, so a runs row that answers here belongs to
# Tier-0 work no host submission or Batch owns (see refuse_tier0_reuse).
_SELECT_RUN_STATUS = "SELECT status FROM runs WHERE id = ?"

# Rerun ordinal allocation. Both counts run INSIDE the transaction that
# inserts the new submission or Batch, over rows that exist at ACCEPTANCE
# time — never over runs rows, which only appear once an earlier rerun has
# executed and would hand two pending reruns the same id.
_COUNT_ACCEPTED_RETRIES = "SELECT COUNT(*) FROM host_submissions WHERE retry_of = ?"
_COUNT_ACCEPTED_BATCH_RETRIES = "SELECT COUNT(*) FROM host_batches WHERE retry_of = ?"

# === Worker registry (host_workers) ===
#
# A worker announces what it can execute and pulses while it lives, so
# "which of these submissions can anything alive actually run?" is a query
# rather than a guess. This is a PULSE, not a lease: it grants no authority
# and fences nothing (the claim CAS and claim_seq still do all of that). It
# only answers who is around.

#: How long a worker's pulse stays believable. The worker rewrites its row
#: once per poll pass (50 ms to a few seconds), so this is two orders of
#: magnitude of slack: a row older than this belongs to a process that is
#: gone, not to one that is merely busy.
WORKER_PULSE_TTL_SECONDS = 90.0
_UPSERT_WORKER_SQL = (
    "INSERT INTO host_workers (worker_id, started_at, heartbeat_at, served_json, builders_json, endpoint) VALUES (?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at, served_json = excluded.served_json, "
    "builders_json = excluded.builders_json, endpoint = excluded.endpoint"
)
_DELETE_WORKER_SQL = "DELETE FROM host_workers WHERE worker_id = ?"
_SELECT_LIVE_WORKERS_SQL = "SELECT worker_id, served_json, builders_json FROM host_workers WHERE heartbeat_at >= ?"


@dataclass(frozen=True)
class WorkerCoverage:
    """What the workers alive on a Run Home can execute, right now.

    Two registries, read as one answer. ``identities`` is the instance
    registry every ``serve()`` already builds — the exact pinned Definitions
    a worker holds in memory. ``builders`` is the constructor registry: keys
    whose builder any of those workers can call to REBUILD a Definition from
    arguments carried on the submission row.

    ``names`` is coarser than either and answers a different question. An
    exact-identity miss has two very different causes, and the store cannot
    tell them apart from the identity alone:

    - **A version skew.** Something alive serves this Definition NAME at a
      different version or structure. That is a rolling deployment, and
      ``accepts=`` exists precisely to drain it — the submission is waiting,
      not stranded.
    - **A dead address.** Nothing alive serves this name at all. Nobody is
      coming: that is the submission that sits queued forever with no
      executor and no error, which is what a dead letter is for.
    """

    worker_ids: frozenset[str]
    identities: frozenset[DefinitionId]
    names: frozenset[str]
    builders: frozenset[str]

    def covers(self, identity: DefinitionId, builder_key: str | None) -> bool:
        """Whether anything alive could execute this exact address."""
        return identity in self.identities or (builder_key is not None and builder_key in self.builders)

    def may_yet_cover(self, identity: DefinitionId, builder_key: str | None) -> bool:
        """Whether waiting for this submission's executor is still reasonable."""
        return self.covers(identity, builder_key) or identity.name in self.names


def _pulse_cutoff(now_iso: str) -> str:
    """The oldest ``heartbeat_at`` still read as alive, in store-clock shape.

    Derived from the STORE's ``now`` rather than the reader's process clock,
    for the same reason every due predicate is: a notebook whose clock drifts
    must not decide that the API server's worker is dead.
    """
    cutoff = datetime.fromisoformat(now_iso) - timedelta(seconds=WORKER_PULSE_TTL_SECONDS)
    return cutoff.astimezone(timezone.utc).isoformat()


def _reasons_from_rows(rows: Sequence[Sequence[Any]]) -> dict[str, str]:
    """Project ``dead_lettered`` update payloads to ``{run_id: reason}``."""
    reasons: dict[str, str] = {}
    for run_id, payload in rows:
        try:
            reason = json.loads(payload).get("reason")
        except (TypeError, ValueError):  # pragma: no cover - corrupt payload
            continue
        if isinstance(reason, str):
            reasons[str(run_id)] = reason
    return reasons


def _coverage_from_rows(rows: Sequence[Sequence[Any]], exclude: str | None) -> WorkerCoverage:
    """Fold live ``host_workers`` rows into one coverage answer.

    A row whose JSON will not parse is skipped rather than raised on: the
    registry is an availability hint written by another process, and a
    corrupt one must never break a submission or a claim scan.
    """
    worker_ids: set[str] = set()
    identities: set[DefinitionId] = set()
    builders: set[str] = set()
    for worker_id, served_json, builders_json in rows:
        if exclude is not None and worker_id == exclude:
            continue
        worker_ids.add(str(worker_id))
        try:
            served = json.loads(served_json or "[]")
            keys = json.loads(builders_json or "[]")
        except (TypeError, ValueError):  # pragma: no cover - corrupt registry row
            continue
        for entry in served:
            try:
                identities.add(DefinitionId.from_dict(entry))
            except (TypeError, ValueError):  # pragma: no cover - corrupt registry row
                continue
        builders.update(str(key) for key in keys)
    return WorkerCoverage(
        frozenset(worker_ids),
        frozenset(identities),
        frozenset(identity.name for identity in identities),
        frozenset(builders),
    )


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
HOST_COMMAND_VERBS: frozenset[str] = frozenset({STOP_VERB, SCHEDULE_ANSWER_VERB})


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


def _weighted_admission_fits(budget: int, claimed_count: int, claimed_units: int, cost: int) -> bool:
    """Whether the FIFO head may reserve ``cost`` under page admission."""
    if claimed_count == 1 and claimed_units > budget:
        return False  # the one oversized document runs alone
    if cost > budget:
        return claimed_count == 0
    return claimed_count < 2 or claimed_units + cost <= budget


# === Durable timing facts (issue #386) ===
#
# The execution journal already records what every node cost — a `steps`
# row's duration_ms/cached/error, and the run's own duration_ms/node_count.
# Nothing below measures anything or writes anything; it projects rows that
# outlived the process that wrote them, which is the entire point: an
# operator whose notebook kernel died still owes an answer for the work it
# drove.

#: One durable step row, as the timing read needs it.
_STEP_TIMING_COLS = "run_id, superstep, node_name, node_type, status, duration_ms, cached, error, completed_at"
#: How far the nested-run walk descends before it stops. A parent chain is
#: a tree by construction; the bound is what makes a corrupted store return
#: a bounded answer instead of spinning.
_MAX_RUN_NESTING_DEPTH = 32


def _timing_run_rows_query(*, definition: str | None, batch_id: str | None, limit: int) -> tuple[str, list[Any]]:
    """The Host Runs a timing read covers, newest acceptance first.

    Selection is by SUBMISSION, never by runs row: a submission that never
    executed is still an accepted Run the operator asked about, and reports
    itself honestly with no duration rather than vanishing from the answer.
    """
    conditions = []
    params: list[Any] = []
    if definition is not None:
        conditions.append("s.definition_name = ?")
        params.append(definition)
    if batch_id is not None:
        conditions.append("s.batch_id = ?")
        params.append(batch_id)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    return (
        f"SELECT {_QUALIFIED_SUBMISSION_COLS}, {_QUALIFIED_RUN_COLS} FROM host_submissions s "
        f"LEFT JOIN runs r ON r.id = s.workflow_id{where} ORDER BY s.created_at DESC, s.workflow_id DESC LIMIT ?",
        params,
    )


def _descendant_runs_query(root_ids: Sequence[str]) -> tuple[str, list[Any]]:
    """Every run reachable from ``root_ids`` through ``runs.parent_run_id``.

    A Host Run is only the OUTERMOST run of the work it drove: a nested
    graph node, and every item of a ``map``, commits its own runs row with
    the parent recorded. Their step records are as durable as the parent's,
    and a join that only matched ``runs.id = host_submissions.workflow_id``
    threw them away — which is exactly the per-node evidence a fan-out is
    worth reading. ``UNION`` (not ``UNION ALL``) plus the depth bound keeps
    a malformed parent cycle finite.
    """
    placeholders = ", ".join("?" for _ in root_ids)
    return (
        f"""
        WITH RECURSIVE descendants(id, root_id, depth) AS (
            SELECT id, id, 0 FROM runs WHERE id IN ({placeholders})
            UNION
            SELECT child.id, parent.root_id, parent.depth + 1
            FROM runs child JOIN descendants parent ON child.parent_run_id = parent.id
            WHERE parent.depth < ?
        )
        SELECT id, root_id FROM descendants
        """,
        [*root_ids, _MAX_RUN_NESTING_DEPTH],
    )


def _step_timing_query(run_ids: Sequence[str]) -> tuple[str, Sequence[str]]:
    """Durable step facts for one chunk of run ids, in execution order."""
    placeholders = ", ".join("?" for _ in run_ids)
    return (
        f"SELECT {_STEP_TIMING_COLS} FROM steps WHERE run_id IN ({placeholders}) AND {_PUBLIC_STEP_FILTER} "
        "ORDER BY run_id, COALESCE(completed_at, created_at), created_at, id",
        run_ids,
    )


def _batch_children_query(batch_ids: Sequence[str]) -> tuple[str, Sequence[str]]:
    """The joined-children read for one chunk of Batch ids, and its binds.

    Stated once so the sync and async mirrors of ``_batch_children`` can
    never select different columns — the row layout is positional, and a
    drift here would silently mis-slice submissions and runs rows.
    """
    placeholders = ", ".join("?" for _ in batch_ids)
    return (
        f"SELECT {_QUALIFIED_SUBMISSION_COLS}, {_QUALIFIED_RUN_COLS} FROM host_submissions s "
        f"LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id IN ({placeholders})",
        batch_ids,
    )


def _row_to_submission(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(_SUBMISSION_COLS.split(", "), row, strict=True))


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
        max_admission_units: int | None | _Unset = _UNSET,
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
        self._memory_lock_token: int | None = next(_memory_lock_tokens) if self._is_memory else None
        if not isinstance(max_active_runs, _Unset):
            # Explicit argument writes through; omitting it adopts whatever the
            # store already holds (see the `max_active_runs` property).
            self.max_active_runs = max_active_runs
        if not isinstance(max_admission_units, _Unset):
            self.max_admission_units = max_admission_units

    @classmethod
    def open(
        cls,
        uri: str | Path,
        *,
        policy: CheckpointPolicy | None = None,
        serializer: Any = None,
        max_active_runs: int | None | _Unset = _UNSET,
        max_admission_units: int | None | _Unset = _UNSET,
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
        return cls(
            uri,
            policy=policy,
            serializer=serializer,
            max_active_runs=max_active_runs,
            max_admission_units=max_admission_units,
        )

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

    @property
    def max_admission_units(self) -> int | None:
        """Home-scoped weighted admission budget; None leaves it unlimited."""
        with self._sync_lock:
            row = self._sync_db().execute(_SELECT_SETTING_SQL, (_MAX_ADMISSION_UNITS_KEY,)).fetchone()
        return _cap_from_row(row)

    @max_admission_units.setter
    def max_admission_units(self, value: int | None) -> None:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            raise ValueError(f"max_admission_units must be an int >= 1, or None for unlimited; got {value!r}.")
        with self._sync_lock:
            db = self._sync_db()
            db.execute(_UPSERT_SETTING_SQL, (_MAX_ADMISSION_UNITS_KEY, None if value is None else str(value), _now_iso()))
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
        now_iso = await self._store_now()
        await self._ensure_db()
        async with self._txn_lock():
            free = await self._free_admission_slots()
            if free is not None and free <= 0:
                return True
            budget_cursor = await self._db.execute(_SELECT_SETTING_SQL, (_MAX_ADMISSION_UNITS_KEY,))
            budget = _cap_from_row(await budget_cursor.fetchone())
            if budget is None:
                return False
            pending_cursor = await self._db.execute(
                "SELECT s.admission_cost FROM host_submissions s "
                f"WHERE s.state = 'pending' AND s.compat_state = 'compatible' AND {_due_clause('s.start_at', null_is_due=True)} "
                "AND (s.batch_id IS NULL OR NOT EXISTS ("
                f"SELECT 1 FROM batch_updates bu WHERE bu.batch_id = s.batch_id AND bu.kind = '{TRIP_UPDATE_KIND}'"
                ")) ORDER BY s.created_at, s.rowid LIMIT 1",
                (now_iso,),
            )
            pending = await pending_cursor.fetchone()
            if pending is None:
                return False
            usage_cursor = await self._db.execute("SELECT COUNT(*), COALESCE(SUM(admission_cost), 0) FROM host_submissions WHERE state = 'claimed'")
            count, units = await usage_cursor.fetchone()
        return not _weighted_admission_fits(budget, int(count), int(units), int(pending[0]))

    def _admission_is_full_sync(self) -> bool:
        """Sync mirror of ``_admission_is_full``."""
        with self._sync_lock:
            db = self._sync_db()
            cap_row = db.execute(_SELECT_SETTING_SQL, (_MAX_ACTIVE_RUNS_KEY,)).fetchone()
            free = _slots_left(cap_row, db.execute(_ACTIVE_RUN_COUNT_SQL).fetchone())
            if free is not None and free <= 0:
                return True
            budget = _cap_from_row(db.execute(_SELECT_SETTING_SQL, (_MAX_ADMISSION_UNITS_KEY,)).fetchone())
            if budget is None:
                return False
            now_iso = str(db.execute(_STORE_NOW_SQL).fetchone()[0])
            pending = db.execute(
                "SELECT s.admission_cost FROM host_submissions s "
                f"WHERE s.state = 'pending' AND s.compat_state = 'compatible' AND {_due_clause('s.start_at', null_is_due=True)} "
                "AND (s.batch_id IS NULL OR NOT EXISTS ("
                f"SELECT 1 FROM batch_updates bu WHERE bu.batch_id = s.batch_id AND bu.kind = '{TRIP_UPDATE_KIND}'"
                ")) ORDER BY s.created_at, s.rowid LIMIT 1",
                (now_iso,),
            ).fetchone()
            if pending is None:
                return False
            count, units = db.execute("SELECT COUNT(*), COALESCE(SUM(admission_cost), 0) FROM host_submissions WHERE state = 'claimed'").fetchone()
        return not _weighted_admission_fits(budget, int(count), int(units), int(pending[0]))

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
            _INSERT_RUN_UPDATE,
            (run_id, kind, json.dumps(payload), _now_iso(), run_id),
        )

    async def _append_run_update(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        """Append one run_updates row; caller holds the write transaction."""
        await self._db.execute(
            _INSERT_RUN_UPDATE,
            (run_id, kind, json.dumps(payload), _now_iso(), run_id),
        )

    def _reset_recovery_attempts_sync(self, db: Any, run_id: str) -> None:
        """Reset the recovery brake on NEW committed progress (same transaction)."""
        db.execute(
            _RESET_RECOVERY_ATTEMPTS,
            (run_id,),
        )

    def _after_run_mutation_sync(self, db: Any, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        self._append_run_update_sync(db, run_id, kind, payload)
        if _is_committed_progress(kind, payload):
            self._reset_recovery_attempts_sync(db, run_id)
        if kind == "status" and payload.get("status") in _TERMINAL_STATUS_VALUES:
            self._append_child_settled_sync(db, run_id, payload["status"])
        elif kind == "status" and payload.get("status") == WorkflowStatus.PAUSED.value and self._park_submission_sync(db, run_id):
            self._append_occurrence_fact_sync(db, run_id, payload.get("pause_id"), kind=PAUSED_UPDATE_KIND)

    async def _after_run_mutation(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        await self._append_run_update(run_id, kind, payload)
        if _is_committed_progress(kind, payload):
            await self._db.execute(
                _RESET_RECOVERY_ATTEMPTS,
                (run_id,),
            )
        if kind == "status" and payload.get("status") in _TERMINAL_STATUS_VALUES:
            await self._append_child_settled(run_id, payload["status"])
        elif kind == "status" and payload.get("status") == WorkflowStatus.PAUSED.value and await self._park_submission(run_id):
            await self._append_occurrence_fact(run_id, payload.get("pause_id"), kind=PAUSED_UPDATE_KIND)

    def _park_submission_sync(self, db: Any, run_id: str) -> bool:
        """Sync mirror of ``_park_submission``."""
        return bool(db.execute(PARK_SUBMISSION_SQL, (SUBMISSION_STATE_PAUSED, run_id)).rowcount == 1)

    async def _park_submission(self, run_id: str) -> bool:
        """Park a claimed submission, INSIDE the pause transaction.

        THE pause transition. It commits with the pause slot, the ``PAUSED``
        run status, and the ``child_paused`` Batch fact, so there is no
        instant in which a run is durably paused while its submission still
        reads ``claimed``. That instant used to be a real race: an answer
        arriving inside it found nothing to re-admit (the compare-and-set
        looks for ``paused``), and the worker's later release had to notice
        and re-admit instead — a second required transition owned by a
        different transaction, which process death could sit between.

        Compare-and-set on ``claimed`` so it is once-only and never disturbs
        a submission some other path already moved.

        Returns True when this call performed the transition — the same
        shape ``_readmit_answered_pause`` returns, and for the same reason:
        the ``child_paused`` fact is published only by a park that really
        parked something, so the two occurrence facts stay symmetric and a
        Batch stream never carries a state change that did not happen.
        """
        result = await self._db.execute(PARK_SUBMISSION_SQL, (SUBMISSION_STATE_PAUSED, run_id))
        return bool(result.rowcount == 1)

    # === batch_updates appends (same-transaction as Batch facts) ===

    def _append_batch_update_sync(self, db: Any, batch_id: str, kind: str, payload: dict[str, Any], item_key: str | None = None) -> None:
        """Append one batch_updates row; caller holds the write transaction.

        Single INSERT...SELECT so the bseq allocation and the insert are one
        statement (no read-then-write race) — same discipline as run_updates.
        """
        db.execute(
            INSERT_BATCH_UPDATE,
            (batch_id, kind, item_key, json.dumps(payload), _now_iso(), batch_id),
        )

    async def _append_batch_update(self, batch_id: str, kind: str, payload: dict[str, Any], item_key: str | None = None) -> None:
        """Append one batch_updates row; caller holds the write transaction."""
        await self._db.execute(
            INSERT_BATCH_UPDATE,
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
            SELECT_MEMBERSHIP,
            (run_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return
        batch_id, item_key = row
        exists = db.execute(
            SELECT_ACCOUNTED,
            (batch_id, SETTLED_UPDATE_KIND, item_key),
        ).fetchone()
        if exists is not None:
            return
        self._append_batch_update_sync(
            db,
            batch_id,
            SETTLED_UPDATE_KIND,
            {"item_key": item_key, "workflow_id": run_id, "status": status},
            item_key=item_key,
        )
        self._maybe_trip_tolerance_sync(db, batch_id)

    async def _append_child_settled(self, run_id: str, status: str) -> None:
        """Async mirror of ``_append_child_settled_sync``."""
        cursor = await self._db.execute(
            SELECT_MEMBERSHIP,
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            return
        batch_id, item_key = row
        exists_cursor = await self._db.execute(
            SELECT_ACCOUNTED,
            (batch_id, SETTLED_UPDATE_KIND, item_key),
        )
        if await exists_cursor.fetchone() is not None:
            return
        await self._append_batch_update(
            batch_id,
            SETTLED_UPDATE_KIND,
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

    def _append_occurrence_fact_sync(self, db: Any, run_id: str, pause_id: str | None, *, kind: str) -> None:
        """Mirror ONE pause-occurrence fact onto the child's Batch, same txn.

        ``child_paused`` and ``child_runnable`` differ only in which kind
        they append: same membership lookup, same occurrence dedupe, same
        payload. Writing them twice each (once per mirror) was four copies
        of one rule, which is three too many for a guard whose whole job is
        to be identical everywhere.
        """
        membership = db.execute(SELECT_MEMBERSHIP, (run_id,)).fetchone()
        if membership is None or membership[0] is None:
            return
        batch_id, item_key = membership
        last = db.execute(SELECT_LAST_OCCURRENCE, (batch_id, kind, item_key)).fetchone()
        if is_repeat_occurrence(last, pause_id):
            return
        self._append_batch_update_sync(db, batch_id, kind, occurrence_fact(batch_id, item_key, run_id, self.uri, pause_id), item_key=item_key)

    async def _append_occurrence_fact(self, run_id: str, pause_id: str | None, *, kind: str) -> None:
        """Async mirror of ``_append_occurrence_fact_sync``."""
        membership = await self._batch_membership(run_id)
        if membership is None:
            return
        batch_id, item_key = membership
        cursor = await self._db.execute(SELECT_LAST_OCCURRENCE, (batch_id, kind, item_key))
        if is_repeat_occurrence(await cursor.fetchone(), pause_id):
            return
        await self._append_batch_update(batch_id, kind, occurrence_fact(batch_id, item_key, run_id, self.uri, pause_id), item_key=item_key)

    async def _batch_membership(self, run_id: str) -> tuple[str, str] | None:
        """``(batch_id, item_key)`` for a Batch child, else None; caller holds the txn."""
        cursor = await self._db.execute(SELECT_MEMBERSHIP, (run_id,))
        row = await cursor.fetchone()
        return None if row is None or row[0] is None else (str(row[0]), str(row[1]))

    async def _append_child_unstarted(self, run_id: str, *, kind: str = UNSTARTED_UPDATE_KIND) -> None:
        """Record that a Batch item settled without executing, same transaction.

        THE unstarted-child fact (PRD 0019 A9). Every path that settles a
        child WITHOUT ever executing it — admission refusing a child of an
        already-tripped Batch, and a stop that landed before first
        execution — appends this row in the SAME transaction as the state
        flip that causes it, so a detached ``watch(batch_ref)`` accounts
        the item from the durable sequence alone. Items the trip fact
        itself named in ``unstarted_items`` never reach here: their flip
        happened inside the trip.

        ``kind`` selects the honest fact for the disposition:
        ``child_unstarted`` for an item that never began, or
        ``child_abandoned`` for one that began and will not be resumed
        (see ``_append_trip_closeout``).

        No-op for runs without Batch membership. Idempotent per item, like
        ``_append_child_settled``: the flip is already once-only (it moves
        the row out of 'pending'/'claimed'), and the guard keeps a replayed
        path from double-accounting the item even so.

        Async-only on purpose: every caller is a worker path
        (``_claim_eligible``, ``_apply_stop_never_started``) that has no
        sync mirror, so a ``_sync`` twin here would be dead code.
        """
        cursor = await self._db.execute(
            SELECT_MEMBERSHIP,
            (run_id,),
        )
        row = await cursor.fetchone()
        if row is None or row[0] is None:
            return
        batch_id, item_key = row
        exists_cursor = await self._db.execute(
            "SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind IN (?, ?) AND item_key = ? LIMIT 1",
            (batch_id, UNSTARTED_UPDATE_KIND, ABANDONED_UPDATE_KIND, item_key),
        )
        if await exists_cursor.fetchone() is not None:
            return
        await self._append_batch_update(
            batch_id,
            kind,
            {"item_key": item_key, "workflow_id": run_id},
            item_key=item_key,
        )

    async def _append_trip_closeout(self, run_id: str) -> None:
        """Account a child a tolerance trip closed admission on, truthfully.

        Two genuinely different things end here and they must not share a
        name. An item with no runs row never began: ``child_unstarted``,
        and it is safe to rerun from scratch. An item WITH a runs row began,
        committed real steps, and possibly landed side effects — closed
        admission means it will never be resumed, so it is ``child_abandoned``.
        Calling the second one "unstarted" would tell an operator that
        nothing happened when something did.
        """
        started = await self._has_run_row(run_id)
        await self._append_child_unstarted(run_id, kind=closeout_kind(started))

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
        batch_row = db.execute(SELECT_TOLERANCE_INPUTS, (batch_id,)).fetchone()
        if batch_row is None or batch_row[1] is None:
            return
        if db.execute(SELECT_TRIPPED, (batch_id,)).fetchone() is not None:
            return
        (failure_count,) = db.execute(COUNT_FAILURE_EQUIVALENT, (batch_id,)).fetchone()
        payload = trip_payload(batch_row[0], batch_row[1], int(failure_count))
        if payload is None:
            return
        closed = db.execute(SELECT_PENDING_CLOSEOUT, (batch_id,)).fetchall()
        unstarted, abandoned = split_closeout(closed)
        db.execute(
            CLOSE_PENDING_CHILDREN,
            (_now_iso(), batch_id),
        )
        self._append_batch_update_sync(
            db,
            batch_id,
            TRIP_UPDATE_KIND,
            {**payload, "unstarted_items": unstarted, "abandoned_items": abandoned},
        )

    async def _maybe_trip_tolerance(self, batch_id: str) -> None:
        """Async mirror of ``_maybe_trip_tolerance_sync``; caller holds the transaction."""
        batch_cursor = await self._db.execute(SELECT_TOLERANCE_INPUTS, (batch_id,))
        batch_row = await batch_cursor.fetchone()
        if batch_row is None or batch_row[1] is None:
            return
        tripped_cursor = await self._db.execute(SELECT_TRIPPED, (batch_id,))
        if await tripped_cursor.fetchone() is not None:
            return
        count_cursor = await self._db.execute(COUNT_FAILURE_EQUIVALENT, (batch_id,))
        (failure_count,) = await count_cursor.fetchone()
        payload = trip_payload(batch_row[0], batch_row[1], int(failure_count))
        if payload is None:
            return
        pending_cursor = await self._db.execute(SELECT_PENDING_CLOSEOUT, (batch_id,))
        unstarted, abandoned = split_closeout(await pending_cursor.fetchall())
        await self._db.execute(
            CLOSE_PENDING_CHILDREN,
            (_now_iso(), batch_id),
        )
        await self._append_batch_update(
            batch_id,
            TRIP_UPDATE_KIND,
            {**payload, "unstarted_items": unstarted, "abandoned_items": abandoned},
        )

    def _batch_tripped_sync(self, batch_id: str) -> bool:
        """True when a durable ``tolerance_tripped`` fact exists for this Batch."""
        with self._sync_lock:
            db = self._sync_db()
            return db.execute(SELECT_TRIPPED, (batch_id,)).fetchone() is not None

    async def _batch_tripped(self, batch_id: str) -> bool:
        """Async mirror of ``_batch_tripped_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(SELECT_TRIPPED, (batch_id,))
            return await cursor.fetchone() is not None

    # === Worker registry (who is alive, and what can they execute) ===
    #
    # The two WRITE verbs are async-only on purpose: their only callers are
    # worker-loop paths (``work_forever``), which have no sync mirror, so a
    # ``_sync`` twin here would be dead code — the same reasoning
    # ``_append_child_unstarted`` records. The READ has both mirrors, because
    # ``submit`` and ``submit_sync`` both have to ask it.

    async def _pulse_worker(
        self,
        worker_id: str,
        *,
        served: Collection[DefinitionId],
        builders: Collection[str],
        endpoint: str | None = None,
    ) -> None:
        """Announce this worker and refresh its pulse, in one upsert.

        Startup and every later poll pass write the same statement, so a
        worker that gains a Definition (``add_definition``) or a builder
        mid-flight publishes it on its next pass rather than at the next
        restart. ``started_at`` is written once and never overwritten.
        """
        await self._ensure_db()
        now = await self._store_now()
        served_json = json.dumps([identity.to_dict() for identity in sorted(served, key=lambda entry: (entry.name, entry.structural_hash))])
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                await self._db.execute(_UPSERT_WORKER_SQL, (worker_id, now, now, served_json, json.dumps(sorted(builders)), endpoint))
                await self._db.commit()
            except BaseException:
                await self._rollback_async()
                raise

    async def _retire_worker(self, worker_id: str) -> None:
        """Withdraw this worker's registration on a clean exit.

        A stopped worker stops covering work immediately rather than after
        the pulse window: shutting a deployment down and then submitting to
        it should refuse at once, not ninety seconds later. A worker that
        dies without reaching here leaves its row behind, which is exactly
        what the freshness window is for.
        """
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                await self._db.execute(_DELETE_WORKER_SQL, (worker_id,))
                await self._db.commit()
            except BaseException:
                await self._rollback_async()
                raise

    async def _live_worker_coverage(self, now_iso: str | None = None, *, exclude: str | None = None) -> WorkerCoverage:
        """What the workers with a fresh pulse can execute right now.

        A pure read against ``host_workers``. It grants nothing and fences
        nothing — the claim CAS and ``claim_seq`` remain the only authority
        — it answers "is there anybody who could run this?", which is the
        question a submission has to ask before it becomes a durable row.
        """
        await self._ensure_db()
        now_iso = await self._store_now() if now_iso is None else now_iso
        async with self._txn_lock():
            cursor = await self._db.execute(_SELECT_LIVE_WORKERS_SQL, (_pulse_cutoff(now_iso),))
            return _coverage_from_rows(await cursor.fetchall(), exclude)

    def _live_worker_coverage_sync(self, now_iso: str | None = None, *, exclude: str | None = None) -> WorkerCoverage:
        """Sync mirror of ``_live_worker_coverage``."""
        with self._sync_lock:
            db = self._sync_db()
            resolved = db.execute(_STORE_NOW_SQL).fetchone()[0] if now_iso is None else now_iso
            rows = db.execute(_SELECT_LIVE_WORKERS_SQL, (_pulse_cutoff(resolved),)).fetchall()
            return _coverage_from_rows(rows, exclude)

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
        admission_cost: int = 1,
        builder_key: str | None = None,
        builder_args_json: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Insert one submission plus its 'submitted' update, atomically.

        ``workflow_id=None`` means "mint the next rerun of ``retry_of``":
        the ordinal is allocated from accepted submissions INSIDE this
        transaction, the id is derived from it, and the ordinal is stored
        on the row. Allocating it outside handed two reruns requested
        before either executed the same ``<source>-retry-1``, and the
        second silently deduped into the first.

        ``builder_key``/``builder_args_json`` are the optional constructor
        address (schema v7): how a process that does NOT hold this
        Definition in memory rebuilds it. They are deliberately outside the
        start fingerprint — the pinned identity plus inputs already say WHAT
        runs, and these only say how to reconstitute it — so a duplicate
        resubmission dedupes into the stored row and never rewrites the
        builder address it was accepted with.

        Returns ``(created, row)``. When a submission already exists for
        ``workflow_id`` nothing is written: a fingerprint-identical
        nonterminal row returns ``(False, existing)`` (use-existing dedup),
        terminal reuse raises ``AlreadyTerminalError``, and a fingerprint
        mismatch raises ``WorkflowIdConflictError``. Reusing an id owned by
        a Batch is likewise a conflict (AlreadyTerminalError once that
        Batch is settled) — run and Batch workflow ids share one namespace,
        and so does the execution journal: an id already owned by a
        host-less (Tier-0) runs row is refused last, once the host rows have
        had their say (``refuse_tier0_reuse``).
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
                    _SELECT_SUBMISSION,
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
                    SELECT_BATCH_BY_WORKFLOW,
                    (workflow_id,),
                ).fetchone()
                if batch_row is not None:
                    if children_settled_rows(
                        db.execute(
                            SELECT_CHILD_SETTLEMENT,
                            (batch_row[0],),
                        ).fetchall()
                    ):
                        raise AlreadyTerminalError(workflow_id)
                    raise WorkflowIdConflictError(workflow_id, "an existing Batch owns this workflow_id")
                run_row = db.execute(_SELECT_RUN_STATUS, (workflow_id,)).fetchone()
                if run_row is not None:
                    refuse_tier0_reuse(str(run_row[0]), workflow_id=workflow_id)
                now = _now_iso()
                db.execute(
                    _INSERT_SUBMISSION,
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
                        0,  # claim_seq: no claim has been handed out yet
                        admission_cost,
                        builder_key,
                        builder_args_json,
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
        admission_cost: int = 1,
        builder_key: str | None = None,
        builder_args_json: str | None = None,
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
                    _SELECT_SUBMISSION,
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
                    SELECT_BATCH_BY_WORKFLOW,
                    (workflow_id,),
                )
                batch_row = await batch_cursor.fetchone()
                if batch_row is not None:
                    children_cursor = await self._db.execute(
                        SELECT_CHILD_SETTLEMENT,
                        (batch_row[0],),
                    )
                    if children_settled_rows(await children_cursor.fetchall()):
                        raise AlreadyTerminalError(workflow_id)
                    raise WorkflowIdConflictError(workflow_id, "an existing Batch owns this workflow_id")
                run_cursor = await self._db.execute(_SELECT_RUN_STATUS, (workflow_id,))
                run_row = await run_cursor.fetchone()
                if run_row is not None:
                    refuse_tier0_reuse(str(run_row[0]), workflow_id=workflow_id)
                now = _now_iso()
                await self._db.execute(
                    _INSERT_SUBMISSION,
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
                        0,  # claim_seq: no claim has been handed out yet
                        admission_cost,
                        builder_key,
                        builder_args_json,
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
                _SELECT_SUBMISSION,
                (workflow_id,),
            ).fetchone()
            return _row_to_submission(row) if row is not None else None

    async def _get_submission(self, workflow_id: str) -> dict[str, Any] | None:
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(
                _SELECT_SUBMISSION,
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

    def _submit_batch_sync(self, request: BatchAcceptance) -> tuple[bool, dict[str, Any]]:
        """Accept one Batch atomically: manifest + child submissions + bseq 1.

        ONE transaction persists the host_batches manifest row, one child
        submission (with its own 'submitted' run update) per item key, and
        the 'manifest' batch_updates row at bseq 1 — a partial Batch can
        never appear accepted.

        The whole request arrives as one frozen ``BatchAcceptance``, which
        knows how to project itself into every row and fact written below.
        That is deliberate: identity, manifest, timing, provenance, retry,
        and recovery used to travel as thirteen separate parameters through
        four call layers, where a two-of-three mistake on the pinned
        Definition triple was possible at every hop.

        ``request.workflow_id=None`` means "mint the next rerun of
        ``batch_retry_of``": the Batch ordinal is allocated from accepted
        Batches INSIDE this transaction and the id derived from it, so
        concurrent rerun callers can never mint the same
        ``<source>-retry-N`` and dedupe into one Batch. Each rerun child
        gets its own accepted ordinal the same way.

        Returns ``(created, batch_row)``; the reuse contract lives in
        ``_batch_store.resolve_batch_reuse``.
        """
        with self._sync_lock:
            db = self._sync_db()
            try:
                db.execute("BEGIN IMMEDIATE")
                workflow_id = request.workflow_id
                if workflow_id is None:
                    source_row = db.execute(
                        SELECT_BATCH_WORKFLOW_ID,
                        (_require_retry_source(request.batch_retry_of),),
                    ).fetchone()
                    workflow_id = _retry_workflow_id(
                        str(source_row[0]),
                        _next_ordinal(db.execute(_COUNT_ACCEPTED_BATCH_RETRIES, (request.batch_retry_of,)).fetchone()),
                    )
                existing = self._batch_dedup_sync(db, request, workflow_id)
                if existing is not None:
                    db.rollback()
                    return False, existing
                specs = self._reserve_child_ids_sync(db, request, workflow_id)
                now = _now_iso()
                db.execute(
                    INSERT_BATCH,
                    request.batch_row(workflow_id, now),
                )
                self._insert_children_sync(db, request, specs, now=now)
                self._append_batch_update_sync(db, request.batch_id, MANIFEST_UPDATE_KIND, request.manifest_fact(workflow_id))
                db.commit()
            except BaseException:
                self._rollback_sync(db)
                raise
        row = self._get_batch_sync(request.batch_id)
        assert row is not None
        return True, row

    def _batch_dedup_sync(self, db: Any, request: BatchAcceptance, workflow_id: str) -> dict[str, Any] | None:
        """Read what owns ``workflow_id``; the shared policy decides.

        Returns the existing Batch row for a use-existing resubmission and
        None when the id is free. Everything else raises. The reads are the
        only thing this mirror owns — the three refusals it can reach live
        in ``_batch_store``, so the sync and async doors cannot drift on
        which reuse is a dedup and which is a typed conflict.
        """
        batch_row = db.execute(SELECT_BATCH_BY_WORKFLOW, (workflow_id,)).fetchone()
        if batch_row is not None:
            children = db.execute(
                SELECT_CHILD_SETTLEMENT,
                (row_to_batch(batch_row)["batch_id"],),
            ).fetchall()
            return resolve_batch_reuse(
                batch_row,
                workflow_id=workflow_id,
                request=request,
                children_settled=children_settled_rows(children),
            )
        submission_row = db.execute(_SELECT_SUBMISSION_STATE, (workflow_id,)).fetchone()
        if submission_row is not None:
            refuse_run_owned_id(submission_row[0], workflow_id=workflow_id)
        run_row = db.execute(_SELECT_RUN_STATUS, (workflow_id,)).fetchone()
        if run_row is not None:
            refuse_tier0_reuse(str(run_row[0]), workflow_id=workflow_id)
        return None

    def _reserve_child_ids_sync(self, db: Any, request: BatchAcceptance, workflow_id: str) -> tuple[ChildSpec, ...]:
        """Derive every child identity and refuse one already owned."""
        specs = request.child_specs(workflow_id)
        for spec in specs:
            collision = db.execute(
                SELECT_CHILD_ID_COLLISION,
                (spec.workflow_id, spec.workflow_id),
            ).fetchone()
            refuse_child_id_collision(spec, collides=collision is not None)
            child_run_row = db.execute(_SELECT_RUN_STATUS, (spec.workflow_id,)).fetchone()
            if child_run_row is not None:
                refuse_tier0_reuse(str(child_run_row[0]), workflow_id=spec.workflow_id, item_key=spec.item_key)
        return specs

    def _insert_children_sync(self, db: Any, request: BatchAcceptance, specs: Sequence[ChildSpec], *, now: str) -> None:
        """Insert one pending child submission per item, each with its own fact."""
        for spec in specs:
            source = request.child_source(spec.item_key)
            retry_index = None if source is None else _next_ordinal(db.execute(_COUNT_ACCEPTED_RETRIES, (source,)).fetchone())
            db.execute(
                _INSERT_SUBMISSION,
                request.child_row(spec, retry_index=retry_index, now=now),
            )
            self._append_run_update_sync(db, spec.workflow_id, "submitted", request.child_submitted_fact(spec))

    async def _submit_batch(self, request: BatchAcceptance) -> tuple[bool, dict[str, Any]]:
        """Async mirror of ``_submit_batch_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                workflow_id = request.workflow_id
                if workflow_id is None:
                    source_cursor = await self._db.execute(
                        SELECT_BATCH_WORKFLOW_ID,
                        (_require_retry_source(request.batch_retry_of),),
                    )
                    source_row = await source_cursor.fetchone()
                    ordinal_cursor = await self._db.execute(_COUNT_ACCEPTED_BATCH_RETRIES, (request.batch_retry_of,))
                    workflow_id = _retry_workflow_id(str(source_row[0]), _next_ordinal(await ordinal_cursor.fetchone()))
                existing = await self._batch_dedup(request, workflow_id)
                if existing is not None:
                    await self._db.rollback()
                    return False, existing
                specs = await self._reserve_child_ids(request, workflow_id)
                now = _now_iso()
                await self._db.execute(
                    INSERT_BATCH,
                    request.batch_row(workflow_id, now),
                )
                await self._insert_children(request, specs, now=now)
                await self._append_batch_update(request.batch_id, MANIFEST_UPDATE_KIND, request.manifest_fact(workflow_id))
                await self._db.commit()
            except BaseException:
                await self._rollback_async()
                raise
        row = await self._get_batch(request.batch_id)
        assert row is not None
        return True, row

    async def _batch_dedup(self, request: BatchAcceptance, workflow_id: str) -> dict[str, Any] | None:
        """Async mirror of ``_batch_dedup_sync``."""
        batch_cursor = await self._db.execute(SELECT_BATCH_BY_WORKFLOW, (workflow_id,))
        batch_row = await batch_cursor.fetchone()
        if batch_row is not None:
            children_cursor = await self._db.execute(
                SELECT_CHILD_SETTLEMENT,
                (row_to_batch(batch_row)["batch_id"],),
            )
            return resolve_batch_reuse(
                batch_row,
                workflow_id=workflow_id,
                request=request,
                children_settled=children_settled_rows(await children_cursor.fetchall()),
            )
        submission_cursor = await self._db.execute(_SELECT_SUBMISSION_STATE, (workflow_id,))
        submission_row = await submission_cursor.fetchone()
        if submission_row is not None:
            refuse_run_owned_id(submission_row[0], workflow_id=workflow_id)
        run_cursor = await self._db.execute(_SELECT_RUN_STATUS, (workflow_id,))
        run_row = await run_cursor.fetchone()
        if run_row is not None:
            refuse_tier0_reuse(str(run_row[0]), workflow_id=workflow_id)
        return None

    async def _reserve_child_ids(self, request: BatchAcceptance, workflow_id: str) -> tuple[ChildSpec, ...]:
        """Async mirror of ``_reserve_child_ids_sync``."""
        specs = request.child_specs(workflow_id)
        for spec in specs:
            collision_cursor = await self._db.execute(
                SELECT_CHILD_ID_COLLISION,
                (spec.workflow_id, spec.workflow_id),
            )
            refuse_child_id_collision(spec, collides=await collision_cursor.fetchone() is not None)
            child_run_cursor = await self._db.execute(_SELECT_RUN_STATUS, (spec.workflow_id,))
            child_run_row = await child_run_cursor.fetchone()
            if child_run_row is not None:
                refuse_tier0_reuse(str(child_run_row[0]), workflow_id=spec.workflow_id, item_key=spec.item_key)
        return specs

    async def _insert_children(self, request: BatchAcceptance, specs: Sequence[ChildSpec], *, now: str) -> None:
        """Async mirror of ``_insert_children_sync``."""
        for spec in specs:
            source = request.child_source(spec.item_key)
            retry_index = None
            if source is not None:
                ordinal_cursor = await self._db.execute(_COUNT_ACCEPTED_RETRIES, (source,))
                retry_index = _next_ordinal(await ordinal_cursor.fetchone())
            await self._db.execute(
                _INSERT_SUBMISSION,
                request.child_row(spec, retry_index=retry_index, now=now),
            )
            await self._append_run_update(spec.workflow_id, "submitted", request.child_submitted_fact(spec))

    def _get_batch_sync(self, batch_id: str) -> dict[str, Any] | None:
        with self._sync_lock:
            db = self._sync_db()
            row = db.execute(
                SELECT_BATCH_BY_ID,
                (batch_id,),
            ).fetchone()
            return row_to_batch(row) if row is not None else None

    async def _get_batch(self, batch_id: str) -> dict[str, Any] | None:
        """Async mirror of ``_get_batch_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(
                SELECT_BATCH_BY_ID,
                (batch_id,),
            )
            row = await cursor.fetchone()
            return row_to_batch(row) if row is not None else None

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

    def _list_batch_rows_sync(self, *, definition: str | None, limit: int) -> list[dict[str, Any]]:
        """Recent Batch manifest rows, newest first.

        Manifests only — children stay a separate bulk read, so listing N
        Batches costs one statement here plus one join for the whole page
        rather than one query per Batch.
        """
        statement = SELECT_RECENT_BATCHES if definition is None else SELECT_RECENT_BATCHES_BY_DEFINITION
        params: tuple[Any, ...] = (limit,) if definition is None else (definition, limit)
        with self._sync_lock:
            rows = self._sync_db().execute(statement, params).fetchall()
        return [row_to_batch(row) for row in rows]

    async def _list_batch_rows(self, *, definition: str | None, limit: int) -> list[dict[str, Any]]:
        """Async mirror of ``_list_batch_rows_sync``."""
        statement = SELECT_RECENT_BATCHES if definition is None else SELECT_RECENT_BATCHES_BY_DEFINITION
        params: tuple[Any, ...] = (limit,) if definition is None else (definition, limit)
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(statement, params)
            rows = await cursor.fetchall()
        return [row_to_batch(row) for row in rows]

    def _batch_children_sync(self, batch_ids: Sequence[str]) -> dict[str, dict[str, tuple[dict[str, Any], Run | None]]]:
        """Children of MANY Batches at once, keyed by batch id then item key.

        The bulk form of ``_batch_child_rows_sync``: a listing joins every
        page's children in a bounded number of statements, so the per-Batch
        census stays cheap no matter how many Batches (or children) a page
        holds.
        """
        grouped: dict[str, dict[str, tuple[dict[str, Any], Run | None]]] = {batch_id: {} for batch_id in batch_ids}
        for chunk in self._chunk_run_ids(batch_ids):
            with self._sync_lock:
                rows = self._sync_db().execute(*_batch_children_query(chunk)).fetchall()
            self._collect_batch_children(rows, grouped)
        return grouped

    async def _batch_children(self, batch_ids: Sequence[str]) -> dict[str, dict[str, tuple[dict[str, Any], Run | None]]]:
        """Async mirror of ``_batch_children_sync``."""
        grouped: dict[str, dict[str, tuple[dict[str, Any], Run | None]]] = {batch_id: {} for batch_id in batch_ids}
        if not batch_ids:
            return grouped
        await self._ensure_db()
        for chunk in self._chunk_run_ids(batch_ids):
            async with self._txn_lock():
                cursor = await self._db.execute(*_batch_children_query(chunk))
                rows = await cursor.fetchall()
            self._collect_batch_children(rows, grouped)
        return grouped

    def _collect_batch_children(
        self,
        rows: Sequence[Any],
        grouped: dict[str, dict[str, tuple[dict[str, Any], Run | None]]],
    ) -> None:
        """Group joined child rows under their Batch, exactly as the single-Batch read shapes them."""
        sub_count = len(_SUBMISSION_COLS.split(", "))
        for row in rows:
            submission = _row_to_submission(row[:sub_count])
            run = self._row_to_run(row[sub_count:]) if row[sub_count] is not None else None
            grouped.setdefault(str(submission["batch_id"]), {})[submission["item_key"]] = (submission, run)

    def _tripped_batch_ids_sync(self, batch_ids: Collection[str]) -> frozenset[str]:
        """Sync mirror of ``_tripped_batch_ids``, taking its own lock."""
        ids = list(batch_ids)
        if not ids:
            return frozenset()
        placeholders = ", ".join("?" for _ in ids)
        with self._sync_lock:
            rows = (
                self._sync_db()
                .execute(
                    f"SELECT DISTINCT batch_id FROM batch_updates WHERE kind = '{TRIP_UPDATE_KIND}' AND batch_id IN ({placeholders})",
                    ids,
                )
                .fetchall()
            )
        return frozenset(str(row[0]) for row in rows)

    async def _tripped_batch_ids_scan(self, batch_ids: Collection[str]) -> frozenset[str]:
        """``_tripped_batch_ids`` for a caller that holds NO transaction."""
        if not batch_ids:
            return frozenset()
        await self._ensure_db()
        async with self._txn_lock():
            return await self._tripped_batch_ids(batch_ids)

    def _read_batch_updates_sync(self, batch_id: str, after_bseq: int = 0) -> list[tuple[int, str, str, str]]:
        """Read batch_updates rows with bseq > after_bseq, in bseq order."""
        with self._sync_lock:
            db = self._sync_db()
            cursor = db.execute(
                SELECT_BATCH_UPDATES,
                (batch_id, after_bseq),
            )
            return [(int(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in cursor.fetchall()]

    async def _read_batch_updates(self, batch_id: str, after_bseq: int = 0) -> list[tuple[int, str, str, str]]:
        """Async mirror of ``_read_batch_updates_sync``."""
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(
                SELECT_BATCH_UPDATES,
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
                SELECT_CHILD_SETTLEMENT,
                (batch_id,),
            )
            return children_settled_rows(await children_cursor.fetchall())

    async def _all_children_resting(self, batch_id: str) -> bool:
        """True when no manifest child is still running or queued.

        The watch-termination question for ``until="resting"``: the same
        rows ``_all_children_settled`` reads, under the weaker predicate, so
        a child parked on a human ENDS the stream instead of holding it open
        forever. Every fact a resting child earned has already been
        delivered — ``child_paused`` commits in the same transaction as the
        pause it reports.
        """
        await self._ensure_db()
        async with self._txn_lock():
            children_cursor = await self._db.execute(
                SELECT_CHILD_SETTLEMENT,
                (batch_id,),
            )
            return children_resting_rows(await children_cursor.fetchall())

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

        Which children the stop reaches, and the two refusals it can raise,
        are ``_batch_store.resolve_batch_stop``; this mirror owns the reads
        and the writes.

        Returns True when the Batch stop was newly written; False when the
        Batch was already stopped (duplicate).
        """
        stop = BatchStop(batch_id, info, source_ref)
        with self._sync_lock:
            db = self._sync_db()
            try:
                db.execute("BEGIN IMMEDIATE")
                batch_row = db.execute(SELECT_BATCH_BY_ID, (batch_id,)).fetchone()
                child_rows = db.execute(
                    SELECT_STOP_TARGETS,
                    (batch_id,),
                ).fetchall()
                _batch, targets = resolve_batch_stop(batch_row, child_rows, batch_id=batch_id)
                already = db.execute(SELECT_STOPPED, (batch_id,)).fetchone()
                if already is not None:
                    db.rollback()
                    return False
                now = _now_iso()
                self._append_batch_update_sync(db, batch_id, "stopped", stop.fact)
                for child_workflow_id in targets:
                    existing = db.execute(
                        SELECT_UNAPPLIED_COMMAND,
                        (child_workflow_id, STOP_VERB),
                    ).fetchone()
                    if existing is not None:
                        continue
                    db.execute(
                        INSERT_COMMAND,
                        stop.child_command_row(child_workflow_id, now),
                    )
                    self._append_run_update_sync(db, child_workflow_id, "command", stop.fact)
                db.commit()
                return True
            except BaseException:
                self._rollback_sync(db)
                raise

    async def _write_batch_stop(self, batch_id: str, info: Any, source_ref: str | None = None) -> bool:
        """Async mirror of ``_write_batch_stop_sync``."""
        stop = BatchStop(batch_id, info, source_ref)
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                batch_cursor = await self._db.execute(SELECT_BATCH_BY_ID, (batch_id,))
                batch_row = await batch_cursor.fetchone()
                children_cursor = await self._db.execute(
                    SELECT_STOP_TARGETS,
                    (batch_id,),
                )
                child_rows = await children_cursor.fetchall()
                _batch, targets = resolve_batch_stop(batch_row, child_rows, batch_id=batch_id)
                already_cursor = await self._db.execute(SELECT_STOPPED, (batch_id,))
                if await already_cursor.fetchone() is not None:
                    await self._db.rollback()
                    return False
                now = _now_iso()
                await self._append_batch_update(batch_id, "stopped", stop.fact)
                for child_workflow_id in targets:
                    existing_cursor = await self._db.execute(
                        SELECT_UNAPPLIED_COMMAND,
                        (child_workflow_id, STOP_VERB),
                    )
                    if await existing_cursor.fetchone() is not None:
                        continue
                    await self._db.execute(
                        INSERT_COMMAND,
                        stop.child_command_row(child_workflow_id, now),
                    )
                    await self._append_run_update(child_workflow_id, "command", stop.fact)
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
        settlement cannot re-admit twice. And it is the ONLY re-admission
        there is: the worker's release owns no such transition, and since it
        compare-and-sets on the claim it held, it cannot even reach a
        submission this call has already moved. An answer that lands in the
        window between the pause commit and ``_release_submission`` is
        therefore re-admitted here and only here — the late release finds a
        claim that is no longer its own and does nothing.

        Returns True when this call performed the transition.
        """
        result = await self._db.execute(READMIT_ANSWERED_SQL, (run_id, SUBMISSION_STATE_PAUSED))
        if result.rowcount != 1:
            return False
        await self._append_occurrence_fact(run_id, pause_id, kind=RUNNABLE_UPDATE_KIND)
        return True

    def _readmit_answered_pause_sync(self, db: Any, run_id: str, pause_id: str | None) -> bool:
        """Sync mirror of ``_readmit_answered_pause``."""
        result = db.execute(READMIT_ANSWERED_SQL, (run_id, SUBMISSION_STATE_PAUSED))
        if result.rowcount != 1:
            return False
        self._append_occurrence_fact_sync(db, run_id, pause_id, kind=RUNNABLE_UPDATE_KIND)
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
        self,
        now_iso: str | None = None,
        *,
        served: Collection[DefinitionId],
        builders: Collection[str] = (),
        worker_id: str | None = None,
        limit: int = _CLAIM_BATCH,
    ) -> list[dict[str, Any]]:
        """CAS-claim eligible pending submissions (state -> 'claimed').

        Every claim bumps the submission's ``claim_seq`` and the returned row
        carries the bumped value, so the claimant holds the IDENTITY of its
        own claim and not merely the knowledge that some claim exists. That
        is what ``_release_submission`` compares against: park, answer, and
        re-claim can return a submission to 'claimed' under a different
        attempt while this one is still unwinding, and a release that
        compared only the state name would settle that live claim.

        Eligible means ``state='pending'``, ``compat_state='compatible'``,
        and ``start_at`` absent or past. ``now_iso`` defaults to the STORE's
        clock (``_store_now``), so eligibility never depends on a worker
        process's own clock; a caller that scans several due sets in one
        pass passes one instant down, and tests drive time explicitly.

        A submission is claimed when this worker can execute it EITHER way:
        its pinned Definition identity is in ``served`` (the exact served
        identities plus any ``accepts=`` declarations), or it carries a
        ``builder_key`` this worker registered in ``builders``. The builder
        gate is deliberately the KEY alone — the identity a builder produces
        is knowable only after building — so the exact identity check moves
        to the build step in ``Host._execute_submission``, where a mismatch
        dead-letters instead of running a graph against topology it was never
        pinned to.

        A submission this worker cannot execute gets one of two dispositions,
        and telling them apart is what the worker registry is for:

        - **Somebody might yet run it** — a live worker (or this one) serves
          the pinned Definition NAME, just not that exact identity. That is a
          rolling deployment, which ``accepts=`` exists to drain, so the row
          is marked ``compat_state='incompatible'`` exactly as before: later
          scans skip it so it never starves claimable rows, and clients see
          ``WaitingCondition.VERSION_INCOMPATIBLE``. Waiting, not lost.
        - **Nothing alive answers to that name at all** (and no live builder
          registers its key): retire it as a ``dead_letter`` with a durable
          reason. This is the case that used to be a silent park — work
          accepted against code no live deployment runs, sitting pending
          forever with no executor and no error. Now it is settled, carries
          ``WaitingCondition.DEAD_LETTER`` and a ``dead_lettered`` run update
          naming why, tells its Batch (``child_settled``), and is revived —
          like recovery-exhausted work — by ``client.rerun()``.

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
        non-claiming dispositions: tripped-Batch children, incompatible rows,
        and dead letters are still settled on this scan.
        """
        served_set = frozenset(served)
        builder_set = frozenset(builders)
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
                    "ORDER BY created_at, rowid",
                    (now_iso,),
                )
                submissions = [_row_to_submission(row) for row in await cursor.fetchall()]
                free_slots = await self._free_admission_slots()
                budget_cursor = await self._db.execute(_SELECT_SETTING_SQL, (_MAX_ADMISSION_UNITS_KEY,))
                admission_budget = _cap_from_row(await budget_cursor.fetchone())
                usage_cursor = await self._db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(admission_cost), 0) FROM host_submissions WHERE state = 'claimed'"
                )
                usage_row = await usage_cursor.fetchone()
                claimed_count = int(usage_row[0])
                claimed_units = int(usage_row[1])
                oversized_active = admission_budget is not None and claimed_count == 1 and claimed_units > admission_budget
                tripped = await self._tripped_batch_ids({s["batch_id"] for s in submissions if s["batch_id"] is not None})
                claimed: list[dict[str, Any]] = []
                admission_blocked = False
                # Both read lazily and at most once per scan: the common pass
                # claims everything it sees and needs neither.
                coverage: WorkerCoverage | None = None
                served_names: frozenset[str] | None = None
                for submission in submissions:
                    if submission["batch_id"] in tripped:
                        # A tripped Batch has CLOSED ADMISSION: no pending
                        # child is newly claimed, and none is re-admitted —
                        # including one an answered pause just returned to
                        # claim order. Tolerance is a stop-the-line decision;
                        # exempting work that happens to have started would
                        # make the threshold advisory.
                        result = await self._db.execute(
                            "UPDATE host_submissions SET state = 'finished', finished_at = ? WHERE workflow_id = ? AND state = 'pending'",
                            (now_iso, submission["workflow_id"]),
                        )
                        if result.rowcount == 1:
                            # A9: this item settles AFTER the trip fact
                            # already listed its items, so it gets its own
                            # durable row in the SAME transaction as the
                            # state flip. Without it a detached watch()
                            # would never learn the item's outcome and the
                            # stream could not reconstruct the view.
                            await self._append_trip_closeout(submission["workflow_id"])
                        continue
                    identity = DefinitionId(
                        submission["definition_name"],
                        submission["def_version"],
                        submission["def_struct_hash"],
                    )
                    builder_key = submission["builder_key"]
                    if identity not in served_set and (builder_key is None or builder_key not in builder_set):
                        # This worker cannot execute it either way. Whether
                        # that is a wait or a dead end is a question about
                        # everybody, so ask the registry — once per scan, and
                        # only when a row actually needs the answer.
                        if coverage is None:
                            cursor = await self._db.execute(_SELECT_LIVE_WORKERS_SQL, (_pulse_cutoff(now_iso),))
                            coverage = _coverage_from_rows(await cursor.fetchall(), worker_id)
                        if served_names is None:
                            served_names = frozenset(entry.name for entry in served_set)
                        if coverage.may_yet_cover(identity, builder_key) or identity.name in served_names:
                            # Somebody serves this Definition NAME — this
                            # worker at another version, or another worker
                            # outright. That is a rolling deployment, and
                            # `accepts=` is how it drains, so the submission
                            # parks exactly as it always has.
                            logger.warning(
                                "Worker cannot serve submission %s: pinned identity %s is not served here; "
                                "marking it version-incompatible (it stays parked until a serving worker or explicit migration).",
                                submission["workflow_id"],
                                identity.to_dict(),
                            )
                            await self._db.execute(
                                "UPDATE host_submissions SET compat_state = 'incompatible' WHERE workflow_id = ? AND state = 'pending'",
                                (submission["workflow_id"],),
                            )
                            continue
                        reason = DEAD_LETTER_BUILDER_MISSING if builder_key is not None else DEAD_LETTER_UNSERVED_IDENTITY
                        await self._dead_letter_in_txn(submission, reason, now_iso, identity=identity)
                        continue
                    if len(claimed) >= limit:
                        continue
                    if free_slots is not None and free_slots <= 0:
                        # Over the active-Run cap: leave it pending. Claim
                        # order is the scan order, so the oldest waiting
                        # submission takes the next freed slot.
                        continue
                    cost = int(submission["admission_cost"])
                    if admission_budget is not None:
                        fits_units = (
                            not admission_blocked
                            and not oversized_active
                            and _weighted_admission_fits(admission_budget, claimed_count, claimed_units, cost)
                        )
                        if not fits_units:
                            # Weighted admission remains FIFO. In particular,
                            # an oversized head drains the queue until it can
                            # run alone rather than starving behind smaller
                            # items that happen to fit.
                            admission_blocked = True
                            continue
                    result = await self._db.execute(
                        "UPDATE host_submissions SET state = 'claimed', claimed_at = ?, claim_seq = claim_seq + 1 "
                        "WHERE workflow_id = ? AND state = 'pending'",
                        (now_iso, submission["workflow_id"]),
                    )
                    if result.rowcount == 1:
                        # The row was read before the bump, so the claim this
                        # caller now owns is one past what it says. Handing
                        # the stale value back would let the releaser match a
                        # claim it never held.
                        submission["claim_seq"] = int(submission["claim_seq"]) + 1
                        claimed.append(submission)
                        if free_slots is not None:
                            free_slots -= 1
                        claimed_count += 1
                        claimed_units += cost
                        oversized_active = cost > admission_budget if admission_budget is not None else False
                await self._db.commit()
                return claimed
            except BaseException:
                await self._rollback_async()
                raise

    async def _dead_letter_in_txn(
        self,
        submission: dict[str, Any],
        reason: str,
        now_iso: str,
        *,
        identity: DefinitionId | None = None,
        claim_seq: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> bool:
        """Retire one submission nothing can execute; caller holds the txn.

        THE dead-letter transition, and the only one. The state flip, the
        durable ``dead_lettered`` run update carrying the reason, and the
        Batch's ``child_settled`` fact commit together — the same discipline
        the recovery brake follows — so a detached ``watch`` learns the
        item's fate from the stream alone and can never wait on it forever.

        The transition is a COMPARE-AND-SET, on whichever state the caller
        is entitled to retire:

        - ``claim_seq=None`` (the claim scan): only a still-``pending`` row.
          A submission a worker claimed between the scan's read and this
          write is live work and must not be retired out from under it.
        - ``claim_seq=N`` (a worker that claimed the row and then could not
          build it): only ``claimed`` at exactly that claim, the same fence
          ``_release_submission`` uses. A stale claimant cannot retire a
          submission a newer claim already owns.

        Returns True when this call was the one that retired the row.
        """
        workflow_id = submission["workflow_id"]
        identity = identity or DefinitionId(submission["definition_name"], submission["def_version"], submission["def_struct_hash"])
        if claim_seq is None:
            result = await self._db.execute(
                "UPDATE host_submissions SET state = ?, finished_at = ? WHERE workflow_id = ? AND state = 'pending'",
                (SUBMISSION_STATE_DEAD_LETTER, now_iso, workflow_id),
            )
        else:
            result = await self._db.execute(
                "UPDATE host_submissions SET state = ?, finished_at = ? WHERE workflow_id = ? AND state = 'claimed' AND claim_seq = ?",
                (SUBMISSION_STATE_DEAD_LETTER, now_iso, workflow_id, claim_seq),
            )
        if result.rowcount != 1:
            return False
        logger.warning(
            "Dead-lettering submission %s (%s): pinned identity %s, builder %r. "
            "Serve the Definition (or register the builder) where the work should run, then client.rerun() to revive it.",
            workflow_id,
            reason,
            identity.to_dict(),
            submission["builder_key"],
        )
        await self._append_run_update(
            workflow_id,
            DEAD_LETTERED_UPDATE_KIND,
            {
                "reason": reason,
                "definition_id": identity.to_dict(),
                "builder_key": submission["builder_key"],
                **(detail or {}),
            },
        )
        await self._append_child_settled(workflow_id, BATCH_OUTCOME_DEAD_LETTER)
        return True

    async def _dead_letter(self, workflow_id: str, reason: str, *, claim_seq: int, detail: dict[str, Any] | None = None) -> bool:
        """Retire a submission THIS claim turned out to be unable to execute.

        The worker's door into the same transition the claim scan uses: it
        owns the transaction, the scan owns its own. Both write exactly one
        state flip plus its durable facts, and both fence — the scan on
        'pending', this on the claim it holds.
        """
        await self._ensure_db()
        now = await self._store_now()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                cursor = await self._db.execute(_SELECT_SUBMISSION, (workflow_id,))
                row = await cursor.fetchone()
                if row is None:
                    await self._db.rollback()
                    return False
                retired = await self._dead_letter_in_txn(_row_to_submission(row), reason, now, claim_seq=claim_seq, detail=detail)
                await self._db.commit()
                return retired
            except BaseException:
                await self._rollback_async()
                raise

    async def _has_run_row(self, workflow_id: str) -> bool:
        """Whether this submission ever started executing; caller holds the txn."""
        cursor = await self._db.execute(_SELECT_RUN_EXISTS, (workflow_id,))
        return await cursor.fetchone() is not None

    async def _tripped_batch_ids(self, batch_ids: Collection[str]) -> frozenset[str]:
        """Which of ``batch_ids`` have a durable trip; caller holds the transaction."""
        ids = list(batch_ids)
        if not ids:
            return frozenset()
        placeholders = ", ".join("?" for _ in ids)
        cursor = await self._db.execute(
            f"SELECT DISTINCT batch_id FROM batch_updates WHERE kind = '{TRIP_UPDATE_KIND}' AND batch_id IN ({placeholders})",
            ids,
        )
        return frozenset(str(row[0]) for row in await cursor.fetchall())

    async def _release_submission(self, workflow_id: str, claim_seq: int) -> bool:
        """Settle the submission THIS claim finished executing.

        Deliberately a COMPARE-AND-SET on the claim — ``state = 'claimed'``
        **and** ``claim_seq``, the value ``_claim_eligible`` handed this
        caller — and deliberately the owner of only one transition: settled
        work becoming ``finished``.

        Every other outcome was already decided by the transaction that
        caused it, so this call is a no-op for it:

        - a run that paused was parked by the PAUSE transaction
          (``_park_submission``), together with its slot, its ``PAUSED``
          status, and its ``child_paused`` fact;
        - a run whose pause was answered was re-admitted by the ANSWER
          transaction (``settle_pause`` -> ``_readmit_answered_pause``),
          together with the stored answer and its ``child_runnable`` fact.

        Owning a second required transition here is what made the release
        window a race: an answer landing inside it had to be noticed and
        re-applied by the worker, so process death between the two left the
        decision durable and the run unclaimable. Now each transition has
        exactly one owner and one commit, and this call cannot undo either.

        Matching the state NAME alone was not enough to keep that promise.
        Park, answer, and re-claim walk a submission 'claimed' → 'paused' →
        'pending' → 'claimed', so a release still unwinding from the first
        attempt found the name it expected and finished the SECOND attempt's
        live claim: the batch stream ended while the question was open, the
        item was reported abandoned, and the restart scan never re-adopted
        it. Comparing ``claim_seq`` makes a stale release exactly what it
        should be — nothing.

        Returns True when this claim was the one settled.
        """
        await self._ensure_db()
        async with self._txn_lock():
            try:
                await self._db.execute("BEGIN IMMEDIATE")
                result = await self._db.execute(RELEASE_SUBMISSION_SQL, (SUBMISSION_STATE_FINISHED, _now_iso(), workflow_id, claim_seq))
                await self._db.commit()
                return bool(result.rowcount == 1)
            except BaseException:
                await self._rollback_async()
                raise

    def _register_sync_wait_cancellation(self) -> tuple[threading.Event, Token[threading.Event | None]]:
        """Register the fence a cancelled ``to_thread`` execution sets."""
        event = threading.Event()
        return event, _sync_wait_cancellation.set(event)

    def _clear_sync_wait_cancellation(self, token: Token[threading.Event | None]) -> None:
        """Restore the caller context after ``to_thread`` copied its fence."""
        _sync_wait_cancellation.reset(token)

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
                    _SELECT_SUBMISSION_STATE,
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
                    SELECT_UNAPPLIED_COMMAND,
                    (workflow_id, STOP_VERB),
                ).fetchone()
                if existing is not None:
                    db.rollback()
                    return False
                db.execute(
                    INSERT_COMMAND,
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
                    _SELECT_SUBMISSION_STATE,
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
                    SELECT_UNAPPLIED_COMMAND,
                    (workflow_id, STOP_VERB),
                )
                if await existing_cursor.fetchone() is not None:
                    await self._db.rollback()
                    return False
                await self._db.execute(
                    INSERT_COMMAND,
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
    ) -> ScheduledAnswerRow:
        """Normalize one scheduled answer into its stored row shape."""
        return scheduled_answer_row(
            workflow_id,
            pause_id,
            value,
            _normalize_utc_iso(due_at, field="due_at"),
            source_ref,
            now=_now_iso(),
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
                    SELECT_UNAPPLIED_SCHEDULED_ANSWER,
                    (row.run_id, SCHEDULE_ANSWER_VERB, slot.pause_id),
                ).fetchone()
                if existing is not None:
                    db.rollback()
                    return False
                db.execute(
                    INSERT_SCHEDULED_ANSWER,
                    (row.run_id, SCHEDULE_ANSWER_VERB, row.payload, slot.pause_id, row.due_at, row.source_ref, row.created_at),
                )
                self._append_run_update_sync(
                    db,
                    row.run_id,
                    "command",
                    scheduled_answer_fact(pause_id=slot.pause_id, due_at=row.due_at, source_ref=row.source_ref, outcome=None),
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
                    SELECT_UNAPPLIED_SCHEDULED_ANSWER,
                    (row.run_id, SCHEDULE_ANSWER_VERB, slot.pause_id),
                )
                if await existing_cursor.fetchone() is not None:
                    await self._db.rollback()
                    return False
                await self._db.execute(
                    INSERT_SCHEDULED_ANSWER,
                    (row.run_id, SCHEDULE_ANSWER_VERB, row.payload, slot.pause_id, row.due_at, row.source_ref, row.created_at),
                )
                await self._append_run_update(
                    row.run_id,
                    "command",
                    scheduled_answer_fact(pause_id=slot.pause_id, due_at=row.due_at, source_ref=row.source_ref, outcome=None),
                )
                await self._db.commit()
                return True
            except BaseException:
                await self._rollback_async()
                raise

    async def _due_scheduled_answers(self, now_iso: str) -> list[DueScheduledAnswer]:
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
        return [DueScheduledAnswer.from_row(row) for row in rows]

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

    async def _fire_scheduled_answer(self, due: DueScheduledAnswer) -> ScheduledAnswerOutcome | None:
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
                    scheduled_answer_fact(pause_id=due.pause_id, due_at=due.due_at, source_ref=due.source_ref, outcome=outcome),
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
                    SELECT_UNAPPLIED_COMMAND,
                    (workflow_id, STOP_VERB),
                )
                if await stop_cursor.fetchone() is None:
                    await self._db.commit()
                    return False
                run_cursor = await self._db.execute(_SELECT_RUN_EXISTS, (workflow_id,))
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

    # === durable timing reads (issue #386) ===

    def _timing_run_rows_sync(self, *, definition: str | None, batch_id: str | None, limit: int) -> list[tuple[dict[str, Any], Run | None]]:
        """Host Runs a timing read covers, joined with their runs row."""
        statement, params = _timing_run_rows_query(definition=definition, batch_id=batch_id, limit=limit)
        with self._sync_lock:
            rows = self._sync_db().execute(statement, params).fetchall()
        return self._join_timing_rows(rows)

    async def _timing_run_rows(self, *, definition: str | None, batch_id: str | None, limit: int) -> list[tuple[dict[str, Any], Run | None]]:
        """Async mirror of ``_timing_run_rows_sync``."""
        statement, params = _timing_run_rows_query(definition=definition, batch_id=batch_id, limit=limit)
        await self._ensure_db()
        async with self._txn_lock():
            cursor = await self._db.execute(statement, params)
            rows = await cursor.fetchall()
        return self._join_timing_rows(rows)

    def _join_timing_rows(self, rows: Sequence[Any]) -> list[tuple[dict[str, Any], Run | None]]:
        sub_count = len(_SUBMISSION_COLS.split(", "))
        return [(_row_to_submission(row[:sub_count]), self._row_to_run(row[sub_count:]) if row[sub_count] is not None else None) for row in rows]

    def _descendant_run_ids_sync(self, root_ids: Sequence[str]) -> dict[str, str]:
        """Every run under ``root_ids``, mapped to the root that owns it."""
        owners: dict[str, str] = {}
        for chunk in self._chunk_run_ids(root_ids):
            with self._sync_lock:
                rows = self._sync_db().execute(*_descendant_runs_query(chunk)).fetchall()
            owners.update({str(run_id): str(root_id) for run_id, root_id in rows})
        return owners

    async def _descendant_run_ids(self, root_ids: Sequence[str]) -> dict[str, str]:
        """Async mirror of ``_descendant_run_ids_sync``."""
        owners: dict[str, str] = {}
        if not root_ids:
            return owners
        await self._ensure_db()
        for chunk in self._chunk_run_ids(root_ids):
            async with self._txn_lock():
                cursor = await self._db.execute(*_descendant_runs_query(chunk))
                rows = await cursor.fetchall()
            owners.update({str(run_id): str(root_id) for run_id, root_id in rows})
        return owners

    def _step_timing_rows_sync(self, run_ids: Sequence[str]) -> list[tuple[Any, ...]]:
        """Durable step facts for these runs, in execution order."""
        facts: list[tuple[Any, ...]] = []
        for chunk in self._chunk_run_ids(run_ids):
            with self._sync_lock:
                facts.extend(self._sync_db().execute(*_step_timing_query(chunk)).fetchall())
        return facts

    async def _step_timing_rows(self, run_ids: Sequence[str]) -> list[tuple[Any, ...]]:
        """Async mirror of ``_step_timing_rows_sync``."""
        facts: list[tuple[Any, ...]] = []
        if not run_ids:
            return facts
        await self._ensure_db()
        for chunk in self._chunk_run_ids(run_ids):
            async with self._txn_lock():
                cursor = await self._db.execute(*_step_timing_query(chunk))
                facts.extend(await cursor.fetchall())
        return facts

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

    def _latest_run_update_times_sync(self, run_ids: list[str]) -> dict[str, str]:
        """Newest durable fact timestamp per requested Run, in one read."""
        if not run_ids:
            return {}
        placeholders = ", ".join("?" for _ in run_ids)
        with self._sync_lock:
            rows = (
                self._sync_db()
                .execute(
                    f"""
                SELECT update_row.run_id, update_row.created_at
                FROM run_updates AS update_row
                JOIN (
                    SELECT run_id, MAX(seq) AS seq
                    FROM run_updates
                    WHERE run_id IN ({placeholders})
                    GROUP BY run_id
                ) AS latest
                  ON latest.run_id = update_row.run_id AND latest.seq = update_row.seq
                """,
                    run_ids,
                )
                .fetchall()
            )
        return {str(run_id): str(created_at) for run_id, created_at in rows}

    async def _latest_run_update_times(self, run_ids: list[str]) -> dict[str, str]:
        """Async mirror of ``_latest_run_update_times_sync``."""
        if not run_ids:
            return {}
        await self._ensure_db()
        placeholders = ", ".join("?" for _ in run_ids)
        async with self._txn_lock():
            cursor = await self._db.execute(
                f"""
                SELECT update_row.run_id, update_row.created_at
                FROM run_updates AS update_row
                JOIN (
                    SELECT run_id, MAX(seq) AS seq
                    FROM run_updates
                    WHERE run_id IN ({placeholders})
                    GROUP BY run_id
                ) AS latest
                  ON latest.run_id = update_row.run_id AND latest.seq = update_row.seq
                """,
                run_ids,
            )
            rows = await cursor.fetchall()
        return {str(run_id): str(created_at) for run_id, created_at in rows}

    def _dead_letter_reasons_sync(self, run_ids: Sequence[str]) -> dict[str, str]:
        """Why each of these Runs was retired, in one read.

        Callers pass ONLY ids whose submission already says ``dead_letter``,
        so an ordinary listing — where none is — issues no query at all and
        this read costs what it returns rather than what the Home holds.
        """
        if not run_ids:
            return {}
        placeholders = ", ".join("?" for _ in run_ids)
        with self._sync_lock:
            rows = (
                self._sync_db()
                .execute(
                    f"SELECT run_id, payload FROM run_updates WHERE kind = ? AND run_id IN ({placeholders})",
                    (DEAD_LETTERED_UPDATE_KIND, *run_ids),
                )
                .fetchall()
            )
        return _reasons_from_rows(rows)

    async def _dead_letter_reasons(self, run_ids: Sequence[str]) -> dict[str, str]:
        """Async mirror of ``_dead_letter_reasons_sync``."""
        if not run_ids:
            return {}
        await self._ensure_db()
        placeholders = ", ".join("?" for _ in run_ids)
        async with self._txn_lock():
            cursor = await self._db.execute(
                f"SELECT run_id, payload FROM run_updates WHERE kind = ? AND run_id IN ({placeholders})",
                (DEAD_LETTERED_UPDATE_KIND, *run_ids),
            )
            rows = await cursor.fetchall()
        return _reasons_from_rows(rows)
