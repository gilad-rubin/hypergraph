"""The exit ladder: one spelling per template, and it finishes even when it fails."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

import hypergraph.runners._shared.template_sync as template_sync_module
from hypergraph import AsyncRunner, Graph, SyncRunner, node
from hypergraph.runners._shared import run_teardown as run_teardown_module
from hypergraph.runners._shared._inspect import InspectionSession
from hypergraph.runners._shared.run_teardown import InspectionSettlement
from hypergraph.runners._shared.stop import get_stop_signal

SHARED = Path(template_sync_module.__file__).parent
TEMPLATES = ("template_sync.py", "template_async.py")


def _source(name: str) -> str:
    return (SHARED / name).read_text()


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
    """The nine exits per template are nine settle() calls, not nine ladders."""
    assert _source(template).count("teardown.settle(") == 9


def test_the_ladder_is_written_once_per_runner_family() -> None:
    """One release, one signal reset, one shutdown in each teardown class."""
    source = Path(run_teardown_module.__file__).read_text()
    assert source.count("self._reservation.release()") == 2
    assert source.count("reset_stop_signal(self._signal_token)") == 2
    assert source.count("self._shutdown_dispatcher(dispatcher)") == 2
    # Two meanings, two calls: publish what the run reached, or abort it.
    assert source.count("session.finish(") == 2


@node(output_name="greeting")
def greet(name: str) -> str:
    return f"hello {name}"


def _graph() -> Graph:
    return Graph([greet], name="teardown")


class _ShutdownRefused(RuntimeError):
    """Stands in for a processor whose shutdown raises."""


class _SyncShutdownRefusingRunner(SyncRunner):
    def _shutdown_dispatcher_sync(self, dispatcher: Any) -> None:
        raise _ShutdownRefused("SYNC-SHUTDOWN-REFUSED")


class _AsyncShutdownRefusingRunner(AsyncRunner):
    async def _shutdown_dispatcher_async(self, dispatcher: Any) -> None:
        raise _ShutdownRefused("ASYNC-SHUTDOWN-REFUSED")


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


def test_a_run_without_an_inspection_session_tells_its_transport_instead() -> None:
    """No owned session to publish: the notebook shell is settled, not left spinning."""

    class _Shell:
        def __init__(self) -> None:
            self.failed_with: BaseException | None = None

        def fail_to_start(self, error: BaseException) -> None:
            self.failed_with = error

    shell = _Shell()
    settlement = InspectionSettlement(None, transport=shell, started_at=time.time())
    error = RuntimeError("no session to publish")

    settlement.abort(error)

    assert settlement.publish(status="completed", total_duration_ms=1.0) is None
    assert shell.failed_with is error
