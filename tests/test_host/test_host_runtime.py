"""Process-level HostRuntime lifecycle and incremental serving."""

from __future__ import annotations

import asyncio

import pytest

from hypergraph import AsyncRunner, BatchRef, Graph, HostRuntime, RunHome, RunHomeClient, RunRef, WaitingCondition, node, serve
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


def _gated_increment_graph(name: str, *, started: dict[int, asyncio.Event], release: dict[int, asyncio.Event]) -> Graph:
    """One Definition whose every Run announces itself and parks on its OWN gate."""

    @node(output_name="out")
    async def increment(x: int) -> int:
        started[x].set()
        await release[x].wait()
        return x + 1

    return Graph([increment], name=name)


async def _stored_cap(path) -> int | None:
    """The active-Run cap as a SECOND process would read it out of the store."""
    opened = RunHome.open(path)
    try:
        return opened.max_active_runs
    finally:
        await opened.close()


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


class TestRegisteringWithoutWorking:
    """Holding a Definition and executing it are separate decisions.

    Submission is graph-first, so a process that wants work run SOMEWHERE ELSE
    still has to hold the Definition it submits against. While one Home
    admitted one worker, `serving()` could conflate the two safely — the
    second process was refused the worker by name. Leases retire that refusal,
    so conflating them would silently make every submitter an executor.
    """

    async def test_registering_lets_a_process_submit_without_becoming_a_worker(self, tmp_path):
        path = tmp_path / "runs.db"
        graph = _increment_graph("increment")

        client = HostRuntime(path, deployment_version="v1", worker_id="client")
        try:
            host = await client.registering(graph)
            assert client._worker is None, "registering must not arm a worker"
            receipt = await host.submit(graph, {"x": 1}, workflow_id="run-over-there")
            await asyncio.sleep(0.05)
            assert client._home._get_submission_sync("run-over-there")["state"] == "pending"
        finally:
            await client.close()

        # Nothing about the submission was degraded: another process's worker
        # picks it up exactly as if the submitter had been the executor.
        executor = HostRuntime(path, deployment_version="v1", worker_id="executor")
        try:
            await executor.serving(graph)
            view = await asyncio.wait_for(_terminal(executor.client, receipt.run_ref), timeout=10)
            assert view.status == WorkflowStatus.COMPLETED
        finally:
            await executor.close()

    async def test_serving_after_registering_arms_the_same_registration(self, tmp_path):
        """Deciding to run it here yourself is one later call, not a new Home."""
        graph = _increment_graph("increment")
        runtime = HostRuntime(tmp_path / "runs.db", deployment_version="v1", worker_id="solo")
        try:
            host = await runtime.registering(graph)
            receipt = await host.submit(graph, {"x": 1}, workflow_id="mine-after-all")
            assert runtime._worker is None
            assert await runtime.serving(graph) is host
            assert runtime._worker is not None
            view = await asyncio.wait_for(_terminal(runtime.client, receipt.run_ref), timeout=10)
            assert view.status == WorkflowStatus.COMPLETED
        finally:
            await runtime.close()

    async def test_registering_builder_registers_a_constructor_and_no_worker(self, tmp_path):
        runtime = HostRuntime(tmp_path / "runs.db", worker_id="builder-only")
        try:
            host = await runtime.registering_builder("x.increment", lambda args: _increment_graph(args["name"]))
            assert runtime._worker is None
            assert host.builder_keys == frozenset({"x.increment"})
        finally:
            await runtime.close()


class TestStableWorkerId:
    async def test_a_named_worker_reclaims_its_own_claims_across_a_restart(self, tmp_path):
        """A supervised restart resumes at once only if it reuses its name.

        `_reclaim_expired(adopt_own=True)` adopts THIS worker_id's outstanding
        claims at any lease, because the process holding them is the one that
        just started and is executing nothing. Under a per-process name, a
        restart is a stranger to its own half-finished work and waits out the
        lease instead.
        """
        path = tmp_path / "runs.db"
        graph = _increment_graph("increment")
        home = RunHome.open(path)
        host = serve(graph.with_runner(AsyncRunner()), home=home, deployment_version="v1")
        receipt = await host.submit(graph, {"x": 1}, workflow_id="half-done")
        now = await home._store_now()
        await home._claim_eligible(now, served=host._served_identities, worker_id="panda-api", lease_ttl=3600.0)
        row = home._get_submission_sync("half-done")
        assert (row["state"], row["claimed_by"]) == ("claimed", "panda-api")
        await home.close()

        restarted = HostRuntime(path, deployment_version="v1", worker_id="panda-api")
        try:
            await restarted.serving(graph)
            view = await asyncio.wait_for(_terminal(restarted.client, receipt.run_ref), timeout=10)
            assert view.status == WorkflowStatus.COMPLETED
        finally:
            await restarted.close()

    async def test_the_default_worker_id_is_still_per_process(self, tmp_path):
        runtime = HostRuntime(tmp_path / "runs.db")
        assert runtime._worker_id.startswith("host-runtime-")
        await runtime.close()

    async def test_a_non_string_worker_id_is_refused_by_name(self, tmp_path):
        with pytest.raises(TypeError, match="worker_id"):
            HostRuntime(tmp_path / "runs.db", worker_id=7)


