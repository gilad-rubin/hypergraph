"""RunHomeClient — backend-neutral inspection of existing work.

A client is constructed from a Run Home alone — no Definition code needed.
It owns ``get`` and ``watch`` for existing runs; submission stays with the
Host. The same surface runs against SQLite now and other backends later.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import get_close_matches
from typing import TYPE_CHECKING, Any

from hypergraph.checkpointers.types import PauseSlot, WorkflowStatus
from hypergraph.host._bus import _bus_for, _PreviewBus
from hypergraph.host.batch import BatchTolerance
from hypergraph.host.definition import DefinitionId
from hypergraph.host.errors import RerunError
from hypergraph.host.fingerprint import batch_fingerprint, start_fingerprint
from hypergraph.host.refs import BatchCommandReceipt, BatchRef, BatchSubmitReceipt, CommandReceipt, RunRef, SubmitReceipt
from hypergraph.host.views import (
    BATCH_COUNT_KEYS,
    BATCH_OUTCOME_RECOVERY_EXHAUSTED,
    TERMINAL_WORKFLOW_STATUSES,
    BatchUpdate,
    BatchView,
    RunQuery,
    RunUpdate,
    RunView,
    WaitingCondition,
    is_child_settled,
)

if TYPE_CHECKING:
    from hypergraph.checkpointers.types import Run
    from hypergraph.host.home import RunHome


def _parse_iso(value: str) -> datetime:
    """Parse an ISO timestamp from either store ('Z' runs rows, offset submissions)."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_cursor(after: str | int | None) -> int:
    """Normalize a watch cursor to a durable sequence number."""
    if after is None:
        return 0
    if isinstance(after, int):
        return max(after, 0)  # negative seqs clamp to the stream start
    if isinstance(after, str) and after.startswith("seq:"):
        try:
            return max(int(after[len("seq:") :]), 0)
        except ValueError:
            pass
    raise ValueError(f"Invalid watch cursor {after!r}. Expected None, an int seq, or a 'seq:N' cursor string from a durable RunUpdate.")


def _parse_batch_cursor(after: str | int | None) -> int:
    """Normalize a Batch watch cursor to a durable bseq number."""
    if after is None:
        return 0
    if isinstance(after, int):
        return max(after, 0)  # negative bseqs clamp to the stream start
    if isinstance(after, str) and after.startswith("bseq:"):
        try:
            return max(int(after[len("bseq:") :]), 0)
        except ValueError:
            pass
    raise ValueError(f"Invalid batch watch cursor {after!r}. Expected None, an int bseq, or a 'bseq:N' cursor string from a durable BatchUpdate.")


def _child_settled(submission: dict[str, Any], run: Run | None) -> bool:
    """Adapt one joined child row to the shared settled-child rule.

    The rule itself lives in ``views.is_child_settled`` — the rerun gate and
    ``BatchView.settled`` must never disagree about whether a child can
    still change outcome.
    """
    return is_child_settled(submission["state"], run.status.value if run is not None else None)


def _build_batch_view(
    home_uri: str,
    batch: dict[str, Any],
    child_rows: dict[str, tuple[dict[str, Any], Run | None]],
    tripped: bool,
) -> BatchView:
    """Build the keyed BatchView from the manifest row and joined children.

    Every manifest item lands in exactly one counts bucket; outcomes and
    unstarted items stay keyed by logical item key in manifest order —
    completion order never changes result identity.
    """
    item_keys = list(json.loads(batch["items_json"]))
    counts = {key: 0 for key in BATCH_COUNT_KEYS}
    outcomes: dict[str, str | None] = {}
    unstarted: list[str] = []
    for key in item_keys:
        submission, run = child_rows[key]
        if run is not None and run.status in TERMINAL_WORKFLOW_STATUSES:
            bucket, outcome = run.status.value, run.status.value
        elif submission["state"] == "exhausted":
            bucket, outcome = BATCH_OUTCOME_RECOVERY_EXHAUSTED, BATCH_OUTCOME_RECOVERY_EXHAUSTED
        elif run is not None:
            bucket, outcome = "active", None
        elif submission["state"] == "finished":
            # Finished with no runs row: stopped before first execution.
            bucket, outcome = "unstarted", None
            unstarted.append(key)
        else:
            bucket, outcome = "queued", None
        counts[bucket] += 1
        outcomes[key] = outcome
    # Settlement uses THE settled-child rule, the same one the rerun gate
    # applies — a child is never settled for rerun and in flight for the view.
    settled = all(_child_settled(submission, run) for submission, run in child_rows.values())
    return BatchView(
        batch_ref=BatchRef(home=home_uri, batch_id=batch["batch_id"]),
        workflow_id=batch["workflow_id"],
        definition_id=DefinitionId(batch["definition_name"], batch["def_version"], batch["def_struct_hash"]),
        counts=counts,
        outcomes=outcomes,
        unstarted_items=tuple(unstarted),
        settled=settled,
        tolerance_tripped=tripped,
        retry_of=batch["retry_of"],
    )


