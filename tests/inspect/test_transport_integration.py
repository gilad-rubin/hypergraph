"""Runtime integration contract for notebook inspection transport."""

from __future__ import annotations

import asyncio
import inspect as python_inspect
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, interrupt, node
from hypergraph.runners._shared import _inspect_transport
from hypergraph.runners._shared._inspect import MapInspection, RunInspection
from hypergraph.runners._shared.input_normalization import runner_option_names


class _RecordingTransport:
    def __init__(self, initial_artifact: RunInspection | MapInspection) -> None:
        self.initial_artifact = initial_artifact
        self.attach_threads: list[int] = []
        self.attach_loops: list[asyncio.AbstractEventLoop | None] = []
        self.publication_threads: list[int] = []
        self.artifacts: list[RunInspection | MapInspection] = []
        self.failures: list[BaseException] = []
        self.closed = False

    def attach(self, session: Any) -> None:
        self.attach_threads.append(threading.get_ident())
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        self.attach_loops.append(loop)

        def publish(
            artifact: RunInspection | MapInspection,
            _urgent: bool,
        ) -> None:
            if self.closed:
                return
            self.publication_threads.append(threading.get_ident())
            self.artifacts.append(artifact)
            if artifact.terminal:
                self.closed = True

        snapshot, _unsubscribe = session.subscribe_with_snapshot(publish)
        self.artifacts.append(snapshot)
        if snapshot.terminal:
            self.closed = True

    def fail_to_start(self, error: BaseException) -> None:
        if not self.closed:
            self.failures.append(error)
            self.closed = True


@dataclass(frozen=True)
class _FactoryCall:
    thread_id: int
    loop: asyncio.AbstractEventLoop | None
    artifact: RunInspection | MapInspection
    require_cross_thread: bool


class _FactoryRecorder:
    def __init__(self) -> None:
        self.calls: list[_FactoryCall] = []
        self.transports: list[_RecordingTransport] = []

    def __call__(
        self,
        initial_artifact: RunInspection | MapInspection,
        *,
        require_cross_thread: bool = False,
        **_kwargs: object,
    ) -> _RecordingTransport:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        self.calls.append(
            _FactoryCall(
                thread_id=threading.get_ident(),
                loop=loop,
                artifact=initial_artifact,
                require_cross_thread=require_cross_thread,
            )
        )
        transport = _RecordingTransport(initial_artifact)
        self.transports.append(transport)
        return transport


@pytest.fixture
def factory(monkeypatch: pytest.MonkeyPatch) -> _FactoryRecorder:
    recorder = _FactoryRecorder()
    monkeypatch.setattr(
        _inspect_transport,
        "open_notebook_inspection_transport",
        recorder,
    )
    return recorder


def _graph(name: str = "transport-graph") -> Graph:
    @node(output_name="doubled")
    def double(value: int) -> int:
        return value * 2

    return Graph([double], name=name)


def test_direct_sync_run_and_map_open_once_and_preserve_settled_identity(
    factory: _FactoryRecorder,
) -> None:
    runner = SyncRunner()
    run_result = runner.run(_graph("sync-run"), {"value": 7}, inspect=True)
    run_transport = factory.transports[-1]
    map_result = runner.map(
        _graph("sync-map"),
        {"value": [3, 5]},
        map_over="value",
        inspect=True,
    )
    map_transport = factory.transports[-1]

    assert len(factory.calls) == 2
    assert isinstance(factory.calls[0].artifact, RunInspection)
    assert isinstance(factory.calls[1].artifact, MapInspection)
    assert run_transport.artifacts[-1] is run_result.inspect().artifact
    assert map_transport.artifacts[-1] is map_result.inspect().artifact
    assert run_result["doubled"] == 14
    assert [item["doubled"] for item in map_result] == [6, 10]


