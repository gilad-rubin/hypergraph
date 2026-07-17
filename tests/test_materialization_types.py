"""Tests for materialization types and definition-hash computation."""

from __future__ import annotations

import hashlib
import inspect
import os
import subprocess
import sys
import textwrap
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


class Summarizer:
    """A configured object whose bound method serves as a derive node."""

    def __init__(self, model: str):
        self.model = model

    def summarize(self, text: str) -> str:
        return f"{self.model}:{text}"


class TestDefinitionHash:
    def test_deterministic(self):
        h1 = compute_definition_hash(sample_derive)
        h2 = compute_definition_hash(sample_derive)
        assert h1 == h2

    def test_different_functions_differ(self):
        def other(utt: Utterance) -> EmbeddedUtterance:
            return EmbeddedUtterance(utt_id=utt.utt_id, text=utt.text, vector=[1.0])

        assert compute_definition_hash(sample_derive) != compute_definition_hash(other)

    def test_node_definition_hash_attribute_short_circuits(self):
        """An object exposing ``definition_hash`` (a GraphNode) is its own basis."""

        class FakeGraphNode:
            definition_hash = "f" * 64

        assert compute_definition_hash(FakeGraphNode()) == "f" * 64

    def test_configured_instances_hash_differently(self):
        """Bound methods of differently-configured instances are different recipes.

        ``summarizer.summarize`` with ``model="gpt-4"`` derives different content
        than with ``model="o3"`` — sharing a fingerprint would silently skip the
        re-derive when the configuration changes.
        """
        gpt4 = Summarizer(model="gpt-4")
        o3 = Summarizer(model="o3")

        assert compute_definition_hash(gpt4.summarize) != compute_definition_hash(o3.summarize)

    def test_same_config_instances_hash_identically(self):
        """Equal configuration means equal recipe — no spurious re-derives."""
        assert compute_definition_hash(Summarizer(model="gpt-4").summarize) == compute_definition_hash(Summarizer(model="gpt-4").summarize)

    def test_dynamic_function_hash_is_stable_across_processes(self, tmp_path):
        """An exec-created function (no retrievable source) fingerprints identically
        in two separate interpreter processes — a per-process basis (repr address)
        would mark every such row as drifted on every run."""
        script = tmp_path / "dynamic_fingerprint.py"
        script.write_text(
            textwrap.dedent(
                """
                from hypergraph.materialization._fingerprint import compute_definition_hash

                namespace = {}
                exec("def derive(text): return text.upper()", namespace)
                print(compute_definition_hash(namespace["derive"]))
                """
            ),
            encoding="utf-8",
        )

        outputs = []
        for seed in ("1", "2"):
            result = subprocess.run(
                [sys.executable, str(script)],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONHASHSEED": seed},
            )
            outputs.append(result.stdout.strip())

        assert len(set(outputs)) == 1
        assert len(outputs[0]) == 64

    def test_ordinary_function_keeps_source_hash_basis(self):
        """A module-level function's fingerprint is the sha256 of its source text —
        the same value the pre-hardening scheme produced, so existing rows derived
        from plain functions do NOT re-derive."""
        expected = hashlib.sha256(inspect.getsource(sample_derive).encode()).hexdigest()

        assert compute_definition_hash(sample_derive) == expected

    def test_bound_method_hash_departs_from_bare_source_hash(self):
        """A configured instance's bound method no longer hashes to bare source:
        instance state joins the fingerprint. Rows previously derived from such
        nodes re-derive once after this change (see changelog)."""
        bound = Summarizer(model="gpt-4").summarize
        bare_source_hash = hashlib.sha256(inspect.getsource(bound).encode()).hexdigest()

        assert compute_definition_hash(bound) != bare_source_hash


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
