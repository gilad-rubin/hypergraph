"""Tests for materialization types and definition-hash computation."""

from __future__ import annotations

from dataclasses import dataclass

from hypergraph.materialization import (
    ErroredRow,
    RowReceipt,
    RowStatus,
    TableReceipt,
    WriteOutcome,
)
from hypergraph.materialization._fingerprint import compute_definition_hash

# ---------------------------------------------------------------------------
# Definition hash
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Utterance:
    utt_id: str
    text: str


@dataclass(frozen=True)
class EmbeddedUtterance:
    utt_id: str
    text: str
    vector: list[float]


def sample_derive(utt: Utterance) -> EmbeddedUtterance:
    return EmbeddedUtterance(utt_id=utt.utt_id, text=utt.text, vector=[0.0])


class TestDefinitionHash:
    def test_deterministic(self):
        h1 = compute_definition_hash(sample_derive)
        h2 = compute_definition_hash(sample_derive)
        assert h1 == h2

    def test_different_functions_differ(self):
        def other(utt: Utterance) -> EmbeddedUtterance:
            return EmbeddedUtterance(utt_id=utt.utt_id, text=utt.text, vector=[1.0])

        assert compute_definition_hash(sample_derive) != compute_definition_hash(other)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class TestErroredRow:
    def test_fields(self):
        error = ErroredRow(id="u1", error="ValueError: bad", row={"utt_id": "u1"})

        assert error.id == "u1"
        assert error.error == "ValueError: bad"
        assert error.row == {"utt_id": "u1"}


class TestTableReceipt:
    def test_aggregates_row_receipts(self):
        receipt = TableReceipt(
            (
                RowReceipt("u1", WriteOutcome.INSERTED, RowStatus.COMPLETE),
                RowReceipt("u2", WriteOutcome.UPDATED, RowStatus.ERROR, error="bad"),
                RowReceipt("u3", WriteOutcome.SKIPPED, RowStatus.COMPLETE),
            ),
            deleted=3,
        )

        assert receipt.inserted == 1
        assert receipt.updated == 1
        assert receipt.skipped == 1
        assert receipt.deleted == 3
        assert receipt.failed
        assert len(receipt.errors) == 1

    def test_empty_receipt_is_settled_complete(self):
        receipt = TableReceipt(())

        assert receipt.completed
        assert not receipt.paused
        assert not receipt.failed
