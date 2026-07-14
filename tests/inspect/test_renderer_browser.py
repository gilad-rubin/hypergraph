"""Real-browser falsifiers for the offline inspect renderer."""

from __future__ import annotations

import asyncio
import re
import textwrap
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace

import pytest
from playwright.sync_api import Browser, sync_playwright

from hypergraph import AsyncRunner, Graph, SyncRunner, node
from hypergraph.runners._shared._inspect import (
    MapInspection,
    MapItemInspection,
    NodeInspection,
    RunInspection,
)
from hypergraph.runners._shared._inspect_html import (
    build_inspection_payload,
    render_map_inspection,
    render_run_inspection,
)
from hypergraph.runners._shared.results import FailureEvidence
from hypergraph.runners.inspection import InspectionDisplay


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as runtime:
        instance = runtime.chromium.launch(headless=True)
        yield instance
        instance.close()


def test_public_display_remains_interactive_inside_sandboxed_srcdoc(
    browser: Browser,
) -> None:
    node = NodeInspection(
        run_id="run-customer-23",
        span_id="span-load",
        node_name="load_customer",
        qualified_name="load_customer",
        graph_name="customer_enrichment",
        item_index=None,
        superstep=0,
        sequence=0,
        status="completed",
        values_captured=True,
        inputs={"customer_id": "maya-23"},
        outputs={"segment": "enterprise"},
        started_at_ms=1_000.0,
        ended_at_ms=1_084.0,
        duration_ms=84.0,
    )
    artifact = RunInspection(
        run_id="run-customer-23",
        graph_name="customer_enrichment",
        workflow_id="workflow-customers",
        item_index=None,
        status="completed",
        nodes=(node,),
        failures=(),
        total_duration_ms=84.0,
        captured=True,
        terminal=True,
    )
    requests: list[str] = []
    errors: list[Exception] = []
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.on("request", lambda request: requests.append(request.url))
    page.on("pageerror", lambda error: errors.append(error))
    rendered = InspectionDisplay(artifact)._repr_html_()
    assert isinstance(rendered, str)
    page.set_content(rendered)

    frame = page.frame_locator('iframe[title="Hypergraph execution inspection"]')
    root = frame.locator('[data-hypergraph-inspect="run"]')
    assert root.get_by_role("heading", name="customer_enrichment").is_visible()
    graph_tab = root.get_by_role("tab", name="Graph")
    graph_tab.click()

    assert graph_tab.get_attribute("aria-selected") == "true"
    assert requests == []
    assert errors == []
    page.close()


def test_run_ladder_navigates_relative_timeline_values_and_failure_evidence(
    browser: Browser,
) -> None:
    failure = FailureEvidence(
        node_name="research/lookup",
        error=ValueError("provider quota exhausted"),
        inputs={"customer_id": "maya-23"},
        superstep=1,
        duration_ms=200.0,
        graph_name="research",
        workflow_id="workflow-customers",
        item_index=None,
    )
    nodes = (
        NodeInspection(
            run_id="run-customer-23",
            span_id="span-load",
            node_name="load_customer",
            qualified_name="load_customer",
            graph_name="customer_enrichment",
            item_index=None,
            superstep=0,
            sequence=0,
            status="completed",
            values_captured=True,
            inputs={"customer_id": "maya-23"},
            outputs={"profile": {"segment": "enterprise"}},
            started_at_ms=1_000.0,
            ended_at_ms=1_084.0,
            duration_ms=84.0,
        ),
        NodeInspection(
            run_id="run-customer-23",
            span_id="span-normalize",
            node_name="normalize_profile",
            qualified_name="normalize_profile",
            graph_name="customer_enrichment",
            item_index=None,
            superstep=0,
            sequence=1,
            status="restored",
            values_captured=False,
            started_at_ms=1_100.0,
            ended_at_ms=1_105.0,
            duration_ms=5.0,
            cached=True,
        ),
        NodeInspection(
            run_id="child-run-research",
            span_id="span-lookup",
            node_name="lookup",
            qualified_name="research/lookup",
            graph_name="research",
            item_index=None,
            superstep=1,
            sequence=2,
            status="failed",
            values_captured=True,
            inputs={"customer_id": "maya-23"},
            failure=failure,
            started_at_ms=1_250.0,
            ended_at_ms=1_450.0,
            duration_ms=200.0,
        ),
    )
    artifact = RunInspection(
        run_id="run-customer-23",
        graph_name="customer_enrichment",
        workflow_id="workflow-customers",
        item_index=None,
        status="failed",
        nodes=nodes,
        failures=(failure,),
        total_duration_ms=450.0,
        captured=True,
        terminal=True,
        _runner_kind="sync",
    )
    errors: list[Exception] = []
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.on("pageerror", lambda error: errors.append(error))
    page.set_content(render_run_inspection(artifact))
    root = page.locator('[data-hypergraph-inspect="run"]')

    assert errors == []
    assert root.get_by_role("heading", name="customer_enrichment").is_visible()
    assert root.get_by_text("Saved snapshot", exact=True).is_visible()
    assert root.get_by_role("tab", name="Timeline").get_attribute("aria-selected") == "true"
    timeline_rows = root.locator("[data-hg-timeline-row]")
    assert timeline_rows.count() == 3
    assert timeline_rows.nth(0).get_attribute("data-offset-ms") == "0"
    assert timeline_rows.nth(1).get_attribute("data-offset-ms") == "100"
    assert timeline_rows.nth(2).get_attribute("data-offset-ms") == "250"
    assert "restored" in timeline_rows.nth(1).inner_text()
    assert "cached" in timeline_rows.nth(1).inner_text()

    timeline_rows.nth(2).click()
    detail = root.locator("[data-hg-detail]")
    assert "research/lookup" in detail.inner_text()
    assert "ValueError" in detail.inner_text()
    assert "provider quota exhausted" in detail.inner_text()
    assert "span-lookup" in detail.inner_text()
    assert "failure = result.failure" in detail.inner_text()
    detail.get_by_text("Inputs · 1 value", exact=True).click()
    assert detail.get_by_text("maya-23", exact=True).is_visible()

    timeline_rows.nth(1).click()
    restored_detail = root.locator("[data-hg-detail]").inner_text()
    assert "restored values not captured" in restored_detail
    page.close()


