"""Notebook display-boundary tests for live inspection transport."""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import pytest

from hypergraph.runners._shared import _inspect_transport as inspect_transport
from hypergraph.runners._shared._inspect import InspectionSession, RunInspection
from hypergraph.runners._shared._inspect_transport import (
    INSPECTION_PROTOCOL_VERSION,
    InspectionDelivery,
    InspectionEnvelope,
    NotebookInspectionTransport,
    OwnerThreadScheduler,
    _IPythonNotebookDisplay,
    open_notebook_inspection_transport,
    render_payload_channel,
)


def _artifact(*, terminal: bool = False, status: str = "running") -> RunInspection:
    return RunInspection(
        run_id="run-notebook",
        graph_name="customer_enrichment",
        workflow_id="workflow-customers",
        item_index=None,
        status=status,
        nodes=(),
        failures=(),
        total_duration_ms=20.0 if terminal else 0.0,
        captured=True,
        terminal=terminal,
    )


@dataclass
class _Call:
    deadline: float
    callback: Callable[[], None]
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


class _QueuedOwnerScheduler:
    def __init__(
        self,
        *,
        supports_delayed_calls: bool = True,
        supports_cross_thread: bool = True,
    ) -> None:
        self.current = 0.0
        self.owner_thread_id = threading.get_ident()
        self.supports_delayed_calls = supports_delayed_calls
        self.supports_cross_thread = supports_cross_thread
        self.calls: list[_Call] = []

    def now(self) -> float:
        return self.current

    def call_at(self, deadline: float, callback: Callable[[], None]) -> _Call:
        call = _Call(deadline, callback)
        self.calls.append(call)
        return call

    def run_due(self) -> None:
        pending, self.calls = self.calls, []
        for call in pending:
            if not call.cancelled and call.deadline <= self.current:
                call.callback()
            elif not call.cancelled:
                self.calls.append(call)

    def advance(self, seconds: float) -> None:
        self.current += seconds
        self.run_due()


class _RejectingOwnerScheduler(_QueuedOwnerScheduler):
    def call_at(self, _deadline: float, _callback: Callable[[], None]) -> None:
        return None


class _FakeDisplayHandle:
    def __init__(self, *, fail_update: bool = False) -> None:
        self.updates: list[str] = []
        self.update_attempt_threads: list[int] = []
        self.update_threads: list[int] = []
        self.fail_update = fail_update

    def update(self, markup: str) -> None:
        self.update_attempt_threads.append(threading.get_ident())
        if self.fail_update:
            raise RuntimeError("display update failed")
        self.updates.append(markup)
        self.update_threads.append(threading.get_ident())


class _FakeNotebookDisplay:
    def __init__(self, *, fail_channel: bool = False, fail_update: bool = False) -> None:
        self.shells: list[str] = []
        self.channels: list[tuple[str, str]] = []
        self.handle = _FakeDisplayHandle(fail_update=fail_update)
        self.fail_channel = fail_channel

    def display_shell(self, markup: str) -> None:
        self.shells.append(markup)

    def display_channel(self, markup: str, *, display_id: str) -> _FakeDisplayHandle:
        if self.fail_channel:
            raise RuntimeError("notebook display unavailable")
        self.channels.append((display_id, markup))
        return self.handle


def _wire(markup: str) -> dict[str, object]:
    match = re.search(
        r'<script type="application/json" data-hg-inspect-envelope>(.*?)</script>',
        markup,
        flags=re.DOTALL,
    )
    assert match is not None
    value = json.loads(match.group(1))
    assert isinstance(value, dict)
    return value


def test_terminal_channel_builds_one_wire_for_bridge_and_portable_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = InspectionEnvelope(
        protocol_version=INSPECTION_PROTOCOL_VERSION,
        widget_id="hg-inspect-one-wire",
        nonce="nonce-one-wire",
        sequence=2,
        delivery=InspectionDelivery(state="saved", label="Saved snapshot"),
        artifact=_artifact(terminal=True, status="completed"),
    )
    real_to_wire = inspect_transport.inspection_envelope_to_wire
    calls = 0

    def counted_to_wire(value: InspectionEnvelope) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return real_to_wire(value)

    monkeypatch.setattr(inspect_transport, "inspection_envelope_to_wire", counted_to_wire)

    markup = render_payload_channel(envelope)

    assert calls == 1
    assert 'data-hg-inspect-portable-frame="hg-inspect-one-wire"' in markup
    assert _wire(markup)["sequence"] == 2


