"""Real-browser falsifiers for the notebook shell/channel bridge."""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from dataclasses import replace

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from hypergraph.runners._shared._inspect import (
    MapInspection,
    MapItemInspection,
    NodeInspection,
    RunInspection,
)
from hypergraph.runners._shared._inspect_serialization import (
    SerializedEntry,
    SerializedTable,
    SerializedTableRow,
    SerializedValue,
    serialize_value,
)
from hypergraph.runners._shared._inspect_transport import (
    INSPECTION_PROTOCOL_VERSION,
    InspectionDelivery,
    InspectionEnvelope,
    inspection_envelope_to_wire,
    render_notebook_shell,
    render_payload_channel,
)
from hypergraph.runners._shared.results import FailureEvidence


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as runtime:
        instance = runtime.chromium.launch(headless=True)
        yield instance
        instance.close()


def _node(index: int, *, item_index: int | None = None) -> NodeInspection:
    return NodeInspection(
        run_id=f"run-{item_index if item_index is not None else 'single'}",
        span_id=f"span-{item_index}-{index}",
        node_name=f"node_{index}",
        qualified_name=f"research/node_{index}",
        graph_name="research",
        item_index=item_index,
        superstep=index,
        sequence=index,
        status="completed",
        values_captured=True,
        inputs={"customer_id": f"customer-{item_index}", "step": index},
        outputs={"decision": "approved", "step": index * 2},
        started_at_ms=float(index * 20),
        ended_at_ms=float(index * 20 + 10),
        duration_ms=10.0,
    )


def _run(
    *,
    graph_name: str = "customer_enrichment",
    status: str = "running",
    terminal: bool = False,
    item_index: int | None = None,
    node_count: int = 2,
) -> RunInspection:
    return RunInspection(
        run_id=f"run-{item_index if item_index is not None else 'single'}",
        graph_name=graph_name,
        workflow_id="workflow-customers",
        item_index=item_index,
        status=status,
        nodes=tuple(_node(index, item_index=item_index) for index in range(node_count)),
        failures=(),
        total_duration_ms=float(node_count * 20),
        captured=True,
        terminal=terminal,
    )


def _map(*, terminal: bool = False, status: str = "running") -> MapInspection:
    items = tuple(
        MapItemInspection(
            item_index=index,
            status="completed",
            requested_inputs={"customer_id": f"customer-{index}"},
            run=_run(
                status="completed",
                terminal=True,
                item_index=index,
                node_count=3,
            ),
        )
        for index in range(30)
    )
    return MapInspection(
        run_id="map-customers",
        graph_name="customer_enrichment",
        workflow_id="workflow-customers",
        status=status,
        map_over=("customer_id",),
        map_mode="zip",
        requested_count=30,
        items=items,
        unstarted_item_indexes=(),
        total_duration_ms=600.0,
        captured=True,
        terminal=terminal,
    )


def _partial_customer_map() -> tuple[MapInspection, MapInspection]:
    completed_items = {
        index: MapItemInspection(
            item_index=index,
            status="completed",
            requested_inputs={"customer_id": customer_id},
            run=_run(
                graph_name="customer_review",
                status="completed",
                terminal=True,
                item_index=index,
                node_count=1,
            ),
        )
        for index, customer_id in ((0, "alex-10"), (2, "sam-04"))
    }
    running_node = NodeInspection(
        run_id="run-1",
        span_id="span-1-score",
        node_name="score_customer",
        qualified_name="score_customer",
        graph_name="customer_review",
        item_index=1,
        superstep=0,
        sequence=0,
        status="running",
        values_captured=True,
        inputs={"customer_id": "maya-23"},
        started_at_ms=20.0,
    )
    running_run = RunInspection(
        run_id="run-1",
        graph_name="customer_review",
        workflow_id="workflow-customers",
        item_index=1,
        status="running",
        nodes=(running_node,),
        failures=(),
        total_duration_ms=0.0,
        captured=True,
        terminal=False,
    )
    failure = FailureEvidence(
        node_name="score_customer",
        error=ValueError("Customer maya-23 requires manual review"),
        inputs={"customer_id": "maya-23"},
        superstep=0,
        duration_ms=25.0,
        graph_name="customer_review",
        workflow_id="workflow-customers",
        item_index=1,
    )
    failed_node = replace(
        running_node,
        status="failed",
        failure=failure,
        ended_at_ms=45.0,
        duration_ms=25.0,
    )
    failed_run = RunInspection(
        run_id="run-1",
        graph_name="customer_review",
        workflow_id="workflow-customers",
        item_index=1,
        status="failed",
        nodes=(failed_node,),
        failures=(failure,),
        total_duration_ms=25.0,
        captured=True,
        terminal=True,
        error=failure.error,
    )
    initial = MapInspection(
        run_id="map-customers",
        graph_name="customer_review",
        workflow_id="workflow-customers",
        status="running",
        map_over=("customer_id",),
        map_mode="zip",
        requested_count=3,
        items=(
            completed_items[0],
            MapItemInspection(1, "running", {"customer_id": "maya-23"}, running_run),
            completed_items[2],
        ),
        unstarted_item_indexes=(),
        total_duration_ms=20.0,
        captured=True,
        terminal=False,
    )
    terminal = replace(
        initial,
        status="partial",
        items=(
            completed_items[0],
            MapItemInspection(1, "failed", {"customer_id": "maya-23"}, failed_run),
            completed_items[2],
        ),
        total_duration_ms=45.0,
        terminal=True,
        revision=1,
    )
    return initial, terminal


