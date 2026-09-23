"""Four read-outs a queue page needs, read from facts the Run Home already holds.

A page that lists durable work (an upload queue, an ingest monitor) asks four
questions of every row. Each used to cost the product a few lines of glue
that guessed; each now has an answer on the read model:

* **Which step is it on?** ``RunReadModel.pending_nodes`` — the boundaries
  the runner recorded as runnable (PRD 0013) that have not started settling.
  A frontier, never a claim that the node is executing this instant.
* **Why did it fail, in words a person can read?** ``RunReadModel.failure``
  with ``RunFailure.public_reason`` — static wording the exception CLASS
  declares, stored with the failed StepRecord. The durable ``error`` stays
  type-only, and instance text never reaches the store.
* **How many are ahead of it?** ``RunReadModel.runs_ahead`` — its place in
  claim order among the Home's claimable submissions.
* **Only the newest repeat of each Run.** ``RunQuery(repeated=False)`` —
  a Run some rerun names in ``retry_of`` is left out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from hypergraph import AsyncRunner, Graph, RunHome, RunHomeReadModel, RunQuery, node, serve
from hypergraph.checkpointers.types import WorkflowStatus

pytest.importorskip("aiosqlite")


# === Helpers ===


@pytest_asyncio.fixture
async def capped_home(tmp_path):
    """A Run Home whose worker executes one Run at a time."""
    h = RunHome.open(f"file:{tmp_path / 'runs.db'}", max_active_runs=1)
    yield h
    await h.close()


class _Gates:
    """One event per node a test holds open, and one it sets on entry."""

    def __init__(self) -> None:
        self.release: dict[str, asyncio.Event] = {}
        self.entered: dict[str, asyncio.Event] = {}

    def at(self, key: str) -> tuple[asyncio.Event, asyncio.Event]:
        return self.entered.setdefault(key, asyncio.Event()), self.release.setdefault(key, asyncio.Event())


def _ingest_graph(gates: _Gates, name: str = "ingest") -> Graph:
    """``read`` then ``extract``, each held open until the test releases it."""

    @node(output_name="text")
    async def read(path: str) -> str:
        entered, release = gates.at(f"{path}:read")
        entered.set()
        await release.wait()
        return f"text of {path}"

    @node(output_name="fields")
    async def extract(text: str) -> dict:
        path = text.removeprefix("text of ")
        entered, release = gates.at(f"{path}:extract")
        entered.set()
        await release.wait()
        return {"title": path}

    return Graph([read, extract], name=name).with_runner(AsyncRunner())


@contextlib.asynccontextmanager
async def _worker(host):
    task = asyncio.create_task(host.work_forever("w-readouts", poll_interval=0.01, drain_timeout=1.0))
    try:
        yield task
    finally:
        host.shutdown()
        await asyncio.wait_for(task, timeout=20)


def _row(rows, workflow_id: str):
    return next(row for row in rows if row.workflow_id == workflow_id)


# === G1: which step a Run is on ===


class TestPendingNodes:
    async def test_a_running_run_reports_the_step_it_is_on_and_nothing_once_settled(self, home):
        gates = _Gates()
        graph = _ingest_graph(gates)
        host = serve(graph, home=home, deployment_version="v1")
        read = RunHomeReadModel(host.client)
        receipt = await host.submit(graph, {"path": "a.pdf"}, workflow_id="paper-a")

        queued = await read.get_run(receipt.run_ref)
        assert queued is not None and queued.pending_nodes == ()  # not started: no frontier yet

        async with _worker(host):
            await asyncio.wait_for(gates.at("a.pdf:read")[0].wait(), 15)
            on_read = await read.get_run(receipt.run_ref)
            assert (on_read.status, on_read.pending_nodes) == ("running", ("read",))

            gates.at("a.pdf:read")[1].set()
            await asyncio.wait_for(gates.at("a.pdf:extract")[0].wait(), 15)
            listed = _row(await read.list_runs(RunQuery(definition="ingest")), "paper-a")
            assert listed.pending_nodes == ("extract",)
            assert read.get_run_sync(receipt.run_ref).pending_nodes == ("extract",)
            assert _row(read.list_runs_sync(RunQuery(definition="ingest")), "paper-a").pending_nodes == ("extract",)
            assert listed.to_dict()["pending_nodes"] == ["extract"]

            gates.at("a.pdf:extract")[1].set()
            await host.client.follow(receipt.run_ref, deadline=15)

        done = await read.get_run(receipt.run_ref)
        assert (done.status, done.pending_nodes) == ("completed", ())

    async def test_a_failed_run_has_no_frontier(self, home):
        @node(output_name="text")
        def read(path: str) -> str:
            raise RuntimeError("unreadable")

        @node(output_name="fields")
        def extract(text: str) -> dict:
            return {}

        graph = Graph([read, extract], name="ingest").with_runner(AsyncRunner())
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await host.submit(graph, {"path": "x.pdf"}, workflow_id="paper-x")
        async with _worker(host):
            await host.client.follow(receipt.run_ref, deadline=15)

        row = await RunHomeReadModel(host.client).get_run(receipt.run_ref)
        assert (row.status, row.pending_nodes) == ("failed", ())


# === G2: a failure reason a person can read ===


class ScanNeedsOcr(Exception):
    public_reason = "This scan needs OCR before it can be read."


class DeclaresButLeaks(Exception):
    """Declares a static reason, then tries to smuggle instance text past it."""

    public_reason = "The document could not be opened."

    def __init__(self, secret: str) -> None:
        super().__init__(secret)
        self.public_reason = f"leaked {secret}"  # instance text: never stored


class DeclaresNothing(Exception):
    pass


class DeclaresANonString(Exception):
    public_reason = 42


SECRET = "sk-live-PUBLIC-REASON-4417"


def _failing_graph(error: BaseException) -> Graph:
    @node(output_name="text")
    def read(path: str) -> str:
        raise error

    @node(output_name="fields")
    def extract(text: str) -> dict:
        return {}

    return Graph([read, extract], name="ingest").with_runner(AsyncRunner())


async def _settle_failure(home, error: BaseException, workflow_id: str):
    graph = _failing_graph(error)
    host = serve(graph, home=home, deployment_version="v1")
    receipt = await host.submit(graph, {"path": "scan.pdf"}, workflow_id=workflow_id)
    async with _worker(host):
        await host.client.follow(receipt.run_ref, deadline=15)
    return host, receipt


class SubclassOfScan(ScanNeedsOcr):
    pass


def test_the_reason_is_read_from_the_class_and_rides_the_diagnostic_wire():
    from hypergraph.diagnostics import DIAGNOSTIC_WIRE_SCHEMA, declared_public_reason, derive_diagnostic

    assert declared_public_reason(ScanNeedsOcr()) == "This scan needs OCR before it can be read."
    assert declared_public_reason(SubclassOfScan()) == "This scan needs OCR before it can be read."
    assert declared_public_reason(DeclaresButLeaks(SECRET)) == "The document could not be opened."
    assert declared_public_reason(DeclaresNothing()) is None
    assert declared_public_reason(DeclaresANonString()) is None

    wire = derive_diagnostic(ScanNeedsOcr(), node_name="read").to_wire()
    assert wire["schema"] == DIAGNOSTIC_WIRE_SCHEMA
    assert (wire["code"], wire["public_reason"]) == ("HG_NODE_FAILED", "This scan needs OCR before it can be read.")
    assert derive_diagnostic(DeclaresNothing()).to_wire()["public_reason"] is None


class TestPublicReason:
    async def test_a_declared_reason_reaches_the_read_model_and_the_outcome(self, home):
        host, receipt = await _settle_failure(home, ScanNeedsOcr("/private/uploads/scan.pdf"), "paper-scan")
        read = RunHomeReadModel(host.client)

        row = await read.get_run(receipt.run_ref)
        assert row.status == "failed"
        assert row.failure is not None
        assert row.failure.public_reason == "This scan needs OCR before it can be read."
        assert row.failure.node_name == "read"
        # The durable error projection is unchanged: type, code, static wording.
        assert row.failure.error.endswith("ScanNeedsOcr [HG_NODE_FAILED]: Node 'read' raised tests.test_host.test_run_readouts.ScanNeedsOcr.")

        listed = _row(await read.list_runs(RunQuery(definition="ingest")), "paper-scan")
        assert listed.failure == row.failure
        assert read.get_run_sync(receipt.run_ref).failure == row.failure
        assert _row(read.list_runs_sync(RunQuery(definition="ingest")), "paper-scan").failure == row.failure
        assert listed.to_dict()["failure"]["public_reason"] == "This scan needs OCR before it can be read."
        json.dumps(listed.to_dict())

        outcome = await host.client.result(receipt.run_ref)
        assert outcome.failure == row.failure
        assert host.client.result_sync(receipt.run_ref).failure == row.failure

    async def test_instance_text_never_reaches_the_store(self, home, tmp_path):
        host, receipt = await _settle_failure(home, DeclaresButLeaks(SECRET), "paper-leak")
        row = await RunHomeReadModel(host.client).get_run(receipt.run_ref)

        assert row.failure.public_reason == "The document could not be opened."
        dump = "\n".join(home._sync_db().iterdump())
        assert SECRET not in dump
        assert "leaked" not in dump

    async def test_a_chained_exception_keeps_the_reason_of_the_exception_the_node_raised(self, home):
        try:
            raise ValueError(SECRET)
        except ValueError as cause:
            chained = ScanNeedsOcr()
            chained.__cause__ = cause
        host, receipt = await _settle_failure(home, chained, "paper-chained")
        row = await RunHomeReadModel(host.client).get_run(receipt.run_ref)
        assert row.failure.public_reason == "This scan needs OCR before it can be read."

    @pytest.mark.parametrize("error", [DeclaresNothing("boom"), DeclaresANonString("boom")], ids=["undeclared", "non-string"])
    async def test_an_undeclared_reason_is_none(self, home, error):
        host, receipt = await _settle_failure(home, error, "paper-plain")
        row = await RunHomeReadModel(host.client).get_run(receipt.run_ref)
        assert row.failure is not None
        assert row.failure.public_reason is None

    async def test_a_run_that_did_not_fail_carries_no_failure(self, home):
        gates = _Gates()
        graph = _ingest_graph(gates)
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await host.submit(graph, {"path": "ok.pdf"}, workflow_id="paper-ok")
        gates.at("ok.pdf:read")[1].set()
        gates.at("ok.pdf:extract")[1].set()
        async with _worker(host):
            await host.client.follow(receipt.run_ref, deadline=15)
        row = await RunHomeReadModel(host.client).get_run(receipt.run_ref)
        assert (row.status, row.failure) == ("completed", None)


# === G3: how many Runs are ahead ===


class TestRunsAhead:
    async def test_waiting_runs_count_the_runs_ahead_of_them_in_claim_order(self, capped_home):
        gates = _Gates()
        graph = _ingest_graph(gates)
        host = serve(graph, home=capped_home, deployment_version="v1")
        read = RunHomeReadModel(host.client)
        ids = ["paper-1", "paper-2", "paper-3"]
        for index, workflow_id in enumerate(ids):
            await host.submit(graph, {"path": f"{index}.pdf"}, workflow_id=workflow_id)

        before = await read.list_runs(RunQuery(definition="ingest"))
        assert [_row(before, workflow_id).runs_ahead for workflow_id in ids] == [0, 1, 2]

        async with _worker(host):
            await asyncio.wait_for(gates.at("0.pdf:read")[0].wait(), 15)
            rows = await read.list_runs(RunQuery(definition="ingest"))
            assert [_row(rows, workflow_id).runs_ahead for workflow_id in ids] == [None, 0, 1]
            assert [(await read.get_run(_row(rows, workflow_id).run_ref)).runs_ahead for workflow_id in ids] == [None, 0, 1]
            assert [_row(read.list_runs_sync(RunQuery(definition="ingest")), workflow_id).runs_ahead for workflow_id in ids] == [None, 0, 1]
            assert read.get_run_sync(_row(rows, "paper-3").run_ref).runs_ahead == 1
            assert _row(rows, "paper-3").to_dict()["runs_ahead"] == 1
            for key in ("0.pdf:read", "0.pdf:extract", "1.pdf:read", "1.pdf:extract", "2.pdf:read", "2.pdf:extract"):
                gates.at(key)[1].set()
            for row in rows:
                await host.client.follow(row.run_ref, deadline=15)

        settled = await read.list_runs(RunQuery(definition="ingest"))
        assert [row.runs_ahead for row in settled] == [None, None, None]

    async def test_a_run_scheduled_for_later_is_not_in_line(self, capped_home):
        graph = _ingest_graph(_Gates())
        host = serve(graph, home=capped_home, deployment_version="v1")
        later = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        await host.submit(graph, {"path": "later.pdf"}, workflow_id="paper-later", start_at=later)
        await host.submit(graph, {"path": "now.pdf"}, workflow_id="paper-now")

        rows = await RunHomeReadModel(host.client).list_runs(RunQuery(definition="ingest"))
        assert (_row(rows, "paper-later").condition, _row(rows, "paper-later").runs_ahead) == ("scheduled", None)
        assert _row(rows, "paper-now").runs_ahead == 0


# === G4: only the newest repeat of each Run ===


class TestRepeatedFilter:
    async def _failed_then_rerun(self, home):
        graph = _failing_graph(ScanNeedsOcr())
        host = serve(graph, home=home, deployment_version="v1")
        first = await host.submit(graph, {"path": "scan.pdf"}, workflow_id="paper-scan")
        async with _worker(host):
            await host.client.follow(first.run_ref, deadline=15)
            second = await host.client.rerun(first.run_ref)
            await host.client.follow(second.run_ref, deadline=15)
            third = await host.client.rerun(second.run_ref)
            await host.client.follow(third.run_ref, deadline=15)
        return host, [first.workflow_id, second.workflow_id, third.workflow_id]

    async def test_repeated_false_keeps_only_the_newest_repeat(self, home):
        host, (first, second, third) = await self._failed_then_rerun(home)
        read = RunHomeReadModel(host.client)

        every = {row.workflow_id for row in await read.list_runs(RunQuery(definition="ingest"))}
        newest = [row.workflow_id for row in await read.list_runs(RunQuery(definition="ingest", repeated=False))]
        repeated = {row.workflow_id for row in await read.list_runs(RunQuery(definition="ingest", repeated=True))}

        assert every == {first, second, third}
        assert newest == [third]
        assert repeated == {first, second}
        assert [view.workflow_id for view in await host.client.list(RunQuery(repeated=False))] == [third]
        assert [view.workflow_id for view in host.client.list_sync(RunQuery(repeated=False))] == [third]
        assert [row.workflow_id for row in read.list_runs_sync(RunQuery(repeated=False))] == [third]

    async def test_repeated_is_decided_over_every_run_not_only_the_matched_ones(self, home):
        """A failed source a successful rerun repeated is not "the newest failure".

        The rerun that repeats it is COMPLETED, so a status filter drops it;
        whether the source was repeated must still be answered from it.
        """
        calls = {"read": 0}

        @node(output_name="text")
        def read(path: str) -> str:
            calls["read"] += 1
            if calls["read"] == 1:
                raise ScanNeedsOcr()
            return path

        graph = Graph([read], name="ingest").with_runner(AsyncRunner())
        host = serve(graph, home=home, deployment_version="v1")
        first = await host.submit(graph, {"path": "scan.pdf"}, workflow_id="paper-scan")
        async with _worker(host):
            await host.client.follow(first.run_ref, deadline=15)
            second = await host.client.rerun(first.run_ref)
            await host.client.follow(second.run_ref, deadline=15)

        assert [view.workflow_id for view in await host.client.list(RunQuery(status=WorkflowStatus.FAILED))] == ["paper-scan"]
        assert await host.client.list(RunQuery(status=WorkflowStatus.FAILED, repeated=False)) == []
        newest = await host.client.list(RunQuery(repeated=False))
        assert [(view.workflow_id, view.status) for view in newest] == [(second.workflow_id, WorkflowStatus.COMPLETED)]

    async def test_repeated_must_be_a_bool_or_none(self, home):
        host = serve(_ingest_graph(_Gates()), home=home, deployment_version="v1")
        with pytest.raises(TypeError, match="repeated"):
            await host.client.list(RunQuery(repeated="no"))  # type: ignore[arg-type]
