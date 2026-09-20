"""Characterization of the ROUTED reconcile: what a gate does to a stored row.

Column reconciliation normally settles one node at a time. When the node it
reaches is routed by a gate, the planner cannot run that node alone — it runs
the gate's whole slice and settles everything downstream from that one run.
These tests pin what that observably does today: the stored value, the stored
``_provenance_<column>`` stamp, the receipt, and how many times each node ran.

They are deliberately assertive about stamps, including one stamp that is
wrong today (``test_routed_union_stamp_alternates_...``). A refactor of the
reconcile state machine must not change any of it by accident; changing it on
purpose is a separate, named decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, TypedDict

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, ifelse, interrupt, node
from hypergraph.materialization import LanceDBStore, RowStatus, WriteOutcome


@dataclass(frozen=True)
class ReviewQuestion:
    answer_type: ClassVar[object] = str
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple[object, ...] = ()


def _latest(store: LanceDBStore, table_name: str) -> dict[str, Any]:
    rows = store.read_rows(table_name)
    return max(rows, key=lambda row: row.get("_write_gen", 0))


def _stamp_of(table: Any, producer: Any, values: dict[str, Any]) -> str:
    """The provenance stamp ``producer`` would write for ``values``.

    Reaches into the table's own policy object on purpose: a stamp is a hash,
    and the only honest way to say "this row carries NEGATIVE's stamp" is to
    ask the same policy that wrote it.
    """
    table._ensure_analyzed()
    return table._provenance_policy.node_provenance(producer, values)


def test_routed_reconcile_flips_value_stamp_and_reruns_only_the_live_branch(tmp_path) -> None:
    calls = {"choose": 0, "positive": 0, "negative": 0}

    @ifelse(when_true="positive", when_false="negative")
    def choose(positive_number: bool) -> bool:
        calls["choose"] += 1
        return positive_number

    @node(output_name="label")
    def positive(value: int) -> str:
        calls["positive"] += 1
        return f"positive:{value}"

    @node(output_name="label")
    def negative(value: int) -> str:
        calls["negative"] += 1
        return f"negative:{value}"

    store = LanceDBStore(str(tmp_path / "flip"))
    table = Graph([choose, positive, negative]).as_table(identity="item_id", store=store, runner=SyncRunner())

    inserted = table.insert(item_id="i1", positive_number=True, value=3)
    positive_stamp = _stamp_of(table, positive, {"value": 3})
    negative_stamp = _stamp_of(table, negative, {"value": 3})
    row = _latest(store, table.table_name)

    assert (inserted.outcome, inserted.status) == (WriteOutcome.INSERTED, RowStatus.COMPLETE)
    assert row["label"] == "positive:3"
    assert row["_provenance_label"] == positive_stamp
    assert calls == {"choose": 1, "positive": 1, "negative": 0}

    flipped = table.update("i1", positive_number=False)
    row = _latest(store, table.table_name)

    assert (flipped.outcome, flipped.status) == (WriteOutcome.UPDATED, RowStatus.COMPLETE)
    assert row["label"] == "negative:3"
    assert row["_provenance_label"] == negative_stamp
    # The gate re-ran and took the other branch; the dead branch stayed dead.
    assert calls == {"choose": 2, "positive": 1, "negative": 1}
    assert table.status().is_fresh

    rederived = table.rederive("label")
    row = _latest(store, table.table_name)

    assert [(receipt.outcome, receipt.status) for receipt in rederived.receipts] == [(WriteOutcome.UPDATED, RowStatus.COMPLETE)]
    assert row["label"] == "negative:3"
    assert row["_provenance_label"] == negative_stamp
    assert calls == {"choose": 3, "positive": 1, "negative": 2}

    flipped_back = table.update("i1", positive_number=True)
    row = _latest(store, table.table_name)

    assert (flipped_back.outcome, flipped_back.status) == (WriteOutcome.UPDATED, RowStatus.COMPLETE)
    assert row["label"] == "positive:3"
    assert row["_provenance_label"] == positive_stamp
    assert calls == {"choose": 4, "positive": 2, "negative": 2}
    assert table.status().is_fresh
    assert store.column_names(table.table_name).count("label") == 1


def test_routed_union_stamp_alternates_between_producers_on_unrelated_updates(tmp_path) -> None:
    """PINNED AS-IS, NOT AS-WANTED.

    An update that touches nothing the gate can see still re-runs the gate's
    slice, and the stamp stored on ``label`` alternates between the two
    producers — every other write stamps the value with the provenance of a
    branch that provably never ran (``negative`` is called zero times).

    That is a defect in the value chain, filed separately. It lives here so a
    refactor of the state machine cannot quietly change it in either
    direction: the day it converges, this assertion is the one that moves.
    """
    calls = {"choose": 0, "positive": 0, "negative": 0, "other": 0}

    @ifelse(when_true="positive", when_false="negative")
    def choose(positive_number: bool) -> bool:
        calls["choose"] += 1
        return positive_number

    @node(output_name="label")
    def positive(value: int) -> str:
        calls["positive"] += 1
        return f"positive:{value}"

    @node(output_name="label")
    def negative(value: int) -> str:
        calls["negative"] += 1
        return f"negative:{value}"

    @node(output_name="summary")
    def other(extra: str) -> str:
        calls["other"] += 1
        return f"summary:{extra}"

    store = LanceDBStore(str(tmp_path / "oscillate"))
    table = Graph([choose, positive, negative, other]).as_table(identity="item_id", store=store, runner=SyncRunner())

    table.insert(item_id="i1", positive_number=True, value=3, extra="a")
    positive_stamp = _stamp_of(table, positive, {"value": 3})
    negative_stamp = _stamp_of(table, negative, {"value": 3})
    names = {positive_stamp: "positive", negative_stamp: "negative"}

    observed = [(_latest(store, table.table_name)["label"], names[_latest(store, table.table_name)["_provenance_label"]])]
    for index in range(5):
        table.update("i1", extra=f"x{index}")
        row = _latest(store, table.table_name)
        observed.append((row["label"], names[row["_provenance_label"]]))

    assert observed == [
        ("positive:3", "positive"),
        ("positive:3", "negative"),
        ("positive:3", "positive"),
        ("positive:3", "negative"),
        ("positive:3", "positive"),
        ("positive:3", "negative"),
    ]
    assert _latest(store, table.table_name)["summary"] == "summary:x4"
    # `negative` never ran, yet its stamp is stored on three of those six rows.
    assert calls == {"choose": 6, "positive": 6, "negative": 0, "other": 6}


class _Item(TypedDict):
    item_id: str
    value: int
    positive_number: bool


def test_routed_reconcile_inside_a_child_graph_settles_the_child_row(tmp_path) -> None:
    """The gate lives in the child graph, so the slice is cut from it, not the root."""
    calls = {"split": 0, "choose": 0, "positive": 0, "negative": 0}

    @node(output_name="items")
    def split(text: str, flag: bool) -> list[_Item]:
        calls["split"] += 1
        return [_Item(item_id="a", value=len(text), positive_number=flag)]

    @ifelse(when_true="positive", when_false="negative")
    def choose(positive_number: bool) -> bool:
        calls["choose"] += 1
        return positive_number

    @node(output_name="positive_label")
    def positive(value: int) -> str:
        calls["positive"] += 1
        return f"positive:{value}"

    @node(output_name="negative_label")
    def negative(value: int) -> str:
        calls["negative"] += 1
        return f"negative:{value}"

    child_graph = Graph([choose, positive, negative], name="process_item")
    graph = Graph([split, child_graph.as_node().map_over("items", identity="item_id")], name="documents")
    store = LanceDBStore(str(tmp_path / "child_gate"))
    table = graph.as_table(identity="doc_id", store=store, runner=SyncRunner())

    parent = table.insert(doc_id="d1", text="abcd", flag=True)
    positive_stamp = _stamp_of(table, positive, {"value": 4})
    negative_stamp = _stamp_of(table, negative, {"value": 4})
    child = _latest(store, "item")

    assert (parent.outcome, parent.status) == (WriteOutcome.INSERTED, RowStatus.COMPLETE)
    assert (child["positive_label"], child["negative_label"]) == ("positive:4", None)
    assert child["_provenance_positive_label"] == positive_stamp
    assert child["_provenance_negative_label"] == negative_stamp
    assert calls == {"split": 1, "choose": 1, "positive": 1, "negative": 0}

    # Flipping the parent source rewrites the child item, so the stored child
    # row is reconciled — and its first non-fresh node is under the gate.
    flipped = table.update("d1", flag=False)
    child = _latest(store, "item")

    assert (flipped.outcome, flipped.status) == (WriteOutcome.UPDATED, RowStatus.COMPLETE)
    assert (child["positive_label"], child["negative_label"]) == (None, "negative:4")
    assert child["_provenance_positive_label"] == positive_stamp
    assert child["_provenance_negative_label"] == negative_stamp
    assert calls == {"split": 2, "choose": 2, "positive": 1, "negative": 1}

    flipped_back = table.update("d1", flag=True)
    child = _latest(store, "item")

    assert (flipped_back.outcome, flipped_back.status) == (WriteOutcome.UPDATED, RowStatus.COMPLETE)
    assert (child["positive_label"], child["negative_label"]) == ("positive:4", None)
    assert calls == {"split": 3, "choose": 3, "positive": 2, "negative": 1}
    assert table.status().is_fresh


@pytest.mark.asyncio
async def test_routed_slice_pause_waits_then_the_answer_resumes_without_reasking(tmp_path) -> None:
    """An interrupt inside the gate's slice: the whole slice pauses, once."""
    calls = {"prepare": 0, "gate": 0, "review": 0, "shortcut": 0}

    @node(output_name="prepared")
    def prepare(text: str) -> str:
        calls["prepare"] += 1
        return text.strip().lower()

    @ifelse(when_true="review", when_false="shortcut")
    def gate(strict: bool) -> bool:
        calls["gate"] += 1
        return strict

    @interrupt(answer_name="decision")
    def review(prepared: str) -> ReviewQuestion:
        calls["review"] += 1
        return ReviewQuestion(prompt=f"Publish {prepared}?", options=("publish", "archive"))

    @node(output_name="note")
    def shortcut(prepared: str) -> str:
        calls["shortcut"] += 1
        return f"auto:{prepared}"

    store = LanceDBStore(str(tmp_path / "routed_pause"))
    table = Graph([prepare, gate, review, shortcut]).as_table(identity="doc_id", store=store, runner=AsyncRunner())

    inserted = await table.insert(doc_id="d1", text=" Draft ", strict=True, decision="publish")

    assert (inserted.outcome, inserted.status) == (WriteOutcome.INSERTED, RowStatus.COMPLETE)
    assert _latest(store, table.table_name)["decision"] == "publish"
    assert calls == {"prepare": 1, "gate": 1, "review": 0, "shortcut": 0}

    # `prepared` changes, so `review` is stale — but `review` is routed, so the
    # gate's whole slice runs and the pause comes back out of the routed run.
    waiting = await table.update("d1", text=" New ")
    row = _latest(store, table.table_name)

    assert (waiting.outcome, waiting.status) == (WriteOutcome.UPDATED, RowStatus.WAITING)
    assert waiting.paused and waiting.pause is not None and waiting.pause.response_key == "decision"
    assert row["_status"] == "waiting"
    assert row["decision"] is None
    assert row["prepared"] == "new"
    assert '"prompt":"Publish new?"' in row["_question"]
    assert [(entry.id, entry.pause.response_key) for entry in table.waiting()] == [("d1", "decision")]
    assert calls == {"prepare": 2, "gate": 2, "review": 1, "shortcut": 0}

    answered = await table.update("d1", decision="archive")
    row = _latest(store, table.table_name)

    assert (answered.outcome, answered.status) == (WriteOutcome.UPDATED, RowStatus.COMPLETE)
    assert row["decision"] == "archive"
    assert row["_question"] is None
    assert table.waiting() == ()
    # The answer settles the interrupt; nothing in the slice is asked again.
    assert calls == {"prepare": 2, "gate": 2, "review": 1, "shortcut": 0}

    # Routing the other way runs the branch that had never run.
    rerouted = await table.update("d1", strict=False)
    row = _latest(store, table.table_name)

    assert (rerouted.outcome, rerouted.status) == (WriteOutcome.UPDATED, RowStatus.COMPLETE)
    assert row["note"] == "auto:new"
    assert calls == {"prepare": 2, "gate": 3, "review": 1, "shortcut": 1}