def _envelope(
    artifact: RunInspection | MapInspection,
    *,
    widget_id: str,
    nonce: str,
    sequence: int,
    state: str = "live",
) -> InspectionEnvelope:
    label = {
        "live": "Live",
        "saved": "Saved snapshot",
        "stale": "Live inspection unavailable",
    }[state]
    return InspectionEnvelope(
        protocol_version=INSPECTION_PROTOCOL_VERSION,
        widget_id=widget_id,
        nonce=nonce,
        sequence=sequence,
        delivery=InspectionDelivery(state=state, label=label),  # type: ignore[arg-type]
        artifact=artifact,
    )


def _mount(page: Page, envelope: InspectionEnvelope, *, timeout_ms: int = 500) -> None:
    page.set_content(
        render_notebook_shell(envelope, handshake_timeout_ms=timeout_ms) + render_payload_channel(envelope),
        wait_until="load",
    )


def _frame(page: Page, widget_id: str):
    frame = page.frame(name=f"{widget_id}-frame")
    assert frame is not None
    frame.wait_for_selector("[data-hypergraph-inspect]")
    return frame


def _defer_child_ready(shell: str) -> str:
    match = re.search(r'srcdoc="([^"]*)"', shell)
    assert match is not None
    child_document = html.unescape(match.group(1))
    child_document, replacements = re.subn(
        r"window\.__hypergraphInspectTransport\.installChild\((\{.*?\})\);",
        r"window.__deferredHypergraphInspectConfig=\1;",
        child_document,
        count=1,
    )
    assert replacements == 1
    return shell[: match.start(1)] + html.escape(child_document, quote=True) + shell[match.end(1) :]


def _append_notebook_shell(page: Page, shell: str) -> None:
    scripts = re.findall(r"<script(?: [^>]*)?>(.*?)</script>", shell, flags=re.DOTALL)
    assert len(scripts) == 2
    page.evaluate(
        """markup => {
          const template = document.createElement('template');
          template.innerHTML = markup;
          for (const node of Array.from(template.content.childNodes)) {
            if (node.nodeName !== 'SCRIPT') document.body.appendChild(node);
          }
        }""",
        shell,
    )
    for script in scripts:
        page.add_script_tag(content=script)


def _replace_channel(page: Page, envelope: InspectionEnvelope) -> None:
    markup = render_payload_channel(envelope)
    page.evaluate(
        """markup => {
          const template = document.createElement('template');
          template.innerHTML = markup;
          const incoming = template.content.firstElementChild;
          const inertRuntime = incoming.querySelector('[data-hg-inspect-channel-runtime]');
          const runtime = inertRuntime.textContent;
          inertRuntime.remove();
          const logicalChannel = incoming.getAttribute('data-hg-inspect-channel');
          for (const old of document.querySelectorAll('[data-hg-inspect-channel]')) {
            if (old.getAttribute('data-hg-inspect-channel') === logicalChannel) old.remove();
          }
          document.body.appendChild(incoming);
          const executable = document.createElement('script');
          executable.setAttribute('data-hg-inspect-channel-runtime', '');
          executable.textContent = runtime;
          incoming.appendChild(executable);
        }""",
        markup,
    )