def test_run_boundary_error_is_visible_on_failed_node_without_failure_evidence(
    browser: Browser,
) -> None:
    failed_node = NodeInspection(
        run_id="run-cache-save",
        span_id="span-cache-save",
        node_name="persist_customer",
        qualified_name="persist_customer",
        graph_name="customer_enrichment",
        item_index=None,
        superstep=0,
        sequence=0,
        status="failed",
        values_captured=True,
        inputs={"customer_id": "maya-23"},
        outputs={"decision": "approved"},
        failure=None,
        started_at_ms=1_000.0,
        ended_at_ms=1_020.0,
        duration_ms=20.0,
    )
    artifact = RunInspection(
        run_id="run-cache-save",
        graph_name="customer_enrichment",
        workflow_id="workflow-customers",
        item_index=None,
        status="failed",
        nodes=(failed_node,),
        failures=(),
        total_duration_ms=20.0,
        captured=True,
        terminal=True,
        error=RuntimeError("CACHE-SAVE-BOOM"),
    )

    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.set_content(render_run_inspection(artifact))
    root = page.locator('[data-hypergraph-inspect="run"]')
    detail = root.locator("[data-hg-detail]")

    assert root.get_by_text("The run failed at persist_customer.", exact=False).is_visible()
    assert detail.get_by_text("Exact run exception", exact=True).is_visible()
    assert detail.get_by_text("RuntimeError: CACHE-SAVE-BOOM", exact=True).is_visible()
    assert "Smallest useful evidence" not in detail.inner_text()
    assert "failure = result.failure" not in detail.inner_text()
    page.close()


def test_full_renderer_node_exception_heading_follows_repr_preview_truth(
    browser: Browser,
) -> None:
    class RedactedError(Exception):
        def __repr__(self) -> str:
            return "<redacted>"

    failure = FailureEvidence(
        node_name="review_customer",
        error=RedactedError("secret"),
        inputs={"customer_id": "maya-23"},
        superstep=0,
        duration_ms=1.0,
        graph_name="customer_review",
        workflow_id=None,
        item_index=None,
    )
    failed_node = NodeInspection(
        run_id="run-redacted",
        span_id="span-redacted",
        node_name="review_customer",
        qualified_name="review_customer",
        graph_name="customer_review",
        item_index=None,
        superstep=0,
        sequence=0,
        status="failed",
        values_captured=True,
        inputs=failure.inputs,
        failure=failure,
        duration_ms=1.0,
    )
    artifact = RunInspection(
        run_id="run-redacted",
        graph_name="customer_review",
        workflow_id=None,
        item_index=None,
        status="failed",
        nodes=(failed_node,),
        failures=(failure,),
        total_duration_ms=1.0,
        captured=True,
        terminal=True,
        error=failure.error,
    )

    page = browser.new_page()
    page.set_content(render_run_inspection(artifact))
    detail_text = page.locator("[data-hg-detail]").inner_text()

    assert "Exception preview (bounded repr)" in detail_text
    assert "Exact exception" not in detail_text
    assert detail_text.count("RedactedError") == 1
    assert "RedactedError: <redacted>" in detail_text
    page.close()