def _build_view(
    home_uri: str,
    run_id: str,
    submission: dict[str, Any] | None,
    run: Run | None,
    *,
    admission_full: bool = False,
) -> RunView | None:
    """Build one RunView.

    ``admission_full`` answers "is the active-Run cap out of slots right
    now?" from the STORE — the cap and the claim count are both Home-scoped
    facts, so an operator's client process reports exactly what the worker
    holds back. It is computed once per ``get``/``list`` call so a due,
    compatible, still-pending submission reports ``ADMISSION_LIMITED``
    instead of a bare ``QUEUED``, and it is False for an uncapped Home,
    which never produces the condition at all.
    """
    if submission is None and run is None:
        return None
    waiting = None
    if run is not None and run.status is WorkflowStatus.PAUSED:
        waiting = WaitingCondition.PAUSED
    elif submission is not None and submission["state"] == "exhausted":
        waiting = WaitingCondition.RECOVERY_EXHAUSTED
    elif submission is not None and run is None:
        if (
            submission["state"] == "pending"
            and submission["start_at"] is not None
            and _parse_iso(submission["start_at"]) > datetime.now(timezone.utc)
        ):
            waiting = WaitingCondition.SCHEDULED
        elif submission["state"] == "pending" and submission["compat_state"] == "incompatible":
            waiting = WaitingCondition.VERSION_INCOMPATIBLE
        elif submission["state"] == "pending" and admission_full:
            # Due, compatible, and claimable — held back only by the
            # active-Run cap. A claimed submission is already executing and
            # holds a slot, so it stays QUEUED until its runs row appears.
            waiting = WaitingCondition.ADMISSION_LIMITED
        elif submission["state"] in ("pending", "claimed"):
            waiting = WaitingCondition.QUEUED
    definition_id = None
    if submission is not None:
        definition_name = submission["definition_name"]
        definition_id = DefinitionId(submission["definition_name"], submission["def_version"], submission["def_struct_hash"])
    elif run is not None:
        definition_name = run.graph_name or ""
        config = run.config or {}
        definition_id = DefinitionId(definition_name, config.get("deployment_version", ""), config.get("graph_struct_hash", ""))
    else:  # pragma: no cover - guarded by the None check above
        definition_name = ""
    # Lineage never merges: retry_of (repetition) and forked_from
    # (migration) are mutually exclusive at the host surface. The runs row
    # wins when present; the submission's recorded lineage is the fallback
    # (and the truth while the run has not executed yet).
    retry_of = (run.retry_of if run is not None else None) or (submission["retry_of"] if submission is not None else None)
    forked_from = None
    if retry_of is None:
        forked_from = (run.forked_from if run is not None else None) or (submission["forked_from"] if submission is not None else None)
    return RunView(
        run_ref=RunRef(home=home_uri, run_id=run_id),
        workflow_id=run_id,
        definition_name=definition_name,
        status=run.status if run is not None else None,
        waiting=waiting,
        definition_id=definition_id,
        retry_of=retry_of,
        forked_from=forked_from,
    )


@dataclass(frozen=True)
class _PlannedBatchRerun:
    """The new Batch submission a subset rerun derives from its source.

    Every field is read from the immutable source manifest (or minted for
    the new Batch), so the sync and async mirrors submit byte-identical
    work and differ only in the call they make.

    Attributes:
        batch_id: Freshly minted id for the new Batch.
        workflow_id: New Batch workflow id (``<source>-retry-N``).
        definition_id: The source's pinned Definition identity, verbatim.
        items: Manifest-ordered ``(item_key, inputs_json)`` pairs for the
            selected source items.
        tolerance_json: The source's pinned tolerance, verbatim (or None).
        fingerprint: Batch fingerprint of the new manifest.
        batch_retry_of: Source ``batch_id`` recorded as Batch lineage.
        child_retry_of: Item key → source child workflow id.
    """

    batch_id: str
    workflow_id: str
    definition_id: DefinitionId
    items: list[tuple[str, str]]
    tolerance_json: str | None
    fingerprint: str
    batch_retry_of: str
    child_retry_of: dict[str, str]


def _valid_item_keys(manifest: dict[str, Any], limit: int = 20) -> str:
    """Render the source manifest's keys as an error's valid-options list."""
    keys = sorted(manifest)
    if len(keys) <= limit:
        return repr(keys)
    return f"{keys[:limit]!r} … (+{len(keys) - limit} more, {len(keys)} total)"


def _did_you_mean(key: str, manifest: dict[str, Any]) -> str:
    """A ``Did you mean 'X'?`` clause for a misspelled item key, or ''."""
    matches = get_close_matches(key, list(manifest), n=1, cutoff=0.6)
    return f"Did you mean {matches[0]!r}? " if matches else ""