@pytest.mark.parametrize("reported_version", [None, "0.1.1a5"], ids=["missing", "unrecognized"])
def test_ipython_channel_uses_display_handle_updates_unless_exact_broken_version(
    monkeypatch: pytest.MonkeyPatch,
    reported_version: str | None,
) -> None:
    from IPython.display import HTML

    calls: list[tuple[object, str]] = []
    updates: list[object] = []

    class DisplayHandle:
        def update(self, value: object) -> None:
            updates.append(value)

    def package_version(distribution_name: str) -> str:
        assert distribution_name == "jupyter-server-nbmodel"
        if reported_version is None:
            raise importlib.metadata.PackageNotFoundError(distribution_name)
        return reported_version

    def display(value: object, *, display_id: str) -> DisplayHandle:
        calls.append((value, display_id))
        return DisplayHandle()

    monkeypatch.setattr(importlib.metadata, "version", package_version)
    monkeypatch.setattr("IPython.display.display", display)
    notebook_display = _IPythonNotebookDisplay()

    handle = notebook_display.display_channel(
        "<div>initial payload</div>",
        display_id="hg-inspect-logical-payload",
    )
    handle.update("<div>later payload</div>")

    assert [display_id for _, display_id in calls] == ["hg-inspect-logical-payload"]
    assert all(isinstance(value, HTML) for value, _ in calls)
    assert [value.data for value, _ in calls if isinstance(value, HTML)] == [
        "<div>initial payload</div>",
    ]
    assert [value.data for value in updates if isinstance(value, HTML)] == [
        "<div>later payload</div>",
    ]


def test_ipython_channel_resends_updates_only_for_exact_broken_server_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from IPython.display import HTML

    calls: list[tuple[object, str]] = []
    updates: list[object] = []

    class DisplayHandle:
        def update(self, value: object) -> None:
            updates.append(value)

    def package_version(distribution_name: str) -> str:
        assert distribution_name == "jupyter-server-nbmodel"
        return "0.1.1a4"

    def display(value: object, *, display_id: str) -> DisplayHandle:
        calls.append((value, display_id))
        return DisplayHandle()

    monkeypatch.setattr(importlib.metadata, "version", package_version)
    monkeypatch.setattr("IPython.display.display", display)
    notebook_display = _IPythonNotebookDisplay()

    handle = notebook_display.display_channel(
        "<div>initial payload</div>",
        display_id="hg-inspect-logical-payload",
    )
    handle.update("<div>later payload</div>")

    assert [display_id for _, display_id in calls] == [
        "hg-inspect-logical-payload",
        "hg-inspect-logical-payload",
    ]
    assert all(isinstance(value, HTML) for value, _ in calls)
    assert [value.data for value, _ in calls if isinstance(value, HTML)] == [
        "<div>initial payload</div>",
        "<div>later payload</div>",
    ]
    assert updates == []


@pytest.mark.parametrize(
    ("reported_version", "expected_display_calls", "expected_handle_updates"),
    [(None, 2, 2), ("0.1.1a4", 4, 0)],
    ids=["capable-update", "exact-append"],
)
def test_ipython_transport_keeps_physical_output_shape_and_only_terminal_is_portable(
    monkeypatch: pytest.MonkeyPatch,
    reported_version: str | None,
    expected_display_calls: int,
    expected_handle_updates: int,
) -> None:
    from IPython.display import HTML

    calls: list[tuple[object, str | None]] = []
    updates: list[object] = []

    class DisplayHandle:
        def update(self, value: object) -> None:
            updates.append(value)

    def package_version(distribution_name: str) -> str:
        assert distribution_name == "jupyter-server-nbmodel"
        if reported_version is None:
            raise importlib.metadata.PackageNotFoundError(distribution_name)
        return reported_version

    def display(value: object, *, display_id: str | None = None) -> DisplayHandle:
        calls.append((value, display_id))
        return DisplayHandle()

    monkeypatch.setattr(importlib.metadata, "version", package_version)
    monkeypatch.setattr("IPython.display.display", display)
    scheduler = _QueuedOwnerScheduler()
    transport = NotebookInspectionTransport.create(
        _artifact(),
        display=_IPythonNotebookDisplay(),
        scheduler=scheduler,
        widget_id="hg-inspect-ipython-shape",
        nonce="nonce-ipython-shape",
    )
    transport.publish(_artifact(status="running"), urgent=False)
    scheduler.advance(0.25)
    transport.publish(_artifact(status="completed", terminal=True), urgent=True)
    scheduler.run_due()

    assert len(calls) == expected_display_calls
    assert len(updates) == expected_handle_updates
    if reported_version is None:
        assert [display_id for _, display_id in calls] == [None, "hg-inspect-ipython-shape-payload"]
        channel_values = [calls[1][0], *updates]
    else:
        assert [display_id for _, display_id in calls] == [
            None,
            "hg-inspect-ipython-shape-payload",
            "hg-inspect-ipython-shape-payload",
            "hg-inspect-ipython-shape-payload",
        ]
        channel_values = [value for value, _ in calls[1:]]
    channel_markups = [value.data for value in channel_values if isinstance(value, HTML)]
    assert len(channel_markups) == 3
    assert all("data-hg-inspect-portable-frame" not in markup for markup in channel_markups[:2])
    assert all("data-hg-inspect-native-summary" not in markup for markup in channel_markups[:2])
    assert 'data-hg-inspect-portable-frame="hg-inspect-ipython-shape"' in channel_markups[2]
    assert 'data-hg-inspect-native-summary="hg-inspect-ipython-shape"' in channel_markups[2]


