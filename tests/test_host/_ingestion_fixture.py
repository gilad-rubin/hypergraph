"""The Panda-shaped ingestion fixture for the durable-Batch interrupt suites.

One document-level lifecycle Graph with a **conditional** duplicate review:
clean items bypass the interrupt entirely, possible duplicates park on a
human, and the typed answer routes to create / replace / archive. It uses
small deterministic fake domain effects recorded in a file-backed ledger —
never Panda private methods — so the same fixture works in-process and in a
killed-and-reopened child process.

Two shape rules the graph obeys deliberately, because they are the contract
issue #342 pins:

- **The head consumes the start input; everything downstream threads node
  outputs.** ``work_item_id`` is read once by ``stage_candidate``, which
  emits ``validated_id``. A durable resume replays only
  ``{response_key: answer}`` (never the pinned start inputs), and a
  checkpoint stores node OUTPUTS only — a post-interrupt node reading a raw
  graph-boundary input could not be satisfied. ``test_resume_contract.py``
  pins that limitation directly.
- **The domain decision lives in routes, not in the Host.** Host stop is
  execution control; create / replace / archive is a graph route over the
  answer value.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from hypergraph import AsyncRunner, Graph, SyncRunner, interrupt, node, route

#: Env var naming the deterministic effect ledger, so a child process the
#: kill matrix spawns appends to the same file this process reads.
LEDGER_ENV = "HYPERGRAPH_342_LEDGER"


@dataclass(frozen=True)
class DuplicateDecision:
    """Panda's domain answer, as its JSON projection travels durably."""

    decision: str
    target_doc_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"decision": self.decision, "target_doc_id": self.target_doc_id}


@dataclass(frozen=True)
class DuplicateReviewQuestion:
    """The structural ask: handler returns the question, class types the port."""

    answer_type: ClassVar[type] = DuplicateDecision
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple = ()