def _plan_batch_rerun(
    batch: dict[str, Any],
    child_rows: dict[str, tuple[dict[str, Any], Run | None]],
    item_keys: Sequence[str] | None,
    retry_count: int,
) -> _PlannedBatchRerun:
    """Validate a subset rerun and derive the new Batch submission.

    Everything is read from the immutable SOURCE manifest — Definition
    identity, per-item inputs, pinned tolerance — so a rerun repeats and
    never redefines. ``item_keys=None`` selects the whole manifest.

    Args:
        batch: The source manifest row.
        child_rows: Source children joined with their runs rows, keyed by
            item key.
        item_keys: Source item keys to repeat, or None for the whole
            manifest.
        retry_count: Batches already minted as a rerun of this source.

    Returns:
        The planned submission, ready to hand to ``_submit_batch``.

    Raises:
        TypeError: If ``item_keys`` is not a sequence of strings.
        ValueError: If ``item_keys`` is empty, holds a non-string or empty
            key, or names the same key twice.
        RerunError: If ``item_keys`` names keys outside the source manifest,
            or any selected child is still in flight.
    """
    manifest = json.loads(batch["items_json"])
    if item_keys is None:
        selected = list(manifest)
    else:
        if isinstance(item_keys, str) or not isinstance(item_keys, Sequence):
            raise TypeError(
                f"rerun() item_keys must be a sequence of item key strings, got {type(item_keys).__name__}.\n\n"
                f"Valid item keys: {_valid_item_keys(manifest)}\n\n"
                "How to fix: pass a list of source manifest keys (item_keys=['a', 'b']), "
                "or omit item_keys to repeat the whole manifest."
            )
        seen: set[str] = set()
        for key in item_keys:
            if not isinstance(key, str) or not key:
                raise ValueError(
                    f"rerun() item keys must be non-empty strings, got {key!r}.\n\n"
                    f"Valid item keys: {_valid_item_keys(manifest)}\n\n"
                    "How to fix: name source manifest keys as non-empty strings."
                )
            if key in seen:
                raise ValueError(
                    f"rerun() duplicate item key {key!r}; name every source item at most once.\n\n"
                    "How to fix: drop the repeated key — a rerun mints exactly one new child per selected source item."
                )
            seen.add(key)
        if not seen:
            raise ValueError(
                "rerun() item_keys must name at least one source item; an empty rerun is not a rerun.\n\n"
                f"Valid item keys: {_valid_item_keys(manifest)}\n\n"
                "How to fix: name the source item keys to repeat, or omit item_keys to repeat the whole manifest."
            )
        unknown = [key for key in item_keys if key not in manifest]
        if unknown:
            raise RerunError(
                batch["workflow_id"],
                f"Cannot rerun items {unknown!r} of Batch {batch['workflow_id']!r}: they are not in the source manifest.\n\n"
                f"Valid item keys: {_valid_item_keys(manifest)}\n\n"
                f"How to fix: {_did_you_mean(unknown[0], manifest)}name only keys from the source manifest. "
                "Subset rerun repeats source items only; a different manifest is a new submit_batch().",
            )
        # Manifest order, never caller order: item order is manifest truth.
        selected = [key for key in manifest if key in seen]
    unsettled = [key for key in selected if not _child_settled(*child_rows[key])]
    if unsettled:
        raise RerunError(
            batch["workflow_id"],
            f"Cannot rerun items {unsettled!r} of Batch {batch['workflow_id']!r}: those children are still in flight.\n\n"
            "How to fix: wait for them to settle (watch the Batch, or poll client.get(batch_ref).settled) "
            "and rerun then, or name only settled item keys. Rerun repeats settled work so one logical item "
            "never has two live Runs.",
        )
    pairs = [(key, json.dumps(manifest[key])) for key in selected]
    tolerance = BatchTolerance.from_dict(json.loads(batch["tolerance_json"])) if batch["tolerance_json"] is not None else None
    definition_id = DefinitionId(batch["definition_name"], batch["def_version"], batch["def_struct_hash"])
    return _PlannedBatchRerun(
        batch_id=f"b-{uuid.uuid4().hex[:12]}",
        workflow_id=f"{batch['workflow_id']}-retry-{retry_count + 1}",
        definition_id=definition_id,
        items=pairs,
        tolerance_json=batch["tolerance_json"],
        fingerprint=batch_fingerprint(definition_id, {key: json.loads(value) for key, value in pairs}, tolerance, None),
        batch_retry_of=batch["batch_id"],
        # Each new child records retry_of against its SOURCE child's
        # workflow id — lineage names the run it repeats, not the item key.
        child_retry_of={key: str(child_rows[key][0]["workflow_id"]) for key in selected},
    )


def _reject_run_item_keys(item_keys: Sequence[str] | None) -> None:
    if item_keys is not None:
        raise TypeError(
            "rerun() item_keys is only valid for a BatchRef; a Run has no items.\n\n"
            "How to fix: pass the BatchRef to repeat named Batch items, or drop item_keys to repeat this Run."
        )


def _validate_query(query: RunQuery) -> RunQuery:
    if not isinstance(query, RunQuery):
        raise TypeError(f"list() expects a RunQuery, got {type(query).__name__}.")
    if isinstance(query.limit, bool) or not isinstance(query.limit, int) or query.limit < 1:
        raise ValueError(f"RunQuery.limit must be a positive int, got {query.limit!r}.")
    if query.batch is not None and not isinstance(query.batch, (str, BatchRef)):
        raise TypeError(f"RunQuery.batch must be a BatchRef, a batch id string, or None, got {type(query.batch).__name__}.")
    return query


def _query_batch_id(query: RunQuery) -> str | None:
    if query.batch is None:
        return None
    return query.batch.batch_id if isinstance(query.batch, BatchRef) else query.batch