def test_payload_channels_have_sequence_qualified_dom_ids_and_local_script_lookup() -> None:
    scheduler = _QueuedOwnerScheduler()
    display = _FakeNotebookDisplay()
    transport = NotebookInspectionTransport.create(
        _artifact(),
        display=display,
        scheduler=scheduler,
        widget_id="hg-inspect-sequenced",
        nonce="nonce-sequenced",
    )

    transport.publish(_artifact(status="completed", terminal=True), urgent=True)
    scheduler.run_due()
    channels = [display.channels[0][1], display.handle.updates[0]]

    assert 'id="hg-inspect-sequenced-payload-output-s1"' in channels[0]
    assert 'id="hg-inspect-sequenced-payload-output-s2"' in channels[1]
    assert "document.currentScript.closest('[data-hg-inspect-channel]')" in channels[0]
    assert "document.currentScript.closest('[data-hg-inspect-channel]')" in channels[1]
    assert all("document.getElementById" not in channel for channel in channels)


def test_transport_emits_one_immutable_shell_and_one_stable_payload_display_id() -> None:
    scheduler = _QueuedOwnerScheduler()
    display = _FakeNotebookDisplay()

    transport = NotebookInspectionTransport.create(
        _artifact(),
        display=display,
        scheduler=scheduler,
        widget_id="hg-inspect-notebook",
        nonce="nonce-notebook",
    )
    original_shell = display.shells[0]
    channel_id, initial_channel = display.channels[0]

    transport.publish(_artifact(status="completed", terminal=True), urgent=True)
    scheduler.run_due()

    assert len(display.shells) == 1
    assert display.shells[0] == original_shell
    assert len(display.channels) == 1
    assert channel_id == "hg-inspect-notebook-payload"
    assert _wire(initial_channel)["sequence"] == 1
    assert len(display.handle.updates) == 1
    assert _wire(display.handle.updates[0])["sequence"] == 2
    assert _wire(display.handle.updates[0])["payload"]["delivery"] == {
        "state": "saved",
        "label": "Saved snapshot",
    }
    assert 'data-hg-inspect-portable-frame="hg-inspect-notebook"' in display.handle.updates[0]


@pytest.mark.parametrize("delivery_state", ["saved", "stale"])
def test_initial_settled_channel_is_portable_without_a_third_output(
    delivery_state: str,
) -> None:
    scheduler = _QueuedOwnerScheduler()
    display = _FakeNotebookDisplay()
    artifact = _artifact(terminal=delivery_state == "saved", status="completed" if delivery_state == "saved" else "running")
    delivery = (
        InspectionDelivery(state="saved", label="Saved snapshot")
        if delivery_state == "saved"
        else InspectionDelivery(state="stale", label="Live inspection unavailable")
    )

    NotebookInspectionTransport.create(
        artifact,
        display=display,
        scheduler=scheduler,
        widget_id=f"hg-inspect-initial-{delivery_state}",
        nonce=f"nonce-initial-{delivery_state}",
        initial_delivery=delivery,
        close_after_initial=True,
    )

    assert len(display.shells) == 1
    assert len(display.channels) == 1
    assert display.handle.updates == []
    assert f'data-hg-inspect-portable-frame="hg-inspect-initial-{delivery_state}"' in display.channels[0][1]


