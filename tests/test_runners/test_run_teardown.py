"""The exit ladder: one spelling per template, one order, and it finishes when it fails.

The order assertions here are the contract that keeps this refactor
behavior-preserving: every list below was measured on the pre-refactor base
(fd524962) with the same instrumentation.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import hypergraph.runners._shared.template_sync as template_sync_module
from hypergraph import AsyncRunner, Graph, SyncRunner, node
from hypergraph.checkpointers import MemoryCheckpointer
from hypergraph.checkpointers.types import StepRecord
from hypergraph.runners._shared import run_teardown as run_teardown_module
from hypergraph.runners._shared._inspect import InspectionSession
from hypergraph.runners._shared.run_teardown import InspectionSettlement
from hypergraph.runners._shared.stop import _WorkflowReservation, get_stop_signal

SHARED = Path(template_sync_module.__file__).parent
TEMPLATES = ("template_sync.py", "template_async.py")

# The ladder, in order. Both exit policies of both teardown classes walk it.
SYNC_LADDER = ["_settle_run_row", "_shut_down", "_release_stop_signal", "_reservation.release"]
ASYNC_LADDER = [
    "_settle_run_row",
    "_shut_down",
    "_forward_checkpoint_errors",
    "_release_limiter",
    "_release_stop_signal",
    "_reservation.release",
]


def _source(name: str) -> str:
    return (SHARED / name).read_text()


# ---------------------------------------------------------------------------
# Structure: the ladder is spelled in one place, in one order
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template", TEMPLATES)
def test_template_never_spells_the_exit_ladder_itself(template: str) -> None:
    """Every release, signal reset and dispatcher shutdown lives in run_teardown."""
    source = _source(template)
    assert "reservation.release()" not in source
    assert "reset_stop_signal(" not in source
    assert "inspection_session.finish(" not in source
    # The abstract declaration and the two constructor wirings — never a call.
    assert "self._shutdown_dispatcher_sync(dispatcher)" not in source
    assert "self._shutdown_dispatcher_async(dispatcher)" not in source


@pytest.mark.parametrize("template", TEMPLATES)
def test_every_former_shutdown_site_routes_through_one_guard(template: str) -> None:
    """The nine exits per template are nine settle calls, not nine ladders."""
    source = _source(template)
    ordinary = source.count("teardown.settle(")
    last_chance = source.count("teardown.settle_completely(")
    assert (ordinary, last_chance) == (5, 4)
    assert ordinary + last_chance == 9


def _step_order(method: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """The teardown steps this method calls, in source order."""
    steps: list[tuple[int, str]] = []
    for node_ in ast.walk(method):
        if not isinstance(node_, ast.Call) or not isinstance(node_.func, ast.Attribute):
            continue
        target = node_.func
        if isinstance(target.value, ast.Name) and target.value.id == "self":
            name = target.attr
        elif isinstance(target.value, ast.Attribute) and isinstance(target.value.value, ast.Name) and target.value.value.id == "self":
            name = f"{target.value.attr}.{target.attr}"
        else:
            continue
        if name in SYNC_LADDER or name in ASYNC_LADDER:
            steps.append((target.lineno, name))
    return [name for _, name in sorted(steps)]


@pytest.mark.parametrize(
    ("class_name", "ladder"),
    [("RunTeardown", SYNC_LADDER), ("AsyncRunTeardown", ASYNC_LADDER)],
)
def test_both_exit_policies_walk_the_same_ladder_in_the_same_order(class_name: str, ladder: list[str]) -> None:
    """settle and settle_completely differ in policy, never in order."""
    tree = ast.parse(Path(run_teardown_module.__file__).read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    policies = {
        node.name: _step_order(node)
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {"settle", "settle_completely"}
    }
    assert policies == {"settle": ladder, "settle_completely": ladder}


def test_each_step_is_implemented_once_per_runner_family() -> None:
    """One release, one signal reset, one shutdown per teardown class."""
    source = Path(run_teardown_module.__file__).read_text()
    assert source.count("self._shutdown_dispatcher(dispatcher)") == 2
    assert source.count("reset_stop_signal(self._signal_token)") == 2
    assert source.count("self._release_concurrency_limiter(self._limiter_token)") == 1
    # Two meanings, two calls: publish what the run reached, or abort it.
    assert source.count("session.finish(") == 2


# ---------------------------------------------------------------------------
# Order: the ladder's effect sequence, per exit kind
# ---------------------------------------------------------------------------


@node(output_name="greeting")
def greet(name: str) -> str:
    return f"hello {name}"


def _graph() -> Graph:
    return Graph([greet], name="teardown")


class _ShutdownRefused(RuntimeError):
    """Stands in for a processor whose shutdown raises."""


# A clean top-level run, measured on base: the template's `finally` walks the
# ladder again and re-releases the already-released reservation.
CLEAN_RUN = ["dispatcher.shutdown", "reset_stop_signal", "reservation.release", "reservation.release"]
REFUSED_RUN = [
    "dispatcher.shutdown[REFUSED]",
    "dispatcher.shutdown[REFUSED]",
    "reset_stop_signal",
    "reservation.release",
]


@pytest.fixture()
def ladder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the teardown steps a run actually takes, in order."""
    events: list[str] = []
    reset_signal = run_teardown_module.reset_stop_signal
    release = _WorkflowReservation.release

    def recording_reset(token: Any) -> None:
        events.append("reset_stop_signal")
        reset_signal(token)

    def recording_release(self: _WorkflowReservation) -> None:
        events.append("reservation.release")
        release(self)

    monkeypatch.setattr(run_teardown_module, "reset_stop_signal", recording_reset)
    monkeypatch.setattr(_WorkflowReservation, "release", recording_release)
    return events


