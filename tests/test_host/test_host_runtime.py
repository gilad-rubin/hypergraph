"""Process-level HostRuntime lifecycle and incremental serving."""

from __future__ import annotations

import asyncio

import pytest

from hypergraph import AsyncRunner, Graph, HostRuntime, RunHome, node, serve
from hypergraph.checkpointers.types import WorkflowStatus
from hypergraph.host.host import Host


def _increment_graph(name: str, *, started: asyncio.Event | None = None, release: asyncio.Event | None = None) -> Graph:
    @node(output_name="out")
    async def increment(x: int) -> int:
        if started is not None:
            started.set()
        if release is not None:
            await release.wait()
        return x + 1

    return Graph([increment], name=name)


async def _terminal(client, ref):
    async for _update in client.watch(ref):
        pass
    view = await client.get(ref)
    if view is None:
        raise AssertionError("watch ended without a terminal view")
    return view


class TestHostRuntimeLifecycle:
    async def test_constructor_is_lazy_and_client_opens_on_first_use(self, tmp_path, monkeypatch):
        opened = []
        original_open = RunHome.open

        def observe_open(path, **kwargs):
            opened.append(path)
            return original_open(path, **kwargs)

        monkeypatch.setattr(RunHome, "open", observe_open)
        runtime = HostRuntime(tmp_path / "nested" / "runs.db", deployment_version="v1")

        assert opened == []
        client = runtime.client
        assert opened == [tmp_path / "nested" / "runs.db"]
        assert client is runtime.client
        await runtime.close()

    async def test_serving_is_idempotent_and_adds_definitions_without_restarting_worker(self, tmp_path):
        started = asyncio.Event()
        release = asyncio.Event()
        first = _increment_graph("first", started=started, release=release)
        second = _increment_graph("second")
        runtime = HostRuntime(tmp_path / "runs.db", deployment_version="v1")

        try:
            host = await runtime.serving(first)
            first_receipt = await host.submit(first, {"x": 1}, workflow_id="first-run")
            await asyncio.wait_for(started.wait(), timeout=10)
            worker = runtime._worker

            assert await runtime.serving(first) is host
            assert await runtime.serving(second) is host
            assert runtime._worker is worker

            second_receipt = await host.submit(second, {"x": 10}, workflow_id="second-run")
            second_view = await asyncio.wait_for(_terminal(runtime.client, second_receipt.run_ref), timeout=10)
            assert second_view.status == WorkflowStatus.COMPLETED
            assert second_view.definition_id is not None
            assert second_view.definition_id.deployment_version == "v1"

            release.set()
            first_view = await asyncio.wait_for(_terminal(runtime.client, first_receipt.run_ref), timeout=10)
            assert first_view.status == WorkflowStatus.COMPLETED
            assert first_view.definition_id is not None
            assert first_view.definition_id.deployment_version == "v1"
            assert (await runtime.client.result(first_receipt.run_ref)).outputs == {"out": 2}
            assert (await runtime.client.result(second_receipt.run_ref)).outputs == {"out": 11}
        finally:
            release.set()
            await runtime.close()

    async def test_close_keeps_completed_work_durable(self, tmp_path):
        path = tmp_path / "runs.db"
        graph = _increment_graph("increment")
        runtime = HostRuntime(path, deployment_version="v1")
        host = await runtime.serving(graph)
        receipt = await host.submit(graph, {"x": 1}, workflow_id="durable-run")
        await asyncio.wait_for(_terminal(runtime.client, receipt.run_ref), timeout=10)

        await runtime.close()

        reopened = RunHome.open(path)
        try:
            assert reopened.get_run("durable-run").status == WorkflowStatus.COMPLETED
            assert reopened.values("durable-run") == {"out": 2}
        finally:
            await reopened.close()

    async def test_client_cannot_reopen_home_while_close_is_suspended(self, tmp_path, monkeypatch):
        closing = asyncio.Event()
        finish_close = asyncio.Event()
        original_close = RunHome.close

        async def gated_close(home):
            closing.set()
            await finish_close.wait()
            await original_close(home)

        monkeypatch.setattr(RunHome, "close", gated_close)
        runtime = HostRuntime(tmp_path / "runs.db")
        client = runtime.client
        close_task = asyncio.create_task(runtime.close())
        await asyncio.wait_for(closing.wait(), timeout=10)

        with pytest.raises(RuntimeError, match="is closing"):
            _ = runtime.client
        assert runtime._home is not None

        finish_close.set()
        await close_task
        assert runtime._home is None
        assert runtime.client is not client
        await runtime.close()

    async def test_cancelled_close_keeps_worker_drain_retryable(self, tmp_path, monkeypatch):
        started = asyncio.Event()
        release = asyncio.Event()
        shutdown_called = asyncio.Event()
        runtime = HostRuntime(tmp_path / "runs.db")
        graph = _increment_graph("increment", started=started, release=release)
        host = await runtime.serving(graph)
        await host.submit(graph, {"x": 1}, workflow_id="active-run")
        await asyncio.wait_for(started.wait(), timeout=10)
        original_shutdown = host.shutdown

        def observe_shutdown():
            original_shutdown()
            shutdown_called.set()

        monkeypatch.setattr(host, "shutdown", observe_shutdown)
        close_task = asyncio.create_task(runtime.close())
        await asyncio.wait_for(shutdown_called.wait(), timeout=10)
        close_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_task

        assert runtime._home is not None
        release.set()
        await runtime.close()
        assert runtime._home is None

    async def test_cancelled_home_close_remains_single_flight_and_retryable(self, tmp_path, monkeypatch):
        closing = asyncio.Event()
        finish_close = asyncio.Event()
        close_calls = 0
        original_close = RunHome.close

        async def gated_close(home):
            nonlocal close_calls
            close_calls += 1
            closing.set()
            await finish_close.wait()
            await original_close(home)

        monkeypatch.setattr(RunHome, "close", gated_close)
        runtime = HostRuntime(tmp_path / "runs.db")
        _ = runtime.client
        close_task = asyncio.create_task(runtime.close())
        await asyncio.wait_for(closing.wait(), timeout=10)
        close_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_task

        with pytest.raises(RuntimeError, match="is closing"):
            _ = runtime.client
        finish_close.set()
        await runtime.close()
        assert close_calls == 1
        assert runtime._home is None

    async def test_worker_failure_is_raised_on_next_call(self, tmp_path, monkeypatch):
        class WorkerFailure(Exception):
            pass

        failed = asyncio.Event()
        failure = WorkerFailure("worker exploded")

        async def fail_worker(self, worker_id, **kwargs):
            failed.set()
            raise failure

        monkeypatch.setattr(Host, "work_forever", fail_worker)
        runtime = HostRuntime(tmp_path / "runs.db")
        graph = _increment_graph("increment")
        await runtime.serving(graph)
        await asyncio.wait_for(failed.wait(), timeout=10)
        assert runtime._worker is not None and runtime._worker.done()

        with pytest.raises(RuntimeError, match="worker stopped unexpectedly") as raised:
            await runtime.serving(graph)
        assert raised.value.__cause__ is failure
        await runtime.close()

    async def test_a_cancelled_worker_is_a_clean_close_but_a_loud_next_use(self, tmp_path):
        """Close winds the worker down, so a cancellation racing the
        cooperative shutdown (an event-loop teardown, a task-group exit) is a
        CLEAN close outcome — submitted work is durable either way. What stays
        loud is USING the runtime after its worker was independently killed."""
        runtime = HostRuntime(tmp_path / "runs.db")
        await runtime.serving(_increment_graph("increment"))
        assert runtime._worker is not None
        runtime._worker.cancel()

        await runtime.close()

        survivor = HostRuntime(tmp_path / "runs2.db")
        await survivor.serving(_increment_graph("increment"))
        assert survivor._worker is not None
        survivor._worker.cancel()
        await asyncio.sleep(0)

        with pytest.raises(RuntimeError, match="worker stopped unexpectedly") as raised:
            await survivor.serving(_increment_graph("increment"))
        assert isinstance(raised.value.__cause__, asyncio.CancelledError)
        await survivor.close()


