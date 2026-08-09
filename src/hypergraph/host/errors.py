"""Exceptions for the durable host (Tier 1 local host).

These are coordination errors, not execution errors: they describe the
host/worker lifecycle and submission identity, never a node's failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypergraph.exceptions import HostError

if TYPE_CHECKING:
    from collections.abc import Collection

    from hypergraph.host.definition import DefinitionId

__all__ = [
    "AlreadyTerminalError",
    "BuilderIdentityError",
    "ForkCompatibilityError",
    # Defined in ``hypergraph.exceptions`` so layers below the host (the
    # checkpointers' pause-settlement refusals) can subclass it without an
    # import cycle; this module stays its canonical import site.
    "HostError",
    "ItemKeyError",
    "NoServingWorkerError",
    "RerunError",
    "UnservedGraphError",
    "WorkerLockError",
    "WorkflowIdConflictError",
]


class WorkerLockError(HostError):
    """RETIRED — nothing raises this, and nothing will.

    A Run Home used to admit exactly one ``work_forever()`` worker,
    enforced by an OS-level lock taken at startup, and this is what the
    loser got. Several workers may now share one Home: each claim is a
    compare-and-set that takes a time-bounded lease, so two workers can
    never hold one submission, and a worker that stops renewing has its
    claims adopted rather than waited on.

    The name stays exported for one release so an ``except WorkerLockError``
    written against the old rule still imports and still compiles. Delete
    the handler — it can no longer fire.
    """

    def __init__(self, lock_path: str, message: str | None = None) -> None:
        self.lock_path = lock_path
        self.message = message or (
            f"Run Home worker lock {lock_path!r} is retired. "
            "Several workers may share one Run Home: each claim takes a lease, "
            "and a claim whose holder stops renewing is adopted by another worker."
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


class UnservedGraphError(HostError):
    """A Graph this host does not serve was passed to a submission verb.

    Submission is graph-first: ``host.submit(graph, ...)`` resolves the
    served Definition from the Graph object's own pinned identity (its name
    plus its ``structural_hash``). A Graph whose name this host never
    served, or whose topology no longer matches the served Definition's
    structural hash, names code no worker could execute — so it is refused
    at the call site rather than parked forever as version-incompatible
    work.
    """

    def __init__(self, graph_name: str, structural_hash: str, served: dict[str, str], message: str | None = None) -> None:
        self.graph_name = graph_name
        self.structural_hash = structural_hash
        self.served = dict(served)
        served_hash = self.served.get(graph_name)
        if served_hash is None:
            detail = (
                f"This host serves: {sorted(self.served)}.\n\n"
                "How to fix: pass a Graph named in serve(...), or add this one — "
                "serve(this_graph, ..., home=home) — so a worker can execute it."
            )
        else:
            detail = (
                f"The served Definition {graph_name!r} pins structural_hash {served_hash!r}, "
                f"but this Graph has {structural_hash!r}.\n\n"
                "How to fix: submit the Graph object this host served. A changed topology is a new "
                "Definition: re-serve it, and migrate parked work with host.fork(ref, into=new_graph, reason=...)."
            )
        self.message = message or f"Graph {graph_name!r} is not served by this host.\n\n{detail}"
        super().__init__(self.message)


class NoServingWorkerError(HostError):
    """A submission named work nothing alive could execute.

    ``UnservedGraphError`` asks the SUBMITTING process's registry; this asks
    the durable one. A submission carrying a ``builder`` address records a
    constructor a worker is expected to call, so the address has to resolve
    somewhere: in this Host's builder registry, or in a live worker's
    ``host_workers`` row. Naming one nothing registered is how a batch gets
    accepted, parked as version-incompatible, and sits queued forever with no
    executor and no error — so it is refused here, before the rows exist.
    """

    def __init__(
        self,
        builder_key: str,
        *,
        registered: Collection[str] = (),
        workers: Collection[str] = (),
        message: str | None = None,
    ) -> None:
        self.builder_key = builder_key
        self.registered = sorted(registered)
        self.workers = sorted(workers)
        known = f"This process registers: {self.registered}." if self.registered else "This process registers no builders."
        alive = f" Live workers on this Run Home: {self.workers}." if self.workers else " No worker has a fresh pulse on this Run Home."
        self.message = message or (
            f"No worker can rebuild this submission: nothing registers the builder {builder_key!r}.\n\n"
            f"{known}{alive}\n\n"
            f"How to fix: register it where the work will run — host.serve_builder({builder_key!r}, build_fn) — "
            "and start that worker before submitting, or submit without builder= so the pinned "
            "Definition identity alone selects the executor."
        )
        super().__init__(self.message)


class BuilderIdentityError(HostError):
    """A registered builder produced a Definition other than the pinned one.

    A submission pins the complete ``DefinitionId`` at accept time, and
    strict checkpoint resume depends on that pin (ADR 0007). A builder whose
    output drifted would resume a half-finished run against different
    topology, so the built Definition is verified against the pin and refused
    on mismatch — at submit time as this error, and at claim time as a
    ``builder_identity_mismatch`` dead letter. Changed topology is a fork,
    not a silent substitution.
    """

    def __init__(self, builder_key: str, pinned: DefinitionId, built: DefinitionId, message: str | None = None) -> None:
        self.builder_key = builder_key
        self.pinned = pinned
        self.built = built
        self.message = message or (
            f"Builder {builder_key!r} built {built.to_dict()!r}, but the submission pins {pinned.to_dict()!r}.\n\n"
            "How to fix: pass the builder arguments the submission was accepted with, or migrate the "
            "run explicitly — host.fork(ref, into=new_graph, reason=...). A pinned identity is never "
            "reinterpreted."
        )
        super().__init__(self.message)


class ItemKeyError(HostError):
    """``submit_batch(..., identity=...)`` could not derive stable item identity.

    A durable Batch item key must be a JSON-safe scalar that names one
    logical item for the life of the manifest, so restart and out-of-order
    completion never make an item anonymous. A missing, empty, non-scalar,
    or duplicated key is refused before acceptance — never silently
    replaced by a generated map index.
    """

    def __init__(self, identity: str, message: str) -> None:
        self.identity = identity
        self.message = message
        super().__init__(message)


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
