"""The one Definition both processes of the lease-adoption test serve.

Two processes must agree on a pinned ``DefinitionId`` while doing different
things inside the node, so the graph's SHAPE lives here and its BODY is
chosen by the caller. ``structural_hash`` ignores node source, so
``blocking_graph()`` and ``completing_graph()`` are one Definition: worker A
claims it and hangs, worker B adopts the very same identity and finishes it.

The ledger is how the parent proves which process did what after the child
is gone — a SIGKILLed worker returns no result and closes no file it did not
already flush.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from hypergraph import Graph, SyncRunner, node

#: The Definition every worker in these tests serves.
DEFINITION_NAME = "leasedef"

#: Env var naming an append-only file each execution writes one line to:
#: ``<pid>:<phase>``. Append-only and flushed per line, so a process killed
#: mid-run still leaves everything it had already claimed to have done.
LEDGER_ENV = "HYPERGRAPH_LEASE_LEDGER"


def _append(line: str) -> None:
    path = os.environ.get(LEDGER_ENV)
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")
            handle.flush()


def blocking_graph() -> Graph:
    """Worker A's body: announce the claim, then never finish.

    The wait is unbounded on purpose. A timeout would be a second way for
    this run to end, and the whole test rests on the claim still being
    outstanding at the instant the process is killed.
    """

    @node(output_name="out")
    def work(x: int) -> int:
        _append(f"{os.getpid()}:started")
        threading.Event().wait()
        return x  # pragma: no cover - the process is killed first

    return Graph([work], name=DEFINITION_NAME).with_runner(SyncRunner())


def completing_graph(gate: threading.Event) -> Graph:
    """Worker B's body: announce the adoption, wait for the parent, finish.

    The gate is what lets the parent act while B genuinely HOLDS the claim,
    which is the only moment at which a stale release could do damage: the
    row reads ``claimed``, the state name a naive release would match, and
    only ``claim_seq`` says the release belongs to a claim that is over.
    """

    @node(output_name="out")
    def work(x: int) -> int:
        _append(f"{os.getpid()}:adopted")
        gate.wait(timeout=30)
        _append(f"{os.getpid()}:completed")
        return x

    return Graph([work], name=DEFINITION_NAME).with_runner(SyncRunner())


def read_ledger(path: str | Path) -> list[str]:
    """Every recorded phase, in the order it was written."""
    file = Path(path)
    return file.read_text(encoding="utf-8").splitlines() if file.exists() else []
