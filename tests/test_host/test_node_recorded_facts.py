"""Issue #404 — ``ctx.record``: a node's own facts in the run's durable log.

Before this, a node could put facts only in its OUTPUT: ``ctx.stream`` is an
in-process preview the bus projects away, so a long-running node had nowhere
durable to say what it was doing. ``ctx.record(kind, payload)`` appends to the
SAME per-Run gap-free sequence the host writes ``step`` and ``status`` to, so
``client.watch(ref, after=cursor)`` replays node facts and host facts in one
order, and a watcher that connects after the node finished still sees them.

The suite pins four things: the durable interleave and its replay, the closed
framework vocabulary a node may not borrow, the identical behavior of both
runner families and of a node nested inside ``as_node()``, and the honest
no-op when there is no log to append to.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import aclosing

import pytest
import pytest_asyncio

from hypergraph import (
    AsyncRunner,
    Graph,
    NodeContext,
    RunHome,
    RunRef,
    SqliteCheckpointer,
    SyncRunner,
    node,
    serve,
)
from hypergraph.checkpointers.types import RESERVED_FACT_KINDS, ReservedFactKindError, WorkflowStatus
from hypergraph.host import ReservedFactKindError as HostReservedFactKindError

aiosqlite = pytest.importorskip("aiosqlite")


# === Helpers ===


def _home_uri(tmp_path, filename: str = "runs.db") -> str:
    return f"file:{tmp_path / filename}"


@pytest_asyncio.fixture
async def home(tmp_path):
    h = RunHome.open(_home_uri(tmp_path))
    yield h
    await h.close()


def _recording_graph(name: str, *, runner, kind: str = "tool_call", payload=None, then_raise: bool = False) -> Graph:
    """One node that records two facts, optionally dying afterwards."""
    fact = {"name": "search"} if payload is None else payload

    @node(output_name="answer")
    def agent_turn(prompt: str, ctx: NodeContext) -> str:
        ctx.record(kind, fact)
        ctx.record("progress", {"pct": 100})
        if then_raise:
            raise RuntimeError("node died after recording")
        return prompt.upper()

    @node(output_name="answer")
    async def agent_turn_async(prompt: str, ctx: NodeContext) -> str:
        ctx.record(kind, fact)
        ctx.record("progress", {"pct": 100})
        if then_raise:
            raise RuntimeError("node died after recording")
        return prompt.upper()

    body = agent_turn_async if isinstance(runner, AsyncRunner) else agent_turn
    return Graph([body], name=name).with_runner(runner)


async def _worker(host, worker_id="w-404"):
    return asyncio.create_task(host.work_forever(worker_id))


async def _run_to_arrival(host, receipt, *, deadline: float = 30.0):
    """Work the Home until this ref arrives, then shut the worker down."""
    task = await _worker(host)
    try:
        return await host.client.follow(receipt.run_ref, deadline=deadline)
    finally:
        host.shutdown()
        await asyncio.wait_for(task, timeout=20)


async def _facts(client, ref, **kwargs):
    return [update async for update in client.watch(ref, **kwargs)]


def _kinds(updates):
    return [update.kind for update in updates if update.durable]


def _seq(update) -> int:
    return int(update.cursor.split(":", 1)[1])


# === The durable interleave ===


class TestNodeFactsInTheRunLog:
    @pytest.mark.parametrize("runner_factory", [SyncRunner, AsyncRunner], ids=["sync", "async"])
    async def test_node_facts_interleave_with_host_facts_on_one_sequence(self, home, runner_factory):
        """A node's facts land between ``run_started`` and the node's ``step``."""
        graph = _recording_graph("agentgraph", runner=runner_factory())
        host = serve(graph, home=home)
        receipt = await host.submit(graph, {"prompt": "hi"})

        view = await _run_to_arrival(host, receipt)
        assert view.status is WorkflowStatus.COMPLETED

        # A watcher connecting NOW — long after the node finished — replays
        # everything, node facts included. That is the late-attach promise.
        updates = await _facts(host.client, receipt.run_ref)
        assert _kinds(updates) == ["submitted", "run_started", "tool_call", "progress", "step", "status"]

        recorded = [u for u in updates if u.kind == "tool_call"]
        assert [u.payload for u in recorded] == [{"name": "search"}]
        assert all(u.durable for u in recorded)
        # One gap-free sequence shared with the host's own facts.
        assert [_seq(u) for u in updates if u.durable] == [1, 2, 3, 4, 5, 6]

    async def test_both_runner_families_produce_the_same_stream(self, tmp_path):
        """Sync and async executors behave identically — same kinds, same order."""
        streams = {}
        for label, runner in (("sync", SyncRunner()), ("async", AsyncRunner())):
            home = RunHome.open(_home_uri(tmp_path, f"{label}.db"))
            try:
                graph = _recording_graph(f"agent-{label}", runner=runner)
                host = serve(graph, home=home)
                receipt = await host.submit(graph, {"prompt": "hi"})
                view = await _run_to_arrival(host, receipt)
                assert view.status is WorkflowStatus.COMPLETED
                updates = await _facts(host.client, receipt.run_ref)
                streams[label] = [(u.kind, u.payload) for u in updates if u.durable and u.kind in {"tool_call", "progress"}]
            finally:
                await home.close()

        assert streams["sync"] == streams["async"]
        assert streams["sync"] == [("tool_call", {"name": "search"}), ("progress", {"pct": 100})]

    async def test_watch_after_a_cursor_resumes_without_repeating_node_facts(self, home):
        """``after=`` is the same cursor contract for node facts as for host facts."""
        graph = _recording_graph("agentgraph", runner=AsyncRunner())
        host = serve(graph, home=home)
        receipt = await host.submit(graph, {"prompt": "hi"})
        await _run_to_arrival(host, receipt)

        updates = await _facts(host.client, receipt.run_ref)
        cursor = next(u.cursor for u in updates if u.kind == "tool_call")

        rest = await _facts(host.client, receipt.run_ref, after=cursor)
        assert _kinds(rest) == ["progress", "step", "status"]

    async def test_a_live_watcher_sees_the_fact_while_the_node_still_runs(self, home):
        """The point of the verb: progress reaches a watcher DURING the work.

        The node records, then parks on a gate the test only opens after the
        watcher has seen the fact. If ``record`` merely buffered until the
        step committed, this would deadlock rather than fail.
        """
        gate = asyncio.Event()

        @node(output_name="answer")
        async def agent_turn(prompt: str, ctx: NodeContext) -> str:
            ctx.record("tool_call", {"name": "search"})
            await gate.wait()
            return prompt.upper()

        graph = Graph([agent_turn], name="long").with_runner(AsyncRunner())
        host = serve(graph, home=home)
        receipt = await host.submit(graph, {"prompt": "hi"})
        task = await _worker(host)
        try:
            live = []
            async with aclosing(host.client.watch(receipt.run_ref)) as stream:
                async for update in stream:
                    live.append(update)
                    if update.durable and update.kind == "tool_call":
                        break
            assert _kinds(live)[-1] == "tool_call"
            assert live[-1].payload == {"name": "search"}
        finally:
            gate.set()
            host.shutdown()
            await asyncio.wait_for(task, timeout=20)

    async def test_each_record_commits_on_its_own(self, home):
        """A node that dies after recording still leaves both facts behind.

        The crash criterion: every ``record`` is its own short transaction, so
        what is lost is at most the one that had not committed — never the
        facts already written, and never because the node's own outcome was
        a failure.
        """
        graph = _recording_graph("dying", runner=AsyncRunner(), then_raise=True)
        host = serve(graph, home=home)
        receipt = await host.submit(graph, {"prompt": "hi"})

        view = await _run_to_arrival(host, receipt)
        assert view.status is WorkflowStatus.FAILED

        updates = await _facts(host.client, receipt.run_ref)
        assert "tool_call" in _kinds(updates)
        assert "progress" in _kinds(updates)

    async def test_a_nested_node_records_to_its_own_run_log(self, home, tmp_path):
        """A node inside ``as_node()`` records exactly where its steps go.

        A nested graph executes under its own child workflow id, and its
        ``step`` facts are addressed there; a node's facts follow the same
        rule, so nested behaves like flat rather than inventing a second
        destination.
        """
        inner = _recording_graph("inner", runner=AsyncRunner())
        outer = Graph([inner.as_node(name="inner")], name="outer").with_runner(AsyncRunner())
        host = serve(outer, home=home)
        receipt = await host.submit(outer, {"prompt": "hi"})

        view = await _run_to_arrival(host, receipt)
        assert view.status is WorkflowStatus.COMPLETED

        child_ref = RunRef(home=receipt.run_ref.home, run_id=f"{receipt.run_ref.run_id}/inner")
        child = await _facts(host.client, child_ref)
        assert _kinds(child) == ["run_started", "tool_call", "progress", "step", "status"]
        # The parent's own log carries the host's facts, not the child's —
        # the same address a nested `step` fact uses.
        assert "tool_call" not in _kinds(await _facts(host.client, receipt.run_ref))


# === The closed framework vocabulary ===


class TestReservedKinds:
    async def test_a_framework_kind_is_refused_and_fails_the_run(self, home):
        """Borrowing ``step`` fails the node loudly; nothing is written."""
        graph = _recording_graph("borrower", runner=AsyncRunner(), kind="step")
        host = serve(graph, home=home)
        receipt = await host.submit(graph, {"prompt": "hi"})

        view = await _run_to_arrival(host, receipt)
        assert view.status is WorkflowStatus.FAILED

        updates = await _facts(host.client, receipt.run_ref)
        assert _kinds(updates) == ["submitted", "run_started", "step", "status"]
        # The only `step` in the log is the FRAMEWORK's, reporting the
        # failure: the refusal happened before any write, so the node's
        # borrowed `step` never reached the table.
        step = next(u for u in updates if u.kind == "step")
        assert step.payload == {"node_name": "agent_turn_async", "superstep": 0, "status": "failed"}

    def test_a_framework_kind_is_refused_with_no_store_at_all(self):
        """The refusal is a coding mistake, so it does not wait for a host."""
        graph = _recording_graph("borrower", runner=SyncRunner(), kind="child_settled")
        with pytest.raises(ReservedFactKindError, match="child_settled") as caught:
            SyncRunner().run(graph, prompt="hi")
        assert caught.value.kind == "child_settled"

    def test_the_error_is_importable_from_the_host(self):
        assert HostReservedFactKindError is ReservedFactKindError

    def test_a_non_dict_payload_is_refused(self):
        graph = _recording_graph("sloppy", runner=SyncRunner(), payload=["not", "a", "dict"])
        with pytest.raises(TypeError, match="payload must be a dict"):
            SyncRunner().run(graph, prompt="hi")

    def test_every_kind_the_framework_writes_is_reserved(self):
        """The mirror guard: a new framework kind must join the closed set."""
        from hypergraph.host import _batch_store

        batch_kinds = {
            _batch_store.MANIFEST_UPDATE_KIND,
            _batch_store.SETTLED_UPDATE_KIND,
            _batch_store.PAUSED_UPDATE_KIND,
            _batch_store.RUNNABLE_UPDATE_KIND,
            _batch_store.TRIP_UPDATE_KIND,
            _batch_store.UNSTARTED_UPDATE_KIND,
            _batch_store.ABANDONED_UPDATE_KIND,
        }
        assert batch_kinds <= RESERVED_FACT_KINDS

        # The run vocabulary has no constants — it is spelled as literals at
        # the mutation hooks and documented ONCE on `RunUpdate.kind`. Read it
        # from there, so a kind added to the docs but not to the closed set
        # fails here instead of becoming a name a node could borrow.
        from hypergraph.host.views import RunUpdate

        sentence = (RunUpdate.__doc__ or "").split("kind: Fact kind", 1)[1].split("or an event class name", 1)[0]
        documented = set(re.findall(r"``(\w+)``", sentence))
        assert documented >= {"submitted", "run_started", "step", "status"}, documented
        assert documented <= RESERVED_FACT_KINDS, documented - RESERVED_FACT_KINDS


# === No log to append to ===


class TestWithoutADurableLog:
    @pytest.mark.parametrize("runner_factory", [SyncRunner, AsyncRunner], ids=["sync", "async"])
    async def test_a_tier0_run_records_nothing_and_does_not_raise(self, runner_factory):
        """The same node body runs in-process; ``record`` returns ``None``."""
        seen: list[int | None] = []

        @node(output_name="answer")
        def agent_turn(prompt: str, ctx: NodeContext) -> str:
            seen.append(ctx.record("tool_call", {"name": "search"}))
            return prompt.upper()

        @node(output_name="answer")
        async def agent_turn_async(prompt: str, ctx: NodeContext) -> str:
            seen.append(ctx.record("tool_call", {"name": "search"}))
            return prompt.upper()

        runner = runner_factory()
        body = agent_turn_async if isinstance(runner, AsyncRunner) else agent_turn
        graph = Graph([body], name="tier0")
        result = runner.run(graph, prompt="hi") if isinstance(runner, SyncRunner) else await runner.run(graph, prompt="hi")

        assert result["answer"] == "HI"
        assert seen == [None]

    async def test_a_plain_checkpointer_keeps_no_run_log(self, tmp_path):
        """Only a Run Home keeps a run log; a plain store no-ops honestly."""
        checkpointer = SqliteCheckpointer(str(tmp_path / "plain.db"))
        try:
            assert checkpointer.append_run_fact_sync("wf-1", "tool_call", {"name": "search"}) is None
        finally:
            await checkpointer.close()

    async def test_a_run_home_returns_the_seq_it_allocated(self, home):
        """The seam itself: gap-free seqs, in call order."""
        assert home.append_run_fact_sync("wf-1", "tool_call", {"name": "search"}) == 1
        assert home.append_run_fact_sync("wf-1", "progress", {"pct": 10}) == 2
        # Per-run sequences are independent.
        assert home.append_run_fact_sync("wf-2", "tool_call", {"name": "other"}) == 1