def test_shell_is_sandboxed_and_only_terminal_channel_is_portable() -> None:
    scheduler = _QueuedOwnerScheduler()
    display = _FakeNotebookDisplay()
    transport = NotebookInspectionTransport.create(
        _artifact(),
        display=display,
        scheduler=scheduler,
        widget_id="hg-inspect-notebook",
        nonce="nonce-notebook",
    )

    shell = display.shells[0]
    _, initial_channel = display.channels[0]
    transport.publish(_artifact(status="running"), urgent=False)
    scheduler.advance(0.25)
    ordinary_update = display.handle.updates[0]

    transport.publish(_artifact(status="completed", terminal=True), urgent=True)
    scheduler.run_due()
    terminal_update = display.handle.updates[1]

    assert re.search(r'<iframe\b[^>]*\bsandbox="allow-scripts"', shell)
    assert "allow-same-origin" not in shell
    assert "default-src &#x27;none&#x27;" in shell
    assert "connect-src &#x27;none&#x27;" in shell
    assert "data-hg-inspect-runtime" in shell
    for channel in (initial_channel, ordinary_update):
        assert "srcdoc=" not in channel
        assert "data-hg-inspect-style" not in channel
        assert "data-hg-inspect-runtime" not in channel
        assert "data-hg-inspect-native-summary" not in channel
        assert "GraphIR" not in channel
        assert "hypergraph.inspect.update" in channel

    assert 'data-hg-inspect-portable-frame="hg-inspect-notebook"' in terminal_update
    assert 'data-hg-inspect-native-summary="hg-inspect-notebook"' in terminal_update
    native_summary = re.search(
        r'(<section data-hg-inspect-native-summary="hg-inspect-notebook".*?</section>)',
        terminal_update,
        flags=re.DOTALL,
    )
    assert native_summary is not None
    assert not re.search(r"<(iframe|script|style)\b", native_summary.group(1), flags=re.IGNORECASE)
    assert "http://" not in native_summary.group(1)
    assert "https://" not in native_summary.group(1)
    assert 'sandbox="allow-scripts"' in terminal_update
    assert "srcdoc=" in terminal_update
    assert "default-src &#x27;none&#x27;" in terminal_update
    assert "connect-src &#x27;none&#x27;" in terminal_update
    assert "data-hg-inspect-runtime" in terminal_update
    assert "hypergraph.inspect.update" in terminal_update


def test_channel_json_is_script_safe_for_closing_tags_and_line_separators() -> None:
    scheduler = _QueuedOwnerScheduler()
    display = _FakeNotebookDisplay()
    hostile = RunInspection(
        run_id="run-hostile",
        graph_name="</script>\u2028\u2029",
        workflow_id=None,
        item_index=None,
        status="running",
        nodes=(),
        failures=(),
        total_duration_ms=0.0,
        captured=True,
        terminal=False,
    )

    NotebookInspectionTransport.create(
        hostile,
        display=display,
        scheduler=scheduler,
        widget_id="hg-inspect-hostile",
        nonce="nonce-hostile",
    )
    channel = display.channels[0][1]

    assert "</script>\u2028\u2029" not in channel
    assert "\\u003c/script\\u003e" in channel
    assert "\\u2028\\u2029" in channel


def test_worker_publication_is_queued_until_the_owner_thread_runs_it() -> None:
    scheduler = _QueuedOwnerScheduler()
    display = _FakeNotebookDisplay()
    transport = NotebookInspectionTransport.create(
        _artifact(),
        display=display,
        scheduler=scheduler,
        widget_id="hg-inspect-notebook",
        nonce="nonce-notebook",
    )
    owner_thread = threading.get_ident()

    worker = threading.Thread(
        target=lambda: transport.publish(
            _artifact(status="completed", terminal=True),
            urgent=True,
        )
    )
    worker.start()
    worker.join(timeout=2)

    assert worker.is_alive() is False
    assert display.handle.updates == []

    scheduler.run_due()

    assert display.handle.update_threads == [owner_thread]


def test_attach_replays_current_session_before_future_publications() -> None:
    scheduler = _QueuedOwnerScheduler()
    display = _FakeNotebookDisplay()
    transport = NotebookInspectionTransport.create(
        _artifact(),
        display=display,
        scheduler=scheduler,
        widget_id="hg-inspect-notebook",
        nonce="nonce-notebook",
    )
    session = InspectionSession(
        graph_name="customer_enrichment",
        workflow_id="workflow-customers",
        item_index=None,
    )

    transport.attach(session)
    scheduler.advance(0.25)

    assert _wire(display.handle.updates[-1])["payload"]["run"]["run_id"] == "pending"

    session.bind_run("run-from-session")
    session.start_node(
        run_id="run-from-session",
        span_id="span-load",
        node_name="load",
        qualified_name="load",
        graph_name="customer_enrichment",
        item_index=None,
        superstep=0,
        inputs={"customer_id": "maya-23"},
        started_at_ms=10.0,
    )
    scheduler.advance(0.25)

    latest = _wire(display.handle.updates[-1])
    assert latest["payload"]["run"]["run_id"] == "run-from-session"
    assert latest["payload"]["run"]["nodes"][0]["qualified_name"] == "load"


def test_attach_rejects_snapshot_that_races_behind_newer_callback() -> None:
    scheduler = _QueuedOwnerScheduler()
    display = _FakeNotebookDisplay()
    transport = NotebookInspectionTransport.create(
        _artifact(),
        display=display,
        scheduler=scheduler,
        widget_id="hg-inspect-attach-race",
        nonce="nonce-attach-race",
    )

    class RacingSession(InspectionSession):
        def subscribe_with_snapshot(self, callback: Any) -> Any:
            older, unsubscribe = super().subscribe_with_snapshot(callback)
            self.start_node(
                run_id="run-attach-race",
                span_id="span-newer",
                node_name="newer",
                qualified_name="newer",
                graph_name="attach-race",
                item_index=None,
                superstep=0,
                inputs={"value": 2},
                started_at_ms=1.0,
            )
            return older, unsubscribe

    session = RacingSession(
        graph_name="attach-race",
        workflow_id=None,
        item_index=None,
    )
    session.bind_run("run-attach-race")

    transport.attach(session)
    scheduler.advance(0.25)

    latest = _wire(display.handle.updates[-1])
    assert latest["payload"]["run"]["nodes"][0]["qualified_name"] == "newer"


