"""Deployment-supplied event processors on durable Host execution.

``serve(..., event_processors=[...])`` and ``HostRuntime(...,
event_processors=[...])`` are the seam an embedding application uses to
observe the Runs a Host worker executes. The runners are built by the
library — the runtime constructs one for an unbound graph — so without this
seam a durable Run emits no node events an application can reach, while the
same graph run in-process does.
"""

from __future__ import annotations

import asyncio

import pytest

from hypergraph import AsyncRunner, Graph, HostRuntime, RunHome, node, serve
from hypergraph.checkpointers.types import WorkflowStatus
from hypergraph.events import EventProcessor
from hypergraph.events.types import NodeEndEvent, NodeStartEvent, RunEndEvent, RunStartEvent


class Recorder(EventProcessor):
    """Collects every event it is handed, with the run it belongs to."""

    def __init__(self, label: str = "", journal: list | None = None) -> None:
        self.label = label
        self.events: list = []
        self._journal = journal

    def on_event(self, event) -> None:
        self.events.append(event)
        if self._journal is not None:
            self._journal.append((self.label, type(event).__name__))

    def node_names(self) -> list[str]:
        return [event.node_name for event in self.events if isinstance(event, NodeStartEvent)]

    def of_type(self, cls) -> list:
        return [event for event in self.events if isinstance(event, cls)]


class Exploder(EventProcessor):
    """Raises on every event — for the failure-isolation contract."""

    def on_event(self, event) -> None:
        raise RuntimeError("processor boom")


def _two_step_graph(name: str) -> Graph:
    @node(output_name="doubled")
    async def double(x: int) -> int:
        return x * 2

    @node(output_name="tripled")
    async def triple(doubled: int) -> int:
        return doubled * 3

    return Graph([double, triple], name=name)


async def _terminal(client, ref):
    async for _update in client.watch(ref):
        pass
    view = await client.get(ref)
    if view is None:
        raise AssertionError("watch ended without a terminal view")
    return view