class TestIncrementalHostDefinitions:
    async def test_different_definition_identity_cannot_replace_live_name(self, tmp_path):
        @node(output_name="different")
        async def decrement(x: int) -> int:
            return x - 1

        original = _increment_graph("calculation").with_runner(AsyncRunner())
        replacement = Graph([decrement], name="calculation").with_runner(AsyncRunner())
        home = RunHome.open(tmp_path / "runs.db")
        try:
            host = serve(original, home=home)
            host.add_definition(original)
            with pytest.raises(ValueError, match="cannot be replaced in-place"):
                host.add_definition(replacement)
        finally:
            await home.close()

    async def test_runtime_re_adopts_work_claimed_before_process_loss(self, tmp_path):
        path = tmp_path / "runs.db"
        graph = _increment_graph("increment")
        home = RunHome.open(path)
        original = serve(graph.with_runner(AsyncRunner()), home=home, deployment_version="v1")
        receipt = await original.submit(graph, {"x": 1}, workflow_id="claimed-run")
        claimed = await home._claim_eligible(await home._store_now(), served=original._served_identities)
        assert [row["workflow_id"] for row in claimed] == ["claimed-run"]
        assert home._get_submission_sync("claimed-run")["state"] == "claimed"
        await home.close()

        runtime = HostRuntime(path, deployment_version="v1")
        try:
            await runtime.serving(graph)
            view = await asyncio.wait_for(_terminal(runtime.client, receipt.run_ref), timeout=10)
            assert view.status == WorkflowStatus.COMPLETED
            assert (await runtime.client.result(receipt.run_ref)).outputs == {"out": 2}
        finally:
            await runtime.close()