@pytest.mark.asyncio
async def test_direct_async_run_and_map_open_once_and_preserve_settled_identity(
    factory: _FactoryRecorder,
) -> None:
    runner = AsyncRunner()
    run_result = await runner.run(_graph("async-run"), {"value": 7}, inspect=True)
    run_transport = factory.transports[-1]
    map_result = await runner.map(
        _graph("async-map"),
        {"value": [3, 5]},
        map_over="value",
        inspect=True,
    )
    map_transport = factory.transports[-1]

    assert len(factory.calls) == 2
    assert all(call.loop is asyncio.get_running_loop() for call in factory.calls)
    assert run_transport.artifacts[-1] is run_result.inspect().artifact
    assert map_transport.artifacts[-1] is map_result.inspect().artifact


def test_sync_background_run_and_map_preopen_on_caller_and_keep_handle_surface(
    factory: _FactoryRecorder,
) -> None:
    caller = threading.get_ident()
    runner = SyncRunner()

    run_handle = runner.start_run(_graph("sync-start-run"), {"value": 2}, inspect=True)
    assert factory.calls[0].thread_id == caller
    assert factory.calls[0].require_cross_thread is True
    run_result = run_handle.result()

    map_handle = runner.start_map(
        _graph("sync-start-map"),
        {"value": [2, 4]},
        map_over="value",
        inspect=True,
    )
    assert factory.calls[1].thread_id == caller
    assert factory.calls[1].require_cross_thread is True
    map_result = map_handle.result()

    assert factory.transports[0].attach_threads == [caller]
    assert factory.transports[1].attach_threads
    assert factory.transports[1].attach_threads[0] != caller
    assert any(thread != caller for thread in factory.transports[0].publication_threads)
    assert factory.transports[0].artifacts[-1] is run_result.inspect().artifact
    assert factory.transports[1].artifacts[-1] is map_result.inspect().artifact
    for handle in (run_handle, map_handle):
        assert {name for name in vars(type(handle)) if not name.startswith("_")} == {
            "done",
            "stop",
            "result",
        }


@pytest.mark.asyncio
async def test_async_background_run_and_map_preopen_on_calling_loop(
    factory: _FactoryRecorder,
) -> None:
    loop = asyncio.get_running_loop()
    caller = threading.get_ident()
    runner = AsyncRunner()
    run_handle = runner.start_run(_graph("async-start-run"), {"value": 2}, inspect=True)
    assert len(factory.calls) == 1
    map_handle = runner.start_map(
        _graph("async-start-map"),
        {"value": [2, 4]},
        map_over="value",
        inspect=True,
    )
    assert len(factory.calls) == 2
    run_result, map_result = await asyncio.gather(
        run_handle.result(),
        map_handle.result(),
    )

    assert len(factory.calls) == 2
    assert all(call.thread_id == caller for call in factory.calls)
    assert all(call.loop is loop for call in factory.calls)
    assert all(call.require_cross_thread is False for call in factory.calls)
    assert factory.transports[0].attach_loops == [loop]
    assert factory.transports[1].attach_loops == [loop]
    assert factory.transports[0].artifacts[-1] is run_result.inspect().artifact
    assert factory.transports[1].artifacts[-1] is map_result.inspect().artifact
    for handle in (run_handle, map_handle):
        assert {name for name in vars(type(handle)) if not name.startswith("_")} == {
            "done",
            "stop",
            "result",
        }


@pytest.mark.asyncio
async def test_map_children_and_nested_graphs_never_open_duplicate_widgets(
    factory: _FactoryRecorder,
) -> None:
    sync = SyncRunner()
    sync.map(
        _graph("sync-two-items"),
        {"value": [1, 2]},
        map_over="value",
        inspect=True,
    )
    assert len(factory.calls) == 1

    inner = _graph("inner")
    outer = Graph([inner.as_node(name="child")], name="outer")
    sync.run(outer, {"value": 3}, inspect=True)
    assert len(factory.calls) == 2

    await AsyncRunner().map(
        _graph("async-two-items"),
        {"value": [1, 2]},
        map_over="value",
        inspect=True,
    )
    assert len(factory.calls) == 3