def _append_channel(page: Page, envelope: InspectionEnvelope) -> None:
    markup = render_payload_channel(envelope)
    page.evaluate(
        """markup => {
          const template = document.createElement('template');
          template.innerHTML = markup;
          const incoming = template.content.firstElementChild;
          const inertRuntime = incoming.querySelector('[data-hg-inspect-channel-runtime]');
          const runtime = inertRuntime.textContent;
          inertRuntime.remove();
          document.body.appendChild(incoming);
          const executable = document.createElement('script');
          executable.setAttribute('data-hg-inspect-channel-runtime', '');
          executable.textContent = runtime;
          incoming.appendChild(executable);
        }""",
        markup,
    )


def test_bridge_rejects_wrong_identity_source_and_non_monotonic_sequence(
    browser: Browser,
) -> None:
    page = browser.new_page()
    first = _envelope(
        _run(graph_name="first-graph"),
        widget_id="widget-one",
        nonce="nonce-one",
        sequence=1,
    )
    second = _envelope(
        _run(graph_name="second-graph"),
        widget_id="widget-two",
        nonce="nonce-two",
        sequence=1,
    )
    page.set_content(
        render_notebook_shell(first) + render_payload_channel(first) + render_notebook_shell(second) + render_payload_channel(second),
        wait_until="load",
    )
    first_frame = _frame(page, "widget-one")
    second_frame = _frame(page, "widget-two")
    assert first_frame.locator("[data-hg-title]").inner_text() == "first-graph"
    assert second_frame.locator("[data-hg-title]").inner_text() == "second-graph"

    accepted = _envelope(
        _run(graph_name="newest", status="failed"),
        widget_id="widget-one",
        nonce="nonce-one",
        sequence=3,
    )
    old = _envelope(
        _run(graph_name="old", status="completed"),
        widget_id="widget-one",
        nonce="nonce-one",
        sequence=2,
    )
    wrong_nonce = replace(old, nonce="wrong", sequence=4)
    wrong_version = inspection_envelope_to_wire(replace(old, sequence=5))
    wrong_version["version"] = 2

    for message in (
        inspection_envelope_to_wire(accepted),
        inspection_envelope_to_wire(old),
        inspection_envelope_to_wire(wrong_nonce),
        wrong_version,
        inspection_envelope_to_wire(replace(accepted, widget_id="widget-two", sequence=6)),
    ):
        page.evaluate(
            "([name, message]) => window.frames[name].postMessage(message, '*')",
            ["widget-one-frame", message],
        )

    assert first_frame.locator("[data-hg-title]").inner_text() == "newest"
    assert first_frame.locator("[data-hg-summary]").get_by_text("failed", exact=True).is_visible()
    assert second_frame.locator("[data-hg-title]").inner_text() == "second-graph"

    attacker_message = inspection_envelope_to_wire(
        _envelope(
            _run(graph_name="attacker", status="completed"),
            widget_id="widget-one",
            nonce="nonce-one",
            sequence=7,
        )
    )
    page.evaluate(
        """([name, message]) => {
          const attacker = document.createElement('iframe');
          document.body.appendChild(attacker);
          attacker.contentWindow.eval(
            `parent.frames[${JSON.stringify(name)}].postMessage(${JSON.stringify(message)}, '*')`
          );
        }""",
        ["widget-one-frame", attacker_message],
    )

    assert first_frame.locator("[data-hg-title]").inner_text() == "newest"
    page.close()