def test_nonterminal_factory_needs_delayed_calls_before_subscribing() -> None:
    scheduler = _QueuedOwnerScheduler(
        supports_delayed_calls=False,
        supports_cross_thread=True,
    )
    display = _FakeNotebookDisplay()
    transport = open_notebook_inspection_transport(
        _artifact(),
        notebook=True,
        display=display,
        scheduler=scheduler,
    )
    assert transport is not None
    assert transport.closed is True
    assert _wire(display.channels[0][1])["payload"]["delivery"] == {
        "state": "stale",
        "label": "Live inspection unavailable",
    }
    session = InspectionSession(
        graph_name="customer_enrichment",
        workflow_id="workflow-customers",
        item_index=None,
    )

    transport.attach(session)

    assert session._subscribers == {}  # type: ignore[attr-defined]
    assert display.handle.updates == []


def test_terminal_factory_preserves_saved_truth_without_scheduler_capabilities() -> None:
    scheduler = _QueuedOwnerScheduler(
        supports_delayed_calls=False,
        supports_cross_thread=False,
    )
    display = _FakeNotebookDisplay()

    transport = open_notebook_inspection_transport(
        _artifact(terminal=True, status="completed"),
        notebook=True,
        display=display,
        scheduler=scheduler,
        require_cross_thread=True,
    )

    assert transport is not None
    assert transport.closed is True
    assert _wire(display.channels[0][1])["payload"]["delivery"] == {
        "state": "saved",
        "label": "Saved snapshot",
    }
    assert display.handle.updates == []


def test_cross_thread_requirement_is_independent_of_delayed_calls() -> None:
    scheduler = _QueuedOwnerScheduler(
        supports_delayed_calls=True,
        supports_cross_thread=False,
    )
    unavailable_display = _FakeNotebookDisplay()

    unavailable = open_notebook_inspection_transport(
        _artifact(),
        notebook=True,
        display=unavailable_display,
        scheduler=scheduler,
        require_cross_thread=True,
    )
    assert unavailable is not None
    unavailable_session = InspectionSession(
        graph_name="customer_enrichment",
        workflow_id="workflow-unavailable",
        item_index=None,
    )
    unavailable.attach(unavailable_session)

    assert unavailable.closed is True
    assert unavailable_session._subscribers == {}  # type: ignore[attr-defined]

    live_display = _FakeNotebookDisplay()
    live = open_notebook_inspection_transport(
        _artifact(),
        notebook=True,
        display=live_display,
        scheduler=scheduler,
        require_cross_thread=False,
    )
    assert live is not None
    live_session = InspectionSession(
        graph_name="customer_enrichment",
        workflow_id="workflow-live",
        item_index=None,
    )
    live.attach(live_session)

    assert live.closed is False
    assert live_session._subscribers != {}  # type: ignore[attr-defined]


def test_delayed_scheduler_falsifier_opens_live_and_delivers_on_owner_thread() -> None:
    unavailable_scheduler = _QueuedOwnerScheduler(
        supports_delayed_calls=False,
        supports_cross_thread=True,
    )
    unavailable_display = _FakeNotebookDisplay()
    unavailable = open_notebook_inspection_transport(
        _artifact(),
        notebook=True,
        display=unavailable_display,
        scheduler=unavailable_scheduler,
    )
    assert unavailable is not None
    unavailable_session = InspectionSession(
        graph_name="customer_enrichment",
        workflow_id="workflow-unavailable",
        item_index=None,
    )
    unavailable.attach(unavailable_session)

    assert unavailable.closed is True
    assert unavailable_session._subscribers == {}  # type: ignore[attr-defined]

    scheduler = _QueuedOwnerScheduler(
        supports_delayed_calls=True,
        supports_cross_thread=True,
    )
    display = _FakeNotebookDisplay()
    transport = open_notebook_inspection_transport(
        _artifact(),
        notebook=True,
        display=display,
        scheduler=scheduler,
    )
    assert transport is not None
    session = InspectionSession(
        graph_name="customer_enrichment",
        workflow_id="workflow-live",
        item_index=None,
    )
    transport.attach(session)
    session.bind_run("run-live")
    session.start_node(
        run_id="run-live",
        span_id="span-review",
        node_name="review_customer",
        qualified_name="review_customer",
        graph_name="customer_enrichment",
        item_index=None,
        superstep=0,
        inputs={"customer_id": "maya-23"},
        started_at_ms=1.0,
    )

    assert transport.closed is False
    assert session._subscribers != {}  # type: ignore[attr-defined]
    assert display.handle.updates == []

    scheduler.advance(0.25)

    assert display.handle.update_threads == [scheduler.owner_thread_id]
    latest = _wire(display.handle.updates[-1])
    assert latest["payload"]["run"]["nodes"][0]["qualified_name"] == "review_customer"