def _sync_runner(events: list[str], *, refuse: bool = False) -> SyncRunner:
    class _Recording(SyncRunner):
        def _shutdown_dispatcher_sync(self, dispatcher: Any) -> None:
            events.append("dispatcher.shutdown[REFUSED]" if refuse else "dispatcher.shutdown")
            if refuse:
                raise _ShutdownRefused("SHUTDOWN-REFUSED")
            super()._shutdown_dispatcher_sync(dispatcher)

    return _Recording()


def _async_runner(events: list[str], *, refuse: bool = False, checkpointer: Any = None) -> AsyncRunner:
    class _Recording(AsyncRunner):
        async def _shutdown_dispatcher_async(self, dispatcher: Any) -> None:
            events.append("dispatcher.shutdown[REFUSED]" if refuse else "dispatcher.shutdown")
            if refuse:
                raise _ShutdownRefused("SHUTDOWN-REFUSED")
            await super()._shutdown_dispatcher_async(dispatcher)

        def _reset_concurrency_limiter(self, token: Any) -> None:
            events.append("reset_concurrency_limiter")
            super()._reset_concurrency_limiter(token)

    return _Recording(checkpointer=checkpointer) if checkpointer is not None else _Recording()


class _FailingSaveCheckpointer(MemoryCheckpointer):
    """Every step write fails, so the run has checkpoint errors to forward."""

    async def save_step(self, record: StepRecord) -> None:
        raise RuntimeError("disk full")


def test_sync_completion_shuts_down_then_frees_the_signal_and_the_reservation(ladder: list[str]) -> None:
    """The template's `finally` re-releases an already-released reservation; that is base."""
    _sync_runner(ladder).run(_graph(), name="world", workflow_id="wf-order-sync")

    assert ladder == CLEAN_RUN


async def test_async_completion_shuts_down_then_frees_the_signal_and_the_reservation(ladder: list[str]) -> None:
    await _async_runner(ladder).run(_graph(), name="world", workflow_id="wf-order-async")

    assert ladder == CLEAN_RUN