def test_full_renderer_truncated_node_exception_discloses_original_character_count(
    browser: Browser,
) -> None:
    error = ValueError("x" * 20_001)
    failure = FailureEvidence(
        node_name="review_customer",
        error=error,
        inputs={"customer_id": "maya-23"},
        superstep=0,
        duration_ms=1.0,
        graph_name="customer_review",
        workflow_id=None,
        item_index=None,
    )
    failed_node = NodeInspection(
        run_id="run-truncated",
        span_id="span-truncated",
        node_name="review_customer",
        qualified_name="review_customer",
        graph_name="customer_review",
        item_index=None,
        superstep=0,
        sequence=0,
        status="failed",
        values_captured=True,
        inputs=failure.inputs,
        failure=failure,
        duration_ms=1.0,
    )
    artifact = RunInspection(
        run_id="run-truncated",
        graph_name="customer_review",
        workflow_id=None,
        item_index=None,
        status="failed",
        nodes=(failed_node,),
        failures=(failure,),
        total_duration_ms=1.0,
        captured=True,
        terminal=True,
        error=error,
    )

    page = browser.new_page()
    page.set_content(render_run_inspection(artifact))
    detail_text = page.locator("[data-hg-detail]").inner_text()

    assert "Exception preview (truncated from 20001 characters)" in detail_text
    assert "Exact exception" not in detail_text
    assert "ValueError: " + "x" * 100 in detail_text
    page.close()


def test_full_renderer_run_boundary_placeholder_says_why_details_are_unavailable(
    browser: Browser,
) -> None:
    failed_node = NodeInspection(
        run_id="run-placeholder",
        span_id="span-placeholder",
        node_name="persist_customer",
        qualified_name="persist_customer",
        graph_name="customer_review",
        item_index=None,
        superstep=0,
        sequence=0,
        status="failed",
        values_captured=True,
        duration_ms=1.0,
    )
    artifact = RunInspection(
        run_id="run-placeholder",
        graph_name="customer_review",
        workflow_id=None,
        item_index=None,
        status="failed",
        nodes=(failed_node,),
        failures=(),
        total_duration_ms=1.0,
        captured=True,
        terminal=True,
        error=ValueError(object()),
    )

    page = browser.new_page()
    page.set_content(render_run_inspection(artifact))
    detail_text = page.locator("[data-hg-detail]").inner_text()

    assert "Exception details unavailable" in detail_text
    assert "exception contains unsupported arguments" in detail_text
    assert "Exact run exception" not in detail_text
    page.close()


def test_map_boundary_error_renders_exactly_without_inventing_child_evidence(
    browser: Browser,
) -> None:
    artifact = MapInspection(
        run_id="batch-dispatcher-failure",
        graph_name="customer_enrichment",
        workflow_id=None,
        status="failed",
        map_over=("customer_id",),
        map_mode="zip",
        requested_count=1,
        items=(),
        unstarted_item_indexes=(0,),
        total_duration_ms=12.0,
        captured=True,
        terminal=True,
        error=RuntimeError("PARENT-DISPATCHER-BOOM"),
    )

    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.set_content(render_map_inspection(artifact))
    root = page.locator('[data-hypergraph-inspect="map"]')

    assert root.get_by_text("Exact batch exception", exact=True).is_visible()
    assert root.get_by_text(
        "RuntimeError: PARENT-DISPATCHER-BOOM",
        exact=True,
    ).is_visible()
    assert "Smallest useful evidence" not in root.inner_text()
    assert "failure = result.failure" not in root.inner_text()
    page.close()


def test_full_renderer_batch_boundary_repr_is_bounded_preview_not_exact(
    browser: Browser,
) -> None:
    class RedactedBatchError(Exception):
        def __repr__(self) -> str:
            return "<redacted>"

    artifact = MapInspection(
        run_id="batch-redacted",
        graph_name="customer_review",
        workflow_id=None,
        status="failed",
        map_over=("customer_id",),
        map_mode="zip",
        requested_count=1,
        items=(),
        unstarted_item_indexes=(0,),
        total_duration_ms=1.0,
        captured=True,
        terminal=True,
        error=RedactedBatchError("secret"),
    )

    page = browser.new_page()
    page.set_content(render_map_inspection(artifact))
    root_text = page.locator('[data-hypergraph-inspect="map"]').inner_text()

    assert "Exception preview (bounded repr)" in root_text
    assert "Exact batch exception" not in root_text
    assert root_text.count("RedactedBatchError") == 1
    assert "RedactedBatchError: <redacted>" in root_text
    page.close()