def test_failed_channel_update_closes_transport_and_unsubscribes_session() -> None:
    scheduler = _QueuedOwnerScheduler()
    display = _FakeNotebookDisplay(fail_update=True)
    transport = NotebookInspectionTransport.create(
        _artifact(),
        display=display,
        scheduler=scheduler,
        widget_id="hg-inspect-broken-update",
        nonce="nonce-broken-update",
    )
    session = InspectionSession(
        graph_name="customer_enrichment",
        workflow_id="workflow-customers",
        item_index=None,
    )

    transport.attach(session)
    scheduler.advance(0.25)

    assert transport.closed is True
    assert transport._coalescer.delivery_failed is True  # type: ignore[attr-defined]
    assert session._subscribers == {}  # type: ignore[attr-defined]


def test_rejected_schedule_closes_transport_and_unsubscribes_session() -> None:
    scheduler = _RejectingOwnerScheduler()
    display = _FakeNotebookDisplay()
    transport = NotebookInspectionTransport.create(
        _artifact(),
        display=display,
        scheduler=scheduler,
        widget_id="hg-inspect-rejected-schedule",
        nonce="nonce-rejected-schedule",
    )
    session = InspectionSession(
        graph_name="customer_enrichment",
        workflow_id="workflow-customers",
        item_index=None,
    )

    transport.attach(session)

    assert transport.closed is True
    assert transport._coalescer.delivery_failed is True  # type: ignore[attr-defined]
    assert session._subscribers == {}  # type: ignore[attr-defined]
    assert display.handle.updates == []


def test_start_failure_becomes_bounded_stale_transport_state() -> None:
    scheduler = _QueuedOwnerScheduler()
    display = _FakeNotebookDisplay()
    transport = NotebookInspectionTransport.create(
        _artifact(),
        display=display,
        scheduler=scheduler,
        widget_id="hg-inspect-notebook",
        nonce="nonce-notebook",
    )

    transport.fail_to_start(ValueError("missing required input: customer_id"))
    scheduler.run_due()

    wire = _wire(display.handle.updates[-1])
    assert wire["payload"]["delivery"] == {
        "state": "stale",
        "label": "Live inspection unavailable",
    }
    assert wire["payload"]["run"]["run_id"] == "run-notebook"
    assert wire["message"]["type_name"] == "ValueError"
    assert "missing required input" in wire["message"]["text"]
    assert 'data-hg-inspect-portable-frame="hg-inspect-notebook"' in display.handle.updates[-1]
    assert 'data-hg-inspect-channel-message="hg-inspect-notebook"' in display.handle.updates[-1]


def test_plain_and_non_notebook_modes_suppress_automatic_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler = _QueuedOwnerScheduler()
    display = _FakeNotebookDisplay()
    monkeypatch.setenv("HYPERGRAPH_DISPLAY", "plain")

    plain = open_notebook_inspection_transport(
        _artifact(),
        notebook=True,
        display=display,
        scheduler=scheduler,
    )
    monkeypatch.delenv("HYPERGRAPH_DISPLAY")
    non_notebook = open_notebook_inspection_transport(
        _artifact(),
        notebook=False,
        display=display,
        scheduler=scheduler,
    )

    assert plain is None
    assert non_notebook is None
    assert display.shells == []
    assert display.channels == []


def test_notebook_display_failure_is_observational_and_returns_no_transport() -> None:
    display = _FakeNotebookDisplay(fail_channel=True)
    result = open_notebook_inspection_transport(
        _artifact(),
        notebook=True,
        display=display,
        scheduler=_QueuedOwnerScheduler(),
    )

    assert result is None
    assert len(display.shells) == 1
    assert "Waiting for live inspection" in display.shells[0]
    assert "&quot;state&quot;:&quot;stale&quot;" in display.shells[0]


class _KernelLoop:
    def __init__(self) -> None:
        self.callbacks: list[Callable[[], None]] = []

    def add_callback(self, callback: Callable[[], None]) -> None:
        self.callbacks.append(callback)

    def call_later(self, _delay: float, callback: Callable[[], None]) -> _Call:
        call = _Call(0.0, callback)
        self.callbacks.append(callback)
        return call


