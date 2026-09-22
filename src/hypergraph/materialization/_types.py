"""Public value types for materialization."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict

from hypergraph.runners import PauseInfo


class RowStatus(Enum):
    """Current derivation state of one row."""

    COMPLETE = "complete"
    WAITING = "waiting"
    ERROR = "error"
    PARTIAL = "partial"
    """Some derived columns are stored, others are null with a recorded reason.

    Written only under ``on_error="store"`` and only when the runner could
    attribute the failure to the node that produces a column. The row stays
    queryable, counts as needing heal in ``status()``, and the next ``sync()``
    re-derives exactly the null columns.
    """

    @classmethod
    def of_stored(cls, row: Mapping[str, Any] | None) -> RowStatus:
        """Decode the ``_status`` cell of one stored row.

        A row with no ``_status`` cell — or a null one — predates the column and
        was only ever written on the complete path, so it decodes as
        ``COMPLETE``; that legacy state is the reason the raw comparisons this
        replaces all had to spell ``in (None, "complete")``. A missing row is
        likewise nothing to tell apart from a complete one at the call sites
        that ask (they check ``existing is None`` separately when it matters).
        Any other value is a store that did not preserve the column, and says
        so loudly rather than silently reading as "not an error".
        """
        value = None if row is None else row.get("_status")
        if value is None:
            return cls.COMPLETE
        try:
            return cls(value)
        except ValueError:
            raise ValueError(
                "Stored row carries an unknown _status value.\n\n"
                f"Value: {value!r}; known values: {', '.join(status.value for status in cls)}\n\n"
                "How to fix: preserve the HyperTable-managed _status value unchanged in the TableStore."
            ) from None

    @property
    def stored_value(self) -> str:
        """The value this status is written as in the ``_status`` column."""
        return self.value


class ChangeReason(Enum):
    """Why one column of a partially derived row holds no value.

    A column whose producer ran and returned ``None`` is not here: that is a
    value, not a failure, and it is stored with its provenance like any other.
    """

    NODE_ERROR = "node_error"
    """The node that produces this column raised — or, for an entry naming a
    ``map_over`` input, the fan-out boundary that produces the child items did."""
    UPSTREAM_ERROR = "upstream_error"
    """A failed node reaches this column's producer, so its inputs never arrived."""
    NOT_RUN = "not_run"
    """Nothing failed on this column's own path — the failure ended the run
    before its producer was scheduled. Retrying may well derive it."""


@dataclass(frozen=True)
class ColumnChange:
    """One derived column nulled by a failure, and what is known about it.

    ``node`` always names the column's own producer. ``error`` carries the
    raised exception's text for ``NODE_ERROR`` and is ``None`` for the two
    reasons where this producer never ran.

    One entry may instead name a ``map_over`` input: its fan-out boundary
    raised, so the child table could not be rebuilt. That name is not a key of
    the parent row — its "value" is the child table's rows — and ``node`` names
    the boundary. It is always ``NODE_ERROR``.
    """

    column: str
    reason: ChangeReason
    node: str
    error: str | None = None


class WriteOutcome(Enum):
    """Physical effect of a row write."""

    INSERTED = "inserted"
    UPDATED = "updated"
    SKIPPED = "skipped"
    HEALED = "healed"
    """An unchanged parent whose damaged child rows were rebuilt — rows
    physically missing, stored in error under ``on_error="store"``, or extra
    rows left behind by an interrupted write — with everything the repair
    derived landing healthy.

    ``sync()`` and ``insert()`` both report the repair distinctly: a receipt is
    never ``SKIPPED`` on a path that derived something for the row, and a
    repair whose retry failed again reports ``UPDATED`` rather than claiming a
    heal (#204, #314).
    """


@dataclass(frozen=True)
class RowReceipt:
    """What one write did to one row."""

    id: str
    outcome: WriteOutcome
    status: RowStatus
    pause: PauseInfo | None = None
    error: str | None = None

    @property
    def paused(self) -> bool:
        return self.status is RowStatus.WAITING

    @property
    def completed(self) -> bool:
        return self.status is RowStatus.COMPLETE

    @property
    def failed(self) -> bool:
        return self.status is RowStatus.ERROR