def test_live_map_boundary_error_keeps_graph_tab_and_is_always_visible(
    browser: Browser,
) -> None:
    initial = MapInspection(
        run_id="batch-live-boundary",
        graph_name="customer_enrichment",
        workflow_id=None,
        status="running",
        map_over=("customer_id",),
        map_mode="zip",
        requested_count=1,
        items=(MapItemInspection(0, "running", {"customer_id": "maya-23"}),),
        unstarted_item_indexes=(),
        total_duration_ms=0.0,
        captured=True,
        terminal=False,
    )
    failed = replace(
        initial,
        status="failed",
        terminal=True,
        error=RuntimeError("LIVE-PARENT-DISPATCHER-BOOM"),
        revision=1,
    )
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.set_content(render_map_inspection(initial))
    root = page.locator('[data-hypergraph-inspect="map"]')
    root.get_by_role("tab", name="Graph").click()

    root.evaluate(
        "(element, nextPayload) => element.__hypergraphInspect.updatePayload(nextPayload)",
        build_inspection_payload(
            failed,
            delivery_state="saved",
            delivery_label="Saved snapshot",
        ),
    )

    assert root.get_by_role("tab", name="Graph").get_attribute("aria-selected") == "true"
    assert (
        root.locator("[data-hg-detail]")
        .get_by_text(
            "Exact batch exception",
            exact=True,
        )
        .is_visible()
    )
    assert (
        root.locator("[data-hg-detail]")
        .get_by_text(
            "RuntimeError: LIVE-PARENT-DISPATCHER-BOOM",
            exact=True,
        )
        .is_visible()
    )
    assert root.get_by_role("button", name="Show failure").is_hidden()
    page.close()


def test_later_map_failure_keeps_selection_until_show_failure(browser: Browser) -> None:
    completed_node = NodeInspection(
        run_id="run-item-0",
        span_id="span-item-0",
        node_name="load_customer",
        qualified_name="load_customer",
        graph_name="customer_enrichment",
        item_index=0,
        superstep=0,
        sequence=0,
        status="completed",
        values_captured=True,
        inputs={"customer_id": "ari-2"},
        outputs={"profile": "approved:ari-2"},
        started_at_ms=100.0,
        ended_at_ms=110.0,
        duration_ms=10.0,
    )
    completed_run = RunInspection(
        run_id="run-item-0",
        graph_name="customer_enrichment",
        workflow_id=None,
        item_index=0,
        status="completed",
        nodes=(completed_node,),
        failures=(),
        total_duration_ms=10.0,
        captured=True,
        terminal=True,
    )
    running_node = NodeInspection(
        run_id="run-item-1",
        span_id="span-item-1",
        node_name="lookup",
        qualified_name="research/lookup",
        graph_name="research",
        item_index=1,
        superstep=0,
        sequence=0,
        status="running",
        values_captured=True,
        inputs={"customer_id": "maya-23"},
        started_at_ms=200.0,
    )
    running_run = RunInspection(
        run_id="run-item-1",
        graph_name="customer_enrichment",
        workflow_id=None,
        item_index=1,
        status="running",
        nodes=(running_node,),
        failures=(),
        total_duration_ms=0.0,
        captured=True,
        terminal=False,
    )
    initial = MapInspection(
        run_id="batch-customers",
        graph_name="customer_enrichment",
        workflow_id=None,
        status="running",
        map_over=("customer_id",),
        map_mode="zip",
        requested_count=2,
        items=(
            MapItemInspection(0, "completed", {"customer_id": "ari-2"}, completed_run),
            MapItemInspection(1, "running", {"customer_id": "maya-23"}, running_run),
        ),
        unstarted_item_indexes=(),
        total_duration_ms=10.0,
        captured=True,
        terminal=False,
        _runner_kind="sync",
    )
    failure = FailureEvidence(
        node_name="research/lookup",
        error=RuntimeError("manual review required"),
        inputs={"customer_id": "maya-23"},
        superstep=0,
        duration_ms=25.0,
        graph_name="research",
        workflow_id=None,
        item_index=1,
    )
    failed_node = NodeInspection(
        run_id="run-item-1",
        span_id="span-item-1",
        node_name="lookup",
        qualified_name="research/lookup",
        graph_name="research",
        item_index=1,
        superstep=0,
        sequence=0,
        status="failed",
        values_captured=True,
        inputs={"customer_id": "maya-23"},
        failure=failure,
        started_at_ms=200.0,
        ended_at_ms=225.0,
        duration_ms=25.0,
    )
    failed_run = RunInspection(
        run_id="run-item-1",
        graph_name="customer_enrichment",
        workflow_id=None,
        item_index=1,
        status="failed",
        nodes=(failed_node,),
        failures=(failure,),
        total_duration_ms=25.0,
        captured=True,
        terminal=True,
        error=failure.error,
    )
    failed = MapInspection(
        run_id="batch-customers",
        graph_name="customer_enrichment",
        workflow_id=None,
        status="partial",
        map_over=("customer_id",),
        map_mode="zip",
        requested_count=2,
        items=(
            MapItemInspection(0, "completed", {"customer_id": "ari-2"}, completed_run),
            MapItemInspection(1, "failed", {"customer_id": "maya-23"}, failed_run),
        ),
        unstarted_item_indexes=(),
        total_duration_ms=35.0,
        captured=True,
        terminal=True,
        _runner_kind="sync",
    )

    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.set_content(render_map_inspection(initial))
    root = page.locator('[data-hypergraph-inspect="map"]')
    assert root.get_by_role("tab", name="Items").get_attribute("aria-selected") == "true"
    root.get_by_role("button", name=re.compile(r"Item 0 completed")).click()
    assert root.get_by_role("tab", name="Timeline").get_attribute("aria-selected") == "true"
    assert root.locator('[data-item-index="0"]').get_attribute("aria-current") == "true"

    failed_payload = build_inspection_payload(
        failed,
        delivery_state="live",
        delivery_label="Live · partial",
    )
    root.evaluate(
        "(element, nextPayload) => element.__hypergraphInspect.updatePayload(nextPayload)",
        failed_payload,
    )

    assert root.get_by_text("Live · partial", exact=True).is_visible()
    assert root.locator('[data-item-index="0"]').get_attribute("aria-current") == "true"
    assert root.get_by_role("tab", name="Timeline").get_attribute("aria-selected") == "true"
    assert "maya-23" not in root.locator("[data-hg-detail]").inner_text()
    assert root.get_by_role("button", name="Show failure").is_visible()

    root.get_by_role("button", name="Show failure").click()
    assert root.locator('[data-item-index="1"]').get_attribute("aria-current") == "true"
    assert root.locator("[data-hg-detail]").get_by_text("maya-23", exact=True).is_visible()
    assert "item.failure.item_index == 1" in root.locator("[data-hg-detail]").inner_text()

    root.locator("[data-hg-filter]").select_option("failed")
    assert root.locator("[data-hg-item-list] [data-item-index]").count() == 1
    assert root.locator('[data-item-index="1"]').is_visible()

    stale_payload = build_inspection_payload(
        failed,
        delivery_state="stale",
        delivery_label="Snapshot · updates paused",
    )
    root.evaluate(
        "(element, nextPayload) => element.__hypergraphInspect.updatePayload(nextPayload)",
        stale_payload,
    )
    assert root.get_attribute("data-delivery-state") == "stale"
    assert root.get_by_text("Snapshot · updates paused", exact=True).is_visible()
    assert "this view is not live" in root.locator("[data-hg-alert]").inner_text()
    assert root.get_by_role("button", name="Show failure").is_hidden()
    page.close()


