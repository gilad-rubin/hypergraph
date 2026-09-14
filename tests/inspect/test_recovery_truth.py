"""Truth checks for the facts-only native fallback and nested map identity."""

from __future__ import annotations

import asyncio
import html
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, node
from hypergraph.runners._shared import _inspect_transport
from hypergraph.runners._shared._inspect import MapInspection, RunInspection
from hypergraph.runners._shared._inspect_html import _DEBUG_WORKFLOWS_DOC, _native_failure_markup


def _nested_failure_graph() -> Graph:
    @node(output_name="reviewed")
    def review_customer(customer_id: str) -> str:
        if customer_id.startswith("reject-"):
            raise ValueError(f"manual review: {customer_id}")
        return f"approved:{customer_id}"

    inner = Graph([review_customer], name="inner-review")
    return Graph(
        [inner.as_node(name="review_group").map_over("customer_id")],
        name="outer-review",
    )


def _nested_values() -> dict[str, list[list[str]]]:
    return {
        "customer_id": [
            ["approve-outer-0", "reject-outer-0"],
            ["approve-outer-1", "reject-outer-1"],
        ]
    }


def _assert_nested_outer_and_inner_indexes(batch: Any) -> None:
    assert [failed.failure.item_index for failed in batch.failures] == [0, 1]
    artifact = batch.inspect()._artifact
    assert [item.item_index for item in artifact.items] == [0, 1]
    for outer_index, item in enumerate(artifact.items):
        assert item.run is not None
        leaf = next(node for node in item.run.nodes if node.qualified_name == "review_group/review_customer" and node.status == "failed")
        assert leaf.item_index == 1
        assert leaf.failure is not None
        assert leaf.failure.item_index == 1
        assert item.run.failures[0].item_index == outer_index


def test_sync_nested_map_public_failures_use_outer_indexes_while_leaf_keeps_inner_index() -> None:
    batch = SyncRunner().map(
        _nested_failure_graph(),
        _nested_values(),
        map_over="customer_id",
        inspect=True,
        error_handling="continue",
    )

    _assert_nested_outer_and_inner_indexes(batch)


@pytest.mark.asyncio
async def test_async_nested_map_public_failures_use_outer_indexes_while_leaf_keeps_inner_index() -> None:
    batch = await AsyncRunner().map(
        _nested_failure_graph(),
        _nested_values(),
        map_over="customer_id",
        inspect=True,
        error_handling="continue",
    )

    _assert_nested_outer_and_inner_indexes(batch)


def _failure_markup(artifact: RunInspection | MapInspection) -> str:
    return _native_failure_markup(artifact)


def _failed_graph() -> Graph:
    @node(output_name="reviewed")
    def review(customer_id: str) -> str:
        raise ValueError(f"manual review: {customer_id}")

    return Graph([review], name="recovery-review")


def _transient_graph() -> Graph:
    attempts = 0

    @node(output_name="reviewed")
    def review(customer_id: str) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError(f"temporary review outage: {customer_id}")
        return f"approved:{customer_id}"

    return Graph([review], name="transient-recovery-review")


def test_the_native_fallback_emits_facts_and_no_python_source() -> None:
    artifact = (
        SyncRunner()
        .run(
            _failed_graph(),
            {"customer_id": "maya-23"},
            inspect=True,
            error_handling="continue",
        )
        .inspect()
        ._artifact
    )

    markup = html.unescape(_failure_markup(artifact).replace("<wbr>", ""))

    # Facts the reader needs, straight off the artifact.
    assert "Qualified node: <code>review</code>" in markup
    assert "customer_id=maya-23" in markup
    assert "ValueError: manual review: maya-23" in markup
    assert _DEBUG_WORKFLOWS_DOC in markup
    # The rerun snippet belongs to the one renderer, not to the transport.
    assert "runner.run(" not in markup
    assert "runner.map(" not in markup
    assert "try:" not in markup
    assert "Smallest useful" not in markup


def test_the_transport_module_contains_no_python_source_templates() -> None:
    source = Path(_inspect_transport.__file__).read_text(encoding="utf-8")

    for emitted in ("runner.run(", "runner.map(", "except Exception as error", "batch.failures"):
        assert emitted not in source, emitted


@pytest.mark.parametrize("runner_kind", ["sync", "async"])
@pytest.mark.parametrize("outer_index", [0, 1])
def test_native_primary_failure_names_exact_nested_leaf_and_scalar_input(
    runner_kind: str,
    outer_index: int,
) -> None:
    graph = _nested_failure_graph()
    values = _nested_values()
    if runner_kind == "sync":
        batch = SyncRunner().map(
            graph,
            values,
            map_over="customer_id",
            inspect=True,
            error_handling="continue",
        )
    else:

        async def execute_batch():
            return await AsyncRunner().map(
                graph,
                values,
                map_over="customer_id",
                inspect=True,
                error_handling="continue",
            )

        batch = asyncio.run(execute_batch())

    artifact = batch.inspect()._artifact
    single_item = replace(artifact, items=(artifact.items[outer_index],))
    plain_markup = html.unescape(_native_failure_markup(single_item).replace("<wbr>", ""))

    assert "Qualified node: <code>review_group/review_customer</code>" in plain_markup
    assert f"customer_id=reject-outer-{outer_index}" in plain_markup
    assert f"approve-outer-{outer_index}" not in plain_markup
    assert f"ValueError: manual review: reject-outer-{outer_index}" in plain_markup
