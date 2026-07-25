"""Exceptions for the durable host (Tier 1 local host).

These are coordination errors, not execution errors: they describe the
host/worker lifecycle and submission identity, never a node's failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypergraph.exceptions import HostError

if TYPE_CHECKING:
    from hypergraph.host.definition import DefinitionId

__all__ = [
    "AlreadyTerminalError",
    "ForkCompatibilityError",
    # Defined in ``hypergraph.exceptions`` so layers below the host (the
    # checkpointers' pause-settlement refusals) can subclass it without an
    # import cycle; this module stays its canonical import site.
    "HostError",
    "RerunError",
    "WorkerLockError",
    "WorkflowIdConflictError",
]


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

    Completed history never changes identity: reusing a finished
    workflow_id raises this typed error regardless of whether the new
    submission's fingerprint matches. Terminal reuse wins over fingerprint
    mismatch; repetition uses ``client.rerun()``, migration ``host.fork()``.
    """

    def __init__(self, workflow_id: str, message: str | None = None) -> None:
        self.workflow_id = workflow_id
        self.message = message or (
            f"workflow_id {workflow_id!r} is already terminal in this Run Home. Choose a new workflow_id; completed history is never reused."
        )
        super().__init__(self.message)


class WorkflowIdConflictError(HostError):
    """A workflow_id was resubmitted with a different start fingerprint.

    Same id, different meaning: the pinned Definition identity, the
    normalized inputs, or the requested ``start_at`` differs from the stored
    submission. This is distinct from ``AlreadyTerminalError`` (the existing
    run is still nonterminal) and from a fingerprint-identical resubmission
    (which dedupes with ``duplicate=True``).
    """

    def __init__(self, workflow_id: str, aspect: str | None = None, message: str | None = None) -> None:
        self.workflow_id = workflow_id
        self.aspect = aspect
        detail = f" ({aspect} differs)" if aspect else ""
        self.message = message or (
            f"workflow_id {workflow_id!r} already exists in this Run Home with a different start fingerprint{detail}. "
            "Choose a new workflow_id; use client.rerun() to repeat a settled run or host.fork() to migrate one."
        )
        super().__init__(self.message)


class ForkCompatibilityError(HostError):
    """``host.fork()`` targeted a Definition structurally incompatible with the source.

    Fork seeds the new run from recorded history, so the target Definition's
    ``structural_hash`` must equal the source submission's pinned
    ``def_struct_hash`` — otherwise the recorded checkpoints are not
    restorable under the new code.
    """

    def __init__(self, source: DefinitionId, target: DefinitionId, message: str | None = None) -> None:
        self.source = source
        self.target = target
        self.message = message or (
            f"Cannot fork into {target!r}: its structural hash differs from the source run's pinned identity {source!r}. "
            "Fork requires restorable checkpoints (equal structural_hash); changed topology needs a new submit, not a fork."
        )
        super().__init__(self.message)


class RerunError(HostError):
    """``client.rerun()`` named a source that is missing or not terminal.

    Rerun repeats settled work: the source run must exist in the Run Home
    and its runs row must be terminal. A still-running or never-executed
    source is a conflict, not a rerun target.
    """

    def __init__(self, workflow_id: str, message: str | None = None) -> None:
        self.workflow_id = workflow_id
        self.message = message or f"Cannot rerun {workflow_id!r}."
        super().__init__(self.message)