def test_sparse_map_failure_snippet_uses_original_failure_identity(
    browser: Browser,
) -> None:
    completed_run = RunInspection(
        run_id="run-item-1",
        graph_name="customer_enrichment",
        workflow_id=None,
        item_index=1,
        status="completed",
        nodes=(),
        failures=(),
        total_duration_ms=10.0,
        captured=True,
        terminal=True,
    )
    failure = FailureEvidence(
        node_name="decide",
        error=ValueError("manual review required"),
        inputs={"customer_id": "maya-23"},
        superstep=0,
        duration_ms=25.0,
        graph_name="customer_enrichment",
        workflow_id=None,
        item_index=3,
    )
    failed_node = NodeInspection(
        run_id="run-item-3",
        span_id="span-item-3",
        node_name="decide",
        qualified_name="decide",
        graph_name="customer_enrichment",
        item_index=3,
        superstep=0,
        sequence=0,
        status="failed",
        values_captured=True,
        inputs={"customer_id": "maya-23"},
        failure=failure,
        started_at_ms=20.0,
        ended_at_ms=45.0,
        duration_ms=25.0,
    )
    failed_run = RunInspection(
        run_id="run-item-3",
        graph_name="customer_enrichment",
        workflow_id=None,
        item_index=3,
        status="failed",
        nodes=(failed_node,),
        failures=(failure,),
        total_duration_ms=25.0,
        captured=True,
        terminal=True,
        error=failure.error,
    )
    artifact = MapInspection(
        run_id="batch-sparse",
        graph_name="customer_enrichment",
        workflow_id=None,
        status="stopped",
        map_over=("customer_id",),
        map_mode="zip",
        requested_count=4,
        items=(
            MapItemInspection(1, "completed", {"customer_id": "ari-2"}, completed_run),
            MapItemInspection(3, "failed", {"customer_id": "maya-23"}, failed_run),
        ),
        unstarted_item_indexes=(0, 2),
        total_duration_ms=35.0,
        captured=True,
        terminal=True,
        _runner_kind="sync",
    )

    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.set_content(render_map_inspection(artifact))
    root = page.locator('[data-hypergraph-inspect="map"]')
    root.get_by_role("button", name="Show failure").click()
    detail_text = root.locator("[data-hg-detail]").inner_text()

    assert "item.failure for item in batch.failures" in detail_text
    assert "item.failure.item_index == 3" in detail_text
    assert "batch[3]" not in detail_text
    page.close()