def _filter_list_rows(
    home_uri: str,
    rows: list[tuple[dict[str, Any] | None, Run | None]],
    query: RunQuery,
    *,
    admission_full: bool = False,
) -> list[RunView]:
    """Build views for joined rows and apply the RunQuery filters, oldest first."""
    cutoff = datetime.now(timezone.utc) - query.older_than if query.older_than is not None else None
    batch_id = _query_batch_id(query)
    matched: list[tuple[datetime, RunView]] = []
    for submission, run in rows:
        run_id = submission["workflow_id"] if submission is not None else run.id  # type: ignore[union-attr]
        view = _build_view(home_uri, run_id, submission, run, admission_full=admission_full)
        if view is None:  # pragma: no cover - rows always carry one side
            continue
        if query.definition is not None and view.definition_name != query.definition:
            continue
        if query.status is not None and view.status is not query.status:
            continue
        if query.waiting is not None and view.waiting is not query.waiting:
            continue
        if batch_id is not None and (submission is None or submission["batch_id"] != batch_id):
            continue
        created_at = run.created_at if run is not None else _parse_iso(submission["created_at"])  # type: ignore[index]
        if cutoff is not None and created_at > cutoff:
            continue
        matched.append((created_at, view))
    matched.sort(key=lambda pair: (pair[0], pair[1].workflow_id))
    return [view for _, view in matched[: query.limit]]


