"""Exceptions for the durable host (Tier 1 local host).

These are coordination errors, not execution errors: they describe the
host/worker lifecycle and submission identity, never a node's failure.
"""

from __future__ import annotations


class HostError(Exception):
    """Base class for durable-host errors."""


class WorkerLockError(HostError):
    """A second worker tried to claim a Run Home that already has one.

    One exclusive worker per Run Home is enforced by an OS-level lock at
    ``work_forever()`` startup; the loser fails loudly and immediately.
    """

    def __init__(self, lock_path: str, message: str | None = None) -> None:
        self.lock_path = lock_path
        self.message = message or (
            f"Run Home already has an active worker (lock: {lock_path!r}). "
            "Only one work_forever() worker may own a Run Home at a time. "
            "Stop the existing worker first; it releases the lock on exit."
        )
        super().__init__(self.message)


class AlreadyTerminalError(HostError):
    """A workflow_id whose history is terminal was submitted again.

    Completed history never changes identity. Full fingerprint-based
    dedup/conflict semantics arrive with a later host ticket; for now any
    reuse of a finished workflow_id raises this typed error.
    """

    def __init__(self, workflow_id: str, message: str | None = None) -> None:
        self.workflow_id = workflow_id
        self.message = message or (
            f"workflow_id {workflow_id!r} is already terminal in this Run Home. Choose a new workflow_id; completed history is never reused."
        )
        super().__init__(self.message)
