"""Batch persistence policy: what acceptance decides, minus how it writes.

``RunHome`` remains the transaction authority — it owns ``BEGIN IMMEDIATE``,
the commit, and the rollback — but it should not also be the place where a
Batch's identity, manifest, provenance, and refusal rules live. That mixture
is why acceptance had to be written twice, once per mirror, with the whole
decision inlined into each copy.

Everything here is pure. A function takes rows the caller already fetched and
returns a value or raises; nothing touches a connection. So the sync and
async mirrors in ``home.py`` differ only in how they read a row and await a
write, which is the one difference that genuinely cannot be shared.

Two kinds of thing live here:

- **Typed request values.** ``BatchAcceptance`` replaces the thirteen
  positional-and-keyword parameters both acceptance mirrors used to carry
  (identity, manifest, timing, provenance, retry, recovery) with one frozen
  value that knows how to project itself into every row and fact acceptance
  writes.
- **Refusal and accounting policy.** Which reuse of a workflow id is
  use-existing and which is a typed conflict; when a pinned tolerance trips;
  how a trip's still-pending children split into unstarted and abandoned;
  what a lifecycle fact says about one pause occurrence.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from hypergraph.host._pause_lifecycle import STOP_VERB
from hypergraph.host.batch import BatchTolerance, tolerance_trips
from hypergraph.host.definition import DefinitionId
from hypergraph.host.errors import AlreadyTerminalError, HostError, WorkflowIdConflictError
from hypergraph.host.fingerprint import batch_mismatch_aspect, canonical_json, start_fingerprint
from hypergraph.host.views import TERMINAL_STATUS_VALUES, is_child_settled

# === Durable Batch stream kinds ===
#
# One vocabulary, shared by the writer here and the projection in client.py.
# `manifest` is always bseq 1; the rest arrive as children change truth.

MANIFEST_UPDATE_KIND = "manifest"
SETTLED_UPDATE_KIND = "child_settled"
PAUSED_UPDATE_KIND = "child_paused"
RUNNABLE_UPDATE_KIND = "child_runnable"
TRIP_UPDATE_KIND = "tolerance_tripped"
#: An item that ended without ever executing — a stopped Batch's child, or
#: one closed admission reached before a worker did.
UNSTARTED_UPDATE_KIND = "child_unstarted"
#: An item that HAD executed when closed admission settled it. Deliberately
#: not `child_unstarted`: this child committed steps and may have landed
#: side effects, so an operator has to reconcile it before rerunning.
ABANDONED_UPDATE_KIND = "child_abandoned"

# === Batch row shape ===

BATCH_COLS = (
    "batch_id, workflow_id, definition_name, def_version, def_struct_hash, "
    "items_json, tolerance_json, start_at, fingerprint, source_ref, created_at, retry_of, exclusive_by"
)
BATCH_PLACEHOLDERS = ", ".join("?" for _ in BATCH_COLS.split(", "))

#: Every pending child of a Batch, with whether it ever produced a runs row.
#: The LEFT JOIN is what separates "never began" from "began, unfinished".
SELECT_PENDING_CLOSEOUT = (
    "SELECT s.item_key, r.id IS NOT NULL FROM host_submissions s "
    "LEFT JOIN runs r ON r.id = s.workflow_id "
    "WHERE s.batch_id = ? AND s.state = 'pending' ORDER BY s.rowid"
)
SELECT_TRIPPED = f"SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind = '{TRIP_UPDATE_KIND}' LIMIT 1"
#: Failure-equivalent children: failed runs and recovery-exhausted
#: submissions. Paused, queued, delayed, admission-limited, and unstarted
#: children never count (PRD 0019).
COUNT_FAILURE_EQUIVALENT = (
    "SELECT COUNT(*) FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id "
    "WHERE s.batch_id = ? AND (r.status = 'failed' OR s.state = 'exhausted')"
)


# Statements both mirrors bind. A SQL string written twice is a place the
# sync and async doors can silently diverge; naming it makes that impossible.
SELECT_BATCH_BY_ID = f"SELECT {BATCH_COLS} FROM host_batches WHERE batch_id = ?"
SELECT_BATCH_BY_WORKFLOW = f"SELECT {BATCH_COLS} FROM host_batches WHERE workflow_id = ?"
SELECT_BATCH_WORKFLOW_ID = "SELECT workflow_id FROM host_batches WHERE batch_id = ?"
INSERT_BATCH = f"INSERT INTO host_batches ({BATCH_COLS}) VALUES ({BATCH_PLACEHOLDERS})"
SELECT_MEMBERSHIP = "SELECT batch_id, item_key FROM host_submissions WHERE workflow_id = ?"
SELECT_CHILD_SETTLEMENT = "SELECT s.state, r.status FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?"
SELECT_STOP_TARGETS = "SELECT s.workflow_id, s.state, r.status FROM host_submissions s LEFT JOIN runs r ON r.id = s.workflow_id WHERE s.batch_id = ?"
SELECT_CHILD_ID_COLLISION = "SELECT 1 FROM host_submissions WHERE workflow_id = ? UNION SELECT 1 FROM host_batches WHERE workflow_id = ?"
SELECT_TOLERANCE_INPUTS = "SELECT items_json, tolerance_json FROM host_batches WHERE batch_id = ?"
CLOSE_PENDING_CHILDREN = "UPDATE host_submissions SET state = 'finished', finished_at = ? WHERE batch_id = ? AND state = 'pending'"
SELECT_STOPPED = "SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind = 'stopped' LIMIT 1"
SELECT_BATCH_UPDATES = "SELECT bseq, kind, payload, created_at FROM batch_updates WHERE batch_id = ? AND bseq > ? ORDER BY bseq"
#: The bseq allocation and the insert are ONE statement, so two writers can
#: never read the same max and both claim it.
INSERT_BATCH_UPDATE = (
    "INSERT INTO batch_updates (batch_id, bseq, kind, item_key, payload, created_at) "
    "SELECT ?, COALESCE(MAX(bseq), 0) + 1, ?, ?, ?, ? FROM batch_updates WHERE batch_id = ?"
)
#: The once-per-item accounting guard: has this item already been settled,
#: reported unstarted, or reported abandoned?
SELECT_ACCOUNTED = "SELECT 1 FROM batch_updates WHERE batch_id = ? AND kind = ? AND item_key = ? LIMIT 1"


def row_to_batch(row: Sequence[Any]) -> dict[str, Any]:
    """One ``host_batches`` row as a column-keyed dict."""
    return dict(zip(BATCH_COLS.split(", "), row, strict=True))


# === Typed request values ===


@dataclass(frozen=True)
class DefinitionPin:
    """The pinned Definition identity a Batch and every child carries.

    Three fields that always travel together and were passed separately at
    every layer (``definition_name``, ``def_version``, ``def_struct_hash``),
    which made a two-of-three mistake possible at each hop.
    """

    name: str
    version: str
    struct_hash: str

    @property
    def definition_id(self) -> DefinitionId:
        return DefinitionId(self.name, self.version, self.struct_hash)

    def as_fact(self) -> dict[str, str]:
        """The wire shape a durable fact publishes."""
        return {"name": self.name, "deployment_version": self.version, "structural_hash": self.struct_hash}


@dataclass(frozen=True)
class ChildSpec:
    """One manifest item's derived child identity and pinned inputs."""

    item_key: str
    workflow_id: str
    inputs_json: str