class TestHostRuntimeEventProcessors:
    async def test_durable_run_delivers_node_events_to_the_supplied_processor(self, tmp_path):
        recorder = Recorder()
        runtime = HostRuntime(tmp_path / "runs.db", deployment_version="v1", event_processors=[recorder])
        graph = _two_step_graph("calculation")
        try:
            host = await runtime.serving(graph)
            receipt = await host.submit(graph, {"x": 2}, workflow_id="traced-run")
            view = await asyncio.wait_for(_terminal(runtime.client, receipt.run_ref), timeout=10)
            assert view.status == WorkflowStatus.COMPLETED
        finally:
            await runtime.close()

        assert sorted(recorder.node_names()) == ["double", "triple"]
        assert len(recorder.of_type(RunStartEvent)) == 1
        assert len(recorder.of_type(RunEndEvent)) == 1
        assert {event.node_name for event in recorder.of_type(NodeEndEvent)} == {"double", "triple"}

    async def test_no_processors_is_the_default_and_the_run_is_unchanged(self, tmp_path):
        runtime = HostRuntime(tmp_path / "runs.db", deployment_version="v1")
        graph = _two_step_graph("calculation")
        try:
            assert runtime._event_processors == ()
            host = await runtime.serving(graph)
            assert host._event_processors == ()
            receipt = await host.submit(graph, {"x": 2}, workflow_id="plain-run")
            view = await asyncio.wait_for(_terminal(runtime.client, receipt.run_ref), timeout=10)
            assert view.status == WorkflowStatus.COMPLETED
            assert (await runtime.client.result(receipt.run_ref)).outputs == {"doubled": 4, "tripled": 12}
        finally:
            await runtime.close()

    async def test_processors_cover_every_definition_the_runtime_serves(self, tmp_path):
        recorder = Recorder()
        runtime = HostRuntime(tmp_path / "runs.db", event_processors=[recorder])
        first = _two_step_graph("first")
        second = _two_step_graph("second")
        try:
            host = await runtime.serving(first)
            await runtime.serving(second)
            first_receipt = await host.submit(first, {"x": 1}, workflow_id="run-first")
            second_receipt = await host.submit(second, {"x": 10}, workflow_id="run-second")
            await asyncio.wait_for(_terminal(runtime.client, first_receipt.run_ref), timeout=10)
            await asyncio.wait_for(_terminal(runtime.client, second_receipt.run_ref), timeout=10)
        finally:
            await runtime.close()

        graph_names = {event.graph_name for event in recorder.of_type(RunStartEvent)}
        assert graph_names == {"first", "second"}
        assert len(recorder.node_names()) == 4

    async def test_a_graph_that_carries_its_own_runner_is_covered_too(self, tmp_path):
        """The processors are added by the Host at execution, not baked into
        the runner the runtime builds — so binding a runner to control
        concurrency does not silently turn observability off."""
        recorder = Recorder()
        runtime = HostRuntime(tmp_path / "runs.db", event_processors=[recorder])
        graph = _two_step_graph("explicit").with_runner(AsyncRunner(max_concurrency=2))
        try:
            host = await runtime.serving(graph)
            receipt = await host.submit(graph, {"x": 3}, workflow_id="explicit-run")
            view = await asyncio.wait_for(_terminal(runtime.client, receipt.run_ref), timeout=10)
            assert view.status == WorkflowStatus.COMPLETED
        finally:
            await runtime.close()

        assert sorted(recorder.node_names()) == ["double", "triple"]

    async def test_a_failing_processor_cannot_break_the_run_or_the_bus(self, tmp_path):
        recorder = Recorder()
        runtime = HostRuntime(tmp_path / "runs.db", event_processors=[Exploder(), recorder])
        graph = _two_step_graph("resilient")
        try:
            host = await runtime.serving(graph)
            receipt = await host.submit(graph, {"x": 5}, workflow_id="resilient-run")
            view = await asyncio.wait_for(_terminal(runtime.client, receipt.run_ref), timeout=10)
            assert view.status == WorkflowStatus.COMPLETED
            assert (await runtime.client.result(receipt.run_ref)).outputs == {"doubled": 10, "tripled": 30}
        finally:
            await runtime.close()

        assert sorted(recorder.node_names()) == ["double", "triple"]

    def test_a_bare_processor_is_refused_at_construction(self, tmp_path):
        with pytest.raises(TypeError, match="must be a sequence of EventProcessor"):
            HostRuntime(tmp_path / "runs.db", event_processors=Recorder())  # type: ignore[arg-type]


class TestServeEventProcessors:
    async def test_serve_carries_processors_into_durable_execution(self, tmp_path):
        recorder = Recorder()
        graph = _two_step_graph("served")
        home = RunHome.open(f"file:{tmp_path / 'runs.db'}")
        try:
            host = serve(graph.with_runner(AsyncRunner()), home=home, event_processors=[recorder])
            receipt = await host.submit(graph, {"x": 4}, workflow_id="served-run")
            worker = asyncio.create_task(host.work_forever("test-worker"))
            try:
                view = await asyncio.wait_for(_terminal(host.client, receipt.run_ref), timeout=10)
            finally:
                host.shutdown()
                await asyncio.wait_for(worker, timeout=10)
            assert view.status == WorkflowStatus.COMPLETED
        finally:
            await home.close()

        assert sorted(recorder.node_names()) == ["double", "triple"]

    async def test_serve_without_processors_keeps_an_empty_tuple(self, tmp_path):
        graph = _two_step_graph("served").with_runner(AsyncRunner())
        home = RunHome.open(f"file:{tmp_path / 'runs.db'}")
        try:
            host = serve(graph, home=home)
            assert host._event_processors == ()
        finally:
            await home.close()

    async def test_serve_refuses_a_bare_processor(self, tmp_path):
        graph = _two_step_graph("served").with_runner(AsyncRunner())
        home = RunHome.open(f"file:{tmp_path / 'runs.db'}")
        try:
            with pytest.raises(TypeError, match="must be a sequence of EventProcessor"):
                serve(graph, home=home, event_processors=Recorder())  # type: ignore[arg-type]
        finally:
            await home.close()