async def test_async_completion_shuts_the_dispatcher_down_before_forwarding_checkpoint_errors(
    ladder: list[str],
) -> None:
    """Base order: the dispatcher is down before the caller hears about save failures."""
    runner = _async_runner(ladder, checkpointer=_FailingSaveCheckpointer())

    await runner.run(
        _graph(),
        name="world",
        workflow_id="wf-order-sink",
        _checkpoint_error_sink=lambda message: ladder.append("checkpoint_sink"),
    )

    relevant = [event for event in ladder if event in {"dispatcher.shutdown", "checkpoint_sink"}]
    assert relevant[0] == "dispatcher.shutdown"
    assert set(relevant[1:]) == {"checkpoint_sink"}


async def test_async_map_shuts_the_dispatcher_down_before_releasing_the_limiter(ladder: list[str]) -> None:
    """Base order: the limiter outlives the dispatcher, not the other way round."""
    await _async_runner(ladder).map(_graph(), map_over="name", name=["a", "b"], max_concurrency=2)

    assert [event for event in ladder if event in {"dispatcher.shutdown", "reset_concurrency_limiter"}] == [
        "dispatcher.shutdown",
        "reset_concurrency_limiter",
    ]


def test_sync_a_refused_shutdown_is_retried_before_the_signal_and_the_reservation(ladder: list[str]) -> None:
    """Base order: both shutdown attempts precede the reset and the single release."""
    with pytest.raises(_ShutdownRefused):
        _sync_runner(ladder, refuse=True).run(_graph(), name="world", workflow_id="wf-refuse-sync")

    assert ladder == REFUSED_RUN


async def test_async_a_refused_shutdown_is_retried_before_the_signal_and_the_reservation(ladder: list[str]) -> None:
    with pytest.raises(_ShutdownRefused):
        await _async_runner(ladder, refuse=True).run(_graph(), name="world", workflow_id="wf-refuse-async")

    assert ladder == REFUSED_RUN


def test_sync_map_retries_its_shutdown_without_releasing_twice(ladder: list[str]) -> None:
    """A one-item map whose shutdown always refuses, measured on base."""
    with pytest.raises(_ShutdownRefused):
        _sync_runner(ladder, refuse=True).map(_graph(), map_over="name", name=["a"], workflow_id="wf-refuse-map")

    assert ladder == [
        # the item run first: its dispatcher belongs to the map, so it never shuts one down
        "reset_stop_signal",
        "reservation.release",
        "reservation.release",
        # then the map's own three attempts, before one reset and one release
        "dispatcher.shutdown[REFUSED]",
        "dispatcher.shutdown[REFUSED]",
        "dispatcher.shutdown[REFUSED]",
        "reset_stop_signal",
        "reservation.release",
    ]


# ---------------------------------------------------------------------------
# The failure path still settles everything it owns
# ---------------------------------------------------------------------------


def test_sync_a_refused_shutdown_still_frees_the_workflow_and_the_signal() -> None:
    runner = _SyncShutdownRefusingRunner()

    with pytest.raises(_ShutdownRefused, match="SYNC-SHUTDOWN-REFUSED"):
        runner.run(_graph(), name="world", workflow_id="wf-sync-teardown")

    assert not runner._active_workflows.has("wf-sync-teardown")
    assert get_stop_signal() is None
    # Free again: the reservation was released, not leaked by the failure.
    runner._active_workflows.reserve("wf-sync-teardown").release()


async def test_async_a_refused_shutdown_still_frees_the_workflow_and_the_signal() -> None:
    runner = _AsyncShutdownRefusingRunner()

    with pytest.raises(_ShutdownRefused, match="ASYNC-SHUTDOWN-REFUSED"):
        await runner.run(_graph(), name="world", workflow_id="wf-async-teardown")

    assert not runner._active_workflows.has("wf-async-teardown")
    assert get_stop_signal() is None
    runner._active_workflows.reserve("wf-async-teardown").release()


class _SyncShutdownRefusingRunner(SyncRunner):
    def _shutdown_dispatcher_sync(self, dispatcher: Any) -> None:
        raise _ShutdownRefused("SYNC-SHUTDOWN-REFUSED")


class _AsyncShutdownRefusingRunner(AsyncRunner):
    async def _shutdown_dispatcher_async(self, dispatcher: Any) -> None:
        raise _ShutdownRefused("ASYNC-SHUTDOWN-REFUSED")


