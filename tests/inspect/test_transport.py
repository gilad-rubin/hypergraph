"""Deterministic contract tests for the live inspection transport core."""

from __future__ import annotations

import heapq
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace

import pytest

from hypergraph.runners._shared._inspect import (
    InspectionSession,
    MapInspectionSession,
    NodeInspection,
    RunInspection,
)
from hypergraph.runners._shared._inspect_transport import (
    INSPECTION_PROTOCOL_VERSION,
    InspectionCoalescer,
    InspectionDelivery,
    InspectionEnvelope,
    inspection_envelope_to_wire,
)


def _node(index: int) -> NodeInspection:
    return NodeInspection(
        run_id="run-live",
        span_id=f"span-{index}",
        node_name=f"node_{index}",
        qualified_name=f"workflow/node_{index}",
        graph_name="workflow",
        item_index=None,
        superstep=index,
        sequence=index,
        status="completed",
        values_captured=True,
        inputs={"value": index},
        outputs={"value": index * 2},
        started_at_ms=float(index * 10),
        ended_at_ms=float(index * 10 + 5),
        duration_ms=5.0,
    )


def _run(
    node_count: int,
    *,
    status: str = "running",
    terminal: bool = False,
    error: BaseException | None = None,
) -> RunInspection:
    return RunInspection(
        run_id="run-live",
        graph_name="workflow",
        workflow_id="workflow-live",
        item_index=None,
        status=status,
        nodes=tuple(_node(index) for index in range(node_count)),
        failures=(),
        total_duration_ms=float(node_count * 10),
        captured=True,
        terminal=terminal,
        error=error,
    )


@dataclass
class _Scheduled:
    deadline: float
    order: int
    callback: Callable[[], None]
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


class _ManualScheduler:
    """Heap-backed virtual scheduler; no wall-clock sleeps enter hammer tests."""

    def __init__(self) -> None:
        self.current = 0.0
        self._order = 0
        self._calls: list[tuple[float, int, _Scheduled]] = []
        self.all_calls: list[_Scheduled] = []

    def now(self) -> float:
        return self.current

    def call_at(self, deadline: float, callback: Callable[[], None]) -> _Scheduled:
        call = _Scheduled(deadline, self._order, callback)
        self._order += 1
        self.all_calls.append(call)
        heapq.heappush(self._calls, (deadline, call.order, call))
        return call

    def run_due(self) -> None:
        while self._calls and self._calls[0][0] <= self.current:
            _, _, call = heapq.heappop(self._calls)
            if not call.cancelled:
                call.callback()

    def advance(self, seconds: float) -> None:
        self.current += seconds
        self.run_due()


def _coalescer(
    scheduler: _ManualScheduler,
    delivered: list[InspectionEnvelope],
) -> InspectionCoalescer:
    return InspectionCoalescer(
        widget_id="hg-inspect-one",
        nonce="nonce-one",
        scheduler=scheduler,
        deliver=delivered.append,
    )