def test_payload_updates_preserve_iframe_identity_and_local_renderer_state(
    browser: Browser,
) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    initial = _envelope(
        _map(),
        widget_id="widget-state",
        nonce="nonce-state",
        sequence=1,
    )
    _mount(page, initial)
    frame = _frame(page, "widget-state")
    page.evaluate(
        """() => {
          const frame = document.querySelector('[data-hg-inspect-frame="widget-state"]');
          window.__frameBefore = frame;
          window.__windowBefore = frame.contentWindow;
          window.__srcdocBefore = frame.getAttribute('srcdoc');
        }"""
    )
    before = frame.evaluate(
        """() => {
          const root = document.querySelector('[data-hypergraph-inspect]');
          const state = root.__hypergraphInspect.state;
          state.activeView = 'graph';
          state.selectedItem = 21;
          state.selectedExecution = 'run-21|span-21-1|1|1';
          state.filter = 'completed';
          state.page = 2;
          state.detailsOpen['node.run-21|span-21-1|1|1.inputs'] = true;
          state.tablePages['node.21.table'] = 2;
          state.graphViewport.zoom = 130;
          state.graphViewport.panX = 8;
          state.graphViewport.panY = 12;
          state.graphViewport.expanded['research'] = true;
          document.querySelector('[data-hg-items]').scrollTop = 18;
          document.querySelector('[data-hg-main]').scrollTop = 24;
          document.querySelector('[data-hg-detail]').scrollTop = 12;
          return JSON.parse(JSON.stringify(state));
        }"""
    )

    updated = _envelope(
        _map(status="completed"),
        widget_id="widget-state",
        nonce="nonce-state",
        sequence=2,
    )
    _replace_channel(page, updated)
    frame.wait_for_function("document.querySelector('[data-hypergraph-inspect]').__hypergraphInspect.payload().map.status === 'completed'")
    after = frame.evaluate(
        """() => JSON.parse(JSON.stringify(
          document.querySelector('[data-hypergraph-inspect]').__hypergraphInspect.state
        ))"""
    )

    assert page.evaluate(
        """() => {
          const frame = document.querySelector('[data-hg-inspect-frame="widget-state"]');
          return frame === window.__frameBefore
            && frame.contentWindow === window.__windowBefore
            && frame.getAttribute('srcdoc') === window.__srcdocBefore;
        }"""
    )
    assert page.locator('[data-hg-inspect-channel="widget-state"]').count() == 1
    for key in ("activeView", "selectedItem", "selectedExecution", "filter", "page"):
        assert after[key] == before[key]
    assert after["detailsOpen"] == before["detailsOpen"]
    assert after["tablePages"] == before["tablePages"]
    assert after["graphViewport"] == before["graphViewport"]
    assert after["scroll"] == before["scroll"]
    page.close()


def test_saved_terminal_two_output_replay_is_interactive_without_a_kernel(
    browser: Browser,
) -> None:
    initial = _envelope(
        _run(),
        widget_id="widget-saved",
        nonce="nonce-saved",
        sequence=1,
    )
    terminal = _envelope(
        _run(status="completed", terminal=True),
        widget_id="widget-saved",
        nonce="nonce-saved",
        sequence=2,
        state="saved",
    )
    saved_outputs = render_notebook_shell(initial) + render_payload_channel(terminal)

    page = browser.new_page()
    page.set_content(saved_outputs, wait_until="load")
    frame = _frame(page, "widget-saved")

    assert frame.get_by_text("Saved snapshot", exact=True).is_visible()
    assert frame.locator("[data-hg-summary]").get_by_text("completed", exact=True).is_visible()
    frame.get_by_role("tab", name="Graph").click()
    assert frame.locator('[data-hg-panel="graph"]').is_visible()
    frame.get_by_role("tab", name="Timeline").click()
    assert frame.locator("[data-hg-timeline-row]").count() == 2
    page.close()


def test_append_mode_terminal_replay_preserves_iframe_state_and_exposes_failure(
    browser: Browser,
) -> None:
    initial_artifact, terminal_artifact = _partial_customer_map()
    initial = _envelope(
        initial_artifact,
        widget_id="widget-append-live",
        nonce="nonce-append-live",
        sequence=1,
    )
    terminal = _envelope(
        terminal_artifact,
        widget_id="widget-append-live",
        nonce="nonce-append-live",
        sequence=2,
        state="saved",
    )
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    _mount(page, initial)
    frame = _frame(page, "widget-append-live")
    frame.get_by_role("tab", name="Graph").click()
    page.evaluate(
        """() => {
          const frame = document.querySelector('[data-hg-inspect-frame="widget-append-live"]');
          window.__appendFrameBefore = frame;
          window.__appendWindowBefore = frame.contentWindow;
          window.__appendSrcdocBefore = frame.getAttribute('srcdoc');
        }"""
    )

    _append_channel(page, terminal)
    root = frame.locator('[data-hypergraph-inspect="map"]')
    root.get_by_text("Saved snapshot", exact=True).wait_for(timeout=2_000)

    assert page.locator('[data-hg-inspect-frame="widget-append-live"]').count() == 1
    assert page.evaluate(
        """() => {
          const frame = document.querySelector('[data-hg-inspect-frame="widget-append-live"]');
          return frame === window.__appendFrameBefore
            && frame.contentWindow === window.__appendWindowBefore
            && frame.getAttribute('srcdoc') === window.__appendSrcdocBefore;
        }"""
    )
    assert root.get_by_role("tab", name="Graph").get_attribute("aria-selected") == "true"
    assert root.get_by_text("Saved snapshot", exact=True).is_visible()
    assert "2 completed" in root.inner_text()
    assert "1 failed" in root.inner_text()

    root.get_by_role("button", name="Show failure").click()
    detail = root.locator("[data-hg-detail]")
    assert detail.get_by_text("maya-23", exact=True).is_visible()
    assert "Customer maya-23 requires manual review" in detail.inner_text()
    page.close()