def test_sync_a_refused_shutdown_still_settles_the_inspection_artifact() -> None:
    """A teardown that raises must not leave a live inspection artifact running."""
    runner = _SyncShutdownRefusingRunner()
    session = InspectionSession(graph_name="teardown", workflow_id=None, item_index=None, runner_kind="sync")

    with pytest.raises(_ShutdownRefused):
        runner.run(_graph(), name="world", inspect=True, _inspection_session=session)

    artifact = session.snapshot()
    assert artifact.terminal
    assert artifact.status == "failed"
    assert isinstance(artifact.error, _ShutdownRefused)


async def test_async_a_refused_shutdown_still_settles_the_inspection_artifact() -> None:
    runner = _AsyncShutdownRefusingRunner()
    session = InspectionSession(graph_name="teardown", workflow_id=None, item_index=None, runner_kind="async")

    with pytest.raises(_ShutdownRefused):
        await runner.run(_graph(), name="world", inspect=True, _inspection_session=session)

    artifact = session.snapshot()
    assert artifact.terminal
    assert artifact.status == "failed"
    assert isinstance(artifact.error, _ShutdownRefused)


# ---------------------------------------------------------------------------
# The duration clock: 0.0 until the run has actually started
# ---------------------------------------------------------------------------


def test_sync_an_abort_before_the_run_starts_reports_no_duration() -> None:
    """Base value: a run refused at reserve took 0.0 ms, because nothing ran."""
    runner = SyncRunner()
    held = runner._active_workflows.reserve("wf-duplicate-sync")
    session = InspectionSession(graph_name="teardown", workflow_id="wf-duplicate-sync", item_index=None, runner_kind="sync")

    try:
        with pytest.raises(Exception, match="wf-duplicate-sync"):
            runner.run(_graph(), name="world", inspect=True, workflow_id="wf-duplicate-sync", _inspection_session=session)
    finally:
        held.release()

    artifact = session.snapshot()
    assert artifact.status == "failed"
    assert artifact.total_duration_ms == 0.0


async def test_async_an_abort_before_the_run_starts_reports_no_duration() -> None:
    runner = AsyncRunner()
    held = runner._active_workflows.reserve("wf-duplicate-async")
    session = InspectionSession(graph_name="teardown", workflow_id="wf-duplicate-async", item_index=None, runner_kind="async")

    try:
        with pytest.raises(Exception, match="wf-duplicate-async"):
            await runner.run(_graph(), name="world", inspect=True, workflow_id="wf-duplicate-async", _inspection_session=session)
    finally:
        held.release()

    artifact = session.snapshot()
    assert artifact.status == "failed"
    assert artifact.total_duration_ms == 0.0


def test_an_unstarted_settlement_reports_no_duration_and_a_started_one_does() -> None:
    session = InspectionSession(graph_name="g", workflow_id=None, item_index=None, runner_kind="sync")
    settlement = InspectionSettlement(session, transport=None)

    settlement.abort(RuntimeError("before the clock starts"))

    assert session.snapshot().total_duration_ms == 0.0

    started = InspectionSettlement(
        InspectionSession(graph_name="g", workflow_id=None, item_index=None, runner_kind="sync"),
        transport=None,
    )
    started.start()
    started.abort(RuntimeError("after the clock starts"))
    assert started._session is not None
    assert started._session.snapshot().total_duration_ms >= 0.0


def test_a_run_without_an_inspection_session_tells_its_transport_instead() -> None:
    """No owned session to publish: the notebook shell is settled, not left spinning."""

    class _Shell:
        def __init__(self) -> None:
            self.failed_with: BaseException | None = None

        def fail_to_start(self, error: BaseException) -> None:
            self.failed_with = error

    shell = _Shell()
    settlement = InspectionSettlement(None, transport=shell)
    error = RuntimeError("no session to publish")

    settlement.abort(error)

    assert settlement.publish(status="completed", total_duration_ms=1.0) is None
    assert shell.failed_with is error