def test_a_routed_interrupt_answer_without_provenance_is_a_structural_error() -> None:
    """The failure path a routed pause must keep: a named error, not a KeyError."""
    from hypergraph.materialization._writes import _pause_provenance
    from hypergraph.runners import PauseInfo

    pause = PauseInfo(node_name="review", value=ReviewQuestion(prompt="?"), response_key="decision")

    with pytest.raises(RuntimeError) as routed:
        _pause_provenance({}, pause, routed=True)

    assert "a routed interrupt answer" in str(routed.value)
    assert "'decision'" in str(routed.value)

    with pytest.raises(RuntimeError) as plain:
        _pause_provenance({}, pause)

    assert "an interrupt answer" in str(plain.value)
    assert _pause_provenance({"decision": "stamp"}, pause, routed=True) == "stamp"


def test_rederive_on_a_routed_union_column_runs_the_gate_and_skips_when_present(tmp_path) -> None:
    """``rederive`` nulls the target stamp, so the union column comes back through the gate."""
    calls = {"choose": 0, "positive": 0, "negative": 0}

    @ifelse(when_true="positive", when_false="negative")
    def choose(positive_number: bool) -> bool:
        calls["choose"] += 1
        return positive_number

    @node(output_name="label")
    def positive(value: int) -> str:
        calls["positive"] += 1
        return f"positive:{value}"

    @node(output_name="label")
    def negative(value: int) -> str:
        calls["negative"] += 1
        return f"negative:{value}"

    store = LanceDBStore(str(tmp_path / "rederive"))
    table = Graph([choose, positive, negative]).as_table(identity="item_id", store=store, runner=SyncRunner())

    table.insert(item_id="i1", positive_number=False, value=7)
    negative_stamp = _stamp_of(table, negative, {"value": 7})

    assert _latest(store, table.table_name)["label"] == "negative:7"
    assert calls == {"choose": 1, "positive": 0, "negative": 1}

    rederived = table.rederive("label")
    row = _latest(store, table.table_name)

    assert [(receipt.outcome, receipt.status) for receipt in rederived.receipts] == [(WriteOutcome.UPDATED, RowStatus.COMPLETE)]
    assert row["label"] == "negative:7"
    assert row["_provenance_label"] == negative_stamp
    # The gate's slice ran again; the dead branch still never did.
    assert calls == {"choose": 2, "positive": 0, "negative": 2}

    backfilled = table.rederive("label", missing_only=True)

    assert [(receipt.outcome, receipt.status) for receipt in backfilled.receipts] == [(WriteOutcome.SKIPPED, RowStatus.COMPLETE)]
    assert _latest(store, table.table_name)["label"] == "negative:7"
    assert calls == {"choose": 2, "positive": 0, "negative": 2}
    assert table.status().is_fresh


