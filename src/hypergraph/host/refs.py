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
