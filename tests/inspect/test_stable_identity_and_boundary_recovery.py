"""RED contracts for stable inspect identity and real boundary recovery."""

from __future__ import annotations

import asyncio
import contextlib
import html
import io
import re
import textwrap
import threading
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import replace
from typing import Any, Literal, TypeVar

import pytest
from playwright.sync_api import Browser, Page, sync_playwright

from hypergraph import AsyncRunner, Graph, SyncRunner, node
from hypergraph.runners._shared import _inspect_transport
from hypergraph.runners._shared._inspect import MapInspection, RunInspection
from hypergraph.runners._shared._inspect_html import (
    build_inspection_payload,
    render_map_inspection,
    render_run_inspection,
)
from hypergraph.runners._shared._inspect_serialization import (
    serialize_value,
    serialized_value_to_wire,
)
from hypergraph.runners._shared._inspect_transport import (
    INSPECTION_PROTOCOL_VERSION,
    InspectionDelivery,
    InspectionEnvelope,
    _first_failure_and_node,
    _native_failure_markup,
    render_payload_channel,
)
from hypergraph.runners._shared.results import MapResult, RunResult

_T = TypeVar("_T")
_RunnerKind = Literal["sync", "async"]
_Surface = Literal["full", "native"]
_BoundarySource = Literal["start", "run", "map_item", "batch"]


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as runtime:
        instance = runtime.chromium.launch(headless=True)
        yield instance
        instance.close()


class _ChangingReprCustomer:
    def __init__(self, label: str) -> None:
        self.label = label
        self.repr_calls = 0

    def __repr__(self) -> str:
        self.repr_calls += 1
        return f"Customer({self.label}, presentation={self.repr_calls})"


def _unstable_nested_graph() -> Graph:
    @node(output_name="reviewed")
    def review_customer(customer_id: _ChangingReprCustomer) -> str:
        if customer_id.label.startswith("reject-"):
            raise ValueError(f"manual review: {customer_id.label}")
        return f"approved:{customer_id.label}"

    inner = Graph([review_customer], name="inner-review")
    return Graph(
        [inner.as_node(name="review_group").map_over("customer_id")],
        name="outer-review",
    )


def _unstable_nested_values() -> dict[str, list[list[_ChangingReprCustomer]]]:
    return {
        "customer_id": [
            [
                _ChangingReprCustomer("approve-outer-0"),
                _ChangingReprCustomer("reject-outer-0"),
            ],
            [
                _ChangingReprCustomer("approve-outer-1"),
                _ChangingReprCustomer("reject-outer-1"),
            ],
        ]
    }