@dataclass(frozen=True)
class BatchAcceptance:
    """Everything ONE Batch acceptance transaction needs, as one value.

    Frozen because acceptance pins it: the manifest, the tolerance, and the
    start time a Batch is accepted with can never change afterwards, and a
    request object the transaction could edit halfway through would make
    that promise unenforceable.

    ``workflow_id`` is the one field that may be None on arrival — a rerun
    asks the acceptance transaction to mint ``<source>-retry-N`` from an
    ordinal only that transaction can allocate. Every projection below
    therefore takes the resolved id as an argument rather than reading it
    from the value.
    """

    batch_id: str
    workflow_id: str | None
    definition: DefinitionPin
    items: tuple[tuple[str, str], ...]
    fingerprint: str
    tolerance_json: str | None = None
    start_at: str | None = None
    source_ref: str | None = None
    recovery_cap: int = 3
    exclusive_by: str | None = None
    batch_retry_of: str | None = None
    child_retry_of: Mapping[str, str] = field(default_factory=dict)
    child_exclusive_keys: Mapping[str, str] = field(default_factory=dict)

    @property
    def items_map(self) -> dict[str, Any]:
        """The manifest as ``{item_key: inputs}``, in expansion order."""
        return {key: json.loads(inputs_json) for key, inputs_json in self.items}

    def batch_row(self, workflow_id: str, now: str) -> tuple[Any, ...]:
        """The ``host_batches`` INSERT parameters, in ``BATCH_COLS`` order."""
        return (
            self.batch_id,
            workflow_id,
            self.definition.name,
            self.definition.version,
            self.definition.struct_hash,
            json.dumps(self.items_map),
            self.tolerance_json,
            self.start_at,
            self.fingerprint,
            self.source_ref,
            now,
            self.batch_retry_of,
            self.exclusive_by,
        )

    def manifest_fact(self, workflow_id: str) -> dict[str, Any]:
        """The ``manifest`` Batch fact at ``bseq=1``."""
        return {
            "batch_id": self.batch_id,
            "workflow_id": workflow_id,
            "definition_id": self.definition.as_fact(),
            "item_keys": [key for key, _ in self.items],
            "tolerance": json.loads(self.tolerance_json) if self.tolerance_json is not None else None,
            "start_at": self.start_at,
            "source_ref": self.source_ref,
            "retry_of": self.batch_retry_of,
            "exclusive_by": self.exclusive_by,
        }

    def child_specs(self, workflow_id: str) -> tuple[ChildSpec, ...]:
        """Derive every child identity: ``<batch workflow id>:<item key>``."""
        return tuple(ChildSpec(key, f"{workflow_id}:{key}", inputs_json) for key, inputs_json in self.items)

    def child_source(self, item_key: str) -> str | None:
        """The source child workflow id this item repeats, for a rerun."""
        return self.child_retry_of.get(item_key)

    def child_row(self, spec: ChildSpec, *, retry_index: int | None, now: str) -> tuple[Any, ...]:
        """One child submission row — ordinary pending work, with membership."""
        return (
            spec.workflow_id,
            self.definition.name,
            self.definition.version,
            self.definition.struct_hash,
            spec.inputs_json,
            self.start_at,
            "pending",
            0,
            self.recovery_cap,
            self.source_ref,
            now,
            None,
            None,
            start_fingerprint(self.definition.definition_id, spec.inputs_json, self.start_at),
            "compatible",
            self.child_source(spec.item_key),
            retry_index,
            None,
            None,
            0,
            self.batch_id,
            spec.item_key,
            0,  # claim_seq: no claim has been handed out yet
            self.exclusive_by,
            self.child_exclusive_keys.get(spec.item_key)
            if self.exclusive_by is not None and spec.item_key in self.child_exclusive_keys
            else (None if self.exclusive_by is None else str(json.loads(spec.inputs_json)[self.exclusive_by])),
        )

    def child_submitted_fact(self, spec: ChildSpec) -> dict[str, Any]:
        """The child's own ``submitted`` run update, naming its membership."""
        return {
            "definition_name": self.definition.name,
            "workflow_id": spec.workflow_id,
            "batch_id": self.batch_id,
            "item_key": spec.item_key,
        }


