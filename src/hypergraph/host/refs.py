"""Inert reference value objects for the durable host.

Refs carry identity only — no liveness, status, result, or control methods.
They are JSON-serializable so a product table can store them, and they are
never durable handles (ADR 0004): live control stays process-local.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RunRef:
    """Inert, serializable address of one Run in one Run Home.

    Attributes:
        home: The Run Home URI string (e.g. ``"file:./panda-runs.db"``).
        run_id: The run's workflow id within that Home.
    """

    home: str
    run_id: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict for storage."""
        return {"home": self.home, "run_id": self.run_id}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRef:
        """Rebuild a RunRef from ``to_dict()`` output."""
        if not isinstance(data, dict):
            raise TypeError(f"RunRef.from_dict() expects a dict, got {type(data).__name__}.")
        try:
            home = data["home"]
            run_id = data["run_id"]
        except KeyError as missing:
            raise ValueError(f"RunRef.from_dict() missing key {missing}; expected keys 'home' and 'run_id'.") from None
        if not isinstance(home, str) or not isinstance(run_id, str):
            raise TypeError("RunRef 'home' and 'run_id' must be strings.")
        return cls(home=home, run_id=run_id)


@dataclass(frozen=True)
class BatchRef:
    """Inert, serializable address of one Batch in one Run Home.

    Attributes:
        home: The Run Home URI string (e.g. ``"file:./panda-runs.db"``).
        batch_id: The Batch's id within that Home (distinct from its
            caller-chosen ``workflow_id``).
    """

    home: str
    batch_id: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict for storage."""
        return {"home": self.home, "batch_id": self.batch_id}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BatchRef:
        """Rebuild a BatchRef from ``to_dict()`` output."""
        if not isinstance(data, dict):
            raise TypeError(f"BatchRef.from_dict() expects a dict, got {type(data).__name__}.")
        try:
            home = data["home"]
            batch_id = data["batch_id"]
        except KeyError as missing:
            raise ValueError(f"BatchRef.from_dict() missing key {missing}; expected keys 'home' and 'batch_id'.") from None
        if not isinstance(home, str) or not isinstance(batch_id, str):
            raise TypeError("BatchRef 'home' and 'batch_id' must be strings.")
        return cls(home=home, batch_id=batch_id)


@dataclass(frozen=True)
class BatchSubmitReceipt:
    """Acknowledgement of one accepted (or deduplicated) Batch submission.

    Attributes:
        batch_ref: Inert address of the Batch — safe to store in a product
            table.
        workflow_id: The Batch's caller-chosen workflow id.
        duplicate: True when this submission matched an existing nonterminal
            Batch workflow_id (use-existing; no new row was written).
    """

    batch_ref: BatchRef
    workflow_id: str
    duplicate: bool


@dataclass(frozen=True)
class BatchCommandReceipt:
    """Acknowledgement of one accepted (or deduplicated) durable Batch command.

    Attributes:
        batch_ref: Inert address of the Batch the command targets.
        verb: The command verb — ``"stop"`` today; a Batch has no
            scheduled-answer form (its children are answered individually).
        duplicate: True when the Batch was already stopped — the first stop
            owns its payload and nothing new was written.
    """

    batch_ref: BatchRef
    verb: str = "stop"
    duplicate: bool = False


@dataclass(frozen=True)
class SubmitReceipt:
    """Acknowledgement of one accepted (or deduplicated) submission.

    Attributes:
        run_ref: Inert address of the run — safe to store in a product table.
        workflow_id: The run's workflow id.
        duplicate: True when this submission matched an existing nonterminal
            workflow_id (use-existing; no new row was written).
    """

    run_ref: RunRef
    workflow_id: str
    duplicate: bool


@dataclass(frozen=True)
class CommandReceipt:
    """Acknowledgement of one accepted (or deduplicated) durable command.

    Attributes:
        run_ref: Inert address of the run the command targets.
        verb: The command verb — ``"stop"`` or ``"schedule_answer"``. The
            closed vocabulary is the host's own; callers never name a verb.
        duplicate: True when an unapplied command with the same verb already
            existed for the run — for ``schedule_answer``, for the same pause
            occurrence. The first accepted command owns its payload and
            nothing new was written.
    """

    run_ref: RunRef
    verb: str = "stop"
    duplicate: bool = False