def test_the_planner_settles_a_whole_routed_scope_with_no_store_and_no_runner() -> None:
    """The routed case is now a step kind, so it can be driven as pure state.

    One ``RunRoutedGraph`` settles every node the gate decides: the second
    producer of the union column is advanced from the same run, with no second
    request for the runner.
    """
    from hypergraph.materialization._provenance import (
        Provenance,
        ReconcileComplete,
        RunRoutedGraph,
    )
    from hypergraph.materialization._schema import analyze_table

    @ifelse(when_true="positive", when_false="negative")
    def choose(positive_number: bool) -> bool:
        return positive_number

    @node(output_name="label")
    def positive(value: int) -> str:
        return f"positive:{value}"

    @node(output_name="label")
    def negative(value: int) -> str:
        return f"negative:{value}"

    graph = Graph([choose, positive, negative])
    spec = analyze_table(graph, "item_id", {}, [])
    policy = Provenance(graph, spec, {}, {})
    gate_column = policy.node_columns(choose)[0].name

    values = {"item_id": "i1", "positive_number": True, "value": 3, "label": "positive:3"}
    stored = {
        **values,
        gate_column: True,
        f"_provenance_{gate_column}": policy.node_provenance(choose, values),
        "_provenance_label": None,  # exactly what rederive("label") nulls
    }

    state = policy.start_reconcile(spec, stored, {}, graph=graph)
    state, step = policy.next_reconcile_step(state)

    assert isinstance(step, RunRoutedGraph)
    assert step.node is positive
    assert step.scope == {"positive", "negative"}
    assert step.input_values() == {"positive_number": True, "value": 3}
    # The node the gate was reached FOR keeps its own provenance for the settle.
    assert step.provenance == policy.node_provenance(positive, values)

    state = policy.apply_routed_result(
        state,
        step,
        {gate_column: True, "label": "positive:3"},
        frozenset({"choose", "positive"}),
    )
    state, done = policy.next_reconcile_step(state)

    assert isinstance(done, ReconcileComplete)
    assert done.result.output_values() == {gate_column: True, "label": "positive:3"}
    assert done.result.provenance_values() == {
        gate_column: policy.node_provenance(choose, values),
        "label": policy.node_provenance(positive, values),
    }
