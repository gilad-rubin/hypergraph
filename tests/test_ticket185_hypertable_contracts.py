"""Foreman-owned structural contracts for ticket #185 HyperTable extraction."""

from __future__ import annotations

import inspect

from hypergraph.materialization import HyperTable


def test_insert_has_typed_variadic_row_inputs() -> None:
    parameters = inspect.signature(HyperTable.insert).parameters

    assert parameters["args"].kind is inspect.Parameter.VAR_POSITIONAL
    assert parameters["args"].annotation == "Any"
    assert parameters["kwargs"].kind is inspect.Parameter.VAR_KEYWORD
    assert parameters["kwargs"].annotation == "Any"
    assert inspect.signature(HyperTable.insert).return_annotation != inspect.Signature.empty