def test_saved_append_replay_uses_highest_authenticated_sequence_without_kernel(
    browser: Browser,
) -> None:
    initial_artifact, terminal_artifact = _partial_customer_map()
    initial = _envelope(
        initial_artifact,
        widget_id="widget-append-saved",
        nonce="nonce-append-saved",
        sequence=1,
    )
    terminal = _envelope(
        terminal_artifact,
        widget_id="widget-append-saved",
        nonce="nonce-append-saved",
        sequence=3,
        state="saved",
    )
    late_older = replace(initial, sequence=2)
    saved_outputs = (
        render_notebook_shell(initial) + render_payload_channel(initial) + render_payload_channel(terminal) + render_payload_channel(late_older)
    )

    page = browser.new_page()
    page.set_content(saved_outputs, wait_until="load")
    frame = _frame(page, "widget-append-saved")
    root = frame.locator('[data-hypergraph-inspect="map"]')

    assert frame.evaluate("window.__hypergraphInspectBridgeState.lastSequence") == 3
    assert root.get_by_text("Saved snapshot", exact=True).is_visible()
    assert "2 completed" in root.inner_text()
    assert "1 failed" in root.inner_text()
    root.get_by_role("button", name="Show failure").click()
    assert root.locator("[data-hg-detail]").get_by_text("maya-23", exact=True).is_visible()
    page.close()


def test_parent_accepts_ready_only_from_expected_frame_with_exact_identity(
    browser: Browser,
) -> None:
    initial = _envelope(
        _run(),
        widget_id="widget-ready",
        nonce="nonce-ready",
        sequence=1,
    )
    shell = render_notebook_shell(initial, handshake_timeout_ms=2_000)
    shell_without_child_bridge = re.sub(
        r'srcdoc="[^"]*"',
        'srcdoc="&lt;!doctype html&gt;&lt;html&gt;&lt;body&gt;ready source test&lt;/body&gt;&lt;/html&gt;"',
        shell,
        count=1,
    )
    page = browser.new_page()
    page.set_content(shell_without_child_bridge, wait_until="load")
    expected = page.frame(name="widget-ready-frame")
    assert expected is not None
    key = "widget-ready::nonce-ready"
    assert page.evaluate("key => window.__hypergraphInspectHosts[key].ready", key) is False

    page.evaluate(
        """() => {
          const attacker = document.createElement('iframe');
          attacker.name = 'ready-attacker';
          document.body.appendChild(attacker);
        }"""
    )
    attacker = page.frame(name="ready-attacker")
    assert attacker is not None
    exact_ready = {
        "type": "hypergraph.inspect.ready",
        "version": 1,
        "widget_id": "widget-ready",
        "nonce": "nonce-ready",
    }
    attacker.evaluate("message => parent.postMessage(message, '*')", exact_ready)
    assert page.evaluate("key => window.__hypergraphInspectHosts[key].ready", key) is False

    wrong_messages = [
        {**exact_ready, "version": 2},
        {**exact_ready, "widget_id": "widget-other"},
        {**exact_ready, "nonce": "nonce-other"},
    ]
    for message in wrong_messages:
        expected.evaluate("value => parent.postMessage(value, '*')", message)
    assert page.evaluate("key => window.__hypergraphInspectHosts[key].ready", key) is False

    expected.evaluate("message => parent.postMessage(message, '*')", exact_ready)

    assert page.evaluate("key => window.__hypergraphInspectHosts[key].ready", key) is True
    assert page.evaluate("key => window.__hypergraphInspectHosts[key].readyCount", key) == 1
    assert page.locator('[data-hg-inspect-host-status="widget-ready"]').is_hidden()
    page.close()


