"""Public value types for materialization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from hypergraph.runners import PauseInfo


class RowStatus(Enum):
    """Current derivation state of one row."""

    COMPLETE = "complete"
    WAITING = "waiting"
    ERROR = "error"


class WriteOutcome(Enum):
    """Physical effect of a row write."""

    INSERTED = "inserted"
    UPDATED = "updated"
    SKIPPED = "skipped"


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
    def waiting(self) -> tuple[RowReceipt, ...]:
        return tuple(receipt for receipt in self.receipts if receipt.paused)

    @property
    def errors(self) -> tuple[RowReceipt, ...]:
        return tuple(receipt for receipt in self.receipts if receipt.failed)

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