class RunHomeClient:
    """Inspect existing runs in a Run Home — no graph code required.

    Construct directly from a ``RunHome``::

        client = RunHomeClient(RunHome.open("file:./runs.db"))

    ``rerun`` repeats a settled (or recovery-exhausted) run under a new
    workflow id with retry lineage, or mints a new immutable Batch from
    named source item keys; ``stop`` records a durable stop command;
    ``list`` filters joined run views through a typed ``RunQuery``;
    ``answer`` settles one observed durable pause occurrence.
    """

    def __init__(self, home: RunHome, *, _bus: _PreviewBus | None = None) -> None:
        from hypergraph.host.home import RunHome as _RunHome

        if not isinstance(home, _RunHome):
            raise TypeError(f"RunHomeClient expects a RunHome, got {type(home).__name__}. Open one with RunHome.open(uri).")
        self._home = home
        # An explicit bus wins; otherwise pick up the bus serve() registered
        # for this Home URI when a worker lives in the same process.
        self._bus = _bus if _bus is not None else _bus_for(home.uri)

    async def get(self, ref: RunRef | BatchRef) -> RunView | BatchView | None:
        """Return persisted facts for ``ref``, or None if unknown.

        Accepts a ``RunRef`` (→ ``RunView``) or a ``BatchRef`` (→
        ``BatchView`` with keyed counts, outcomes, and unstarted items).
        """
        if isinstance(ref, BatchRef):
            batch = await self._home._get_batch(ref.batch_id)
            if batch is None:
                return None
            child_rows = await self._home._batch_child_rows(ref.batch_id)
            return _build_batch_view(self._home.uri, batch, child_rows, await self._home._batch_tripped(ref.batch_id))
        if not isinstance(ref, RunRef):
            raise TypeError(f"get() expects a RunRef or BatchRef, got {type(ref).__name__}.")
        submission = await self._home._get_submission(ref.run_id)
        run = await self._home.get_run_async(ref.run_id)
        return _build_view(self._home.uri, ref.run_id, submission, run, admission_full=await self._home._admission_is_full())

    def get_sync(self, ref: RunRef | BatchRef) -> RunView | BatchView | None:
        """Sync mirror of ``get``."""
        if isinstance(ref, BatchRef):
            batch = self._home._get_batch_sync(ref.batch_id)
            if batch is None:
                return None
            child_rows = self._home._batch_child_rows_sync(ref.batch_id)
            return _build_batch_view(self._home.uri, batch, child_rows, self._home._batch_tripped_sync(ref.batch_id))
        if not isinstance(ref, RunRef):
            raise TypeError(f"get() expects a RunRef or BatchRef, got {type(ref).__name__}.")
        submission = self._home._get_submission_sync(ref.run_id)
        run = self._home.get_run(ref.run_id)
        return _build_view(self._home.uri, ref.run_id, submission, run, admission_full=self._home._admission_is_full_sync())

    async def stop(self, ref: RunRef | BatchRef, *, info: Any = None, source_ref: str | None = None) -> CommandReceipt | BatchCommandReceipt:
        """Record a durable stop command for ``ref``.

        The command facts commit before this returns — the stop survives
        process loss. For a ``RunRef`` the command row and its durable
        ``command`` update commit in one transaction; the worker applies it
        on its next scan: an executing run receives
        ``runner.stop(workflow_id, info=info)``; a run that never started
        is finished without executing (no runs row is invented); a run
        that already completed is unaffected. For a ``BatchRef`` ONE
        transaction appends the durable ``stopped`` batch update and writes
        a stop command for every unsettled child: pending children finish
        without ever executing (they become explicit unstarted items) and
        executing children settle cooperatively.

        Returns a receipt with ``duplicate=True`` when the stop was already
        recorded — the first stop's ``info`` wins. ``source_ref`` is an
        opaque caller provenance marker (ADR 0005 A11) stored on the
        command row; it is never authentication and never affects dedup.

        Raises ``AlreadyTerminalError`` when the run (or every Batch child)
        is already settled at write time and ``HostError`` when the ref is
        unknown to this Run Home.
        """
        if isinstance(ref, BatchRef):
            created = await self._home._write_batch_stop(ref.batch_id, info, source_ref)
            return BatchCommandReceipt(batch_ref=ref, duplicate=not created)
        if not isinstance(ref, RunRef):
            raise TypeError(f"stop() expects a RunRef or BatchRef, got {type(ref).__name__}.")
        created = await self._home._write_stop_command(ref.run_id, info, source_ref)
        return CommandReceipt(run_ref=ref, duplicate=not created)

    def stop_sync(self, ref: RunRef | BatchRef, *, info: Any = None, source_ref: str | None = None) -> CommandReceipt | BatchCommandReceipt:
        """Sync mirror of ``stop``."""
        if isinstance(ref, BatchRef):
            created = self._home._write_batch_stop_sync(ref.batch_id, info, source_ref)
            return BatchCommandReceipt(batch_ref=ref, duplicate=not created)
        if not isinstance(ref, RunRef):
            raise TypeError(f"stop() expects a RunRef or BatchRef, got {type(ref).__name__}.")
        created = self._home._write_stop_command_sync(ref.run_id, info, source_ref)
        return CommandReceipt(run_ref=ref, duplicate=not created)

    async def answer(self, ref: RunRef, *, pause_id: str | None = None, value: Any) -> PauseSlot:
        """Settle the pause occurrence ``pause_id`` with one typed ``value``.

        Unlike ``stop`` — a durable command a worker applies later — an
        answer is applied here: the schema check, the compare-and-set on the
        occurrence, the resume-input write, and the durable ``answer`` fact
        commit in one transaction. So this returns the settled
        :class:`~hypergraph.checkpointers.types.PauseSlot` (durable truth)
        rather than a receipt for work still to come.

        Read the occurrence first — ``(await client.get_run_slot(ref))`` or
        ``run.pause_slot`` — and pass its ``pause_id``: every answer names the
        occurrence it observed, so a stale answer can never settle a later
        pause.

        Raises:
            AnswerRejectedError: no ``pause_id``, an unknown one, a run that
                is not paused, or a value failing the slot's
                ``answer_schema``. Nothing is written and the occurrence
                stays open, so a corrected value can answer it.
            PauseAlreadySettledError: this occurrence was already answered;
                the first caller's value wins.
            StalePauseError: a later pause occurrence is current.
        """
        if not isinstance(ref, RunRef):
            raise TypeError(f"answer() expects a RunRef, got {type(ref).__name__}. A Batch answers through its child runs.")
        return await self._home.settle_pause(ref.run_id, pause_id=pause_id, value=value)

    def answer_sync(self, ref: RunRef, *, pause_id: str | None = None, value: Any) -> PauseSlot:
        """Sync mirror of ``answer``."""
        if not isinstance(ref, RunRef):
            raise TypeError(f"answer() expects a RunRef, got {type(ref).__name__}. A Batch answers through its child runs.")
        return self._home.settle_pause_sync(ref.run_id, pause_id=pause_id, value=value)

    async def schedule_answer(
        self,
        ref: RunRef,
        *,
        pause_id: str | None = None,
        value: Any,
        due_at: datetime | str,
        source_ref: str | None = None,
    ) -> CommandReceipt:
        """Arm ONE typed answer to apply when store time reaches ``due_at``.

        This is the durable form of "if nobody answers by Friday, treat it as
        declined" (ADR 0008). It is a pause-scoped command, not a scheduler:
        there is no recurrence, no cron expression, no caller-chosen verb,
        and no non-interrupting reminder — those stay product concerns.

        The scheduled answer is admitted through the SAME refusal cascade
        ``answer`` uses, so an unarmable timer is refused now rather than
        discovered dead later, and it is applied through the same
        compare-and-set, so a human answer and a timer racing the same
        occurrence resolve by commit order:

        ```python
        slot = await client.get_run_slot(ref)
        await client.schedule_answer(
            ref,
            pause_id=slot.pause_id,
            value=False,                       # checked against slot.answer_schema now
            due_at=datetime.now(timezone.utc) + timedelta(hours=72),
            source_ref="review-console:req-91",
        )
        ```

        A human answer voids the timer: once the occurrence is settled — or
        replaced by a later pause in a loop — the scheduled answer can never
        apply. It is not deleted; when it comes due it is refused and the
        command row records the refusal, so the audit trail keeps the timer,
        its due time, its value, and its ``source_ref``.

        Args:
            ref: The paused run's inert address.
            pause_id: The occurrence this answer is armed against, read from
                ``client.get_run_slot(ref)``. Omitting it is an
                ``AnswerRejectedError`` — a timer that does not name its
                occurrence could settle a later pause.
            value: One typed value, validated against the slot's
                ``answer_schema`` before anything is written.
            due_at: When the answer becomes applicable (datetime or ISO
                string, naive read as UTC). Required — an answer with no due
                time is ``client.answer``.
            source_ref: Opaque caller provenance recorded on the command row
                for audit. Never authentication, and never part of dedup.

        Returns:
            ``CommandReceipt`` with ``verb="schedule_answer"`` and
            ``duplicate=True`` when this occurrence already had an unapplied
            scheduled answer — one timer per pause, the first one wins.

        Raises:
            AnswerRejectedError: no ``pause_id``, an unknown one, a run with
                no durable pause, a run that is not paused, or a value
                failing the slot's ``answer_schema``. Nothing is written.
            PauseAlreadySettledError: this occurrence is already answered.
            StalePauseError: a later pause occurrence is current.
        """
        if not isinstance(ref, RunRef):
            raise TypeError(f"schedule_answer() expects a RunRef, got {type(ref).__name__}. A Batch answers through its child runs.")
        created = await self._home._write_scheduled_answer(
            ref.run_id,
            pause_id=pause_id,
            value=value,
            due_at=due_at,
            source_ref=source_ref,
        )
        return CommandReceipt(run_ref=ref, verb="schedule_answer", duplicate=not created)

    def schedule_answer_sync(
        self,
        ref: RunRef,
        *,
        pause_id: str | None = None,
        value: Any,
        due_at: datetime | str,
        source_ref: str | None = None,
    ) -> CommandReceipt:
        """Sync mirror of ``schedule_answer``."""
        if not isinstance(ref, RunRef):
            raise TypeError(f"schedule_answer() expects a RunRef, got {type(ref).__name__}. A Batch answers through its child runs.")
        created = self._home._write_scheduled_answer_sync(
            ref.run_id,
            pause_id=pause_id,
            value=value,
            due_at=due_at,
            source_ref=source_ref,
        )
        return CommandReceipt(run_ref=ref, verb="schedule_answer", duplicate=not created)

    async def get_run_slot(self, ref: RunRef) -> PauseSlot | None:
        """The run's current durable pause occurrence, or None.

        ``settled_at`` says whether it was answered; the run's status says
        whether it is still waiting.
        """
        if not isinstance(ref, RunRef):
            raise TypeError(f"get_run_slot() expects a RunRef, got {type(ref).__name__}.")
        return await self._home.get_pause_slot(ref.run_id)

    def get_run_slot_sync(self, ref: RunRef) -> PauseSlot | None:
        """Sync mirror of ``get_run_slot``."""
        if not isinstance(ref, RunRef):
            raise TypeError(f"get_run_slot() expects a RunRef, got {type(ref).__name__}.")
        return self._home.get_pause_slot_sync(ref.run_id)

    async def list(self, query: RunQuery) -> list[RunView]:
        """List runs matching ``query``, oldest first.

        Joins submissions with their runs rows and includes bare Tier-0
        runs (a runs row with no submission). Every filter is the same
        typed vocabulary views report: ``status`` matches the runs row
        (runs without one never match), ``waiting`` is computed exactly
        like ``RunView.waiting``, ``older_than`` compares the row's
        creation time, and ``limit`` caps the result after oldest-first
        ordering.
        """
        _validate_query(query)
        rows = await self._home._list_run_rows()
        return _filter_list_rows(self._home.uri, rows, query, admission_full=await self._home._admission_is_full())

    def list_sync(self, query: RunQuery) -> list[RunView]:
        """Sync mirror of ``list``."""
        _validate_query(query)
        rows = self._home._list_run_rows_sync()
        return _filter_list_rows(self._home.uri, rows, query, admission_full=self._home._admission_is_full_sync())

    async def rerun(self, ref: RunRef | BatchRef, *, item_keys: Sequence[str] | None = None) -> SubmitReceipt | BatchSubmitReceipt:
        """Repeat settled work under a new id with retry lineage.

        For a ``RunRef`` the new submission carries the source's pinned
        Definition identity and inputs verbatim — repetition never migrates
        and never overrides inputs (A16); there is deliberately no
        ``inputs`` parameter. The new workflow id is ``<source>-retry-N``
        where N = existing retries + 1, matching the checkpointer's own
        ``retry_workflow`` derivation. The worker executes the submission
        with ``retry_from=<source>`` so the runs row records
        ``retry_of``/``retry_index`` lineage.

        For a ``BatchRef``, ``item_keys`` names source item keys to repeat
        (omit it to repeat the whole manifest) and a **new immutable Batch
        manifest** is minted with a new ``BatchRef``: new child Runs under
        new workflow ids for the selected keys only, carrying the source's
        pinned Definition identity, source inputs, and pinned tolerance.
        The new Batch records ``retry_of`` against the source Batch and
        each child records ``retry_of`` against its source child. The
        source Batch is never mutated — it stays settled and queryable
        forever.

        Raises ``RerunError`` when the source is unknown or not terminal
        (rerun repeats settled work only) — unless the source submission
        is recovery-exhausted, which is exactly the rerun case: reviving
        braked work under a fresh workflow id — and, for a Batch, when
        ``item_keys`` names keys outside the source manifest or children
        that are still in flight. ``item_keys`` with a ``RunRef`` is a
        ``TypeError``: a Run has no items. Needs no loaded Definition code
        — unlike ``host.fork()``, which migrates and therefore stays
        Host-side.
        """
        if isinstance(ref, BatchRef):
            batch, child_rows = await self._require_batch_source(ref)
            retry_count = await self._home._count_batch_retries(ref.batch_id)
            plan = _plan_batch_rerun(batch, child_rows, item_keys, retry_count)
            created, row = await self._home._submit_batch(
                batch_id=plan.batch_id,
                workflow_id=plan.workflow_id,
                definition_name=plan.definition_id.name,
                def_version=plan.definition_id.deployment_version,
                def_struct_hash=plan.definition_id.structural_hash,
                items=plan.items,
                tolerance_json=plan.tolerance_json,
                # A repeat starts now, never on the source's past schedule.
                start_at=None,
                source_ref=None,
                fingerprint=plan.fingerprint,
                batch_retry_of=plan.batch_retry_of,
                child_retry_of=plan.child_retry_of,
            )
            return BatchSubmitReceipt(
                batch_ref=BatchRef(home=self._home.uri, batch_id=row["batch_id"]),
                workflow_id=plan.workflow_id,
                duplicate=not created,
            )
        if not isinstance(ref, RunRef):
            raise TypeError(f"rerun() expects a RunRef or BatchRef, got {type(ref).__name__}.")
        _reject_run_item_keys(item_keys)
        submission, run = await self._require_terminal_source(ref)
        retry_count = await self._home._count_retries(ref.run_id)
        workflow_id = f"{ref.run_id}-retry-{retry_count + 1}"
        inputs_json = submission["inputs_json"]
        definition_id = DefinitionId(submission["definition_name"], submission["def_version"], submission["def_struct_hash"])
        created, _row = await self._home._submit(
            workflow_id,
            definition_id.name,
            definition_id.deployment_version,
            definition_id.structural_hash,
            inputs_json,
            None,
            None,
            fingerprint=start_fingerprint(definition_id, inputs_json, None),
            retry_of=ref.run_id,
        )
        return SubmitReceipt(run_ref=RunRef(home=self._home.uri, run_id=workflow_id), workflow_id=workflow_id, duplicate=not created)

    def rerun_sync(self, ref: RunRef | BatchRef, *, item_keys: Sequence[str] | None = None) -> SubmitReceipt | BatchSubmitReceipt:
        """Sync mirror of ``rerun``."""
        if isinstance(ref, BatchRef):
            batch = self._home._get_batch_sync(ref.batch_id)
            if batch is None:
                raise RerunError(ref.batch_id, f"Cannot rerun batch {ref.batch_id!r}: no such Batch in this Run Home.")
            child_rows = self._home._batch_child_rows_sync(ref.batch_id)
            retry_count = self._home._count_batch_retries_sync(ref.batch_id)
            plan = _plan_batch_rerun(batch, child_rows, item_keys, retry_count)
            created, row = self._home._submit_batch_sync(
                batch_id=plan.batch_id,
                workflow_id=plan.workflow_id,
                definition_name=plan.definition_id.name,
                def_version=plan.definition_id.deployment_version,
                def_struct_hash=plan.definition_id.structural_hash,
                items=plan.items,
                tolerance_json=plan.tolerance_json,
                # A repeat starts now, never on the source's past schedule.
                start_at=None,
                source_ref=None,
                fingerprint=plan.fingerprint,
                batch_retry_of=plan.batch_retry_of,
                child_retry_of=plan.child_retry_of,
            )
            return BatchSubmitReceipt(
                batch_ref=BatchRef(home=self._home.uri, batch_id=row["batch_id"]),
                workflow_id=plan.workflow_id,
                duplicate=not created,
            )
        if not isinstance(ref, RunRef):
            raise TypeError(f"rerun() expects a RunRef or BatchRef, got {type(ref).__name__}.")
        _reject_run_item_keys(item_keys)
        submission = self._home._get_submission_sync(ref.run_id)
        if submission is None:
            raise RerunError(ref.run_id, f"Cannot rerun {ref.run_id!r}: no such run in this Run Home.")
        run = self._home.get_run(ref.run_id)
        if submission["state"] != "exhausted" and (run is None or run.status not in TERMINAL_WORKFLOW_STATUSES):
            raise RerunError(
                ref.run_id,
                f"Cannot rerun {ref.run_id!r}: the source run is not terminal. Rerun repeats settled work; wait for it to settle or fork/migrate instead.",
            )
        retry_count = self._home._count_retries_sync(ref.run_id)
        workflow_id = f"{ref.run_id}-retry-{retry_count + 1}"
        inputs_json = submission["inputs_json"]
        definition_id = DefinitionId(submission["definition_name"], submission["def_version"], submission["def_struct_hash"])
        created, _row = self._home._submit_sync(
            workflow_id,
            definition_id.name,
            definition_id.deployment_version,
            definition_id.structural_hash,
            inputs_json,
            None,
            None,
            fingerprint=start_fingerprint(definition_id, inputs_json, None),
            retry_of=ref.run_id,
        )
        return SubmitReceipt(run_ref=RunRef(home=self._home.uri, run_id=workflow_id), workflow_id=workflow_id, duplicate=not created)

    async def _require_batch_source(self, ref: BatchRef) -> tuple[dict[str, Any], dict[str, tuple[dict[str, Any], Run | None]]]:
        """Return the source Batch manifest row and its joined children."""
        batch = await self._home._get_batch(ref.batch_id)
        if batch is None:
            raise RerunError(ref.batch_id, f"Cannot rerun batch {ref.batch_id!r}: no such Batch in this Run Home.")
        return batch, await self._home._batch_child_rows(ref.batch_id)

    async def _require_terminal_source(self, ref: RunRef) -> tuple[dict[str, Any], Run | None]:
        """Return the source submission + runs row, or raise RerunError.

        A recovery-exhausted submission is a rerun source even without a
        terminal runs row: rerun is how braked work is revived under a
        fresh workflow id (the runs row may be None or nonterminal).
        """
        submission = await self._home._get_submission(ref.run_id)
        if submission is None:
            raise RerunError(ref.run_id, f"Cannot rerun {ref.run_id!r}: no such run in this Run Home.")
        run = await self._home.get_run_async(ref.run_id)
        if submission["state"] == "exhausted":
            return submission, run
        if run is None or run.status not in TERMINAL_WORKFLOW_STATUSES:
            raise RerunError(
                ref.run_id,
                f"Cannot rerun {ref.run_id!r}: the source run is not terminal. Rerun repeats settled work; wait for it to settle or fork/migrate instead.",
            )
        return submission, run

    async def watch(
        self, ref: RunRef | BatchRef, *, after: str | int | None = None, poll_interval: float = 0.05
    ) -> AsyncIterator[RunUpdate | BatchUpdate]:
        """Replay durable facts after ``after``, then tail live previews.

        Accepts a ``RunRef`` (yields ``RunUpdate`` with ``seq:N`` cursors)
        or a ``BatchRef`` (yields ``BatchUpdate`` with ``bseq:N`` cursors).
        Durable updates carry ``durable=True`` and a cursor that advances
        monotonically; replay from a stored cursor has no gaps and no
        repeats. Live previews (only when a worker runs in this process)
        carry ``durable=False`` and repeat the last durable cursor — they
        never advance it. Store cursors from durable updates only.

        For a run, the generator ends once the run reaches a terminal
        status and every committed fact has been delivered. For a Batch, it
        ends once every child is terminally settled (or the Batch is
        durably stopped) and every committed Batch fact has been delivered;
        explicit unstarted-item truth comes from ``get(batch_ref)``. A
        ``ref`` unknown to this Run Home terminates immediately with no
        updates, matching ``get()``'s honest ``None``.
        """
        if isinstance(ref, BatchRef):
            async for update in self._watch_batch(ref, after=after, poll_interval=poll_interval):
                yield update
            return
        if not isinstance(ref, RunRef):
            raise TypeError(f"watch() expects a RunRef or BatchRef, got {type(ref).__name__}.")
        cursor_seq = _parse_cursor(after)
        queue: asyncio.Queue | None = None
        if self._bus is not None:
            queue = self._bus.subscribe(ref.run_id)
        terminal = False
        try:
            while True:
                rows = await self._home._read_run_updates(ref.run_id, cursor_seq)
                for seq, kind, payload, created_at in rows:
                    cursor_seq = seq
                    yield RunUpdate(
                        cursor=f"seq:{seq}",
                        durable=True,
                        kind=kind,
                        payload=json.loads(payload),
                        timestamp=created_at,
                    )
                if queue is not None:
                    while True:
                        try:
                            kind, payload = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        yield RunUpdate(
                            cursor=f"seq:{cursor_seq}",
                            durable=False,
                            kind=kind,
                            payload=payload,
                            timestamp=datetime.now(timezone.utc).isoformat(),
                        )
                if rows:
                    continue
                if terminal:
                    return
                run = await self._home.get_run_async(ref.run_id)
                if run is None:
                    submission = await self._home._get_submission(ref.run_id)
                    if submission is None:
                        # Unknown ref: nothing to replay, nothing to tail.
                        return
                    # Stop-before-start (or never-admitted) work is settled
                    # once its submission is finished, with no runs row.
                    terminal = submission["state"] == "finished"
                else:
                    terminal = run.status in TERMINAL_WORKFLOW_STATUSES
                if not terminal:
                    await asyncio.sleep(poll_interval)
        finally:
            if queue is not None and self._bus is not None:
                self._bus.unsubscribe(ref.run_id, queue)

    async def _watch_batch(self, ref: BatchRef, *, after: str | int | None, poll_interval: float) -> AsyncIterator[BatchUpdate]:
        """Replay the per-Batch durable sequence, then tail child previews.

        Durable facts (``manifest``, ``child_settled``,
        ``tolerance_tripped``, ``stopped``) come from batch_updates with one
        gap-free ``bseq`` cursor. Live previews
        are fanned in from every child run's preview queue (same process
        only) and carry the child's ``run_id``/``item_key``; they never
        advance the cursor. The generator ends once the Batch is durably
        stopped or every child is terminally settled — and every committed
        fact has been delivered.
        """
        cursor_seq = _parse_batch_cursor(after)
        # The manifest is immutable, so the child set is read once.
        child_rows = await self._home._batch_child_rows(ref.batch_id)
        queues: dict[str, tuple[str | None, asyncio.Queue]] = {}
        if self._bus is not None:
            for item_key, (submission, _run) in child_rows.items():
                queues[submission["workflow_id"]] = (item_key, self._bus.subscribe(submission["workflow_id"]))
        terminal = False
        try:
            while True:
                rows = await self._home._read_batch_updates(ref.batch_id, cursor_seq)
                for bseq, kind, payload, created_at in rows:
                    cursor_seq = bseq
                    yield BatchUpdate(
                        cursor=f"bseq:{bseq}",
                        durable=True,
                        kind=kind,
                        payload=json.loads(payload),
                        timestamp=created_at,
                    )
                for run_id, (item_key, queue) in queues.items():
                    while True:
                        try:
                            kind, payload = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        yield BatchUpdate(
                            cursor=f"bseq:{cursor_seq}",
                            durable=False,
                            kind=kind,
                            payload={**payload, "run_id": run_id, "item_key": item_key},
                            timestamp=datetime.now(timezone.utc).isoformat(),
                        )
                if rows:
                    continue
                if terminal:
                    return
                batch = await self._home._get_batch(ref.batch_id)
                if batch is None:
                    # Unknown ref: nothing to replay, nothing to tail.
                    return
                stopped, settled = await self._home._batch_settlement(ref.batch_id)
                terminal = stopped or settled
                if not terminal:
                    await asyncio.sleep(poll_interval)
        finally:
            if self._bus is not None:
                for run_id, (_item_key, queue) in queues.items():
                    self._bus.unsubscribe(run_id, queue)