class _LateRejectingKernelLoop:
    def __init__(self) -> None:
        self.callbacks: list[Callable[[], None]] = []
        self.call_later_threads: list[int] = []

    def add_callback(self, callback: Callable[[], None]) -> None:
        self.callbacks.append(callback)

    def call_later(self, delay: float, _callback: Callable[[], None]) -> None:
        assert delay > 0.0
        self.call_later_threads.append(threading.get_ident())
        raise RuntimeError("backend rejected delayed arm")


class _RacingLateRejectingKernelLoop(_LateRejectingKernelLoop):
    def __init__(self) -> None:
        super().__init__()
        self.arm_started = threading.Event()
        self.release_arm = threading.Event()

    def call_later(self, delay: float, _callback: Callable[[], None]) -> None:
        assert delay > 0.0
        self.call_later_threads.append(threading.get_ident())
        self.arm_started.set()
        assert self.release_arm.wait(timeout=5)
        raise RuntimeError("backend rejected stale delayed arm")


class _AddCallbackOnlyKernelLoop:
    def __init__(self) -> None:
        self.callbacks: list[Callable[[], None]] = []

    def add_callback(self, callback: Callable[[], None]) -> None:
        self.callbacks.append(callback)


class _CallLaterOnlyKernelLoop:
    def __init__(self) -> None:
        self.callbacks: list[Callable[[], None]] = []

    def call_later(self, _delay: float, callback: Callable[[], None]) -> _Call:
        call = _Call(0.0, callback)
        self.callbacks.append(callback)
        return call


def test_owner_scheduler_models_delayed_calls_separately_from_cross_thread() -> None:
    no_kernel = OwnerThreadScheduler(
        asyncio_loop=None,
        kernel_ioloop=None,
        clock=lambda: 0.0,
    )
    no_kernel_callbacks: list[int] = []

    assert no_kernel.supports_delayed_calls is False
    assert no_kernel.supports_cross_thread is False
    assert no_kernel.call_at(1.0, lambda: no_kernel_callbacks.append(threading.get_ident())) is None
    assert no_kernel_callbacks == []

    add_callback_only = _AddCallbackOnlyKernelLoop()
    cross_thread_only = OwnerThreadScheduler(
        asyncio_loop=None,
        kernel_ioloop=add_callback_only,
        clock=lambda: 0.0,
    )

    assert cross_thread_only.supports_delayed_calls is False
    assert cross_thread_only.supports_cross_thread is True
    assert cross_thread_only.call_at(1.0, lambda: None) is None
    assert add_callback_only.callbacks == []

    call_later_only = _CallLaterOnlyKernelLoop()
    delayed_only = OwnerThreadScheduler(
        asyncio_loop=None,
        kernel_ioloop=call_later_only,
        clock=lambda: 0.0,
    )
    owner_thread = threading.get_ident()
    callback_threads: list[int] = []

    assert delayed_only.supports_delayed_calls is True
    assert delayed_only.supports_cross_thread is False
    assert delayed_only.call_at(1.0, lambda: callback_threads.append(threading.get_ident())) is not None
    assert callback_threads == []
    assert len(call_later_only.callbacks) == 1

    call_later_only.callbacks.pop()()

    assert callback_threads == [owner_thread]


def test_owner_scheduler_marshals_worker_work_through_kernel_ioloop() -> None:
    kernel_loop = _KernelLoop()
    scheduler = OwnerThreadScheduler(
        asyncio_loop=None,
        kernel_ioloop=kernel_loop,
        clock=lambda: 0.0,
    )
    owner_thread = threading.get_ident()
    callback_threads: list[int] = []

    worker = threading.Thread(
        target=lambda: scheduler.call_at(
            scheduler.now(),
            lambda: callback_threads.append(threading.get_ident()),
        )
    )
    worker.start()
    worker.join(timeout=2)

    assert worker.is_alive() is False
    assert callback_threads == []
    assert len(kernel_loop.callbacks) == 1

    kernel_loop.callbacks.pop()()

    assert callback_threads == [owner_thread]
    assert scheduler.supports_delayed_calls is True
    assert scheduler.supports_cross_thread is True