def test_pre_ready_queue_keeps_newest_sequence_when_older_arrives_late(
    browser: Browser,
) -> None:
    initial = _envelope(
        _run(graph_name="initial"),
        widget_id="widget-pre-ready-order",
        nonce="nonce-pre-ready-order",
        sequence=1,
    )
    page = browser.new_page()
    page.set_content(
        _defer_child_ready(render_notebook_shell(initial, handshake_timeout_ms=2_000)) + render_payload_channel(initial),
        wait_until="load",
    )
    frame = _frame(page, "widget-pre-ready-order")
    key = "widget-pre-ready-order::nonce-pre-ready-order"
    assert page.evaluate("key => window.__hypergraphInspectHosts[key].ready", key) is False

    newest = _envelope(
        _run(graph_name="newest", status="failed"),
        widget_id="widget-pre-ready-order",
        nonce="nonce-pre-ready-order",
        sequence=3,
    )
    late_older = _envelope(
        _run(graph_name="late-older", status="completed"),
        widget_id="widget-pre-ready-order",
        nonce="nonce-pre-ready-order",
        sequence=2,
    )
    _replace_channel(page, newest)
    _replace_channel(page, late_older)

    frame.evaluate(
        """() => window.__hypergraphInspectTransport.installChild(
          window.__deferredHypergraphInspectConfig
        )"""
    )
    frame.get_by_text("newest", exact=True).wait_for(timeout=1_000)

    assert frame.locator("[data-hg-title]").inner_text() == "newest"
    assert frame.locator("[data-hg-summary]").get_by_text("failed", exact=True).is_visible()
    assert frame.evaluate("window.__hypergraphInspectBridgeState.lastSequence") == 3
    page.close()


def test_channel_before_host_keeps_newest_sequence_until_ready(
    browser: Browser,
) -> None:
    newest = _envelope(
        _run(graph_name="newest-before-host", status="failed"),
        widget_id="widget-channel-first",
        nonce="nonce-channel-first",
        sequence=3,
    )
    late_older = _envelope(
        _run(graph_name="late-older-before-host", status="completed"),
        widget_id="widget-channel-first",
        nonce="nonce-channel-first",
        sequence=2,
    )
    initial = _envelope(
        _run(graph_name="initial-shell"),
        widget_id="widget-channel-first",
        nonce="nonce-channel-first",
        sequence=1,
    )
    page = browser.new_page()
    page.set_content(render_payload_channel(newest), wait_until="load")
    channel_only_fallback = page.locator('[data-hg-inspect-channel-fallback="widget-channel-first"]')

    assert channel_only_fallback.locator("strong").inner_text() == "Waiting for live inspection"
    assert channel_only_fallback.get_attribute("data-delivery-state") == "waiting"

    _replace_channel(page, late_older)
    _append_notebook_shell(
        page,
        _defer_child_ready(render_notebook_shell(initial, handshake_timeout_ms=2_000)),
    )
    frame = _frame(page, "widget-channel-first")
    fallback = page.locator('[data-hg-inspect-channel-fallback="widget-channel-first"]')

    assert fallback.locator("strong").inner_text() == "Waiting for live inspection"
    assert fallback.get_attribute("data-delivery-state") == "waiting"
    frame.evaluate(
        """() => window.__hypergraphInspectTransport.installChild(
          window.__deferredHypergraphInspectConfig
        )"""
    )
    frame.get_by_text("newest-before-host", exact=True).wait_for(timeout=1_000)

    assert frame.locator("[data-hg-title]").inner_text() == "newest-before-host"
    assert frame.locator("[data-hg-summary]").get_by_text("failed", exact=True).is_visible()
    assert frame.get_by_text("Live", exact=True).is_visible()
    assert frame.locator("[data-hypergraph-inspect]").get_attribute("data-delivery-state") == "live"
    assert frame.evaluate("window.__hypergraphInspectBridgeState.lastSequence") == 3
    assert fallback.is_hidden()
    page.close()


def test_start_failure_keeps_the_exact_bounded_error_visible(browser: Browser) -> None:
    initial = _envelope(
        _run(),
        widget_id="widget-start-error",
        nonce="nonce-start-error",
        sequence=1,
    )
    failed = replace(
        initial,
        sequence=2,
        delivery=InspectionDelivery(
            state="stale",
            label="Live inspection unavailable",
        ),
        message=serialize_value(ValueError("missing required input: customer_id")),
    )
    page = browser.new_page()
    page.set_content(
        render_notebook_shell(initial) + render_payload_channel(failed),
        wait_until="load",
    )
    frame = _frame(page, "widget-start-error")
    fallback = page.locator('[data-hg-inspect-channel-fallback="widget-start-error"]')

    assert frame.get_by_text("Live inspection unavailable", exact=True).is_visible()
    assert fallback.is_visible()
    assert "ValueError: missing required input: customer_id" in fallback.inner_text()
    page.close()