def test_envelope_has_one_typed_artifact_and_an_explicit_versioned_wire_boundary() -> None:
    envelope = InspectionEnvelope(
        protocol_version=INSPECTION_PROTOCOL_VERSION,
        widget_id="hg-inspect-one",
        nonce="nonce-one",
        sequence=7,
        delivery=InspectionDelivery(state="live", label="Live"),
        artifact=_run(1),
    )

    wire = inspection_envelope_to_wire(envelope)

    assert wire.keys() == {
        "type",
        "version",
        "widget_id",
        "nonce",
        "sequence",
        "payload",
        "message",
    }
    assert wire["type"] == "hypergraph.inspect.update"
    assert wire["version"] == 1
    assert wire["widget_id"] == "hg-inspect-one"
    assert wire["nonce"] == "nonce-one"
    assert wire["sequence"] == 7
    assert wire["message"] is None
    assert wire["payload"]["schema"] == "hypergraph.inspect/v1"
    assert wire["payload"]["delivery"] == {"state": "live", "label": "Live"}
    assert wire["payload"]["run"]["nodes"][0]["outputs"]["entries"][0]["value"]["value"] == 0


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"protocol_version": 2}, "protocol_version"),
        ({"widget_id": ""}, "widget_id"),
        ({"nonce": ""}, "nonce"),
        ({"sequence": 0}, "sequence"),
    ],
)
def test_envelope_rejects_invalid_protocol_identity_and_sequence(
    changes: dict[str, object],
    message: str,
) -> None:
    fields = {
        "protocol_version": INSPECTION_PROTOCOL_VERSION,
        "widget_id": "hg-inspect-one",
        "nonce": "nonce-one",
        "sequence": 1,
        "delivery": InspectionDelivery(state="live", label="Live"),
        "artifact": _run(0),
    }
    fields.update(changes)

    with pytest.raises(ValueError, match=message):
        InspectionEnvelope(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("state", "label", "message"),
    [
        ("connected", "Live", "state"),
        ("live", "", "label"),
        ("live", "Connected", "label"),
    ],
)
def test_delivery_state_and_label_are_not_arbitrary_strings(
    state: str,
    label: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        InspectionDelivery(state=state, label=label)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("attempts", "step_seconds", "maximum_sends"),
    [(10, 0.05, 3), (100, 0.01, 5)],
)
def test_hammer_is_bounded_to_four_hertz_and_latest_state_wins(
    attempts: int,
    step_seconds: float,
    maximum_sends: int,
) -> None:
    scheduler = _ManualScheduler()
    delivered: list[InspectionEnvelope] = []
    coalescer = _coalescer(scheduler, delivered)

    for node_count in range(1, attempts + 1):
        coalescer.publish(_run(node_count), urgent=False)
        scheduler.run_due()
        if node_count < attempts:
            scheduler.advance(step_seconds)

    coalescer.publish(_run(attempts, status="completed", terminal=True), urgent=True)
    scheduler.run_due()
    scheduler.advance(1.0)

    assert len(delivered) <= maximum_sends
    assert delivered[-1].artifact.terminal is True
    assert len(delivered[-1].artifact.nodes) == attempts
    assert [envelope.sequence for envelope in delivered] == list(range(1, len(delivered) + 1))


def test_error_and_terminal_preempt_the_window_and_cancel_stale_ordinary_work() -> None:
    scheduler = _ManualScheduler()
    delivered: list[InspectionEnvelope] = []
    coalescer = _coalescer(scheduler, delivered)

    coalescer.publish(_run(1), urgent=False)
    scheduler.run_due()
    scheduler.advance(0.1)
    coalescer.publish(_run(2), urgent=False)
    stale_callback = scheduler.all_calls[-1].callback

    scheduler.advance(0.01)
    coalescer.publish(
        _run(2, status="failed", error=ValueError("provider failed")),
        urgent=True,
    )
    scheduler.run_due()
    scheduler.advance(0.01)
    coalescer.publish(
        _run(2, status="failed", terminal=True, error=ValueError("provider failed")),
        urgent=True,
    )
    scheduler.run_due()

    # Simulate a backend invoking an already-cancelled timer anyway.
    stale_callback()
    scheduler.advance(1.0)

    assert [envelope.sequence for envelope in delivered] == [1, 2, 3]
    assert delivered[1].artifact.error is not None
    assert delivered[1].artifact.terminal is False
    assert delivered[2].artifact.terminal is True
    assert delivered[2].delivery.state == "saved"


def test_pending_terminal_is_absorbing_when_older_and_later_callbacks_arrive() -> None:
    """A terminal snapshot cannot be overwritten before its scheduled flush."""
    scheduler = _ManualScheduler()
    delivered: list[InspectionEnvelope] = []
    coalescer = _coalescer(scheduler, delivered)

    terminal = replace(_run(2, status="completed", terminal=True), revision=2)
    older_running = replace(_run(1), revision=1)
    accidental_later_running = replace(_run(3), revision=3)
    accidental_later_terminal = replace(
        terminal,
        status="failed",
        error=RuntimeError("late terminal mutation"),
        revision=4,
    )

    coalescer.publish(terminal, urgent=True)
    coalescer.publish(older_running, urgent=False)
    coalescer.publish(accidental_later_running, urgent=False)
    coalescer.publish(accidental_later_terminal, urgent=True)
    scheduler.run_due()

    assert [envelope.artifact for envelope in delivered] == [terminal]
    assert coalescer.closed is True