@pytest.mark.asyncio
async def test_inspect_false_and_factory_failure_are_observational(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def broken_factory(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("display factory failed")

    monkeypatch.setattr(
        _inspect_transport,
        "open_notebook_inspection_transport",
        broken_factory,
    )
    sync_plain = SyncRunner().run(_graph(), {"value": 2})
    async_plain = await AsyncRunner().run(_graph(), {"value": 2})
    assert calls == 0
    sync_captured = SyncRunner().run(_graph(), {"value": 2}, inspect=True)
    async_captured = await AsyncRunner().run(_graph(), {"value": 2}, inspect=True)

    assert calls == 2
    assert sync_plain["doubled"] == async_plain["doubled"] == 4
    assert sync_captured.inspect().artifact.terminal is True
    assert async_captured.inspect().artifact.terminal is True


@pytest.mark.asyncio
async def test_empty_sync_and_async_maps_settle_saved_artifact(
    factory: _FactoryRecorder,
) -> None:
    sync_result = SyncRunner().map(
        _graph("empty-sync"),
        {"value": []},
        map_over="value",
        inspect=True,
    )
    async_result = await AsyncRunner().map(
        _graph("empty-async"),
        {"value": []},
        map_over="value",
        inspect=True,
    )

    assert len(factory.calls) == 2
    assert factory.transports[0].artifacts[-1] is sync_result.inspect().artifact
    assert factory.transports[1].artifacts[-1] is async_result.inspect().artifact
    assert sync_result.inspect().artifact.terminal is True
    assert async_result.inspect().artifact.terminal is True


def test_direct_and_background_validation_failures_settle_exact_pending_shell(
    factory: _FactoryRecorder,
) -> None:
    graph = _graph("missing-value")
    with pytest.raises(Exception) as direct_run:
        SyncRunner().run(graph, inspect=True)
    assert factory.transports[0].failures == [direct_run.value]

    with pytest.raises(Exception) as direct_map:
        SyncRunner().map(
            graph,
            {"value": [1]},
            map_over=object(),  # type: ignore[arg-type]
            inspect=True,
        )
    assert isinstance(factory.calls[1].artifact, MapInspection)
    assert factory.transports[1].failures == [direct_map.value]

    run_handle = SyncRunner().start_run(graph, inspect=True)
    with pytest.raises(Exception) as background_run:
        run_handle.result()
    assert factory.transports[2].failures == [background_run.value]

    map_handle = SyncRunner().start_map(
        graph,
        {"value": [1]},
        map_over=object(),  # type: ignore[arg-type]
        inspect=True,
    )
    with pytest.raises(Exception) as background_map:
        map_handle.result()
    assert isinstance(factory.calls[3].artifact, MapInspection)
    assert factory.transports[3].failures == [background_map.value]


class _SyncBoundaryRunner(SyncRunner):
    def __init__(self, error: RuntimeError, *, map_only: bool) -> None:
        super().__init__()
        self._boundary_error = error
        self._map_only = map_only
        self._raised = False

    def _emit_run_end_sync(self, *args: Any, **kwargs: Any) -> None:
        is_map = kwargs.get("batch_summary") is not None
        if not self._raised and kwargs.get("status") is not None and is_map == self._map_only:
            self._raised = True
            raise self._boundary_error
        return super()._emit_run_end_sync(*args, **kwargs)


class _AsyncBoundaryRunner(AsyncRunner):
    def __init__(self, error: RuntimeError, *, map_only: bool) -> None:
        super().__init__()
        self._boundary_error = error
        self._map_only = map_only
        self._raised = False

    async def _emit_run_end_async(self, *args: Any, **kwargs: Any) -> None:
        is_map = kwargs.get("batch_summary") is not None
        if not self._raised and kwargs.get("status") is not None and is_map == self._map_only:
            self._raised = True
            raise self._boundary_error
        return await super()._emit_run_end_async(*args, **kwargs)


@pytest.mark.parametrize("map_only", [False, True])
def test_sync_late_run_and_map_boundary_errors_never_publish_completed_terminal(
    factory: _FactoryRecorder,
    map_only: bool,
) -> None:
    error = RuntimeError("SYNC-BOUNDARY-BOOM")
    runner = _SyncBoundaryRunner(error, map_only=map_only)
    with pytest.raises(RuntimeError) as raised:
        if map_only:
            runner.map(
                _graph("sync-boundary-map"),
                {"value": [1, 2]},
                map_over="value",
                inspect=True,
            )
        else:
            runner.run(_graph("sync-boundary-run"), {"value": 1}, inspect=True)

    artifacts = factory.transports[-1].artifacts
    assert raised.value is error
    assert not any(item.terminal and item.status == "completed" for item in artifacts)
    assert artifacts[-1].terminal is True
    assert artifacts[-1].error is error


@pytest.mark.asyncio
@pytest.mark.parametrize("map_only", [False, True])
async def test_async_late_run_and_map_boundary_errors_never_publish_completed_terminal(
    factory: _FactoryRecorder,
    map_only: bool,
) -> None:
    error = RuntimeError("ASYNC-BOUNDARY-BOOM")
    runner = _AsyncBoundaryRunner(error, map_only=map_only)
    with pytest.raises(RuntimeError) as raised:
        if map_only:
            await runner.map(
                _graph("async-boundary-map"),
                {"value": [1, 2]},
                map_over="value",
                inspect=True,
            )
        else:
            await runner.run(_graph("async-boundary-run"), {"value": 1}, inspect=True)

    artifacts = factory.transports[-1].artifacts
    assert raised.value is error
    assert not any(item.terminal and item.status == "completed" for item in artifacts)
    assert artifacts[-1].terminal is True
    assert artifacts[-1].error is error


def test_private_transport_parameter_is_not_a_graph_input_or_public_handle_seam() -> None:
    for method in (SyncRunner().run, SyncRunner().map, AsyncRunner().run, AsyncRunner().map):
        options = runner_option_names(method)
        assert "inspect" in options
        assert all(not name.startswith("_") for name in options)
        lifecycle_options = runner_option_names(method, include_private=True)
        assert options < lifecycle_options
        assert "_inspection_transport" in lifecycle_options
        assert "_reservation" in lifecycle_options
    for method in (SyncRunner().start_run, SyncRunner().start_map, AsyncRunner().start_run, AsyncRunner().start_map):
        assert "_inspection_transport" not in python_inspect.signature(method).parameters


def test_clean_import_and_capture_work_without_ipython() -> None:
    script = """
import builtins
real_import = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == 'IPython' or name.startswith('IPython.'):
        raise ImportError('IPython intentionally unavailable')
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked
from hypergraph import Graph, SyncRunner, node
@node(output_name='answer')
def double(value: int) -> int:
    return value * 2
result = SyncRunner().run(Graph([double], name='clean-import'), {'value': 3}, inspect=True)
assert result['answer'] == 6
assert result.inspect().artifact.terminal is True
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


class _FailingSyncCreateCheckpointer:
    def __init__(self, error: RuntimeError) -> None:
        self.error = error

    def get_run(self, _workflow_id: str) -> None:
        return None

    def create_run_sync(self, *_args: object, **_kwargs: object) -> None:
        raise self.error


class _SyncCreateFailureRunner(SyncRunner):
    def __init__(self, error: RuntimeError) -> None:
        super().__init__()
        self.failing_checkpointer = _FailingSyncCreateCheckpointer(error)

    def _get_sync_checkpointer(self, _workflow_id: str | None) -> Any:
        return self.failing_checkpointer


class _FailingAsyncCreateCheckpointer:
    def __init__(self, error: RuntimeError) -> None:
        self.error = error

    async def get_run_async(self, _workflow_id: str) -> None:
        return None

    async def create_run(self, *_args: object, **_kwargs: object) -> None:
        raise self.error


class _AsyncCreateFailureRunner(AsyncRunner):
    def __init__(self, error: RuntimeError) -> None:
        super().__init__()
        self.failing_checkpointer = _FailingAsyncCreateCheckpointer(error)

    @property
    def _checkpointer(self) -> Any:
        return self.failing_checkpointer


@pytest.mark.parametrize("map_only", [False, True])
def test_sync_parent_create_failure_settles_exact_typed_artifact(
    factory: _FactoryRecorder,
    map_only: bool,
) -> None:
    error = RuntimeError("SYNC-PARENT-CREATE-BOOM")
    runner = _SyncCreateFailureRunner(error)
    with pytest.raises(RuntimeError) as raised:
        if map_only:
            runner.map(
                _graph("sync-create-map"),
                {"value": [1]},
                map_over="value",
                workflow_id="sync-create-map",
                inspect=True,
            )
        else:
            runner.run(
                _graph("sync-create-run"),
                {"value": 1},
                workflow_id="sync-create-run",
                inspect=True,
            )

    assert raised.value is error
    assert factory.transports[-1].artifacts[-1].terminal is True
    assert factory.transports[-1].artifacts[-1].error is error


@pytest.mark.asyncio
@pytest.mark.parametrize("map_only", [False, True])
async def test_async_parent_create_failure_settles_exact_typed_artifact(
    factory: _FactoryRecorder,
    map_only: bool,
) -> None:
    error = RuntimeError("ASYNC-PARENT-CREATE-BOOM")
    runner = _AsyncCreateFailureRunner(error)
    with pytest.raises(RuntimeError) as raised:
        if map_only:
            await runner.map(
                _graph("async-create-map"),
                {"value": [1]},
                map_over="value",
                workflow_id="async-create-map",
                inspect=True,
            )
        else:
            await runner.run(
                _graph("async-create-run"),
                {"value": 1},
                workflow_id="async-create-run",
                inspect=True,
            )

    assert raised.value is error
    assert factory.transports[-1].artifacts[-1].terminal is True
    assert factory.transports[-1].artifacts[-1].error is error


class _SyncDoubleBoundaryRunner(SyncRunner):
    def __init__(self, first: RuntimeError, final: RuntimeError) -> None:
        super().__init__()
        self.first = first
        self.final = final

    def _emit_run_end_sync(self, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("status") is not None:
            raise self.first
        if kwargs.get("error") is not None:
            raise self.final
        return super()._emit_run_end_sync(*args, **kwargs)


class _AsyncDoubleBoundaryRunner(AsyncRunner):
    def __init__(self, first: RuntimeError, final: RuntimeError) -> None:
        super().__init__()
        self.first = first
        self.final = final

    async def _emit_run_end_async(self, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("status") is not None:
            raise self.first
        if kwargs.get("error") is not None:
            raise self.final
        return await super()._emit_run_end_async(*args, **kwargs)


def test_sync_failure_reporting_error_uses_final_propagated_stale_truth(
    factory: _FactoryRecorder,
) -> None:
    first = RuntimeError("SYNC-SUCCESS-BOUNDARY-BOOM")
    final = RuntimeError("SYNC-ERROR-REPORTING-BOOM")
    with pytest.raises(RuntimeError) as raised:
        _SyncDoubleBoundaryRunner(first, final).run(
            _graph("sync-double-boundary"),
            {"value": 1},
            inspect=True,
        )

    assert raised.value is final
    assert factory.transports[-1].failures == [final]
    assert not any(item.terminal for item in factory.transports[-1].artifacts)


@pytest.mark.asyncio
async def test_async_failure_reporting_error_uses_final_propagated_stale_truth(
    factory: _FactoryRecorder,
) -> None:
    first = RuntimeError("ASYNC-SUCCESS-BOUNDARY-BOOM")
    final = RuntimeError("ASYNC-ERROR-REPORTING-BOOM")
    with pytest.raises(RuntimeError) as raised:
        await _AsyncDoubleBoundaryRunner(first, final).run(
            _graph("async-double-boundary"),
            {"value": 1},
            inspect=True,
        )

    assert raised.value is final
    assert factory.transports[-1].failures == [final]
    assert not any(item.terminal for item in factory.transports[-1].artifacts)


@pytest.mark.asyncio
async def test_plain_and_nonnotebook_modes_keep_exact_sync_async_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_inspect_transport, "_is_notebook", lambda: True)
    monkeypatch.setenv("HYPERGRAPH_DISPLAY", "plain")
    plain_sync = SyncRunner().run(_graph("plain-sync"), {"value": 2}, inspect=True)
    plain_async = await AsyncRunner().run(_graph("plain-async"), {"value": 3}, inspect=True)
    monkeypatch.delenv("HYPERGRAPH_DISPLAY")
    monkeypatch.setattr(_inspect_transport, "_is_notebook", lambda: False)
    terminal_sync = SyncRunner().run(_graph("terminal-sync"), {"value": 4}, inspect=True)
    terminal_async = await AsyncRunner().run(_graph("terminal-async"), {"value": 5}, inspect=True)

    assert plain_sync.inspect().artifact.nodes[-1].outputs == {"doubled": 4}
    assert plain_async.inspect().artifact.nodes[-1].outputs == {"doubled": 6}
    assert terminal_sync.inspect().artifact.nodes[-1].outputs == {"doubled": 8}
    assert terminal_async.inspect().artifact.nodes[-1].outputs == {"doubled": 10}


@pytest.mark.asyncio
async def test_non_boolean_inspect_never_opens_a_shell(
    factory: _FactoryRecorder,
) -> None:
    with pytest.raises(TypeError, match="inspect must be a bool"):
        SyncRunner().run(_graph("bad-inspect-sync"), {"value": 1}, inspect="yes")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="inspect must be a bool"):
        await AsyncRunner().run(_graph("bad-inspect-async"), {"value": 1}, inspect="yes")  # type: ignore[arg-type]
    sync_handle = SyncRunner().start_run(
        _graph("bad-start-sync"),
        {"value": 1},
        inspect="yes",  # type: ignore[arg-type]
    )
    with pytest.raises(TypeError, match="inspect must be a bool"):
        sync_handle.result()
    async_handle = AsyncRunner().start_run(
        _graph("bad-start-async"),
        {"value": 1},
        inspect="yes",  # type: ignore[arg-type]
    )
    with pytest.raises(TypeError, match="inspect must be a bool"):
        await async_handle.result()

    assert factory.calls == []


@pytest.mark.asyncio
async def test_background_reserve_and_launch_failures_settle_preopened_shell(
    factory: _FactoryRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_reserve_error = RuntimeError("SYNC-RESERVE-BOOM")
    sync_runner = SyncRunner()

    def fail_sync_reserve(_workflow_id: str | None) -> None:
        raise sync_reserve_error

    monkeypatch.setattr(sync_runner._active_workflows, "reserve", fail_sync_reserve)
    with pytest.raises(RuntimeError) as sync_reserve:
        sync_runner.start_run(_graph("sync-reserve"), {"value": 1}, inspect=True)
    assert sync_reserve.value is sync_reserve_error
    assert factory.transports[-1].failures == [sync_reserve_error]

    from hypergraph.runners.async_ import runner as async_runner_module

    async_launch_error = RuntimeError("ASYNC-LAUNCH-BOOM")

    def fail_async_launch(*_args: object, **_kwargs: object) -> None:
        raise async_launch_error

    monkeypatch.setattr(
        async_runner_module,
        "_launch_async_execution",
        fail_async_launch,
    )
    with pytest.raises(RuntimeError) as async_launch:
        AsyncRunner().start_run(_graph("async-launch"), {"value": 1}, inspect=True)
    assert async_launch.value is async_launch_error
    assert factory.transports[-1].failures == [async_launch_error]


@pytest.mark.asyncio
async def test_direct_invalid_policy_settles_exact_run_and_map_shells(
    factory: _FactoryRecorder,
) -> None:
    operations = (
        lambda: SyncRunner().run(
            _graph("invalid-policy-sync-run"),
            {"value": 1},
            inspect=True,
            error_handling="invalid",  # type: ignore[arg-type]
        ),
        lambda: SyncRunner().map(
            _graph("invalid-policy-sync-map"),
            {"value": [1]},
            map_over="value",
            inspect=True,
            error_handling="invalid",  # type: ignore[arg-type]
        ),
    )
    for operation in operations:
        with pytest.raises(ValueError) as raised:
            operation()
        assert factory.transports[-1].failures == [raised.value]

    with pytest.raises(ValueError) as async_run:
        await AsyncRunner().run(
            _graph("invalid-policy-async-run"),
            {"value": 1},
            inspect=True,
            error_handling="invalid",  # type: ignore[arg-type]
        )
    assert factory.transports[-1].failures == [async_run.value]
    with pytest.raises(ValueError) as async_map:
        await AsyncRunner().map(
            _graph("invalid-policy-async-map"),
            {"value": [1]},
            map_over="value",
            inspect=True,
            error_handling="invalid",  # type: ignore[arg-type]
        )
    assert factory.transports[-1].failures == [async_map.value]


@pytest.mark.asyncio
async def test_direct_reservation_failures_settle_exact_run_and_map_shells(
    factory: _FactoryRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runners: tuple[tuple[SyncRunner | AsyncRunner, bool], ...] = (
        (SyncRunner(), False),
        (SyncRunner(), True),
        (AsyncRunner(), False),
        (AsyncRunner(), True),
    )
    for index, (runner, is_map) in enumerate(runners):
        error = RuntimeError(f"DIRECT-RESERVE-{index}-BOOM")

        def fail_reserve(
            _workflow_id: str | None,
            *,
            current_error: RuntimeError = error,
        ) -> None:
            raise current_error

        monkeypatch.setattr(runner._active_workflows, "reserve", fail_reserve)
        with pytest.raises(RuntimeError) as raised:
            if isinstance(runner, AsyncRunner):
                if is_map:
                    await runner.map(
                        _graph(f"async-reserve-map-{index}"),
                        {"value": [1]},
                        map_over="value",
                        inspect=True,
                    )
                else:
                    await runner.run(
                        _graph(f"async-reserve-run-{index}"),
                        {"value": 1},
                        inspect=True,
                    )
            elif is_map:
                runner.map(
                    _graph(f"sync-reserve-map-{index}"),
                    {"value": [1]},
                    map_over="value",
                    inspect=True,
                )
            else:
                runner.run(
                    _graph(f"sync-reserve-run-{index}"),
                    {"value": 1},
                    inspect=True,
                )
        assert raised.value is error
        assert factory.transports[-1].failures == [error]


class _SyncShutdownFailureRunner(SyncRunner):
    def __init__(self, error: RuntimeError) -> None:
        super().__init__()
        self.error = error
        self.raised = False

    def _shutdown_dispatcher_sync(self, dispatcher: Any) -> None:
        if not self.raised:
            self.raised = True
            raise self.error
        return super()._shutdown_dispatcher_sync(dispatcher)


class _AsyncShutdownFailureRunner(AsyncRunner):
    def __init__(self, error: RuntimeError) -> None:
        super().__init__()
        self.error = error
        self.raised = False

    async def _shutdown_dispatcher_async(self, dispatcher: Any) -> None:
        if not self.raised:
            self.raised = True
            raise self.error
        return await super()._shutdown_dispatcher_async(dispatcher)


@pytest.mark.parametrize("map_only", [False, True])
def test_sync_shutdown_failure_never_publishes_completed_terminal(
    factory: _FactoryRecorder,
    map_only: bool,
) -> None:
    error = RuntimeError("SYNC-SHUTDOWN-BOOM")
    runner = _SyncShutdownFailureRunner(error)
    with pytest.raises(RuntimeError) as raised:
        if map_only:
            runner.map(
                _graph("sync-shutdown-map"),
                {"value": [1, 2]},
                map_over="value",
                inspect=True,
            )
        else:
            runner.run(_graph("sync-shutdown-run"), {"value": 1}, inspect=True)

    assert raised.value is error
    artifacts = factory.transports[-1].artifacts
    assert not any(item.terminal and item.status == "completed" for item in artifacts)
    assert artifacts[-1].terminal is True
    assert artifacts[-1].error is error


@pytest.mark.asyncio
@pytest.mark.parametrize("map_only", [False, True])
async def test_async_shutdown_failure_never_publishes_completed_terminal(
    factory: _FactoryRecorder,
    map_only: bool,
) -> None:
    error = RuntimeError("ASYNC-SHUTDOWN-BOOM")
    runner = _AsyncShutdownFailureRunner(error)
    with pytest.raises(RuntimeError) as raised:
        if map_only:
            await runner.map(
                _graph("async-shutdown-map"),
                {"value": [1, 2]},
                map_over="value",
                inspect=True,
            )
        else:
            await runner.run(_graph("async-shutdown-run"), {"value": 1}, inspect=True)

    assert raised.value is error
    artifacts = factory.transports[-1].artifacts
    assert not any(item.terminal and item.status == "completed" for item in artifacts)
    assert artifacts[-1].terminal is True
    assert artifacts[-1].error is error


@pytest.mark.asyncio
async def test_async_pause_shutdown_failure_never_publishes_paused_terminal(
    factory: _FactoryRecorder,
) -> None:
    @interrupt(output_name="decision")
    def review(value: int) -> str | None:
        return None

    error = RuntimeError("ASYNC-PAUSE-SHUTDOWN-BOOM")
    with pytest.raises(RuntimeError) as raised:
        await _AsyncShutdownFailureRunner(error).run(
            Graph([review], name="async-pause-shutdown"),
            {"value": 1},
            inspect=True,
        )

    assert raised.value is error
    assert not any(item.terminal and item.status == "paused" for item in factory.transports[-1].artifacts)
    assert factory.transports[-1].failures == [error]


@pytest.mark.asyncio
async def test_direct_lineage_and_progress_setup_failures_settle_exact_shells(
    factory: _FactoryRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations = (
        lambda: SyncRunner().run(
            _graph("sync-lineage"),
            {"value": 1},
            inspect=True,
            fork_from="missing-parent",
        ),
        lambda: AsyncRunner().run(
            _graph("async-lineage"),
            {"value": 1},
            inspect=True,
            fork_from="missing-parent",
        ),
    )
    for operation in operations:
        with pytest.raises(ValueError) as raised:
            result = operation()
            if python_inspect.isawaitable(result):
                await result
        assert factory.transports[-1].failures == [raised.value]

    error = RuntimeError("PROGRESS-SETUP-BOOM")

    def fail_progress(_processors: object) -> None:
        raise error

    monkeypatch.setattr(
        "hypergraph.runners._shared.scheduling.ensure_progress_processor",
        fail_progress,
    )
    progress_operations = (
        lambda: SyncRunner().run(
            _graph("sync-progress-run"),
            {"value": 1},
            inspect=True,
            show_progress=True,
        ),
        lambda: SyncRunner().map(
            _graph("sync-progress-map"),
            {"value": [1]},
            map_over="value",
            inspect=True,
            show_progress=True,
        ),
        lambda: AsyncRunner().run(
            _graph("async-progress-run"),
            {"value": 1},
            inspect=True,
            show_progress=True,
        ),
        lambda: AsyncRunner().map(
            _graph("async-progress-map"),
            {"value": [1]},
            map_over="value",
            inspect=True,
            show_progress=True,
        ),
    )
    for operation in progress_operations:
        with pytest.raises(RuntimeError) as raised:
            result = operation()
            if python_inspect.isawaitable(result):
                await result
        assert raised.value is error
        assert factory.transports[-1].failures == [error]


@pytest.mark.asyncio
async def test_async_unbounded_map_rejection_settles_exact_shell(
    factory: _FactoryRecorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "hypergraph.runners._shared.template_async.MAX_UNBOUNDED_MAP_TASKS",
        0,
    )
    with pytest.raises(ValueError) as raised:
        await AsyncRunner().map(
            _graph("async-unbounded-map"),
            {"value": [1]},
            map_over="value",
            inspect=True,
        )
    assert factory.transports[-1].failures == [raised.value]
