"""Tests for materialization markers, types, and content key computation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

import pytest

from hypergraph.materialization import (
    ChainedTableError,
    ContentKey,
    DerivationError,
    ErrorRow,
    Identity,
    SyncResult,
)
from hypergraph.materialization._keys import (
    compute_content_key,
    compute_definition_hash,
    compute_schema_fingerprint,
    extract_markers,
)

# ---------------------------------------------------------------------------
# Test entities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Utterance:
    utt_id: Annotated[str, Identity]
    text: Annotated[str, ContentKey]
    speaker: str


@dataclass(frozen=True)
class EmbeddedUtterance:
    utt_id: str
    text: str
    vector: list[float]


@dataclass(frozen=True)
class MultiKey:
    part_a: Annotated[str, Identity]
    part_b: Annotated[str, Identity]
    content: Annotated[str, ContentKey]
    extra: Annotated[int, ContentKey]
    label: str


# ---------------------------------------------------------------------------
# Marker extraction
# ---------------------------------------------------------------------------


class TestMarkerExtraction:
    def test_extracts_identity_fields(self):
        markers = extract_markers(Utterance)
        assert markers.identity_fields == ["utt_id"]

    def test_extracts_content_key_fields(self):
        markers = extract_markers(Utterance)
        assert markers.content_key_fields == ["text"]

    def test_extracts_multiple_identity_fields(self):
        markers = extract_markers(MultiKey)
        assert sorted(markers.identity_fields) == ["part_a", "part_b"]

    def test_extracts_multiple_content_key_fields(self):
        markers = extract_markers(MultiKey)
        assert sorted(markers.content_key_fields) == ["content", "extra"]

    def test_no_identity_raises(self):
        @dataclass(frozen=True)
        class NoIdentity:
            x: Annotated[str, ContentKey]

        with pytest.raises(ValueError, match="Identity"):
            extract_markers(NoIdentity)

    def test_plain_dataclass_no_content_key_uses_all_non_identity(self):
        @dataclass(frozen=True)
        class NoContentKey:
            id: Annotated[str, Identity]
            a: str
            b: int

        markers = extract_markers(NoContentKey)
        assert markers.identity_fields == ["id"]
        assert sorted(markers.content_key_fields) == ["a", "b"]


# ---------------------------------------------------------------------------
# Schema fingerprint
# ---------------------------------------------------------------------------


class TestSchemaFingerprint:
    def test_deterministic(self):
        fp1 = compute_schema_fingerprint(EmbeddedUtterance)
        fp2 = compute_schema_fingerprint(EmbeddedUtterance)
        assert fp1 == fp2

    def test_changes_on_field_addition(self):
        @dataclass(frozen=True)
        class V1:
            x: str

        @dataclass(frozen=True)
        class V2:
            x: str
            y: int

        assert compute_schema_fingerprint(V1) != compute_schema_fingerprint(V2)

    def test_changes_on_type_change(self):
        @dataclass(frozen=True)
        class A:
            x: str

        @dataclass(frozen=True)
        class B:
            x: int

        assert compute_schema_fingerprint(A) != compute_schema_fingerprint(B)


# ---------------------------------------------------------------------------
# Definition hash
# ---------------------------------------------------------------------------


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
# Content key
# ---------------------------------------------------------------------------


class TestContentKey:
    def test_deterministic(self):
        item = Utterance("u1", "hello", "alice")
        configs = {"embedder": {"model": "small"}}
        k1 = compute_content_key(item, configs, "abc", "fp1")
        k2 = compute_content_key(item, configs, "abc", "fp1")
        assert k1 == k2

    def test_changes_on_content(self):
        a = Utterance("u1", "hello", "alice")
        b = Utterance("u1", "world", "alice")
        configs = {"embedder": {"model": "small"}}
        assert compute_content_key(a, configs, "h", "fp") != compute_content_key(b, configs, "h", "fp")

    def test_unchanged_non_content_field_same_key(self):
        a = Utterance("u1", "hello", "alice")
        b = Utterance("u1", "hello", "bob")
        configs = {}
        k1 = compute_content_key(a, configs, "h", "fp")
        k2 = compute_content_key(b, configs, "h", "fp")
        assert k1 == k2

    def test_changes_on_config(self):
        item = Utterance("u1", "hello", "alice")
        k1 = compute_content_key(item, {"e": {"model": "a"}}, "h", "fp")
        k2 = compute_content_key(item, {"e": {"model": "b"}}, "h", "fp")
        assert k1 != k2

    def test_changes_on_definition_hash(self):
        item = Utterance("u1", "hello", "alice")
        k1 = compute_content_key(item, {}, "hash_a", "fp")
        k2 = compute_content_key(item, {}, "hash_b", "fp")
        assert k1 != k2

    def test_changes_on_schema_fingerprint(self):
        item = Utterance("u1", "hello", "alice")
        k1 = compute_content_key(item, {}, "h", "fp_a")
        k2 = compute_content_key(item, {}, "h", "fp_b")
        assert k1 != k2


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


class TestDerivationError:
    def test_carries_succeeded_and_failed(self):
        e = DerivationError(
            succeeded=[{"utt_id": "u1"}],
            failed=[{"utt_id": "u2"}],
        )
        assert e.succeeded == [{"utt_id": "u1"}]
        assert e.failed == [{"utt_id": "u2"}]
        assert "1 succeeded, 1 failed" in str(e)


class TestChainedTableError:
    def test_is_exception(self):
        e = ChainedTableError("insert")
        assert isinstance(e, Exception)