def test_newer_ordinary_revision_keeps_an_already_immediate_urgent_flush() -> None:
    scheduler = _ManualScheduler()
    delivered: list[InspectionEnvelope] = []
    coalescer = _coalescer(scheduler, delivered)
    coalescer.publish(replace(_run(0), revision=1), urgent=False)
    scheduler.run_due()
    scheduler.advance(0.1)

    coalescer.publish(
        replace(_run(1, status="failed", error=RuntimeError("urgent")), revision=2),
        urgent=True,
    )
    latest = replace(_run(2), revision=3)
    coalescer.publish(latest, urgent=False)
    scheduler.run_due()

    assert [envelope.sequence for envelope in delivered] == [1, 2]
    assert delivered[-1].artifact is latest


def test_run_session_reverse_notifications_preserve_terminal_revision() -> None:
    scheduler = _ManualScheduler()
    delivered: list[InspectionEnvelope] = []
    coalescer = _coalescer(scheduler, delivered)
    first_callback = threading.Event()
    release_first = threading.Event()
    session = InspectionSession(
        graph_name="reverse-run",
        workflow_id=None,
        item_index=None,
    )
    session.bind_run("run-reverse")

    def publish(artifact: RunInspection, urgent: bool) -> None:
        if not artifact.terminal:
            first_callback.set()
            assert release_first.wait(timeout=5)
        coalescer.publish(artifact, urgent)

    session.subscribe(publish)
    worker = threading.Thread(
        target=lambda: session.start_node(
            run_id="run-reverse",
            span_id="span-reverse",
            node_name="work",
            qualified_name="work",
            graph_name="reverse-run",
            item_index=None,
            superstep=0,
            inputs={"value": 1},
            started_at_ms=1.0,
        )
    )
    worker.start()
    assert first_callback.wait(timeout=5)
    terminal = session.finish(
        status="failed",
        total_duration_ms=2.0,
        error=RuntimeError("run boundary failed"),
    )
    release_first.set()
    worker.join(timeout=5)
    scheduler.run_due()

    assert worker.is_alive() is False
    assert delivered[-1].artifact is terminal
    assert delivered[-1].artifact.terminal is True


def test_map_session_reverse_notifications_preserve_terminal_revision() -> None:
    scheduler = _ManualScheduler()
    delivered: list[InspectionEnvelope] = []
    coalescer = _coalescer(scheduler, delivered)
    first_callback = threading.Event()
    release_first = threading.Event()
    session = MapInspectionSession(
        graph_name="reverse-map",
        workflow_id=None,
        requested_count=1,
        map_over=("value",),
        map_mode="zip",
    )
    session.bind_run("batch-reverse")

    def publish(artifact: object, urgent: bool) -> None:
        if not artifact.terminal:  # type: ignore[attr-defined]
            first_callback.set()
            assert release_first.wait(timeout=5)
        coalescer.publish(artifact, urgent)  # type: ignore[arg-type]

    session.subscribe(publish)  # type: ignore[arg-type]
    worker = threading.Thread(
        target=lambda: session.claim_item(
            item_index=0,
            requested_inputs={"value": 1},
            workflow_id=None,
        )
    )
    worker.start()
    assert first_callback.wait(timeout=5)
    error = RuntimeError("batch boundary failed")
    terminal = session.finish(
        status="failed",
        total_duration_ms=2.0,
        unstarted_item_indexes=(),
        error=error,
    )
    release_first.set()
    worker.join(timeout=5)
    scheduler.run_due()

    assert worker.is_alive() is False
    assert delivered[-1].artifact is terminal
    assert terminal.error is error


def test_delivery_failure_is_isolated_and_closes_the_observational_transport() -> None:
    scheduler = _ManualScheduler()
    attempts = 0

    def broken_delivery(_envelope: InspectionEnvelope) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("display channel closed")

    coalescer = InspectionCoalescer(
        widget_id="hg-inspect-one",
        nonce="nonce-one",
        scheduler=scheduler,
        deliver=broken_delivery,
    )

    coalescer.publish(_run(1), urgent=False)
    scheduler.run_due()
    coalescer.publish(_run(2), urgent=True)
    scheduler.run_due()

    assert attempts == 1
    assert coalescer.delivery_failed is True
    assert coalescer.closed is True


def test_terminal_close_ignores_late_publications() -> None:
    scheduler = _ManualScheduler()
    delivered: list[InspectionEnvelope] = []
    coalescer = _coalescer(scheduler, delivered)

    coalescer.publish(_run(1, status="completed", terminal=True), urgent=True)
    scheduler.run_due()
    coalescer.publish(replace(_run(2), status="running"), urgent=True)
    scheduler.run_due()

    assert len(delivered) == 1
    assert coalescer.closed is True