def test_shell_without_a_payload_channel_never_claims_to_be_live(browser: Browser) -> None:
    initial = _envelope(
        _run(),
        widget_id="widget-orphan",
        nonce="nonce-orphan",
        sequence=1,
    )
    page = browser.new_page()
    page.set_content(render_notebook_shell(initial), wait_until="load")
    frame = _frame(page, "widget-orphan")

    assert frame.get_by_text("Waiting for live inspection", exact=True).is_visible()
    assert frame.locator("[data-hypergraph-inspect]").get_attribute("data-delivery-state") == "stale"
    assert frame.get_by_text(
        "Live updates are unavailable. Showing the last confirmed snapshot; this view is not live.",
        exact=True,
    ).is_visible()
    page.close()


def test_missing_ready_handshake_marks_live_fallback_stale_without_refresh(
    browser: Browser,
) -> None:
    running = _envelope(
        _run(status="running"),
        widget_id="widget-live-stale",
        nonce="nonce-live-stale",
        sequence=1,
    )
    shell = render_notebook_shell(running, handshake_timeout_ms=500)
    broken_shell = re.sub(
        r'srcdoc="[^"]*"',
        'srcdoc="&lt;!doctype html&gt;&lt;html&gt;&lt;body&gt;no bridge&lt;/body&gt;&lt;/html&gt;"',
        shell,
        count=1,
    )
    requests: list[str] = []
    page = browser.new_page()
    page.on("request", lambda request: requests.append(request.url))
    page.set_content(broken_shell + render_payload_channel(running), wait_until="load")
    page.evaluate(
        """() => {
          const frame = document.querySelector('[data-hg-inspect-frame="widget-live-stale"]');
          window.__frameBeforeTimeout = frame;
          window.__windowBeforeTimeout = frame.contentWindow;
          window.__srcdocBeforeTimeout = frame.getAttribute('srcdoc');
        }"""
    )

    status = page.locator('[data-hg-inspect-host-status="widget-live-stale"]')
    fallback = page.locator('[data-hg-inspect-channel-fallback="widget-live-stale"]')

    assert status.get_attribute("data-state") == "connecting"
    assert fallback.is_visible()
    assert fallback.locator("strong").inner_text() == "Waiting for live inspection"
    assert fallback.get_attribute("data-delivery-state") == "waiting"
    assert "customer_enrichment is running" in fallback.inner_text()
    assert "2 captured nodes" in fallback.inner_text()

    page.wait_for_function("document.querySelector('[data-hg-inspect-host-status=\"widget-live-stale\"]').dataset.state === 'stale'")

    assert "saved snapshot" in status.inner_text().lower()
    assert "not live" in status.inner_text().lower()
    assert fallback.is_visible()
    assert fallback.locator("strong").inner_text() == "Live inspection unavailable"
    assert fallback.get_attribute("data-delivery-state") == "stale"
    assert "customer_enrichment is running" in fallback.inner_text()
    assert "2 captured nodes" in fallback.inner_text()
    assert page.evaluate(
        """() => {
          const frame = document.querySelector('[data-hg-inspect-frame="widget-live-stale"]');
          return frame === window.__frameBeforeTimeout
            && frame.contentWindow === window.__windowBeforeTimeout
            && frame.getAttribute('srcdoc') === window.__srcdocBeforeTimeout;
        }"""
    )
    assert all(url in {"about:blank", "about:srcdoc"} for url in requests)
    page.close()


def test_missing_ready_handshake_exposes_stale_saved_fallback(browser: Browser) -> None:
    terminal = _envelope(
        _run(status="completed", terminal=True),
        widget_id="widget-stale",
        nonce="nonce-stale",
        sequence=1,
        state="saved",
    )
    shell = render_notebook_shell(terminal, handshake_timeout_ms=25)
    broken_shell = re.sub(
        r'srcdoc="[^"]*"',
        'srcdoc="&lt;!doctype html&gt;&lt;html&gt;&lt;body&gt;no bridge&lt;/body&gt;&lt;/html&gt;"',
        shell,
        count=1,
    )
    page = browser.new_page()
    page.set_content(broken_shell + render_payload_channel(terminal), wait_until="load")

    status = page.locator('[data-hg-inspect-host-status="widget-stale"]')
    status.wait_for(state="visible")
    page.wait_for_function("document.querySelector('[data-hg-inspect-host-status=\"widget-stale\"]').dataset.state === 'stale'")
    assert "interactive inspector did not connect" in status.inner_text().lower()
    fallback = page.locator('[data-hg-inspect-channel-fallback="widget-stale"]')
    assert "Saved snapshot" in fallback.inner_text()
    assert "completed" in fallback.inner_text()
    page.close()


