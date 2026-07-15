"""A GraphNode (wrapped subgraph) as a HyperTable column producer.

The graph layer has always allowed a subgraph-as-node; the materialization
layer assumed every column producer is a plain function (``inspect.signature``
on its ``func``). The scenario that forces the generalization: a derived
column produced by a VALIDATION CYCLE — propose → check → @ifelse cycling back
on a bad answer — wrapped as one GraphNode, so the cycle is ATOMIC under
derive-on-insert AND under selective sync recompute.

Asserts, in order of hazard:

1. insert derives the column through the cycle (a first bad proposal re-asks);
2. per-column provenance is stamped and CODE-SENSITIVE via the node's own
   ``definition_hash``;
3. a bound-value change re-derives the column via sync, and the recompute runs
   the WHOLE cycle — a validation failure inside sync re-asks instead of
   silently storing the bad/empty answer (the specific silent-store hazard).
"""

from __future__ import annotations

import pytest

from hypergraph import END, Graph, ifelse, node
from hypergraph.materialization import HyperTable
from hypergraph.materialization._lancedb_store import LanceDBStore
from hypergraph.runners import SyncRunner

CALLS = {"propose": 0, "check": 0}


class FlakyProposer:
    """Returns a REJECTED answer on each first ask, a good one on the re-ask."""

    def __init__(self, tag: str = "v1"):
        self.tag = tag
        self.asks = 0

    def _config(self):
        return {"tag": self.tag}

    def propose(self, text: str, hint: str) -> str:
        self.asks += 1
        if self.asks % 2 == 1:
            return ""  # rejected by check → the cycle must re-ask
        return f"{self.tag}:{hint}:{text}"


@node(output_name="attempt_reply")
def propose_value(text: str, hint: str, proposer: FlakyProposer, retry_feedback: dict | None = None) -> dict:
    CALLS["propose"] += 1
    attempt = int((retry_feedback or {}).get("attempt") or 0) + 1
    return {"value": proposer.propose(text, hint), "attempt": attempt}


@node(output_name=("checked", "retry_feedback"))
def check_value(attempt_reply: dict) -> tuple[str, dict | None]:
    CALLS["check"] += 1
    value = str(attempt_reply["value"])
    if not value:
        if int(attempt_reply["attempt"]) >= 2:
            raise ValueError("proposal rejected twice")
        # The hazard under selective recompute: this empty answer must NEVER be
        # what lands in the column — the cycle has to re-ask.
        return "", {"error": "empty proposal", "attempt": int(attempt_reply["attempt"])}
    return value, None


@ifelse(when_true=END, when_false="propose_value")
def value_ok(retry_feedback: dict | None = None) -> bool:
    return retry_feedback is None


def _cycle_node():
    return (
        Graph(
            [propose_value, check_value, value_ok],
            edges=[(propose_value, check_value), (check_value, value_ok)],
            name="propose_cycle",
            entrypoint="propose_value",
        )
        .select("checked")
        .as_node(name="derive_checked")
    )


def _table(tmp_path, proposer: FlakyProposer, hint: str = "h1") -> HyperTable:
    return (
        Graph([_cycle_node()])
        .bind(proposer=proposer, hint=hint)
        .as_table(identity="row_id", store=LanceDBStore(str(tmp_path / "graphnode_col_store")), on_error="store", runner=SyncRunner())
    )


@pytest.fixture(autouse=True)
def reset_calls():
    for key in CALLS:
        CALLS[key] = 0


def test_insert_derives_through_the_cycle_and_stamps_provenance(tmp_path):
    table = _table(tmp_path, FlakyProposer())
    table.insert(row_id="r1", text="alpha")

    row = table.get("r1")
    # The first proposal was rejected; the CYCLE re-asked and the good answer landed.
    assert row["checked"] == "v1:h1:alpha"
    assert CALLS["propose"] == 2 and CALLS["check"] == 2

    raw = table._store.read_rows(table.table_name)[0]
    assert raw["_provenance_checked"], "GraphNode column must stamp per-column provenance"
    assert raw["_status"] != "error"


def test_provenance_is_code_sensitive_via_the_node_definition_hash(tmp_path):
    table = _table(tmp_path, FlakyProposer())
    table.insert(row_id="r1", text="alpha")
    before = table._store.read_rows(table.table_name)[0]["_provenance_checked"]

    # A DIFFERENT inner graph (an extra no-op node) is different code → the
    # producer's definition hash, and with it the provenance basis, must move.
    @node(output_name="attempt_reply")
    def propose_value_v2(text: str, hint: str, proposer: FlakyProposer, retry_feedback: dict | None = None) -> dict:
        attempt = int((retry_feedback or {}).get("attempt") or 0) + 1  # same behavior, new code
        return {"value": proposer.propose(text, hint), "attempt": attempt}

    @ifelse(when_true=END, when_false="propose_value_v2")
    def value_ok_v2(retry_feedback: dict | None = None) -> bool:
        return retry_feedback is None

    cycle_v2 = (
        Graph(
            [propose_value_v2, check_value, value_ok_v2],
            edges=[(propose_value_v2, check_value), (check_value, value_ok_v2)],
            name="propose_cycle",
            entrypoint="propose_value_v2",
        )
        .select("checked")
        .as_node(name="derive_checked")
    )
    table_v2 = (
        Graph([cycle_v2])
        .bind(proposer=FlakyProposer(), hint="h1")
        .as_table(identity="row_id", store=LanceDBStore(str(tmp_path / "graphnode_col_store")), on_error="store", runner=SyncRunner())
    )
    table_v2.sync([{"row_id": "r1", "text": "alpha"}])
    after = table_v2._store.read_rows(table_v2.table_name)[0]["_provenance_checked"]
    assert after != before


def test_sync_recompute_runs_the_cycle_atomically_never_storing_the_rejected_answer(tmp_path):
    proposer = FlakyProposer()
    table = _table(tmp_path, proposer)
    table.insert(row_id="r1", text="alpha")
    assert table.get("r1")["checked"] == "v1:h1:alpha"

    # A bound-value change (hint) → the column is stale → sync recomputes it.
    # The proposer is flaky again on its next FIRST ask: if the recompute ran
    # the producer as a lone step instead of the whole cycle, the rejected
    # empty answer would land silently. It must not.
    table_rebound = _table(tmp_path, proposer, hint="h2")
    table_rebound.sync([{"row_id": "r1", "text": "alpha"}])

    row = table_rebound.get("r1")
    assert row["checked"] == "v1:h2:alpha", "sync recompute must re-ask through the cycle"
    assert row["checked"] != ""
    raw = table_rebound._store.read_rows(table_rebound.table_name)
    assert all(r["checked"] != "" for r in raw if r.get("checked") is not None)
