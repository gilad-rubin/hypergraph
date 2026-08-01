"""``RunHomeClient.result`` — what a settled Run durably produced.

The host worker discards the runner's ``RunResult``, so before this there
was no way to read back what durable work produced without loading graph
code and re-deriving it. ``result()`` reconstructs outputs from the same
checkpointer rows that back resume.

Pinned here: the four states a caller must tell apart (unknown, unsettled,
stopped-before-start, ran-and-produced-nothing), privacy-safe failure
evidence, keyed Batch reads in manifest order with no fabricated results
for items that never ran, a bounded query count for 100+ children,
crash-recovered and rerun-lineage runs, sync/async parity, and JSON-safe
transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
import pytest_asyncio

from hypergraph import (
    BatchRef,
    BatchTolerance,
    Graph,
    RunHome,
    RunHomeClient,
    RunRef,
    SyncRunner,
    node,
    serve,
)
from hypergraph.checkpointers.types import StepRecord, StepStatus, WorkflowStatus
from hypergraph.host.views import BatchOutcome, RunFailure, RunOutcome
from tests.test_host._batch_api import serve_graphs, submit_keyed

aiosqlite = pytest.importorskip("aiosqlite")

SECRET = "sk-live-RESULT-9182"


@node(output_name="doubled")
def double(x: int, item: str = "") -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3


@node
def side_effect_only(x: int, item: str = "") -> None:
    """Runs, produces no values — the ``outputs == {}`` case."""


@node(output_name="out")
def leaky(x: int, item: str = "") -> int:
    if x == 1:
        raise ValueError(f"token {SECRET} rejected")
    return x * 10


def _chain_graph(name: str = "chain") -> Graph:
    return Graph([double, triple], name=name).with_runner(SyncRunner())


def _empty_graph(name: str = "quiet") -> Graph:
    return Graph([side_effect_only], name=name).with_runner(SyncRunner())


def _leaky_graph(name: str = "leaky") -> Graph:
    return Graph([leaky], name=name).with_runner(SyncRunner())


@pytest_asyncio.fixture
async def home(tmp_path):
    h = RunHome.open(f"file:{tmp_path / 'runs.db'}")
    yield h
    await h.close()


@contextlib.asynccontextmanager
async def _worker(host, worker_id: str = "w-result", **kwargs):
    task = asyncio.create_task(host.work_forever(worker_id, **kwargs))
    try:
        yield task
    finally:
        host.shutdown()
        await asyncio.wait_for(task, timeout=20)


async def _wait_for(check, timeout: float = 15.0, interval: float = 0.02):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await check()
        if value:
            return value
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(interval)


async def _settled(client, ref):
    """Poll until the ref can no longer change outcome (Run or Batch)."""
    outcome = await client.result(ref)
    return outcome if outcome is not None and outcome.settled else None


class TestRunOutcome:
    async def test_unknown_ref_is_none(self, home):
        client = RunHomeClient(home)
        assert await client.result(RunRef(home=home.uri, run_id="never-submitted")) is None
        assert await client.result(BatchRef(home=home.uri, batch_id="no-such-batch")) is None

    async def test_completed_run_reports_its_folded_outputs(self, home):
        host, served = serve_graphs(_chain_graph(), home=home)
        receipt = await host.submit(served["chain"], {"x": 4}, workflow_id="wf-ok")
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), receipt.run_ref))

        outcome = await RunHomeClient(home).result(receipt.run_ref)
        assert isinstance(outcome, RunOutcome)
        assert outcome.workflow_id == "wf-ok"
        assert outcome.status is WorkflowStatus.COMPLETED
        assert outcome.settled is True
        assert outcome.started is True
        assert outcome.failure is None
        # Folded step outputs: every value the run's steps produced.
        assert outcome.outputs == {"doubled": 8, "tripled": 24}

    async def test_unsettled_run_withholds_outputs(self, home):
        host, served = serve_graphs(_chain_graph(), home=home)
        receipt = await host.submit(served["chain"], {"x": 4}, workflow_id="wf-pending")

        # Accepted, never claimed — no worker has run.
        outcome = await RunHomeClient(home).result(receipt.run_ref)
        assert outcome is not None
        assert outcome.settled is False
        assert outcome.started is False
        assert outcome.status is None
        assert outcome.outputs is None, "a result that can still change is never reported"

    async def test_stopped_before_start_is_settled_but_never_started(self, home):
        host, served = serve_graphs(_chain_graph(), home=home)
        receipt = await host.submit(served["chain"], {"x": 4}, workflow_id="wf-stopped")
        await RunHomeClient(home).stop(receipt.run_ref)
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), receipt.run_ref))

        outcome = await RunHomeClient(home).result(receipt.run_ref)
        assert outcome is not None
        assert outcome.settled is True
        assert outcome.started is False, "it never executed"
        assert outcome.outputs is None, "None is 'never ran', not 'produced nothing'"

    async def test_run_that_produced_nothing_reports_empty_outputs(self, home):
        host, served = serve_graphs(_empty_graph(), home=home)
        receipt = await host.submit(served["quiet"], {"x": 4}, workflow_id="wf-quiet")
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), receipt.run_ref))

        outcome = await RunHomeClient(home).result(receipt.run_ref)
        assert outcome is not None
        assert outcome.settled is True
        assert outcome.started is True
        assert outcome.outputs == {}, "ran and produced nothing is {}, never None"
        assert outcome.outputs is not None

    async def test_the_four_states_are_mutually_distinguishable(self, home):
        """The whole point: no two of them read the same."""
        host, served = serve_graphs(_chain_graph(), _empty_graph(), home=home)
        stopped = await host.submit(served["chain"], {"x": 2}, workflow_id="wf-4-stopped")
        await RunHomeClient(home).stop(stopped.run_ref)
        quiet = await host.submit(served["quiet"], {"x": 3}, workflow_id="wf-4-quiet")

        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), quiet.run_ref))
            await _wait_for(lambda: _settled(RunHomeClient(home), stopped.run_ref))

        # Submitted with no worker left to claim it: genuinely in flight.
        pending = await host.submit(served["chain"], {"x": 1}, workflow_id="wf-4-pending")

        client = RunHomeClient(home)
        unknown = await client.result(RunRef(home=home.uri, run_id="wf-4-nope"))
        readings = {
            "unknown": unknown,
            "unsettled": await client.result(pending.run_ref),
            "stopped_before_start": await client.result(stopped.run_ref),
            "produced_nothing": await client.result(quiet.run_ref),
        }
        assert readings["unknown"] is None
        assert (readings["unsettled"].settled, readings["unsettled"].outputs) == (False, None)
        assert (readings["stopped_before_start"].started, readings["stopped_before_start"].outputs) == (False, None)
        assert (readings["produced_nothing"].started, readings["produced_nothing"].outputs) == (True, {})


class TestFailureEvidence:
    async def test_failed_run_reports_privacy_safe_evidence(self, home):
        host, served = serve_graphs(_leaky_graph(), home=home)
        receipt = await host.submit(served["leaky"], {"x": 1}, workflow_id="wf-fail")
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), receipt.run_ref))

        outcome = await RunHomeClient(home).result(receipt.run_ref)
        assert outcome is not None
        assert outcome.status is WorkflowStatus.FAILED
        assert outcome.settled is True
        assert isinstance(outcome.failure, RunFailure)
        assert outcome.failure.node_name == "leaky"
        assert outcome.failure.superstep == 0
        assert "ValueError" in outcome.failure.error
        assert SECRET not in outcome.failure.error, "durable failure evidence is the safe projection"
        assert SECRET not in json.dumps(outcome.to_dict())

    async def test_completed_run_has_no_failure(self, home):
        host, served = serve_graphs(_leaky_graph(), home=home)
        receipt = await host.submit(served["leaky"], {"x": 2}, workflow_id="wf-pass")
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), receipt.run_ref))

        outcome = await RunHomeClient(home).result(receipt.run_ref)
        assert outcome is not None and outcome.failure is None
        assert outcome.outputs == {"out": 20}


class TestBatchOutcome:
    async def test_manifest_only_keys_flow_through_result_watch_and_rerun(self, home):
        graph = _chain_graph()
        host = serve(graph, home=home)
        receipt = await host.submit_batch(
            graph,
            [
                {"case_label": "station-a", "x": 2},
                {"case_label": "station-b", "x": 3},
            ],
            identity="case_label",
            workflow_id="drop-labels",
        )
        async with _worker(host):
            await _wait_for(lambda: _settled(host.client, receipt.batch_ref))

        view = await host.client.get(receipt.batch_ref)
        outcome = await host.client.result(receipt.batch_ref)
        facts = [update async for update in host.client.watch(receipt.batch_ref) if update.durable]
        rerun = await host.client.rerun(receipt.batch_ref, item_keys=["station-b"])

        assert list(view.items) == ["station-a", "station-b"]
        assert list(outcome.items) == ["station-a", "station-b"]
        assert outcome.items["station-a"].outputs == {"doubled": 4, "tripled": 12}
        assert facts[0].kind == "manifest" and facts[0].payload["item_keys"] == ["station-a", "station-b"]
        assert {fact.payload["item_key"] for fact in facts if fact.kind == "child_settled"} == {"station-a", "station-b"}
        assert list((await host.client.get(rerun.batch_ref)).items) == ["station-b"]
        rerun_child = await home._get_submission("drop-labels-retry-1:station-b")
        assert json.loads(rerun_child["inputs_json"]) == {"x": 3}

    async def test_items_are_keyed_in_manifest_order(self, home):
        host, served = serve_graphs(_chain_graph(), home=home)
        manifest = {"p-2": {"x": 2}, "p-1": {"x": 1}, "p-3": {"x": 3}}
        receipt = await submit_keyed(host, served["chain"], manifest, workflow_id="drop-order")
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), receipt.batch_ref))

        outcome = await RunHomeClient(home).result(receipt.batch_ref)
        assert isinstance(outcome, BatchOutcome)
        assert outcome.workflow_id == "drop-order"
        assert outcome.settled is True
        assert list(outcome.items) == ["p-2", "p-1", "p-3"], "manifest order, not completion order"
        assert outcome.items["p-1"].outputs == {"doubled": 2, "tripled": 6}
        assert outcome.items["p-2"].outputs == {"doubled": 4, "tripled": 12}
        assert outcome.items["p-3"].outputs == {"doubled": 6, "tripled": 18}

    async def test_unstarted_items_have_no_fabricated_result(self, home):
        host, served = serve_graphs(_chain_graph(), home=home)
        receipt = await submit_keyed(host, served["chain"], {"a": {"x": 1}, "b": {"x": 2}}, workflow_id="drop-stopped")
        await RunHomeClient(home).stop(receipt.batch_ref)
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), receipt.batch_ref))

        outcome = await RunHomeClient(home).result(receipt.batch_ref)
        view = await RunHomeClient(home).get(receipt.batch_ref)
        assert set(view.unstarted_items), "the scenario must actually produce unstarted items"
        for key in view.unstarted_items:
            assert outcome.items[key] is None, "an item that never ran has no result, not an empty one"

    async def test_mixed_batch_separates_outputs_from_failures(self, home):
        host, served = serve_graphs(_leaky_graph(), home=home)
        receipt = await submit_keyed(
            host,
            served["leaky"],
            {"ok": {"x": 2}, "bad": {"x": 1}},
            workflow_id="drop-mixed",
            tolerance=BatchTolerance(max_failed=5),
        )
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), receipt.batch_ref))

        outcome = await RunHomeClient(home).result(receipt.batch_ref)
        assert outcome.items["ok"].outputs == {"out": 20}
        assert outcome.items["ok"].failure is None
        assert outcome.items["bad"].failure is not None
        assert SECRET not in outcome.items["bad"].failure.error
        assert outcome.items["bad"].status is WorkflowStatus.FAILED

    async def test_hundred_children_read_in_a_bounded_number_of_queries(self, home):
        """One read per Batch, not one per child."""
        host, served = serve_graphs(_chain_graph(), home=home)
        manifest = {f"p-{i}": {"x": i} for i in range(120)}
        receipt = await submit_keyed(host, served["chain"], manifest, workflow_id="drop-big")
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), receipt.batch_ref), timeout=90.0)

        client = RunHomeClient(home)
        calls: list[str] = []
        real_states, real_failures = home.get_states, home.get_step_failures

        async def counted_states(run_ids):
            calls.append("states")
            return await real_states(run_ids)

        async def counted_failures(run_ids):
            calls.append("failures")
            return await real_failures(run_ids)

        home.get_states, home.get_step_failures = counted_states, counted_failures
        try:
            outcome = await client.result(receipt.batch_ref)
        finally:
            home.get_states, home.get_step_failures = real_states, real_failures

        assert len(outcome.items) == 120
        assert all(item is not None and item.outputs for item in outcome.items.values())
        assert len(calls) == 2, f"expected one batched read each, got {calls}"


class TestLineageAndRecovery:
    async def test_checkpoint_reusing_rerun_inherits_material_outputs(self, home):
        host, served = serve_graphs(_chain_graph(), home=home)
        original = await host.submit(served["chain"], {"x": 4}, workflow_id="wf-reused")
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), original.run_ref))

        retry = await host.client.rerun(original.run_ref)
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), retry.run_ref))

        outcome = await host.client.result(retry.run_ref)
        assert outcome.workflow_id == retry.workflow_id, "the requested run keeps its identity"
        assert outcome.outputs == {"doubled": 8, "tripled": 24}
        assert host.client.result_sync(retry.run_ref).outputs == outcome.outputs

    async def test_checkpoint_reusing_rerun_that_produced_nothing_stays_empty(self, home):
        host, served = serve_graphs(_empty_graph(), home=home)
        original = await host.submit(served["quiet"], {"x": 4}, workflow_id="wf-empty-reused")
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), original.run_ref))

        retry = await host.client.rerun(original.run_ref)
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), retry.run_ref))

        assert (await host.client.result(retry.run_ref)).outputs == {}

    async def test_retry_lineage_cycle_terminates(self, home):
        for run_id in ("wf-cycle-a", "wf-cycle-b"):
            home.create_run_sync(run_id, graph_name="quiet")
            home.update_run_status_sync(run_id, WorkflowStatus.COMPLETED)
        db = home._sync_db()
        db.execute("UPDATE runs SET retry_of = 'wf-cycle-b' WHERE id = 'wf-cycle-a'")
        db.execute("UPDATE runs SET retry_of = 'wf-cycle-a' WHERE id = 'wf-cycle-b'")
        db.commit()

        ref = RunRef(home=home.uri, run_id="wf-cycle-a")
        assert (await RunHomeClient(home).result(ref)).outputs == {}
        assert RunHomeClient(home).result_sync(ref).outputs == {}

    async def test_rerun_lineage_reads_each_generation_independently(self, home):
        """Rerun repeats inputs verbatim, so the fix has to be transient."""
        attempts: dict[str, int] = {"n": 0}

        @node(output_name="out")
        def flaky(x: int) -> int:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise ValueError(f"transient {SECRET} outage")
            return x * 10

        graph = Graph([flaky], name="flaky").with_runner(SyncRunner())
        host = serve(graph, home=home)
        first = await host.submit(graph, {"x": 2}, workflow_id="wf-gen-1")
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), first.run_ref))

        client = RunHomeClient(home)
        failed = await client.result(first.run_ref)
        assert failed.status is WorkflowStatus.FAILED
        assert failed.failure is not None and SECRET not in failed.failure.error

        # Repetition under a fresh workflow id, same inputs (A16).
        retry = await client.rerun(first.run_ref)
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), retry.run_ref))

        repeated = await client.result(retry.run_ref)
        assert repeated.workflow_id != failed.workflow_id
        assert repeated.status is WorkflowStatus.COMPLETED
        assert repeated.outputs == {"out": 20}
        assert repeated.failure is None
        # The source generation is untouched by its own repetition.
        still_failed = await client.result(first.run_ref)
        assert still_failed.status is WorkflowStatus.FAILED
        assert still_failed.failure is not None
        assert still_failed.outputs == {}, "the failed run committed no step values"

    async def test_crash_recovered_run_folds_pre_and_post_crash_steps(self, home):
        """A run whose steps span two worker generations reads as one result.

        Staged the way ticket-04 stages a lost process: the dead worker's
        committed step is already in the store, its submission is still
        ``claimed``, and the restart scan re-adopts it. The resumed run must
        NOT re-execute the committed step, and ``result()`` must fold what
        both generations produced into one outputs dict.
        """
        ran: dict[str, int] = {"first": 0, "second": 0}

        @node(output_name="first")
        def first_step(x: int) -> int:
            ran["first"] += 1
            return x + 1

        @node(output_name="second")
        def second_step(first: int) -> int:
            ran["second"] += 1
            return first * 2

        graph = Graph([first_step, second_step], name="crashy").with_runner(SyncRunner())
        host = serve(graph, home=home)
        receipt = await host.submit(graph, {"x": 5}, workflow_id="wf-crash")

        # The dead process's committed work, and the claim it never released.
        home.create_run_sync("wf-crash", graph_name="crashy")
        home.save_step_sync(
            StepRecord(
                run_id="wf-crash",
                superstep=0,
                node_name="first_step",
                index=0,
                status=StepStatus.COMPLETED,
                input_versions={},
                values={"first": 6},
            )
        )
        db = home._sync_db()
        db.execute("UPDATE host_submissions SET state = 'claimed' WHERE workflow_id = 'wf-crash'")
        db.commit()

        await home._restart_scan()
        assert home._get_submission_sync("wf-crash")["state"] == "pending", "the orphan must be re-adopted"

        async with _worker(host, worker_id="w-recovered"):
            await _wait_for(lambda: _settled(RunHomeClient(home), receipt.run_ref), timeout=30.0)

        outcome = await RunHomeClient(home).result(receipt.run_ref)
        assert outcome.settled is True
        assert outcome.started is True
        assert outcome.status is WorkflowStatus.COMPLETED
        assert outcome.outputs == {"first": 6, "second": 12}, "pre-crash and post-recovery steps fold together"
        assert ran["first"] == 0, "the committed step must be resumed, never re-executed"
        assert ran["second"] == 1


async def _flag(event: asyncio.Event):
    return event.is_set()


class TestTransportAndParity:
    async def test_to_dict_is_json_safe_primitives(self, home):
        host, served = serve_graphs(_chain_graph(), home=home)
        receipt = await host.submit(served["chain"], {"x": 4}, workflow_id="wf-json")
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), receipt.run_ref))

        outcome = await RunHomeClient(home).result(receipt.run_ref)
        payload = outcome.to_dict()
        assert json.loads(json.dumps(payload)) == payload
        assert payload["status"] == "completed"
        assert payload["outputs"] == {"doubled": 8, "tripled": 24}
        assert payload["failure"] is None

    async def test_batch_to_dict_keeps_none_for_unstarted(self, home):
        host, served = serve_graphs(_chain_graph(), home=home)
        receipt = await submit_keyed(host, served["chain"], {"a": {"x": 1}, "b": {"x": 2}}, workflow_id="drop-json")
        await RunHomeClient(home).stop(receipt.batch_ref)
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), receipt.batch_ref))

        payload = (await RunHomeClient(home).result(receipt.batch_ref)).to_dict()
        assert json.loads(json.dumps(payload)) == payload
        view = await RunHomeClient(home).get(receipt.batch_ref)
        for key in view.unstarted_items:
            assert payload["items"][key] is None

    async def test_sync_mirror_matches_async(self, home):
        host, served = serve_graphs(_chain_graph(), home=home)
        run = await host.submit(served["chain"], {"x": 4}, workflow_id="wf-parity")
        batch = await submit_keyed(host, served["chain"], {"a": {"x": 1}}, workflow_id="drop-parity")
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), run.run_ref))
            await _wait_for(lambda: _settled(RunHomeClient(home), batch.batch_ref))

        client = RunHomeClient(home)
        assert (await client.result(run.run_ref)).to_dict() == client.result_sync(run.run_ref).to_dict()
        assert (await client.result(batch.batch_ref)).to_dict() == client.result_sync(batch.batch_ref).to_dict()
        assert client.result_sync(RunRef(home=home.uri, run_id="nope")) is None

    async def test_returned_refs_name_this_home_not_the_callers(self, home):
        """An outcome must never claim a foreign home while carrying our rows.

        Refs are inert addresses, so a ref minted against another Home can be
        handed to this client. Every other builder rebuilds the ref from THIS
        Home's uri; ``result`` does the same.
        """
        host, served = serve_graphs(_chain_graph(), home=home)
        run = await host.submit(served["chain"], {"x": 4}, workflow_id="wf-home")
        batch = await submit_keyed(host, served["chain"], {"a": {"x": 1}}, workflow_id="drop-home")
        async with _worker(host):
            await _wait_for(lambda: _settled(RunHomeClient(home), run.run_ref))
            await _wait_for(lambda: _settled(RunHomeClient(home), batch.batch_ref))

        client = RunHomeClient(home)
        foreign_run = RunRef(home="file:/somewhere/else.db", run_id="wf-home")
        foreign_batch = BatchRef(home="file:/somewhere/else.db", batch_id=batch.batch_ref.batch_id)

        outcome = await client.result(foreign_run)
        assert outcome.run_ref.home == home.uri
        batch_outcome = await client.result(foreign_batch)
        assert batch_outcome.batch_ref.home == home.uri
        assert all(item.run_ref.home == home.uri for item in batch_outcome.items.values() if item is not None)

    async def test_wrong_ref_type_is_refused(self, home):
        client = RunHomeClient(home)
        with pytest.raises(TypeError, match="expects a RunRef or BatchRef"):
            await client.result("wf-not-a-ref")
        with pytest.raises(TypeError, match="expects a RunRef or BatchRef"):
            client.result_sync(42)