class TestDeclaredActiveRunCap:
    """A runtime that owns the Home also declares its work-admission cap.

    `serve()` takes an already-open Home, so its caller wrote
    `RunHome.open(uri, max_active_runs=4)` themselves. `HostRuntime` opens the
    Home on its own, so without this keyword a deployment had to reach into
    the runtime's private Home after construction and hope the assignment
    landed before the worker's first claim scan.
    """

    async def test_a_declared_cap_is_written_into_the_store_the_runtime_opens(self, tmp_path):
        path = tmp_path / "runs.db"
        runtime = HostRuntime(path, deployment_version="v1", max_active_runs=4)
        try:
            assert runtime.client is not None  # opening the Home writes the cap through
            assert await _stored_cap(path) == 4
        finally:
            await runtime.close()

    async def test_omitting_the_cap_adopts_the_stored_one_and_an_explicit_none_clears_it(self, tmp_path):
        """`open()`'s three-state rule, carried unchanged onto the runtime.

        A plain `None` default would make every restart of a supervised
        deployment silently overwrite the cap an operator tuned.
        """
        path = tmp_path / "runs.db"
        configured = RunHome.open(path, max_active_runs=3)
        await configured.close()

        adopting = HostRuntime(path, deployment_version="v1")
        try:
            assert adopting.client is not None
            assert await _stored_cap(path) == 3
        finally:
            await adopting.close()

        unlimited = HostRuntime(path, deployment_version="v1", max_active_runs=None)
        try:
            assert unlimited.client is not None
            assert await _stored_cap(path) is None
        finally:
            await unlimited.close()

    @pytest.mark.parametrize("cap", [1, 2])
    async def test_the_declared_cap_bounds_how_many_runs_execute_at_once(self, tmp_path, cap):
        """Three submissions, `cap` of them executing, the rest ADMISSION_LIMITED.

        Freeing one slot admits exactly the next Run in claim order, which is
        what separates "the cap held it back" from "the worker was slow".
        """
        started = {x: asyncio.Event() for x in (1, 2, 3)}
        release = {x: asyncio.Event() for x in (1, 2, 3)}
        graph = _gated_increment_graph("gated", started=started, release=release)
        runtime = HostRuntime(tmp_path / "runs.db", deployment_version="v1", worker_id="capped", max_active_runs=cap)
        try:
            host = await runtime.serving(graph)
            receipts = {x: await host.submit(graph, {"x": x}, workflow_id=f"wf-{x}") for x in (1, 2, 3)}
            admitted = list(range(1, cap + 1))

            for x in admitted:
                await asyncio.wait_for(started[x].wait(), timeout=10)

            for x in (x for x in (1, 2, 3) if x not in admitted):
                assert not started[x].is_set()
                view = await runtime.client.get(receipts[x].run_ref)
                assert view is not None
                # Held, never rejected: no runs row yet, and a typed reason why.
                assert view.status is None
                assert view.waiting is WaitingCondition.ADMISSION_LIMITED

            release[admitted[0]].set()  # one slot frees
            await asyncio.wait_for(started[cap + 1].wait(), timeout=10)

            for event in release.values():
                event.set()
            for x in (1, 2, 3):
                view = await asyncio.wait_for(_terminal(runtime.client, receipts[x].run_ref), timeout=15)
                assert view.status == WorkflowStatus.COMPLETED
        finally:
            for event in release.values():
                event.set()
            await runtime.close()

    @pytest.mark.parametrize("bad", [0, -1, True, 2.0, "2"])
    def test_a_cap_that_is_not_a_positive_int_or_none_is_refused_at_construction(self, tmp_path, bad):
        with pytest.raises(ValueError, match="max_active_runs"):
            HostRuntime(tmp_path / "runs.db", max_active_runs=bad)

    def test_the_refusal_names_the_fix_and_leaves_no_half_open_home_behind(self, tmp_path):
        path = tmp_path / "runs.db"
        with pytest.raises(ValueError, match="How to fix"):
            HostRuntime(path, max_active_runs=0)
        assert not path.exists()

    async def test_a_deployment_declares_its_name_and_its_cap_in_one_constructor(self, tmp_path):
        """Both knobs reach their destinations: the worker's name, the store's cap."""
        path = tmp_path / "runs.db"
        runtime = HostRuntime(path, deployment_version="v1", worker_id="panda-api", max_active_runs=2)
        try:
            assert runtime.client is not None
            assert runtime._worker_id == "panda-api"
            assert await _stored_cap(path) == 2
        finally:
            await runtime.close()