class MaterializationQuestion(BaseModel):
    """JSON-safe structural question carried by a materialization receipt."""

    model_config = ConfigDict(frozen=True)

    prompt: str
    options: tuple[Any, ...] | None
    evidence: tuple[Any, ...]
    answer_type: str


class MaterializationPause(BaseModel):
    """JSON-safe pause information carried by a materialization receipt."""

    model_config = ConfigDict(frozen=True)

    node_name: str
    value: MaterializationQuestion
    response_key: str


class MaterializationReceipt(BaseModel):
    """Checkpoint-safe receipt emitted by a HyperTable materialization node."""

    model_config = ConfigDict(frozen=True)

    id: str
    outcome: WriteOutcome
    status: RowStatus
    pause: MaterializationPause | None = None
    error: str | None = None

    @classmethod
    def from_row_receipt(cls, receipt: RowReceipt) -> MaterializationReceipt:
        pause = None
        if receipt.pause is not None:
            question = receipt.pause.value
            pause = MaterializationPause(
                node_name=receipt.pause.node_name,
                response_key=receipt.pause.response_key,
                value=MaterializationQuestion(
                    prompt=str(question.prompt),
                    options=None if question.options is None else tuple(question.options),
                    evidence=tuple(question.evidence),
                    answer_type=_stable_answer_type(question.answer_type),
                ),
            )
        return cls(
            id=receipt.id,
            outcome=receipt.outcome,
            status=receipt.status,
            pause=pause,
            error=receipt.error,
        )

    @property
    def paused(self) -> bool:
        return self.status is RowStatus.WAITING

    @property
    def completed(self) -> bool:
        return self.status is RowStatus.COMPLETE

    @property
    def failed(self) -> bool:
        return self.status is RowStatus.ERROR


@dataclass(frozen=True)
class TableReceipt:
    """Aggregate receipt for a batch insert, sync, or re-derive."""

    receipts: tuple[RowReceipt, ...]
    deleted: int = 0

    @property
    def inserted(self) -> int:
        return sum(receipt.outcome is WriteOutcome.INSERTED for receipt in self.receipts)

    @property
    def updated(self) -> int:
        return sum(receipt.outcome is WriteOutcome.UPDATED for receipt in self.receipts)

    @property
    def skipped(self) -> int:
        return sum(receipt.outcome is WriteOutcome.SKIPPED for receipt in self.receipts)

    @property
    def healed(self) -> int:
        return sum(receipt.outcome is WriteOutcome.HEALED for receipt in self.receipts)

    @property
    def waiting(self) -> tuple[RowReceipt, ...]:
        return tuple(receipt for receipt in self.receipts if receipt.paused)

    @property
    def errors(self) -> tuple[RowReceipt, ...]:
        return tuple(receipt for receipt in self.receipts if receipt.failed)

    @property
    def partial(self) -> tuple[RowReceipt, ...]:
        return tuple(receipt for receipt in self.receipts if receipt.status is RowStatus.PARTIAL)

    @property
    def paused(self) -> bool:
        return bool(self.waiting)

    @property
    def completed(self) -> bool:
        return all(receipt.completed for receipt in self.receipts)

    @property
    def failed(self) -> bool:
        return bool(self.errors)


@dataclass(frozen=True)
class WaitingRow:
    """A row whose derivation is waiting for one answer."""

    id: str
    pause: PauseInfo
    row: dict[str, Any]
    provenance: str


@dataclass(frozen=True)
class ErroredRow:
    """A row whose derivation raised under ``on_error='store'``."""

    id: str
    error: str
    row: dict[str, Any]


@dataclass(frozen=True)
class PartialRow:
    """A row that kept its derived columns except the ones a failure nulled."""

    id: str
    changes: tuple[ColumnChange, ...]
    row: dict[str, Any]


