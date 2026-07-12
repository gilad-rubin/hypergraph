"""Foreman-owned structural contracts for ticket #185 HyperTable extraction."""

from __future__ import annotations

import inspect

from hypergraph.materialization import HyperTable


def test_insert_keeps_its_existing_public_signature_annotations() -> None:
    parameters = inspect.signature(HyperTable.insert).parameters

    assert parameters["args"].kind is inspect.Parameter.VAR_POSITIONAL
    assert parameters["args"].annotation is inspect.Parameter.empty
    assert parameters["kwargs"].kind is inspect.Parameter.VAR_KEYWORD
    assert parameters["kwargs"].annotation is inspect.Parameter.empty