class TestLiveCoverage:
    async def test_live_coverage_names_what_the_workers_alive_can_execute(self, tmp_path):
        """The fact a process needs BEFORE deciding to become a worker itself.

        `submit` asks it on the caller's behalf and refuses an unanswerable
        address. A process arranging its own execution first — attaching an
        event processor to the runner that will run the work — has to ask
        before there is a submission to ask about.
        """
        path = tmp_path / "runs.db"
        graph = _increment_graph("increment")
        pulse_started = asyncio.Event()
        release_pulse = asyncio.Event()
        original_pulse = RunHome._pulse_worker

        async def gated_pulse(home, *args, **kwargs):
            pulse_started.set()
            await release_pulse.wait()
            return await original_pulse(home, *args, **kwargs)

        onlooker = HostRuntime(path, deployment_version="v1", worker_id="onlooker")
        try:
            host = await onlooker.registering(graph)
            assert (await host.live_coverage()).builders == frozenset()

            executor = HostRuntime(path, deployment_version="v1", worker_id="executor")
            # A previous worker task's publication cannot satisfy this worker's readiness.
            executor._ensure_host()._published = (
                frozenset(),
                frozenset({"x.increment"}),
            )
            patch = pytest.MonkeyPatch()
            patch.setattr(RunHome, "_pulse_worker", gated_pulse)
            try:
                serving = asyncio.create_task(executor.serving_builder("x.increment", lambda args: _increment_graph("increment")))
                await asyncio.wait_for(pulse_started.wait(), timeout=10)
                assert not serving.done(), "serving_builder must wait for durable worker coverage"
                release_pulse.set()
                await asyncio.wait_for(serving, timeout=10)

                coverage = await host.live_coverage()
                assert coverage.builders == frozenset({"x.increment"})
                assert coverage.worker_ids == frozenset({"executor"})
            finally:
                release_pulse.set()
                await executor.close()
                patch.undo()

            # A clean exit withdraws its registration, so coverage is not a
            # memory of who was once here.
            assert (await host.live_coverage()).builders == frozenset()
        finally:
            await onlooker.close()

    async def test_serving_builder_reports_a_worker_that_fails_before_ready(self, tmp_path, monkeypatch):
        failure = OSError("registry unavailable")

        async def fail_pulse(*args, **kwargs):
            raise failure

        monkeypatch.setattr(RunHome, "_pulse_worker", fail_pulse)
        runtime = HostRuntime(tmp_path / "runs.db", worker_id="executor")
        try:
            with pytest.raises(RuntimeError, match="worker stopped unexpectedly") as raised:
                await asyncio.wait_for(
                    runtime.serving_builder("x.increment", lambda args: _increment_graph("increment")),
                    timeout=10,
                )
            assert raised.value.__cause__ is failure
        finally:
            await runtime.close()