@dataclass(frozen=True)
class _StoredQuestion:
    """Frozen structural ask view rebuilt from a persisted question envelope."""

    prompt: str
    options: tuple[Any, ...] | None
    evidence: tuple[Any, ...]
    answer_type: str


def _stable_answer_type(value: Any) -> str:
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    return repr(value)


def serialize_question(pause: PauseInfo, provenance: str) -> str:
    """Serialize the structural ask seam without importing an ask package."""
    ask = pause.value
    missing = [name for name in ("prompt", "options", "evidence", "answer_type") if not hasattr(ask, name)]
    if missing:
        raise TypeError(
            "Interrupt question does not satisfy the persisted structural contract.\n\n"
            f"Missing attribute(s): {', '.join(missing)}\n\n"
            "How to fix: return a frozen question value exposing prompt, options, "
            "evidence, and answer_type."
        )
    evidence = tuple(ask.evidence)
    for index, item in enumerate(evidence):
        try:
            json.dumps(item)
        except (TypeError, ValueError) as error:
            raise TypeError(
                f"Interrupt question evidence item {index} is not JSON-serializable.\n\n"
                f"Item: {item!r}\n\n"
                "How to fix: replace it with a JSON scalar, list, mapping, or other serializable value."
            ) from error
    envelope = {
        "node_name": pause.node_name,
        "response_key": pause.response_key,
        "prompt": str(ask.prompt),
        "options": None if ask.options is None else tuple(ask.options),
        "evidence": evidence,
        "answer_type": _stable_answer_type(ask.answer_type),
        "provenance": provenance,
    }
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"))


def deserialize_question(value: Any) -> tuple[PauseInfo, str]:
    """Rebuild ``PauseInfo`` and its opaque provenance from storage."""
    if not isinstance(value, str):
        raise TypeError(
            "Stored question envelope must be a JSON string.\n\n"
            f"Received: {type(value).__name__}\n\n"
            "How to fix: preserve the HyperTable-managed _question value unchanged in the TableStore."
        )
    envelope = json.loads(value)
    ask = _StoredQuestion(
        prompt=envelope["prompt"],
        options=None if envelope["options"] is None else tuple(envelope["options"]),
        evidence=tuple(envelope["evidence"]),
        answer_type=envelope["answer_type"],
    )
    return (
        PauseInfo(
            node_name=envelope["node_name"],
            value=ask,
            response_key=envelope["response_key"],
        ),
        envelope["provenance"],
    )


def serialize_changes(changes: tuple[ColumnChange, ...]) -> str:
    """Serialize a partial row's change entries for storage."""
    return json.dumps(
        [{"column": change.column, "reason": change.reason.value, "node": change.node, "error": change.error} for change in changes],
        separators=(",", ":"),
    )


def deserialize_changes(value: Any) -> tuple[ColumnChange, ...]:
    """Rebuild a partial row's change entries from storage, or ``()``."""
    if not isinstance(value, str) or not value:
        return ()
    return tuple(
        ColumnChange(
            column=entry["column"],
            reason=ChangeReason(entry["reason"]),
            node=entry["node"],
            error=entry["error"],
        )
        for entry in json.loads(value)
    )


@dataclass(frozen=True)
class RecipeDrift:
    """Per-table recipe-drift report, returned by ``HyperTable.recipe_drift()``."""

    table: str
    total: int
    current: int
    drifted: int
    unknown: int
    children: tuple[RecipeDrift, ...] = ()

    @property
    def stale_total(self) -> int:
        return self.drifted + self.unknown + sum(child.stale_total for child in self.children)


@dataclass(frozen=True)
class TableStatus:
    """Dry-run staleness report for one table, returned by ``status()``."""

    table: str
    total: int
    fresh: int
    stale: int
    errored: int
    stale_ids: tuple[str, ...] = ()
    errored_ids: tuple[str, ...] = ()
    stale_columns: tuple[tuple[str, int], ...] = ()
    children: tuple[TableStatus, ...] = ()

    @property
    def is_fresh(self) -> bool:
        return self.stale == 0 and self.errored == 0 and all(child.is_fresh for child in self.children)
