"""Tests for materialization types and definition-hash computation."""

from __future__ import annotations

from dataclasses import dataclass

from hypergraph.materialization import ErrorRow, SyncResult
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


class TestErrorRow:
    def test_fields(self):
        e = ErrorRow(identity={"utt_id": "u1"}, error_type="ValueError", error_msg="bad")
        assert e.identity == {"utt_id": "u1"}
        assert e.error_type == "ValueError"
        assert e.error_msg == "bad"


class TestSyncResult:
    def test_fields(self):
        r = SyncResult(inserted=1, updated=2, deleted=3, skipped=4, errored=5)
        assert r.inserted == 1
        assert r.updated == 2
        assert r.deleted == 3
        assert r.skipped == 4
        assert r.errored == 5