class TestPublicHomeUri:
    """The Run Home location a runtime opens is readable from the runtime.

    Refs are inert addresses: ``BatchRef(home=..., batch_id=...)``. A product
    that durably stores only the batch id — the half a person recognises —
    has to rebuild the other half later. Before this, the only public route
    to that string was still holding a ref somebody handed you earlier, so
    every such application kept the uri in a second place of its own.
    """

    def test_uri_is_the_home_location_and_does_not_force_the_lazy_open(self, tmp_path):
        """Reading the address must not be what opens the Home."""
        path = tmp_path / "nested" / "runs.db"
        runtime = HostRuntime(path, deployment_version="v1")

        assert runtime.uri == str(path)
        assert not path.exists()  # the whole point of HostRuntime is laziness
        assert not path.parent.exists()

    @pytest.mark.parametrize("spelling", ["absolute", "relative"])
    async def test_uri_equals_what_the_client_and_a_receipt_report(self, tmp_path, monkeypatch, spelling):
        """One string, three public places: runtime, client, ref.

        The relative spelling is the load-bearing case: a property that
        resolved or absolutised the path would still satisfy every
        absolute-``tmp_path`` assertion while breaking the one thing the uri
        is for — being the same string the Home reports into every ref.
        """
        if spelling == "relative":
            monkeypatch.chdir(tmp_path)
            path = "./sub/runs.db"
            expected = "sub/runs.db"  # what RunHome.open() reports back
        else:
            path = tmp_path / "runs.db"
            expected = str(path)

        runtime = HostRuntime(path, deployment_version="v1")
        try:
            before_open = runtime.uri
            host = await runtime.serving(_increment_graph("increment"))
            receipt = await host.submit(_increment_graph("increment"), {"x": 1}, workflow_id="w-1")
            await _terminal(runtime.client, receipt.run_ref)

            assert before_open == expected
            assert runtime.uri == expected
            assert runtime.client.home_uri == expected
            assert receipt.run_ref.home == expected
        finally:
            await runtime.close()

    async def test_a_second_client_opened_from_the_uri_sees_the_same_runs(self, tmp_path):
        """The falsifier: the string has to be re-openable, not just equal."""
        runtime = HostRuntime(tmp_path / "runs.db", deployment_version="v1")
        try:
            host = await runtime.serving(_increment_graph("increment"))
            receipt = await host.submit(_increment_graph("increment"), {"x": 1}, workflow_id="w-1")
            await _terminal(runtime.client, receipt.run_ref)

            onlooker = RunHome.open(runtime.uri)
            try:
                client = RunHomeClient(onlooker)
                assert client.home_uri == runtime.uri
                view = await client.get(RunRef(home=runtime.uri, run_id="w-1"))
                assert view is not None
                assert view.status == WorkflowStatus.COMPLETED
            finally:
                await onlooker.close()
        finally:
            await runtime.close()

    async def test_a_batch_ref_rebuilt_from_the_uri_round_trips(self, tmp_path):
        """The adopter's case: only the batch id was stored durably."""
        runtime = HostRuntime(tmp_path / "runs.db", deployment_version="v1")
        try:
            graph = _increment_graph("increment")
            host = await runtime.serving(graph)
            receipt = await host.submit_batch(graph, {"x": [1, 2]}, map_over="x", identity="x", workflow_id="b-1")
            stored_batch_id = receipt.batch_ref.batch_id  # all a product keeps

            rebuilt = BatchRef(home=runtime.uri, batch_id=stored_batch_id)
            view = await runtime.client.get(rebuilt)

            assert view is not None
            assert view.batch_ref == receipt.batch_ref
        finally:
            await runtime.close()

    async def test_a_batch_id_the_home_never_saw_reads_as_unknown(self, tmp_path):
        """A rebuilt ref is not a promise the Batch exists."""
        runtime = HostRuntime(tmp_path / "runs.db", deployment_version="v1")
        try:
            await runtime.serving(_increment_graph("increment"))

            assert await runtime.client.get(BatchRef(home=runtime.uri, batch_id="never-submitted")) is None
        finally:
            await runtime.close()

    async def test_an_in_memory_home_reports_what_run_home_reports(self, tmp_path):
        """No special-casing in the property: ``:memory:`` is just a string."""
        reference = RunHome.open(":memory:")
        try:
            expected = reference.uri
        finally:
            await reference.close()

        runtime = HostRuntime(":memory:", deployment_version="v1")
        try:
            assert runtime.uri == expected == ":memory:"
            assert runtime.client.home_uri == expected
        finally:
            await runtime.close()

    async def test_both_addresses_are_read_only(self, tmp_path):
        runtime = HostRuntime(tmp_path / "runs.db", deployment_version="v1")
        try:
            with pytest.raises(AttributeError):
                runtime.uri = "file:./elsewhere.db"  # type: ignore[misc]
            with pytest.raises(AttributeError):
                runtime.client.home_uri = "file:./elsewhere.db"  # type: ignore[misc]
        finally:
            await runtime.close()