# === Workflow-id ownership: who may reuse an id, and how it is refused ===


def refuse_tier0_reuse(status: str | None, *, workflow_id: str, item_key: str | None = None) -> None:
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


def resolve_batch_reuse(
    batch_row: Sequence[Any],
    *,
    workflow_id: str,
    request: BatchAcceptance,
    children_settled: bool,
) -> dict[str, Any]:
    """Decide what a taken Batch workflow_id means: use-existing, or a raise.

    THE Batch half of the dedup contract, and the reason it is stated once:
    both mirrors reached the same three conclusions from the same two facts
    (the stored row, and whether its children are all settled), and a drift
    between them would let one API accept work the other refuses.

    Terminal wins first — completed history never changes identity. Then a
    fingerprint mismatch is a distinct typed conflict naming the aspect that
    differs. An identical fingerprint on unsettled children is use-existing:
    the stored row is returned and nothing is written.
    """
    existing = row_to_batch(batch_row)
    if children_settled:
        raise AlreadyTerminalError(workflow_id)
    if existing["fingerprint"] != request.fingerprint:
        raise WorkflowIdConflictError(
            workflow_id,
            batch_mismatch_aspect(
                existing,
                definition_name=request.definition.name,
                def_version=request.definition.version,
                def_struct_hash=request.definition.struct_hash,
                items_canonical=canonical_json(request.items_map),
                tolerance_json=request.tolerance_json,
                start_at=request.start_at,
                exclusive_by=request.exclusive_by,
            ),
        )
    return existing