def _nested_failure_batch_graph() -> Graph:
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


def _nested_failure_batch_values() -> dict[str, list[list[str]]]:
    return {
        "customer_id": [
            ["approve-outer-0", "reject-outer-0"],
            ["approve-outer-1", "reject-outer-1"],
        ]
    }


def _failed_renderer_run_graph() -> Graph:
    @node(output_name="reviewed")
    def review_customer(customer_id: str) -> str:
        raise ValueError(f"manual review: {customer_id}")

    return Graph([review_customer], name="failed-review")


async def _execute_async_renderer_snippet(
    code_text: str,
    *,
    runner: AsyncRunner,
    graph: Graph,
    values: dict[str, object],
) -> dict[str, object]:
    namespace: dict[str, object] = {}
    source = "async def __snippet(runner, graph, values):\n" + textwrap.indent(code_text, "    ") + "\n    return locals()\n"
    exec(source, namespace)
    return await namespace["__snippet"](runner, graph, values)  # type: ignore[operator]


def _run_away_from_browser_loop(factory: Callable[[], object]) -> object:
    values: list[object] = []
    errors: list[BaseException] = []

    def execute() -> None:
        try:
            values.append(asyncio.run(factory()))  # type: ignore[arg-type]
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=execute)
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive()
    if errors:
        raise errors[0]
    assert len(values) == 1
    return values[0]


@pytest.mark.parametrize("runner_kind", ["sync", "async"])
def test_run_full_renderer_recovery_snippet_executes_for_captured_runner(
    browser: Browser,
    runner_kind: str,
) -> None:
    graph = _failed_renderer_run_graph()
    values = {"customer_id": "maya-23"}
    if runner_kind == "sync":
        runner: SyncRunner | AsyncRunner = SyncRunner()
        result = runner.run(
            graph,
            values,
            inspect=True,
            error_handling="continue",
        )
    else:
        runner = AsyncRunner()

        async def run_graph():
            return await runner.run(
                graph,
                values,
                inspect=True,
                error_handling="continue",
            )

        result = _run_away_from_browser_loop(run_graph)

    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.set_content(render_run_inspection(result.inspect()._artifact))
    root = page.locator('[data-hypergraph-inspect="run"]')
    root.locator("[data-hg-timeline-row]").filter(has_text="review_customer").last.click()
    code_text = root.locator("[data-hg-detail] pre.hg-inspect-code code").inner_text()

    if runner_kind == "sync":
        assert "result = runner.run(" in code_text
        assert "await runner.run(" not in code_text
        namespace = {"runner": runner, "graph": graph, "values": values}
        exec(code_text, namespace)
    else:
        assert isinstance(runner, AsyncRunner)
        assert "result = await runner.run(" in code_text
        namespace = _run_away_from_browser_loop(
            lambda code_text=code_text: _execute_async_renderer_snippet(
                code_text,
                runner=runner,
                graph=graph,
                values=values,
            )
        )
    failure = namespace["failure"]
    assert failure.node_name == "review_customer"
    assert failure.inputs == values
    page.close()


@pytest.mark.parametrize("artifact_kind", ["run", "map"])
def test_full_renderer_unknown_runner_origin_never_emits_unbound_recovery_code(
    browser: Browser,
    artifact_kind: str,
) -> None:
    graph = _failed_renderer_run_graph()
    runner = SyncRunner()
    if artifact_kind == "run":
        settled = runner.run(
            graph,
            {"customer_id": "maya-23"},
            error_handling="continue",
        )
        artifact = settled.inspect()._artifact
        rendered = render_run_inspection(artifact)
    else:
        settled = runner.map(
            graph,
            {"customer_id": ["maya-23"]},
            map_over="customer_id",
            error_handling="continue",
        )
        artifact = settled.inspect()._artifact
        rendered = render_map_inspection(artifact)
    assert artifact._runner_kind is None

    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.set_content(rendered)
    root = page.locator(f'[data-hypergraph-inspect="{artifact_kind}"]')
    if artifact_kind == "map":
        root.get_by_role("button", name="Show failure").click()
    root.locator("[data-hg-timeline-row]").filter(has_text="review_customer").last.click()
    detail_text = root.locator("[data-hg-detail]").inner_text()

    assert "Recovery code unavailable" in detail_text
    assert "Runner kind was not captured." in detail_text
    assert "Smallest useful evidence" not in detail_text
    assert "runner.run(" not in detail_text
    assert "runner.map(" not in detail_text
    assert "failure = result.failure" not in detail_text
    assert "item.failure for item in batch.failures" not in detail_text
    page.close()