def record_effect(entry: str) -> None:
    """Append one deterministic domain effect to the ledger.

    Append-only and line-oriented so a SIGKILL mid-write cannot corrupt
    earlier lines: the kill matrix asserts on exact line counts to prove no
    terminal effect ran twice.
    """
    path = os.environ.get(LEDGER_ENV)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(entry + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_ledger(path: str | Path) -> list[str]:
    """Every effect recorded so far, in order."""
    ledger = Path(path)
    if not ledger.exists():
        return []
    return [line for line in ledger.read_text(encoding="utf-8").splitlines() if line]


def decision_of(duplicate_decision: Any) -> DuplicateDecision:
    """Read the answer whether it arrived live or as its JSON projection.

    A settled answer is durable resume input, so the value that comes back
    from the store is the dict form. In-process (never-persisted) runs still
    see the dataclass, and the graph must read both identically.
    """
    if isinstance(duplicate_decision, DuplicateDecision):
        return duplicate_decision
    return DuplicateDecision(
        decision=duplicate_decision["decision"],
        target_doc_id=duplicate_decision.get("target_doc_id"),
    )


#: Item id substrings that steer the deterministic fake domain.
DUPLICATE_MARKER = "dup"
FAILING_MARKER = "boom"


def ingestion_graph(name: str = "ingest", *, sync: bool = False) -> Graph:
    """The document ingestion lifecycle: stage, detect, review, apply."""

    @node(output_name="validated_id")
    def stage_candidate(work_item_id: str) -> str:
        if FAILING_MARKER in work_item_id:
            raise ValueError(f"staging refused {work_item_id}")
        return work_item_id

    @node(output_name="clashes")
    def detect_clashes(validated_id: str) -> list[str]:
        return [f"doc-{abs(hash(validated_id)) % 9000 + 1000}"] if DUPLICATE_MARKER in validated_id else []

    @route(targets=["wait_for_duplicate_review", "default_duplicate_decision"])
    def route_clash_review(clashes: list[str]) -> str:
        return "wait_for_duplicate_review" if clashes else "default_duplicate_decision"

    @node(output_name="duplicate_decision")
    def default_duplicate_decision(clashes: list[str]) -> DuplicateDecision:
        return DuplicateDecision(decision="create_new")

    @interrupt(answer_name="duplicate_decision")
    def wait_for_duplicate_review(clashes: list[str]) -> DuplicateReviewQuestion:
        # No options tuple: the answer is a structured DuplicateDecision
        # (decision + target_doc_id), not a bare option string.
        return DuplicateReviewQuestion(
            prompt="Possible duplicate of an existing document — decide how to proceed.",
            evidence=(tuple(clashes),),
        )

    @route(targets=["keep_candidate", "retarget_replacement", "archive_existing"])
    def route_duplicate_decision(duplicate_decision: Any) -> str:
        targets = {
            "create_new": "keep_candidate",
            "replace_existing": "retarget_replacement",
            "archive_duplicate": "archive_existing",
        }
        decision = decision_of(duplicate_decision).decision
        target = targets.get(decision)
        if target is None:
            raise ValueError(f"Unknown duplicate decision: {decision!r}")
        return target

    @node(output_name="outcome")
    def keep_candidate(validated_id: str) -> str:
        record_effect(f"created:{validated_id}")
        return f"created:{validated_id}"

    @node(output_name="outcome")
    def retarget_replacement(validated_id: str, duplicate_decision: Any) -> str:
        target = decision_of(duplicate_decision).target_doc_id
        record_effect(f"replaced:{validated_id}:{target}")
        return f"replaced:{validated_id}:{target}"

    @node(output_name="outcome")
    def archive_existing(validated_id: str, duplicate_decision: Any) -> str:
        target = decision_of(duplicate_decision).target_doc_id
        record_effect(f"archived:{validated_id}:{target}")
        return f"archived:{validated_id}:{target}"

    runner = SyncRunner() if sync else AsyncRunner()
    return Graph(
        [
            stage_candidate,
            detect_clashes,
            route_clash_review,
            default_duplicate_decision,
            wait_for_duplicate_review,
            route_duplicate_decision,
            keep_candidate,
            retarget_replacement,
            archive_existing,
        ],
        edges=[
            (stage_candidate, detect_clashes),
            (detect_clashes, route_clash_review),
            (wait_for_duplicate_review, route_duplicate_decision),
            (default_duplicate_decision, route_duplicate_decision),
        ],
        name=name,
    ).with_runner(runner)


def answer_value(decision: str, target_doc_id: int | None = None) -> dict[str, Any]:
    """The JSON form of a DuplicateDecision, as an operator surface sends it."""
    return DuplicateDecision(decision=decision, target_doc_id=target_doc_id).to_dict()


def looping_graph(name: str = "loop-ingest") -> Graph:
    """Two interrupts in sequence: answering the first parks on a second.

    Proves a later occurrence is a NEW PauseSlot with its own ``pause_id``,
    and that a settled earlier occurrence can never make it runnable.
    """

    @node(output_name="validated_id")
    def stage_candidate(work_item_id: str) -> str:
        return work_item_id

    @interrupt(answer_name="first_answer")
    def first_review(validated_id: str) -> DuplicateReviewQuestion:
        return DuplicateReviewQuestion(prompt="first pass")

    @node(output_name="midpoint")
    def midpoint(first_answer: Any) -> str:
        return f"mid:{decision_of(first_answer).decision}"

    @interrupt(answer_name="second_answer")
    def second_review(midpoint: str) -> DuplicateReviewQuestion:
        return DuplicateReviewQuestion(prompt="second pass")

    @node(output_name="outcome")
    def finish(midpoint: str, second_answer: Any) -> str:
        result = f"{midpoint}|{decision_of(second_answer).decision}"
        record_effect(result)
        return result

    return Graph(
        [stage_candidate, first_review, midpoint, second_review, finish],
        name=name,
    ).with_runner(AsyncRunner())


def dump_json(value: Any) -> str:
    """Stable JSON for cross-process assertions."""
    return json.dumps(value, sort_keys=True)