def refuse_run_owned_id(submission_state: str, *, workflow_id: str) -> None:
    """Refuse a Batch workflow_id a plain Run submission already owns.

    Runs and Batches share one namespace, so this is a conflict rather than
    a dedup: a Run's fingerprint covers different fields than a Batch's, and
    there is no meaning to "the same submission" across the two shapes.
    """
    if submission_state == "finished":
        raise AlreadyTerminalError(workflow_id)
    raise WorkflowIdConflictError(workflow_id, "an existing Run owns this workflow_id")


def refuse_child_id_collision(spec: ChildSpec, *, collides: bool) -> None:
    """Refuse a derived child id that already names host-owned work."""
    if collides:
        raise WorkflowIdConflictError(
            spec.workflow_id,
            f"the child workflow id for item {spec.item_key!r} collides with existing work in this Run Home",
        )


# === Accounting policy: settlement, tolerance, closeout ===


def children_settled_rows(rows: Sequence[Sequence[Any]]) -> bool:
    """True when every ``(submission_state, run_status)`` pair is settled.

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


def trip_payload(items_json: str, tolerance_json: str, failure_count: int) -> dict[str, Any] | None:
    """The trip fact payload, or None when the Batch has not tripped.

    The percentage denominator is the pinned manifest length, read from the
    immutable manifest row — never a live count of remaining work.
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


def split_closeout(rows: Sequence[Sequence[Any]]) -> tuple[list[str], list[str]]:
    """Split the pending children a trip closes into (unstarted, abandoned).

    A trip closes admission on every pending child at once, but the two
    dispositions are not the same fact: an item with no runs row never began
    and is safe to rerun from scratch, while one with a runs row committed
    steps and may have landed side effects.
    """
    unstarted = [str(item_key) for item_key, started in rows if not started]
    abandoned = [str(item_key) for item_key, started in rows if started]
    return unstarted, abandoned


def closeout_kind(started: bool) -> str:
    """Which accounting fact a closed-admission child earns."""
    return ABANDONED_UPDATE_KIND if started else UNSTARTED_UPDATE_KIND