@pytest.mark.parametrize("display_fails", [False, True], ids=["display-works", "display-fails"])
def test_late_owner_arm_rejection_fails_closed_on_owner_thread(
    display_fails: bool,
) -> None:
    kernel_loop = _LateRejectingKernelLoop()
    scheduler = OwnerThreadScheduler(
        asyncio_loop=None,
        kernel_ioloop=kernel_loop,
        clock=lambda: 0.0,
    )
    display = _FakeNotebookDisplay(fail_update=display_fails)
    transport = NotebookInspectionTransport.create(
        _artifact(),
        display=display,
        scheduler=scheduler,
        widget_id="hg-inspect-late-arm",
        nonce="nonce-late-arm",
    )
    session = InspectionSession(
        graph_name="customer_enrichment",
        workflow_id="workflow-late-arm",
        item_index=None,
    )
    session.bind_run("run-late-arm")
    session.start_node(
        run_id="run-late-arm",
        span_id="span-review",
        node_name="review_customer",
        qualified_name="review_customer",
        graph_name="customer_enrichment",
        item_index=None,
        superstep=0,
        inputs={"customer_id": "maya-23"},
        started_at_ms=1.0,
    )

    worker = threading.Thread(target=lambda: transport.attach(session))
    worker.start()
    worker.join(timeout=2)

    assert worker.is_alive() is False
    assert session._subscribers != {}  # type: ignore[attr-defined]
    assert len(kernel_loop.callbacks) == 1
    assert display.handle.update_attempt_threads == []

    kernel_loop.callbacks.pop()()

    owner_thread = scheduler.owner_thread_id
    assert kernel_loop.call_later_threads == [owner_thread]
    assert transport.closed is True
    assert transport._coalescer.delivery_failed is True  # type: ignore[attr-defined]
    assert transport._coalescer._pending is None  # type: ignore[attr-defined]
    assert transport._coalescer._scheduled_token is None  # type: ignore[attr-defined]
    assert transport._coalescer._scheduled_handle is None  # type: ignore[attr-defined]
    assert session._subscribers == {}  # type: ignore[attr-defined]
    assert display.handle.update_attempt_threads == [owner_thread]
    if display_fails:
        assert display.handle.updates == []
        assert display.handle.update_threads == []
    else:
        assert len(display.handle.updates) == 1
        assert display.handle.update_threads == [owner_thread]
        settled = _wire(display.handle.updates[0])
        assert settled["payload"]["delivery"] == {
            "state": "stale",
            "label": "Live inspection unavailable",
        }
        assert settled["payload"]["run"]["run_id"] == "run-late-arm"
        assert settled["payload"]["run"]["nodes"][0]["qualified_name"] == "review_customer"


def test_stale_late_owner_arm_rejection_does_not_poison_urgent_replacement() -> None:
    kernel_loop = _RacingLateRejectingKernelLoop()
    scheduler = OwnerThreadScheduler(
        asyncio_loop=None,
        kernel_ioloop=kernel_loop,
        clock=lambda: 0.0,
    )
    display = _FakeNotebookDisplay()
    transport = NotebookInspectionTransport.create(
        _artifact(),
        display=display,
        scheduler=scheduler,
        widget_id="hg-inspect-stale-late-arm",
        nonce="nonce-stale-late-arm",
    )
    first = replace(
        _artifact(),
        run_id="run-first-arm",
        revision=1,
    )
    latest = replace(
        _artifact(),
        run_id="run-urgent-replacement",
        revision=2,
    )

    first_worker = threading.Thread(target=lambda: transport.publish(first, urgent=False))
    first_worker.start()
    first_worker.join(timeout=2)

    assert first_worker.is_alive() is False
    assert len(kernel_loop.callbacks) == 1

    def publish_replacement() -> None:
        assert kernel_loop.arm_started.wait(timeout=5)
        transport.publish(latest, urgent=True)
        kernel_loop.release_arm.set()

    replacement_worker = threading.Thread(target=publish_replacement)
    replacement_worker.start()
    kernel_loop.callbacks.pop(0)()
    replacement_worker.join(timeout=5)

    assert replacement_worker.is_alive() is False
    assert transport.closed is False
    assert transport._coalescer.delivery_failed is False  # type: ignore[attr-defined]
    assert display.handle.update_attempt_threads == []
    assert len(kernel_loop.callbacks) == 1

    kernel_loop.callbacks.pop()()

    assert transport.closed is False
    assert transport._coalescer.delivery_failed is False  # type: ignore[attr-defined]
    assert display.handle.update_threads == [scheduler.owner_thread_id]
    delivered = _wire(display.handle.updates[0])
    assert delivered["payload"]["delivery"] == {
        "state": "live",
        "label": "Live",
    }
    assert delivered["payload"]["run"]["run_id"] == "run-urgent-replacement"


@pytest.mark.asyncio
async def test_owner_scheduler_captures_running_asyncio_loop_for_worker_delivery() -> None:
    scheduler = OwnerThreadScheduler.capture()
    owner_thread = threading.get_ident()
    delivered = asyncio.Event()
    callback_threads: list[int] = []

    def callback() -> None:
        callback_threads.append(threading.get_ident())
        delivered.set()

    worker = threading.Thread(target=lambda: scheduler.call_at(scheduler.now(), callback))
    worker.start()
    worker.join(timeout=2)
    await asyncio.wait_for(delivered.wait(), timeout=2)

    assert worker.is_alive() is False
    assert callback_threads == [owner_thread]
    assert scheduler.supports_delayed_calls is True
    assert scheduler.supports_cross_thread is True