def _run_async(factory: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
    values: list[_T] = []
    errors: list[BaseException] = []

    def execute() -> None:
        try:
            values.append(asyncio.run(factory()))
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


def _run_unstable_nested_map(
    runner_kind: _RunnerKind,
) -> tuple[MapResult, dict[str, list[list[_ChangingReprCustomer]]]]:
    graph = _unstable_nested_graph()
    values = _unstable_nested_values()
    if runner_kind == "sync":
        batch = SyncRunner().map(
            graph,
            values,
            map_over="customer_id",
            inspect=True,
            error_handling="continue",
        )
    else:
        batch = _run_async(
            lambda: AsyncRunner().map(
                graph,
                values,
                map_over="customer_id",
                inspect=True,
                error_handling="continue",
            )
        )
    return batch, values


def _repr_counts(
    values: dict[str, list[list[_ChangingReprCustomer]]],
) -> tuple[tuple[int, ...], ...]:
    return tuple(tuple(customer.repr_calls for customer in group) for group in values["customer_id"])


@pytest.mark.parametrize("runner_kind", ["sync", "async"])
@pytest.mark.parametrize("outer_index", [0, 1])
@pytest.mark.parametrize("surface", ["full", "native"])
def test_unstable_repr_keeps_primary_nested_failure_on_exact_leaf(
    browser: Browser,
    runner_kind: _RunnerKind,
    outer_index: int,
    surface: _Surface,
) -> None:
    batch, values = _run_unstable_nested_map(runner_kind)
    artifact = batch.inspect()._artifact
    selected_artifact = replace(artifact, items=(artifact.items[outer_index],))
    expected_label = f"reject-outer-{outer_index}"

    if surface == "native":
        payload = build_inspection_payload(
            selected_artifact,
            delivery_state="saved",
            delivery_label="Saved snapshot",
        )
        data = payload["map"]
        assert isinstance(data, dict)
        item = data["items"][0]  # type: ignore[index]
        run = item["run"]  # type: ignore[index]
        before_consumer = _repr_counts(values)

        failure, failed_node = _first_failure_and_node(
            run,
            containing_item_index=outer_index,
        )
        markup = html.unescape(
            _native_failure_markup(
                kind="map",
                data=data,
                message={},
            ).replace("<wbr>", "")
        )

        assert failure["node_name"] == "review_group/review_customer"
        assert failed_node["qualified_name"] == "review_group/review_customer"
        assert failed_node["item_index"] == 1
        assert expected_label in str(failed_node["inputs"])
        assert "Qualified node: <code>review_group/review_customer</code>" in markup
        assert expected_label in markup
        assert f"ValueError: manual review: {expected_label}" in markup
        assert _repr_counts(values) == before_consumer
        return

    rendered = render_map_inspection(selected_artifact)
    before_consumer = _repr_counts(values)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.set_content(rendered)
    root = page.locator('[data-hypergraph-inspect="map"]')
    root.get_by_role("button", name="Show failure").click()
    detail = root.locator("[data-hg-detail]")

    assert root.locator('[data-hg-timeline-row][aria-current="true"] code').first.inner_text() == "review_group/review_customer"
    assert detail.locator(".hg-inspect-detail-heading code").inner_text() == "review_group/review_customer"
    assert detail.locator("dt").filter(has_text="Item").locator("xpath=following-sibling::dd").inner_text() == "1"
    detail_text = detail.inner_text()
    assert expected_label in detail_text
    assert f"approve-outer-{outer_index}" not in detail_text
    assert f"ValueError: manual review: {expected_label}" in detail_text
    assert _repr_counts(values) == before_consumer
    page.close()


def test_unstable_repr_dedupe_removes_only_selected_occurrence(
    browser: Browser,
) -> None:
    batch, values = _run_unstable_nested_map("sync")
    artifact = batch.inspect()._artifact
    item = artifact.items[0]
    assert item.run is not None
    primary_leaf = next(
        current for current in item.run.nodes if current.qualified_name == "review_group/review_customer" and current.status == "failed"
    )
    assert primary_leaf.failure is not None

    peer_value = _ChangingReprCustomer("reject-peer")
    peer_error = ValueError("manual review: reject-peer")
    peer_node_failure = replace(
        primary_leaf.failure,
        error=peer_error,
        inputs={"customer_id": peer_value},
    )
    peer_public_failure = replace(
        item.run.failures[0],
        error=peer_error,
        inputs={"customer_id": peer_value},
    )
    peer_leaf = replace(
        primary_leaf,
        span_id=f"{primary_leaf.span_id}-peer",
        sequence=primary_leaf.sequence + 100,
        inputs={"customer_id": peer_value},
        failure=peer_node_failure,
    )
    run_with_peer = replace(
        item.run,
        nodes=(*item.run.nodes, peer_leaf),
        failures=(*item.run.failures, peer_public_failure),
    )
    selected_artifact = replace(
        artifact,
        items=(replace(item, run=run_with_peer),),
    )

    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.set_content(render_map_inspection(selected_artifact))
    root = page.locator('[data-hypergraph-inspect="map"]')
    root.get_by_role("button", name="Show failure").click()
    detail_text = root.locator("[data-hg-detail]").inner_text()

    assert root.locator('[data-hg-timeline-row][aria-current="true"] code').first.inner_text() == "review_group/review_customer"
    assert "Run failures · 1" in detail_text
    assert detail_text.count("ValueError: manual review: reject-outer-0") == 1
    assert detail_text.count("ValueError: manual review: reject-peer") == 1
    page.close()


class _RecordingTransport:
    def __init__(self, initial_artifact: RunInspection | MapInspection) -> None:
        self.initial_artifact = initial_artifact
        self.artifacts: list[RunInspection | MapInspection] = []
        self.failures: list[BaseException] = []

    def attach(self, session: Any) -> None:
        def publish(
            artifact: RunInspection | MapInspection,
            _urgent: bool,
        ) -> None:
            self.artifacts.append(artifact)

        snapshot, _unsubscribe = session.subscribe_with_snapshot(publish)
        self.artifacts.append(snapshot)

    def fail_to_start(self, error: BaseException) -> None:
        self.failures.append(error)


class _TransportRecorder:
    def __init__(self) -> None:
        self.transports: list[_RecordingTransport] = []

    def __call__(
        self,
        initial_artifact: RunInspection | MapInspection,
        **_kwargs: object,
    ) -> _RecordingTransport:
        transport = _RecordingTransport(initial_artifact)
        self.transports.append(transport)
        return transport


class _FailurePlan:
    def __init__(
        self,
        source: _BoundarySource,
        *,
        persistent: bool,
    ) -> None:
        mode = "persistent" if persistent else "transient"
        self.error = RuntimeError(f"{mode} {source} boundary")
        self._persistent = persistent
        self._remaining = 1

    def should_fail(self) -> bool:
        if self._persistent:
            return True
        if self._remaining == 0:
            return False
        self._remaining -= 1
        return True


class _SyncShutdownFailureRunner(SyncRunner):
    def __init__(self, plan: _FailurePlan) -> None:
        super().__init__()
        self._plan = plan

    def _shutdown_dispatcher_sync(self, dispatcher: Any) -> None:
        if self._plan.should_fail():
            raise self._plan.error
        return super()._shutdown_dispatcher_sync(dispatcher)


class _AsyncShutdownFailureRunner(AsyncRunner):
    def __init__(self, plan: _FailurePlan) -> None:
        super().__init__()
        self._plan = plan

    async def _shutdown_dispatcher_async(self, dispatcher: Any) -> None:
        if self._plan.should_fail():
            raise self._plan.error
        return await super()._shutdown_dispatcher_async(dispatcher)


class _SyncStartFailureRunner(SyncRunner):
    def __init__(self, plan: _FailurePlan) -> None:
        super().__init__()
        self._plan = plan

    @property
    def capabilities(self):
        if self._plan.should_fail():
            raise self._plan.error
        return super().capabilities


class _AsyncStartFailureRunner(AsyncRunner):
    def __init__(self, plan: _FailurePlan) -> None:
        super().__init__()
        self._plan = plan

    @property
    def capabilities(self):
        if self._plan.should_fail():
            raise self._plan.error
        return super().capabilities


def _boundary_graph() -> Graph:
    @node(output_name="doubled")
    def double(value: int) -> int:
        return value * 2

    return Graph([double], name="boundary-recovery")


def _install_map_item_release_failure(
    runner: SyncRunner | AsyncRunner,
    plan: _FailurePlan,
) -> None:
    reserve = runner._active_workflows.reserve
    reservation_count = 0

    def reserve_with_failure(workflow_id: str | None):
        nonlocal reservation_count
        reservation_count += 1
        reservation = reserve(workflow_id)
        # One-item map calls reserve for the batch, then for its real child run.
        if reservation_count % 2 == 1:
            return reservation
        if not plan.should_fail():
            return reservation
        release = reservation.release
        armed = True

        def fail_first_release() -> None:
            nonlocal armed
            if armed:
                armed = False
                raise plan.error
            release()

        reservation.release = fail_first_release  # type: ignore[method-assign]
        return reservation

    runner._active_workflows.reserve = reserve_with_failure  # type: ignore[method-assign]


def _call_boundary_runner(
    runner: SyncRunner | AsyncRunner,
    source: _BoundarySource,
    graph: Graph,
    values: dict[str, object],
) -> RunResult | MapResult:
    if isinstance(runner, SyncRunner):
        if source in {"map_item", "batch"}:
            return runner.map(
                graph,
                values,
                map_over="value",
                inspect=True,
                error_handling="continue",
            )
        return runner.run(
            graph,
            values,
            inspect=True,
            error_handling="continue",
        )

    async def execute() -> RunResult | MapResult:
        if source in {"map_item", "batch"}:
            return await runner.map(
                graph,
                values,
                map_over="value",
                inspect=True,
                error_handling="continue",
            )
        return await runner.run(
            graph,
            values,
            inspect=True,
            error_handling="continue",
        )

    return _run_async(execute)


def _capture_real_boundary(
    monkeypatch: pytest.MonkeyPatch,
    runner_kind: _RunnerKind,
    source: _BoundarySource,
    *,
    persistent: bool,
) -> tuple[
    SyncRunner | AsyncRunner,
    Graph,
    dict[str, object],
    RunInspection | MapInspection,
    RuntimeError,
]:
    recorder = _TransportRecorder()
    monkeypatch.setattr(
        _inspect_transport,
        "open_notebook_inspection_transport",
        recorder,
    )
    plan = _FailurePlan(source, persistent=persistent)
    if source == "start":
        runner: SyncRunner | AsyncRunner = _SyncStartFailureRunner(plan) if runner_kind == "sync" else _AsyncStartFailureRunner(plan)
    elif source in {"run", "batch"}:
        runner = _SyncShutdownFailureRunner(plan) if runner_kind == "sync" else _AsyncShutdownFailureRunner(plan)
    else:
        runner = SyncRunner() if runner_kind == "sync" else AsyncRunner()
        _install_map_item_release_failure(runner, plan)

    graph = _boundary_graph()
    values: dict[str, object] = {
        "value": [2] if source in {"map_item", "batch"} else 2,
    }
    if source == "map_item":
        batch = _call_boundary_runner(runner, source, graph, values)
        assert isinstance(batch, MapResult)
        assert batch.failed is True
        assert batch.failures[0].error is plan.error
        assert batch.failures[0].failure is None
        artifact = batch.inspect()._artifact
        assert artifact.error is None
        assert artifact.items[0].run is not None
        assert artifact.items[0].run.error is plan.error
        assert artifact.items[0].run.failures == ()
        return runner, graph, values, artifact, plan.error

    with pytest.raises(RuntimeError) as raised:
        _call_boundary_runner(runner, source, graph, values)
    assert raised.value is plan.error
    transport = recorder.transports[-1]
    if source == "start":
        assert transport.failures[-1] is plan.error
        artifact = transport.initial_artifact
        assert artifact.terminal is False
        assert artifact.error is None
    else:
        artifact = transport.artifacts[-1]
        assert artifact.error is plan.error
        if isinstance(artifact, RunInspection):
            assert artifact.failures == ()
    return runner, graph, values, artifact, plan.error


def _native_recovery_code(
    artifact: RunInspection | MapInspection,
    *,
    source: _BoundarySource,
    error: RuntimeError,
) -> str:
    payload = build_inspection_payload(
        artifact,
        delivery_state="saved",
        delivery_label="Saved snapshot",
    )
    kind = payload["kind"]
    assert kind in {"run", "map"}
    data = payload[kind]
    assert isinstance(data, dict)
    message = serialized_value_to_wire(serialize_value(error)) if source == "start" else {}
    markup = _native_failure_markup(
        kind=kind,
        data=data,
        message=message,
    )
    match = re.search(
        r"Smallest useful (?:result evidence|recovery code):</p>"
        r"<pre><code>(.*?)</code></pre>",
        markup,
        flags=re.DOTALL,
    )
    assert match is not None
    return html.unescape(match.group(1).replace("<wbr>", ""))


def _full_recovery_code(
    page: Page,
    artifact: RunInspection | MapInspection,
    *,
    source: _BoundarySource,
    error: RuntimeError,
) -> str:
    if source == "start":
        widget_id = "widget-real-start-boundary"
        envelope = InspectionEnvelope(
            protocol_version=INSPECTION_PROTOCOL_VERSION,
            widget_id=widget_id,
            nonce="nonce-real-start-boundary",
            sequence=2,
            delivery=InspectionDelivery(
                state="stale",
                label="Live inspection unavailable",
            ),
            artifact=artifact,
            message=serialize_value(error),
        )
        page.set_content(render_payload_channel(envelope), wait_until="load")
        frame = page.frame(name=f"{widget_id}-portable-s2-frame")
        assert frame is not None
        root = frame.locator(f'[data-hypergraph-inspect="{"map" if isinstance(artifact, MapInspection) else "run"}"]')
    else:
        page.set_content(render_map_inspection(artifact) if isinstance(artifact, MapInspection) else render_run_inspection(artifact))
        root = page.locator(f'[data-hypergraph-inspect="{"map" if isinstance(artifact, MapInspection) else "run"}"]')
        if source == "map_item":
            root.get_by_role("button", name="Show failure").click()

    code = root.locator("[data-hg-detail] pre.hg-inspect-code code")
    assert code.count() == 1
    return code.inner_text()


def _execute_recovery_code(
    code: str,
    *,
    runner: SyncRunner | AsyncRunner,
    graph: Graph,
    values: dict[str, object],
) -> tuple[dict[str, object], str]:
    if isinstance(runner, SyncRunner):
        namespace: dict[str, object] = {
            "runner": runner,
            "graph": graph,
            "values": values,
        }
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exec(code, namespace)
        return namespace, output.getvalue()

    async def execute() -> tuple[dict[str, object], str]:
        namespace: dict[str, object] = {}
        source = "async def __snippet(runner, graph, values):\n" + textwrap.indent(code, "    ") + "\n    return locals()\n"
        exec(source, namespace)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            local_values = await namespace["__snippet"](runner, graph, values)  # type: ignore[operator]
        return local_values, output.getvalue()

    return _run_async(execute)


_REAL_BOUNDARY_CASES = [
    pytest.param(source, runner_kind, surface, persistent, id=f"{source}-{runner_kind}-{surface}-{'persistent' if persistent else 'transient'}")
    for source in ("run", "map_item")
    for runner_kind in ("sync", "async")
    for surface in ("full", "native")
    for persistent in (False, True)
] + [
    pytest.param("start", "sync", "full", False, id="start-sync-full-transient"),
    pytest.param("start", "async", "native", True, id="start-async-native-persistent"),
    pytest.param("batch", "async", "full", False, id="batch-async-full-transient"),
    pytest.param("batch", "sync", "native", True, id="batch-sync-native-persistent"),
]


@pytest.mark.parametrize(
    ("source", "runner_kind", "surface", "persistent"),
    _REAL_BOUNDARY_CASES,
)
def test_real_boundary_recovery_prints_success_or_caught_error(
    browser: Browser,
    monkeypatch: pytest.MonkeyPatch,
    runner_kind: _RunnerKind,
    surface: _Surface,
    persistent: bool,
    source: _BoundarySource,
) -> None:
    runner, graph, values, artifact, error = _capture_real_boundary(
        monkeypatch,
        runner_kind,
        source,
        persistent=persistent,
    )
    if surface == "native":
        code = _native_recovery_code(
            artifact,
            source=source,
            error=error,
        )
    else:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        code = _full_recovery_code(
            page,
            artifact,
            source=source,
            error=error,
        )
        page.close()

    assert ".inputs" not in code
    assert "node_name" not in code
    if runner_kind == "async":
        assert "await runner." in code
    else:
        assert "await runner." not in code

    namespace, output = _execute_recovery_code(
        code,
        runner=runner,
        graph=graph,
        values=values,
    )
    if persistent:
        assert f"RuntimeError: {error}" in output
        return

    result_name = "batch" if source in {"map_item", "batch"} else "result"
    recovered = namespace[result_name]
    assert isinstance(recovered, (RunResult, MapResult))
    assert recovered.completed is True
    assert str(recovered) in output
