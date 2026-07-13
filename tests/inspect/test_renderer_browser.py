"""Real-browser falsifiers for the offline inspect renderer."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, sync_playwright

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


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as runtime:
        instance = runtime.chromium.launch(headless=True)
        yield instance
        instance.close()


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
    assert "failure = batch[1].failure" in root.locator("[data-hg-detail]").inner_text()

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
