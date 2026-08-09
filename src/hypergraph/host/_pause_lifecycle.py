"""Pause lifecycle policy: the two transitions, and the timer around them.

A durable pause has exactly two state transitions, and the whole safety of
the feature is that each has ONE owner and ONE commit:

- **park** (``claimed -> paused``) belongs to the PAUSE transaction, beside
  the slot, the ``PAUSED`` run status, and the ``child_paused`` fact;
- **re-admit** (``paused -> pending``) belongs to the ANSWER transaction,
  beside the stored answer and the ``child_runnable`` fact.

Both are compare-and-sets, so neither can fire twice and neither disturbs a
submission some other path already moved. ``_release_submission`` owns
neither, which is what closed the window in which an answer could land
between a pause commit and the worker's release.

The two CASes deliberately compare DIFFERENT things, because they assert
different facts:

- park and re-admit assert something about the RUN — "it is parked on a
  person", "its question was answered". Those are true whoever holds the
  claim, so comparing the state name is the whole guard they need.
- the release asserts something about ONE EXECUTION — "the attempt I ran
  came back". That is true only for the claim that ran it, so its CAS also
  compares ``claim_seq``. Comparing the state name alone made it an ABA:
  park, answer, and re-claim take a submission from 'claimed' back to
  'claimed', and a release still unwinding from the FIRST attempt then
  matched — and finished — the SECOND attempt's live claim.

The SQL and the vocabulary live here so a reader can see the pair together;
``RunHome`` still owns the transactions they commit inside. The scheduled
answer (ADR 0008) is here for the same reason: a timer is a deferred answer,
so it settles through exactly the same cascade and earns exactly the same
re-admission, and its normalization and audit shape are pure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

#: THE park transition: claimed -> paused, inside the pause transaction.
#: Compare-and-set on 'claimed' so a submission another path already moved
#: is left alone, and so a replayed pause commit is a no-op.
#:
#: Deliberately NOT claim-scoped, unlike the release. A park is caused by an
#: interrupt RAISING inside the executing attempt and commits with that same
#: attempt's ``PAUSED`` run status; there is no unwind path that parks late,
#: so the ABA interleaving the release suffered has no analogue here. The one
#: way a park can land under a claim that did not cause it is the documented
#: at-least-once case — an orphaned thread from a cancelled ``to_thread``
#: still executing the same run id — and there the park states the truth
#: about the shared runs row anyway: that run really is paused on a question
#: nobody has answered, and the answer transaction re-admits it as usual.
#:
#: ``claimed_by``/``lease_until`` clear alongside ``claimed_at``, for the
#: reason that column already does: the pair names who holds the claim RIGHT
#: NOW, and a parked run is held by nobody. A lease left on a paused row
#: would be a claim nobody is renewing, which is exactly what a reclaim scan
#: reads as adoptable work.
PARK_SUBMISSION_SQL = (
    "UPDATE host_submissions SET state = ?, claimed_at = NULL, claimed_by = NULL, lease_until = NULL, finished_at = NULL "
    "WHERE workflow_id = ? AND state = 'claimed'"
)

#: THE re-admit transition: paused -> pending, inside the answer transaction.
#: Deliberately a plain flip back to ordinary queued work — an answered child
#: is subject to the same Definition-compatibility, delayed-start,
#: admission-cap, stop, and recovery-brake rules as everything else.
#: Answering never jumps the queue, and it never picks the worker: whichever
#: worker claims it next takes the lease.
READMIT_ANSWERED_SQL = (
    "UPDATE host_submissions SET state = 'pending', claimed_at = NULL, claimed_by = NULL, lease_until = NULL, finished_at = NULL "
    "WHERE workflow_id = ? AND state = ?"
)

#: THE release: settled work becoming finished, and nothing else. Every other
#: outcome was already decided by the transaction that caused it.
#:
#: Compare-and-set on the CLAIM (``claim_seq``), not merely on the state
#: name. A releaser speaks only for the attempt it ran, and between that
#: attempt parking and its release returning, an answer plus a re-claim can
#: put the submission back in 'claimed' under a NEW attempt. Matching the
#: name alone finished that live claim: the batch stream ended on an open
#: question, the item was reported abandoned, and the reclaim scan (which
#: re-adopts 'claimed' only) never picked it up again.
#:
#: This same CAS is the FENCE that makes leases safe. A lease expiring proves
#: nothing about its holder — the presumed-dead worker may be partitioned,
#: paused by the OS, or simply slow, and may wake up and finish. What it
#: cannot do is commit: the adopting worker took a new ``claim_seq``, so the
#: old claimant's release matches no row and its work is discarded. Oban's
#: Lifeline documents the opposite outcome ("may cause duplicate execution")
#: precisely because it has no such token.
RELEASE_SUBMISSION_SQL = "UPDATE host_submissions SET state = ?, finished_at = ? WHERE workflow_id = ? AND state = 'claimed' AND claim_seq = ?"

STOP_VERB = "stop"
SCHEDULE_ANSWER_VERB = "schedule_answer"

# What a fired scheduled answer produced. Recorded on the command row and
# republished as a durable `command` run update, so a detached watch consumer
# learns a timer's fate without reading the store.
ScheduledAnswerOutcome = Literal["settled", "already_settled", "superseded", "rejected"]
SCHEDULED_ANSWER_SETTLED: ScheduledAnswerOutcome = "settled"
SCHEDULED_ANSWER_ALREADY_SETTLED: ScheduledAnswerOutcome = "already_settled"
SCHEDULED_ANSWER_SUPERSEDED: ScheduledAnswerOutcome = "superseded"
SCHEDULED_ANSWER_REJECTED: ScheduledAnswerOutcome = "rejected"
#: The same closed vocabulary as stored strings, for the store's own values.
SCHEDULED_ANSWER_OUTCOMES: frozenset[str] = frozenset(
    {SCHEDULED_ANSWER_SETTLED, SCHEDULED_ANSWER_ALREADY_SETTLED, SCHEDULED_ANSWER_SUPERSEDED, SCHEDULED_ANSWER_REJECTED}
)


@dataclass(frozen=True)
class ScheduledAnswerRow:
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
class DueScheduledAnswer:
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

    @classmethod
    def from_row(cls, row: Any) -> DueScheduledAnswer:
        """Read one ``host_commands`` row in the due-scan column order."""
        return cls(
            command_id=int(row[0]),
            run_id=str(row[1]),
            pause_id=row[2],
            value=json.loads(row[3]).get("value"),
            due_at=row[4],
            source_ref=row[5],
        )


def scheduled_answer_row(
    workflow_id: str,
    pause_id: str | None,
    value: Any,
    due_at_iso: str | None,
    source_ref: str | None,
    *,
    now: str,
) -> ScheduledAnswerRow:
    """Normalize one scheduled answer into its stored row shape.

    Raises before any store work: a scheduled answer with no due time is
    just an answer, and ``client.answer`` already applies those. The stored
    ``due_at`` is therefore never NULL, which is why the due predicate
    treats a NULL one as inert rather than instantly due.
    """
    if due_at_iso is None:
        raise ValueError(
            "schedule_answer() requires a due_at time.\n\nHow to fix:\n  Pass due_at=<datetime or ISO string> "
            "for when the answer should apply, or call client.answer(...) to answer the pause now."
        )
    return ScheduledAnswerRow(
        run_id=workflow_id,
        payload=json.dumps({"pause_id": pause_id, "value": value}),
        due_at=due_at_iso,
        source_ref=source_ref,
        created_at=now,
    )


def scheduled_answer_fact(
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


# Command-channel statements both mirrors bind (ADR 0008 / PRD 0017).
SELECT_UNAPPLIED_COMMAND = "SELECT id FROM host_commands WHERE run_id = ? AND verb = ? AND applied_at IS NULL LIMIT 1"
SELECT_UNAPPLIED_SCHEDULED_ANSWER = "SELECT id FROM host_commands WHERE run_id = ? AND verb = ? AND pause_id = ? AND applied_at IS NULL LIMIT 1"
INSERT_COMMAND = "INSERT INTO host_commands (run_id, verb, payload, source_ref, created_at) VALUES (?, ?, ?, ?, ?)"
INSERT_SCHEDULED_ANSWER = "INSERT INTO host_commands (run_id, verb, payload, pause_id, due_at, source_ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