# === Occurrence-scoped lifecycle facts (child_paused / child_runnable) ===
#
# A paused child and its re-admission are the two nonterminal facts a
# detached operator surface needs: "this item is waiting on me" and "my
# answer put it back in the queue". Both commit in the SAME transaction as
# the state change that causes them — the pause commit and the answer
# settlement — so a reader can never see a paused child the Batch stream
# never mentioned, or an answered child that looks permanently parked.
#
# Neither is an ACCOUNTING fact: they never settle a manifest item, and a
# loop that pauses again earns a fresh pair. That is why they dedupe on the
# occurrence (``pause_id``) rather than once per item key.


def occurrence_fact(batch_id: str, item_key: str, run_id: str, home_uri: str, pause_id: str | None) -> dict[str, Any]:
    """The shared payload for both occurrence facts.

    Carries the logical item key AND the child's inert Run address (PRD 0017
    US38), so a consumer never parses ``<batch>:<item>`` child workflow-id
    syntax to act on the item.
    """
    return {
        "batch_id": batch_id,
        "item_key": item_key,
        "workflow_id": run_id,
        "run_ref": {"home": home_uri, "run_id": run_id},
        "pause_id": pause_id,
    }


def is_repeat_occurrence(row: Sequence[Any] | None, pause_id: str | None) -> bool:
    """True when the last fact of this kind already named this occurrence.

    A resumed run that replays its interrupt re-commits the identical
    ``pause_id`` (the slot insert is a no-op by ``ON CONFLICT``), and a
    settlement path may be reached twice for one answer. Comparing the LAST
    fact's occurrence suppresses both without suppressing the next turn of a
    loop, whose ``pause_id`` differs by superstep.
    """
    return row is not None and json.loads(row[0]).get("pause_id") == pause_id


# === Batch stop: one control fact, and the children it reaches ===


@dataclass(frozen=True)
class BatchStop:
    """One durable Batch stop, as one value.

    A Batch stop is two things committed together — the Batch's own
    ``stopped`` control fact, and a per-child stop command for every child
    that can still act on one — and both republish the same three fields.
    Carrying them once is what keeps the two from disagreeing about who
    asked or why.
    """

    batch_id: str
    info: Any
    source_ref: str | None

    @property
    def fact(self) -> dict[str, Any]:
        """The payload shared by the Batch fact and each child's command."""
        return {"verb": STOP_VERB, "info": self.info, "source_ref": self.source_ref}

    def child_command_row(self, child_workflow_id: str, now: str) -> tuple[Any, ...]:
        """One child's ``host_commands`` INSERT parameters."""
        return (child_workflow_id, STOP_VERB, json.dumps({"info": self.info}), self.source_ref, now)


def resolve_batch_stop(
    batch_row: Sequence[Any] | None,
    child_rows: Sequence[Sequence[Any]],
    *,
    batch_id: str,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Refuse an unstoppable Batch, or name the children the stop reaches.

    Two refusals and one selection, stated once for both mirrors: an unknown
    Batch is a ``HostError``, a Batch whose every child already settled is
    ``AlreadyTerminalError`` (there is nothing left to stop), and otherwise
    the stop reaches exactly the unsettled children — pending ones finish
    through the stop-before-start gate, executing ones receive it on the
    worker's next scan.
    """
    if batch_row is None:
        raise HostError(
            f"Cannot stop batch {batch_id!r}: this Run Home has no batch with that id.\n\n"
            "How to fix: stop the BatchRef that submit_batch() returned on THIS host "
            "(receipt.batch_ref), or check the workflow_id you passed. A BatchRef is inert — it "
            "carries no connection, so one made against another Run Home resolves to nothing here."
        )
    batch = row_to_batch(batch_row)
    unsettled = tuple(str(workflow_id) for workflow_id, state, status in child_rows if not is_child_settled(state, status))
    if not unsettled:
        raise AlreadyTerminalError(batch["workflow_id"])
    return batch, unsettled


#: The newest fact of one kind for one item — the occurrence dedupe read.
SELECT_LAST_OCCURRENCE = "SELECT payload FROM batch_updates WHERE batch_id = ? AND kind = ? AND item_key = ? ORDER BY bseq DESC LIMIT 1"