@pytest.mark.parametrize("runner_kind", ["sync", "async"])
def test_nested_map_full_renderer_snippets_execute_against_containing_outer_item(
    browser: Browser,
    runner_kind: str,
) -> None:
    graph = _nested_failure_batch_graph()
    values = _nested_failure_batch_values()
    if runner_kind == "sync":
        runner: SyncRunner | AsyncRunner = SyncRunner()
        batch = runner.map(
            graph,
            values,
            map_over="customer_id",
            inspect=True,
            error_handling="continue",
        )
    else:
        runner = AsyncRunner()

        async def run_batch():
            return await runner.map(
                graph,
                values,
                map_over="customer_id",
                inspect=True,
                error_handling="continue",
            )

        batch = _run_away_from_browser_loop(run_batch)

    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.set_content(render_map_inspection(batch.inspect()._artifact))
    root = page.locator('[data-hypergraph-inspect="map"]')

    for outer_index in (0, 1):
        root.get_by_role("button", name=re.compile(rf"Item {outer_index} failed")).click()
        failed_leaf = root.locator("[data-hg-timeline-row]").filter(has_text="review_group/review_customer").last
        failed_leaf.click()
        code_text = root.locator("[data-hg-detail] pre.hg-inspect-code code").inner_text()
        assert f"item.failure.item_index == {outer_index}" in code_text
        assert f"item.failure.item_index == {1 - outer_index}" not in code_text
        if runner_kind == "sync":
            namespace = {"runner": runner, "graph": graph, "values": values}
            exec(code_text, namespace)
            failure = namespace["failure"]
        else:
            assert isinstance(runner, AsyncRunner)
            namespace = _run_away_from_browser_loop(
                lambda code_text=code_text: _execute_async_renderer_snippet(
                    code_text,
                    runner=runner,
                    graph=graph,
                    values=values,
                )
            )
            failure = namespace["failure"]
        assert failure.item_index == outer_index
        assert failure.inputs == {"customer_id": f"reject-outer-{outer_index}"}

    page.close()


def test_nested_map_selected_failure_appears_once_across_inner_outer_index_projection(
    browser: Browser,
) -> None:
    graph = _nested_failure_batch_graph()
    values = _nested_failure_batch_values()
    batch = SyncRunner().map(
        graph,
        values,
        map_over="customer_id",
        inspect=True,
        error_handling="continue",
    )
    artifact = batch.inspect()._artifact
    for outer_index, item in enumerate(artifact.items):
        assert item.run is not None
        leaf = next(node for node in item.run.nodes if node.qualified_name == "review_group/review_customer" and node.status == "failed")
        assert leaf.failure is not None
        assert leaf.failure.item_index == 1
        assert item.run.failures[0].item_index == outer_index

    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.set_content(render_map_inspection(artifact))
    root = page.locator('[data-hypergraph-inspect="map"]')
    for outer_index in (0, 1):
        root.get_by_role("button", name=re.compile(rf"Item {outer_index} failed")).click()
        root.locator("[data-hg-timeline-row]").filter(has_text="review_group/review_customer").last.click()
        detail_text = root.locator("[data-hg-detail]").inner_text()

        assert detail_text.count(f"ValueError: manual review: reject-outer-{outer_index}") == 1
        assert "Run failures" not in detail_text

    page.close()


def test_failure_dedupe_removes_one_correlated_record_and_keeps_distinct_peers(
    browser: Browser,
) -> None:
    selected = FailureEvidence(
        node_name="review_customer",
        error=ValueError("manual review: maya-23"),
        inputs={"customer_id": "maya-23"},
        superstep=2,
        duration_ms=12.0,
        graph_name="customer-review",
        workflow_id="workflow-customers",
        item_index=None,
    )
    indistinguishable_peer = replace(selected)
    distinct_peer = replace(
        selected,
        error=ValueError("manual review: alex-10"),
        inputs={"customer_id": "alex-10"},
        duration_ms=18.0,
    )
    failed_node = NodeInspection(
        run_id="run-dedupe",
        span_id="span-review",
        node_name="review_customer",
        qualified_name="review_customer",
        graph_name="customer-review",
        item_index=None,
        superstep=2,
        sequence=0,
        status="failed",
        values_captured=True,
        inputs={"customer_id": "maya-23"},
        failure=selected,
        duration_ms=12.0,
    )
    artifact = RunInspection(
        run_id="run-dedupe",
        graph_name="customer-review",
        workflow_id="workflow-customers",
        item_index=None,
        status="failed",
        nodes=(failed_node,),
        failures=(selected, indistinguishable_peer, distinct_peer),
        total_duration_ms=30.0,
        captured=True,
        terminal=True,
        error=selected.error,
        _runner_kind="sync",
    )

    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.set_content(render_run_inspection(artifact))
    root = page.locator('[data-hypergraph-inspect="run"]')
    root.locator("[data-hg-timeline-row]").filter(has_text="review_customer").click()
    detail_text = root.locator("[data-hg-detail]").inner_text()

    assert "Run failures · 2" in detail_text
    assert detail_text.count("ValueError: manual review: maya-23") == 2
    assert detail_text.count("ValueError: manual review: alex-10") == 1
    page.close()