def test_inexact_heterogeneous_table_width_renders_as_a_lower_bound(browser: Browser) -> None:
    rows = [
        {f"column-{index}": index for index in range(21)},
        {f"column-{index}": index + 100 for index in range(21)},
    ]
    node = replace(_node(0), outputs={"rows": rows})
    artifact = replace(
        _run(status="completed", terminal=True, node_count=1),
        nodes=(node,),
    )
    envelope = _envelope(
        artifact,
        widget_id="widget-table-lower-bound",
        nonce="nonce-table-lower-bound",
        sequence=1,
        state="saved",
    )
    page = browser.new_page()
    _mount(page, envelope)
    frame = _frame(page, "widget-table-lower-bound")

    summaries = frame.locator("summary").all_text_contents()
    assert "2 × ≥21 table" in summaries
    assert "2 × 21 table" not in summaries
    page.close()


def test_js_unsafe_table_counts_render_as_exact_decimal_text(
    browser: Browser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe_count = 2**53 + 1
    unsafe_table = SerializedValue(
        kind="table",
        type_name="ndarray",
        table=SerializedTable(
            columns=(serialize_value(0),),
            rows=(SerializedTableRow(cells=(serialize_value(1),)),),
            original_row_count=unsafe_count,
            original_column_count=unsafe_count,
            original_column_count_exact=True,
            rows_truncated=True,
            columns_truncated=True,
        ),
        original_size=unsafe_count,
        truncated=True,
    )
    serialized_outputs = SerializedValue(
        kind="mapping",
        type_name="dict",
        entries=(
            SerializedEntry(
                key=serialize_value("huge_table"),
                value=unsafe_table,
            ),
        ),
        original_size=1,
    )
    node = replace(_node(0), outputs={"huge_table": "prebuilt"})
    marker = node.outputs
    ordinary_serialize = serialize_value

    def serialize_prebuilt_output(value: object) -> SerializedValue:
        if value is marker:
            return serialized_outputs
        return ordinary_serialize(value)

    monkeypatch.setattr(
        "hypergraph.runners._shared._inspect_html.serialize_value",
        serialize_prebuilt_output,
    )

    artifact = replace(
        _run(status="completed", terminal=True, node_count=1),
        nodes=(node,),
    )
    envelope = _envelope(
        artifact,
        widget_id="widget-js-unsafe-count",
        nonce="nonce-js-unsafe-count",
        sequence=1,
        state="saved",
    )
    page = browser.new_page()
    _mount(page, envelope)
    frame = _frame(page, "widget-js-unsafe-count")

    summaries = frame.locator("summary").all_text_contents()
    assert f"{unsafe_count} × {unsafe_count} table" in summaries
    assert "9007199254740992" not in " ".join(summaries)
    truncations = frame.locator(".hg-inspect-truncated").all_text_contents()
    assert any(f"original size {unsafe_count}" in message for message in truncations)
    page.close()


@pytest.mark.parametrize("width", [1280, 360])
def test_transport_is_offline_inert_and_fits_the_viewport(
    browser: Browser,
    width: int,
) -> None:
    errors: list[Exception] = []
    requests: list[str] = []
    page = browser.new_page(viewport={"width": width, "height": 800})
    page.on("pageerror", lambda error: errors.append(error))
    page.on("request", lambda request: requests.append(request.url))
    hostile = _envelope(
        _run(graph_name='</script><img src="https://remote.invalid/x" onerror="window.pwned=1">'),
        widget_id=f"widget-{width}",
        nonce=f"nonce-{width}",
        sequence=1,
    )
    _mount(page, hostile)
    frame = _frame(page, f"widget-{width}")

    assert errors == []
    assert all(url in {"about:blank", "about:srcdoc"} for url in requests)
    assert page.evaluate("window.pwned") is None
    assert frame.evaluate("window.pwned") is None
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert frame.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert frame.locator('meta[http-equiv="Content-Security-Policy"]').get_attribute("content") == (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; "
        "object-src 'none'; base-uri 'none'; form-action 'none'"
    )
    page.close()