def test_item_pagination_keeps_original_map_identity(browser: Browser) -> None:
    items = tuple(
        MapItemInspection(
            item_index=index,
            status="running",
            requested_inputs={"customer_id": f"customer-{index}"},
        )
        for index in range(21)
    )
    artifact = MapInspection(
        run_id="batch-page",
        graph_name="customer_enrichment",
        workflow_id=None,
        status="running",
        map_over=("customer_id",),
        map_mode="zip",
        requested_count=21,
        items=items,
        unstarted_item_indexes=(),
        total_duration_ms=0.0,
        captured=True,
        terminal=False,
    )
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.set_content(render_map_inspection(artifact))
    root = page.locator('[data-hypergraph-inspect="map"]')

    assert root.locator('[data-item-index="0"]').is_visible()
    assert root.locator('[data-item-index="20"]').count() == 0
    root.get_by_role("button", name="Next").click()
    assert root.locator('[data-item-index="20"]').is_visible()
    assert root.locator("[data-hg-page-label]").inner_text() == "Page 2 of 2"
    page.close()


def test_hostile_values_are_inert_offline_and_responsive_at_360px(browser: Browser) -> None:
    attack = '</script><img id="owned" src="https://attacker.invalid/pixel" onerror="document.body.dataset.pwned=1">'

    class HostileRepr:
        def __repr__(self) -> str:
            raise RuntimeError("repr escaped")

    node = NodeInspection(
        run_id="run-security",
        span_id="span-security",
        node_name="unsafe-label",
        qualified_name="research/lookup</script>",
        graph_name="security",
        item_index=None,
        superstep=0,
        sequence=0,
        status="completed",
        values_captured=True,
        inputs={
            "event_handler": attack,
            "remote": "https://attacker.invalid/remote-image.png",
            "markdown": "[click me](https://attacker.invalid/markdown)",
            "invalid": "valid\ud800invalid",
            "hostile": HostileRepr(),
        },
        outputs={"safe": True},
        started_at_ms=0.0,
        ended_at_ms=1.0,
        duration_ms=1.0,
    )
    artifact = RunInspection(
        run_id="run-security",
        graph_name="customer</script><script>globalThis.owned=true</script>",
        workflow_id=None,
        item_index=None,
        status="completed",
        nodes=(node,),
        failures=(),
        total_duration_ms=1.0,
        captured=True,
        terminal=True,
    )
    rendered = render_run_inspection(artifact)
    assert attack not in rendered
    assert "\\u003c/script\\u003e" in rendered

    requests: list[str] = []
    errors: list[Exception] = []
    page = browser.new_page(viewport={"width": 360, "height": 780})
    page.on("request", lambda request: requests.append(request.url))
    page.on("pageerror", lambda error: errors.append(error))
    page.set_content(rendered)
    root = page.locator('[data-hypergraph-inspect="run"]')

    assert requests == []
    assert errors == []
    assert page.locator("#owned").count() == 0
    assert page.locator("img, a, iframe, link").count() == 0
    assert page.locator("body").get_attribute("data-pwned") is None
    assert page.evaluate("globalThis.owned") is None

    detail = root.locator("[data-hg-detail]")
    detail.get_by_text("Inputs · 5 values", exact=True).click()
    assert detail.get_by_text(attack, exact=True).is_visible()
    assert detail.get_by_text("https://attacker.invalid/remote-image.png", exact=True).is_visible()
    assert detail.get_by_text("[click me](https://attacker.invalid/markdown)", exact=True).is_visible()
    assert "invalid Unicode" in detail.inner_text()
    assert "repr failed (RuntimeError)" in detail.inner_text()

    graph_tab = root.get_by_role("tab", name="Graph")
    graph_tab.focus()
    graph_tab.press("Enter")
    assert graph_tab.get_attribute("aria-selected") == "true"
    assert root.evaluate("element => element.scrollWidth <= element.clientWidth") is True
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth") is True
    page.close()
