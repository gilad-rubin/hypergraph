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

import ast
import asyncio
import contextlib
import pathlib
from collections import Counter
from contextlib import aclosing

import pytest
import pytest_asyncio

import hypergraph.checkpointers.sqlite
import hypergraph.host.home
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


def _run_update_kinds_written_in_source() -> set[str]:
    """Every literal ``kind`` handed to a run_updates append, read from the AST.

    The four entry points take ``kind`` third (the ``_sync`` mirrors, which
    lead with the open connection) or second (the async ones). A name rather
    than a literal is resolved against the defining module, so a call site
    using a constant still counts.
    """
    appenders = {"_after_run_mutation_sync": 2, "_append_run_update_sync": 2, "_after_run_mutation": 1, "_append_run_update": 1}
    kinds: set[str] = set()
    for module in (hypergraph.host.home, hypergraph.checkpointers.sqlite):
        tree = ast.parse(pathlib.Path(module.__file__).read_text())
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)
            position = appenders.get(name or "")
            if position is None:
                continue
            argument = next((kw.value for kw in call.keywords if kw.arg == "kind"), None)
            if argument is None and len(call.args) > position:
                argument = call.args[position]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                kinds.add(argument.value)
            elif isinstance(argument, ast.Name):
                resolved = getattr(module, argument.id, None)
                if isinstance(resolved, str):
                    kinds.add(resolved)
    return kinds


def _recording_graph(name: str, *, runner, kind: str = "tool_call", payload=None, then_raise: bool = False, body_kind: str | None = None) -> Graph:
    """One node that records two facts, optionally dying afterwards.

    ``body_kind`` picks the body independently of the runner, so the matrix
    that matters — runner family x body kind — can actually be built. It
    defaults to the pairing whose write path is the family's own: an
    ``async def`` under ``AsyncRunner`` (a loop task), a plain ``def`` under
    ``SyncRunner`` (straight through).
    """
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

    if body_kind is None:
        body_kind = "async def" if isinstance(runner, AsyncRunner) else "def"
    body = agent_turn_async if body_kind == "async def" else agent_turn
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

    async def test_every_supported_family_and_body_kind_produces_the_same_stream(self, tmp_path):
        """The real matrix: runner family x body kind, not one cell per family.

        ``record`` has two write paths, and the body kind — not the family —
        picks between them: only a body on the executor's OWN loop defers. So
        ``AsyncRunner`` + a plain ``def`` (dispatched to a thread) is a third
        cell, and it is the one a family-shaped test never builds.
        """
        cells = [("sync", SyncRunner, "def"), ("async", AsyncRunner, "async def"), ("async-def-body", AsyncRunner, "def")]
        streams = {}
        for label, runner_factory, body_kind in cells:
            home = RunHome.open(_home_uri(tmp_path, f"{label}.db"))
            try:
                graph = _recording_graph(f"agent-{label}", runner=runner_factory(), body_kind=body_kind)
                host = serve(graph, home=home)
                receipt = await host.submit(graph, {"prompt": "hi"})
                view = await _run_to_arrival(host, receipt)
                assert view.status is WorkflowStatus.COMPLETED
                updates = await _facts(host.client, receipt.run_ref)
                streams[label] = [(u.kind, u.payload) for u in updates if u.durable and u.kind in {"tool_call", "progress"}]
            finally:
                await home.close()

        assert len({repr(stream) for stream in streams.values()}) == 1, streams
        assert streams["sync"] == [("tool_call", {"name": "search"}), ("progress", {"pct": 100})]

    def test_the_fourth_cell_is_refused_before_any_node_runs(self):
        """``SyncRunner`` + ``async def`` is not a gap in the matrix above.

        It is the one pairing the framework declines outright, so the matrix
        is three cells by construction rather than three by omission.
        """
        from hypergraph.exceptions import IncompatibleRunnerError

        graph = _recording_graph("mismatched", runner=SyncRunner(), body_kind="async def")
        with pytest.raises(IncompatibleRunnerError, match="doesn't support async"):
            SyncRunner().run(graph, prompt="hi")

    async def test_a_def_body_that_drives_its_own_loop_writes_through_the_thread_path(self, home):
        """A sync body wrapping an async client is not "on the loop".

        ``AsyncRunner`` dispatches a ``def`` body to a worker thread; that
        body is free to drive an ``asyncio.run`` of its own (an async SDK
        behind a sync wrapper). The loop it starts there is one the executor
        will never await, so the write must go straight through on the thread
        rather than being planted on a stranger.
        """
        planted: list[int] = []

        @node(output_name="answer")
        def agent_turn(prompt: str, ctx: NodeContext) -> str:
            async def inner() -> str:
                ctx.record("tool_call", {"name": "search"})
                planted.append(len(ctx._record_tasks))
                return prompt.upper()

            return asyncio.run(inner())

        graph = Graph([agent_turn], name="own-loop").with_runner(AsyncRunner())
        host = serve(graph, home=home)
        receipt = await host.submit(graph, {"prompt": "hi"})

        view = await _run_to_arrival(host, receipt)
        assert view.status is WorkflowStatus.COMPLETED
        assert "tool_call" in _kinds(await _facts(host.client, receipt.run_ref))
        # The mechanism, not just the outcome: nothing was planted on the
        # body's own loop.
        assert planted == [0]

    async def test_every_planted_write_belongs_to_the_executors_loop(self, home):
        """The invariant that makes the settle's ``gather`` sound."""
        loops: list[bool] = []

        @node(output_name="answer")
        async def agent_turn(prompt: str, ctx: NodeContext) -> str:
            ctx.record("tool_call", {"name": "search"})
            ctx.record("progress", {"pct": 100})
            loops.append(all(task.get_loop() is asyncio.get_running_loop() for task in ctx._record_tasks))
            assert len(ctx._record_tasks) == 2
            return prompt.upper()

        graph = Graph([agent_turn], name="one-loop").with_runner(AsyncRunner())
        host = serve(graph, home=home)
        receipt = await host.submit(graph, {"prompt": "hi"})

        assert (await _run_to_arrival(host, receipt)).status is WorkflowStatus.COMPLETED
        assert loops == [True]

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

        Every ``record`` is its own short transaction, and a node's facts are
        settled before its step record — including on the failure path. So the
        node's own outcome never retracts what it already reported.
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


# === Contention: the loop must stay free ===


class TestUnderContention:
    """The case a single uncontended run cannot see.

    A coroutine node recording through a blocking SQLite write would hold the
    event loop inside the store, while the store's own async transaction can
    only commit when that loop runs again — the two wait for each other until
    ``busy_timeout`` expires and the RUN fails with "database is locked". So
    the async family hands the write to a loop task and the executor awaits it
    before the step record; these two tests are the ones that fail if it ever
    goes back to writing straight through.
    """

    @pytest.mark.parametrize("runner_factory", [SyncRunner, AsyncRunner], ids=["sync", "async"])
    async def test_concurrent_hosted_runs_land_every_record(self, home, runner_factory):
        """3 runs x 2 nodes x 30 facts: all 180 land, in order, no failures.

        Parametrized over both families because that is exactly where the
        contract's parity claim was untested: uncontended they were already
        identical, and only a second run in flight tells them apart.
        """
        beats = 30

        @node(output_name="first")
        def step_one(prompt: str, ctx: NodeContext) -> str:
            for index in range(beats):
                ctx.record("beat", {"node": "one", "i": index})
            return prompt

        @node(output_name="answer")
        def step_two(first: str, ctx: NodeContext) -> str:
            for index in range(beats):
                ctx.record("beat", {"node": "two", "i": index})
            return first.upper()

        @node(output_name="first")
        async def step_one_async(prompt: str, ctx: NodeContext) -> str:
            for index in range(beats):
                ctx.record("beat", {"node": "one", "i": index})
            return prompt

        @node(output_name="answer")
        async def step_two_async(first: str, ctx: NodeContext) -> str:
            for index in range(beats):
                ctx.record("beat", {"node": "two", "i": index})
            return first.upper()

        runner = runner_factory()
        bodies = [step_one_async, step_two_async] if isinstance(runner, AsyncRunner) else [step_one, step_two]
        graph = Graph(bodies, name="chatty").with_runner(runner)
        host = serve(graph, home=home)
        receipts = [await host.submit(graph, {"prompt": f"hi-{n}"}, workflow_id=f"chatty-{n}") for n in range(3)]

        task = await _worker(host)
        try:
            views = await asyncio.gather(*(host.client.follow(r.run_ref, deadline=60) for r in receipts))
        finally:
            host.shutdown()
            await asyncio.wait_for(task, timeout=20)

        assert [v.status for v in views] == [WorkflowStatus.COMPLETED] * 3

        landed = 0
        for receipt in receipts:
            updates = await _facts(host.client, receipt.run_ref)
            recorded = [u.payload for u in updates if u.durable and u.kind == "beat"]
            assert recorded == [{"node": "one", "i": i} for i in range(beats)] + [{"node": "two", "i": i} for i in range(beats)]
            landed += len(recorded)
            # Each node's facts precede its OWN step record, still on one sequence.
            kinds = _kinds(updates)
            assert kinds.index("beat") < kinds.index("step")
            assert [_seq(u) for u in updates if u.durable] == list(range(1, len(kinds) + 1))
        assert landed == 3 * 2 * beats

    async def test_finished_writes_do_not_pile_up_on_the_context(self, home):
        """The in-flight set holds writes in flight, never every write made.

        A node that records thousands of facts must not hold a handle to every
        one of them; a finished write drops out as soon as it completes.

        "Finished" has to be a fact here, not a timing accident: without a
        yield the loop never runs a single append, so the node waits for the
        batch it just issued before measuring again. Each batch must then be
        gone from the set — if it is not, the batches accumulate and the peak
        is the total.
        """
        batch, batches = 20, 10
        peaks: list[int] = []
        after_draining: list[int] = []
        contexts: list[NodeContext] = []

        @node(output_name="answer")
        async def agent_turn(prompt: str, ctx: NodeContext) -> str:
            contexts.append(ctx)
            for group in range(batches):
                for index in range(batch):
                    ctx.record("beat", {"i": group * batch + index})
                peaks.append(len(ctx._record_tasks))
                await asyncio.gather(*list(ctx._record_tasks))
                after_draining.append(len(ctx._record_tasks))
            return prompt.upper()

        graph = Graph([agent_turn], name="drains").with_runner(AsyncRunner())
        host = serve(graph, home=home)
        receipt = await host.submit(graph, {"prompt": "hi"})
        view = await _run_to_arrival(host, receipt)

        assert view.status is WorkflowStatus.COMPLETED
        assert len([u for u in await _facts(host.client, receipt.run_ref) if u.durable and u.kind == "beat"]) == batch * batches
        # Exactly one batch is ever in flight: nothing can complete before the
        # node yields, and everything has by the time it measures again.
        assert peaks == [batch] * batches, peaks
        assert after_draining == [0] * batches, after_draining
        # And the flush leaves nothing behind.
        assert contexts[0]._record_tasks == set()

    async def test_a_node_records_while_a_host_write_transaction_is_held(self, home):
        """A held transaction delays the fact; it never stalls the loop.

        The seam-level repro of the three-party cycle: a writer holds
        ``BEGIN IMMEDIATE`` across an ``await`` while a node records in the
        middle of it. A heartbeat proves the loop keeps running throughout —
        a blocking write would freeze it for the full ``busy_timeout`` — and
        the run finishes as soon as the writer commits, rather than failing.
        """
        node_started = asyncio.Event()
        writer_holds = asyncio.Event()
        release_writer = asyncio.Event()

        @node(output_name="answer")
        async def agent_turn(prompt: str, ctx: NodeContext) -> str:
            node_started.set()
            await writer_holds.wait()
            ctx.record("tool_call", {"name": "search"})
            return prompt.upper()

        graph = Graph([agent_turn], name="contended").with_runner(AsyncRunner())
        host = serve(graph, home=home)
        receipt = await host.submit(graph, {"prompt": "hi"})
        task = await _worker(host)

        async def hold_a_write_transaction():
            await home._ensure_db()
            async with home._txn_lock():
                await home._db.execute("BEGIN IMMEDIATE")
                await home._db.execute(
                    "INSERT INTO run_updates (run_id, seq, kind, payload, created_at) VALUES (?, 1, 'status', '{}', '2026-01-01T00:00:00+00:00')",
                    ("some-other-run",),
                )
                writer_holds.set()
                await release_writer.wait()
                await home._db.commit()

        beats = 0

        async def heartbeat():
            nonlocal beats
            while True:
                await asyncio.sleep(0.01)
                beats += 1

        writer = None
        pulse = asyncio.create_task(heartbeat())
        try:
            # The node must be RUNNING before the transaction is held: the
            # worker's own claim needs the same lock to start it.
            await asyncio.wait_for(node_started.wait(), timeout=20)
            writer = asyncio.create_task(hold_a_write_transaction())
            await asyncio.wait_for(writer_holds.wait(), timeout=20)

            before = beats
            await asyncio.sleep(0.3)
            # A loop stuck inside a synchronous SQLite write cannot tick.
            assert beats - before >= 10, f"loop stalled: {beats - before} ticks in 0.3s"

            release_writer.set()
            view = await asyncio.wait_for(host.client.follow(receipt.run_ref, deadline=20), timeout=25)
        finally:
            pulse.cancel()
            release_writer.set()
            if writer is not None:
                await asyncio.wait_for(writer, timeout=20)
            host.shutdown()
            await asyncio.wait_for(task, timeout=20)

        assert view.status is WorkflowStatus.COMPLETED
        assert "tool_call" in _kinds(await _facts(host.client, receipt.run_ref))


# === What a LOST fact costs ===


class BrokenFactHome(RunHome):
    """A Run Home whose node-fact seam is down; everything else works.

    ``Checkpointer`` documents both mirrors as seams a store implements, so
    a third-party store raising here is an ordinary deployment fact — and the
    same failure a real Run Home produces when a sibling process holds the
    write lock past ``busy_timeout``.
    """

    def append_run_fact_sync(self, run_id: str, kind: str, payload: dict) -> None:
        raise RuntimeError("store down")

    async def append_run_fact(self, run_id: str, kind: str, payload: dict) -> None:
        raise RuntimeError("store down")


def _record_then_continue(*, body_kind: str, guard: bool, trace: list[str], side_effects: list[str]):
    """One node that records, then does the rest of its work."""

    def work(text: str, ctx: NodeContext) -> str:
        trace.append("entered")
        if guard:
            try:
                ctx.record("tool_call", {"n": 1})
                trace.append("record-returned")
            except Exception as exc:  # noqa: BLE001
                trace.append(f"record-raised-IN-BODY:{type(exc).__name__}")
        else:
            ctx.record("tool_call", {"n": 1})
            trace.append("record-returned")
        side_effects.append("charged the customer")
        trace.append("body-continued")
        return text.upper()

    if body_kind == "async def":

        @node(output_name="out")
        async def leaf(text: str, ctx: NodeContext) -> str:
            return work(text, ctx)
    else:

        @node(output_name="out")
        def leaf(text: str, ctx: NodeContext) -> str:
            return work(text, ctx)

    return leaf


def _shaped(leaf, shape: str):
    """The same leaf, flat / inside ``as_node()`` / mapped over two items."""
    inner = Graph([leaf], name="inner")
    if shape == "flat":
        return inner, {"text": "hi"}
    if shape == "nested":
        return Graph([inner.as_node(name="inner")], name="outer"), {"text": "hi"}
    if shape == "mapped":
        return Graph([inner.as_node(name="inner").map_over("text")], name="outer"), {"text": ["hi", "yo"]}
    raise AssertionError(shape)


async def _run_with_broken_seam(tmp_path, *, runner_factory, body_kind, guard, shape, name="runs.db"):
    """Run the shaped graph against a down fact seam; report what happened."""
    trace: list[str] = []
    side_effects: list[str] = []
    home = BrokenFactHome.open(_home_uri(tmp_path, name))
    graph, inputs = _shaped(_record_then_continue(body_kind=body_kind, guard=guard, trace=trace, side_effects=side_effects), shape)
    runner = runner_factory().with_checkpointer(home)
    error: BaseException | None = None
    try:
        if isinstance(runner, SyncRunner):
            await asyncio.to_thread(lambda: runner.run(graph, workflow_id="wf-1", **inputs))
        else:
            await runner.run(graph, workflow_id="wf-1", **inputs)
    except BaseException as exc:  # noqa: BLE001
        error = exc
    runs = {run.id: run.status for run in home.runs(limit=None)}
    logged = [] if home.get_run("wf-1") is None else [kind for _seq, kind, _payload, _at in await home._read_run_updates("wf-1")]
    await home.close()
    return trace, side_effects, error, runs, logged


def _expected_trace(*, runner_factory, body_kind: str, guard: bool) -> list[str]:
    """Where the body learns, per pairing — the difference D2 keeps."""
    if runner_factory is AsyncRunner and body_kind == "async def":
        # Deferred to the executor's loop: nothing to tell at the call.
        return ["entered", "record-returned", "body-continued"]
    if guard:
        return ["entered", "record-raised-IN-BODY:RuntimeError", "body-continued"]
    return ["entered"]


_FAILURE_ROWS = [
    ("A-sync-def-unguarded-flat", SyncRunner, "def", False, "flat"),
    ("B-async-coro-unguarded-flat", AsyncRunner, "async def", False, "flat"),
    ("C-async-def-unguarded-flat", AsyncRunner, "def", False, "flat"),
    ("D-sync-def-guarded-flat", SyncRunner, "def", True, "flat"),
    ("E-async-coro-guarded-flat", AsyncRunner, "async def", True, "flat"),
    ("F-async-def-guarded-flat", AsyncRunner, "def", True, "flat"),
    ("G-async-coro-guarded-nested", AsyncRunner, "async def", True, "nested"),
    ("H-async-def-guarded-mapped", AsyncRunner, "def", True, "mapped"),
    ("I-sync-def-guarded-nested", SyncRunner, "def", True, "nested"),
]

_EXPECTED_RUNS = {
    "flat": {"wf-1"},
    "nested": {"wf-1", "wf-1/inner"},
    "mapped": {"wf-1", "wf-1/inner", "wf-1/inner/0", "wf-1/inner/1"},
}


class TestWhenTheWriteFails:
    """ONE failure semantics: a fact that could not be written fails the node.

    The nine pairings a node author can actually reach — runner family x body
    kind x guarded x flat/nested/mapped. Four of them used to report
    COMPLETED over a fact that never landed, because a ``try/except`` in the
    body was load-bearing on the thread paths and dead code on the loop path.
    """

    @pytest.mark.parametrize(("label", "runner_factory", "body_kind", "guard", "shape"), _FAILURE_ROWS, ids=[row[0] for row in _FAILURE_ROWS])
    async def test_a_lost_fact_fails_the_run(self, tmp_path, label, runner_factory, body_kind, guard, shape):
        trace, side_effects, error, runs, logged = await _run_with_broken_seam(
            tmp_path, runner_factory=runner_factory, body_kind=body_kind, guard=guard, shape=shape, name=f"{label}.db"
        )

        # The store's OWN exception reaches the caller — no framework wrapper.
        assert isinstance(error, RuntimeError), error
        assert "store down" in str(error)
        # Every run the shape produced is failed, parent and children alike.
        assert set(runs) == _EXPECTED_RUNS[shape], runs
        assert set(runs.values()) == {WorkflowStatus.FAILED}, runs
        # And the fact the node asked to commit is not in the log.
        assert "tool_call" not in logged, logged

        expected = _expected_trace(runner_factory=runner_factory, body_kind=body_kind, guard=guard)
        if shape == "mapped":
            # Two items on two threads: the shape is per item, the order is not.
            assert Counter(trace) == Counter(expected * 2), trace
        else:
            assert trace == expected, trace
        # The rest of the body ran exactly where the body was allowed to continue.
        assert bool(side_effects) is (trace.count("body-continued") > 0)

    @pytest.mark.parametrize(
        ("label", "runner_factory", "body_kind"),
        [("D", SyncRunner, "def"), ("F", AsyncRunner, "def")],
        ids=["sync-runner", "async-runner-def-body"],
    )
    async def test_the_guard_does_not_buy_a_completed_run(self, tmp_path, label, runner_factory, body_kind):
        """The behaviour change, called out by name.

        On the thread paths a ``try/except`` around ``ctx.record`` used to
        turn a lost fact into ``status=completed``. It still changes the
        BODY's control flow — the body keeps going and its side effect
        happens — but it no longer changes the node's outcome.
        """
        trace, side_effects, error, runs, _logged = await _run_with_broken_seam(
            tmp_path, runner_factory=runner_factory, body_kind=body_kind, guard=True, shape="flat", name=f"guard-{label}.db"
        )

        assert trace == ["entered", "record-raised-IN-BODY:RuntimeError", "body-continued"]
        assert side_effects == ["charged the customer"]
        assert isinstance(error, RuntimeError) and "store down" in str(error)
        assert runs == {"wf-1": WorkflowStatus.FAILED}

    @pytest.mark.parametrize(
        ("runner_factory", "body_kind"),
        [(SyncRunner, "def"), (AsyncRunner, "async def")],
        ids=["sync", "async"],
    )
    async def test_a_node_failing_for_its_own_reason_keeps_its_own_exception(self, tmp_path, caplog, runner_factory, body_kind):
        """The node's own error wins; the lost fact never replaces it.

        Whether the framework also LOGS the lost fact depends on whether the
        body was already handed it: on the loop path ``record`` returned
        cleanly, so the warning is the only place it surfaces; on the thread
        path the body caught the store's own exception at the call and does
        not need telling twice.
        """
        home = BrokenFactHome.open(_home_uri(tmp_path, "own-reason.db"))

        def work(text: str, ctx: NodeContext) -> str:
            with contextlib.suppress(Exception):
                ctx.record("tool_call", {"n": 1})
            raise RuntimeError("node died")

        if body_kind == "async def":

            @node(output_name="out")
            async def leaf(text: str, ctx: NodeContext) -> str:
                return work(text, ctx)
        else:

            @node(output_name="out")
            def leaf(text: str, ctx: NodeContext) -> str:
                return work(text, ctx)

        runner = runner_factory().with_checkpointer(home)
        graph = Graph([leaf], name="dies")
        with caplog.at_level("WARNING", logger="hypergraph.runners"), pytest.raises(RuntimeError, match="node died"):
            if isinstance(runner, SyncRunner):
                await asyncio.to_thread(lambda: runner.run(graph, text="hi", workflow_id="wf-1"))
            else:
                await runner.run(graph, text="hi", workflow_id="wf-1")
        await home.close()

        warned = [record for record in caplog.records if "recorded facts could not be written" in record.getMessage()]
        assert bool(warned) is (body_kind == "async def"), [record.getMessage() for record in caplog.records]

    async def test_the_failure_surfaces_at_the_record_call_on_a_thread_path(self, tmp_path):
        """The traceback's last user frame is the ``ctx.record(...)`` line."""
        home = BrokenFactHome.open(_home_uri(tmp_path, "frame.db"))

        @node(output_name="out")
        def leaf(text: str, ctx: NodeContext) -> str:
            ctx.record("tool_call", {"n": 1})
            return text.upper()

        runner = SyncRunner().with_checkpointer(home)
        graph = Graph([leaf], name="unguarded")
        with pytest.raises(RuntimeError, match="store down") as caught:
            await asyncio.to_thread(lambda: runner.run(graph, text="hi", workflow_id="wf-1"))
        await home.close()

        in_the_node = [frame for frame in caught.traceback if frame.name == "leaf"]
        assert "ctx.record" in str(in_the_node[-1].statement), in_the_node[-1].statement
        # Through ``record``, not through the settle: the body stopped AT the
        # call rather than being told after it had finished.
        names = [frame.name for frame in caught.traceback]
        assert "record" in names and "_resolve_record_failure" not in names, names


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

    @pytest.mark.parametrize("runner_factory", [SyncRunner, AsyncRunner], ids=["sync", "async"])
    async def test_an_unserializable_payload_fails_at_the_call(self, runner_factory):
        """Both families refuse it at ``record``, not later at the flush.

        Deferred on the loop, a payload the store cannot serialize would blow
        up inside the executor's flush and point at the node rather than at
        the line that recorded it.
        """

        class Domain:
            pass

        @node(output_name="answer")
        def agent_turn(prompt: str, ctx: NodeContext) -> str:
            ctx.record("tool_call", {"obj": Domain()})
            return prompt.upper()

        @node(output_name="answer")
        async def agent_turn_async(prompt: str, ctx: NodeContext) -> str:
            ctx.record("tool_call", {"obj": Domain()})
            return prompt.upper()

        runner = runner_factory()
        body = agent_turn_async if isinstance(runner, AsyncRunner) else agent_turn
        graph = Graph([body], name="unserializable")
        with pytest.raises(Exception, match="not JSON-safe") as caught:
            if isinstance(runner, SyncRunner):
                runner.run(graph, prompt="hi", workflow_id="wf-json")
            else:
                await runner.run(graph, prompt="hi", workflow_id="wf-json")
        # The frame that failed is the node's own record call.
        assert "record" in str(caught.value) or "record" in repr(caught.traceback[-1])

    def test_every_kind_the_framework_writes_is_reserved(self):
        """The mirror guard: a new framework kind must join the closed set.

        Read from the CODE, not from prose. The Batch vocabulary has named
        constants; the run vocabulary is spelled as literals at the mutation
        hooks, so this walks the AST of the two modules that write them and
        collects the ``kind`` argument of every append. A kind added to a new
        call site and not to ``RESERVED_FACT_KINDS`` fails here instead of
        becoming a name a node could borrow.
        """
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

        written = _run_update_kinds_written_in_source()
        # A sanity floor, so a parser that silently matched nothing cannot
        # pass this test by finding an empty set.
        assert {"submitted", "run_started", "step", "status", "answer", "command"} <= written, written
        assert written <= RESERVED_FACT_KINDS, written - RESERVED_FACT_KINDS


# === No log to append to ===


class TestWithoutADurableLog:
    @pytest.mark.parametrize("runner_factory", [SyncRunner, AsyncRunner], ids=["sync", "async"])
    async def test_a_tier0_run_records_nothing_and_does_not_raise(self, runner_factory):
        """The same node body runs in-process; ``record`` returns ``None``."""
        seen: list[None] = []

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

    async def test_a_plain_checkpointer_writes_nothing_at_either_seam(self, tmp_path):
        """Only a Run Home keeps a run log; a plain store no-ops honestly."""
        checkpointer = SqliteCheckpointer(str(tmp_path / "plain.db"))
        try:
            checkpointer.append_run_fact_sync("wf-1", "tool_call", {"name": "search"})
            await checkpointer.append_run_fact("wf-1", "tool_call", {"name": "search"})
            rows = checkpointer._sync_db().execute("SELECT COUNT(*) FROM run_updates WHERE run_id = ?", ("wf-1",)).fetchone()
            assert rows[0] == 0
        finally:
            await checkpointer.close()

    async def test_both_run_home_seams_write_one_gap_free_sequence(self, home):
        """The thread mirror and the loop mirror share one per-Run sequence."""
        home.append_run_fact_sync("wf-1", "tool_call", {"name": "search"})
        await home.append_run_fact("wf-1", "progress", {"pct": 10})
        home.append_run_fact_sync("wf-1", "tool_call", {"name": "again"})
        rows = await home._read_run_updates("wf-1")
        assert [(seq, kind) for seq, kind, _payload, _at in rows] == [(1, "tool_call"), (2, "progress"), (3, "tool_call")]
        # Per-run sequences are independent.
        await home.append_run_fact("wf-2", "tool_call", {"name": "other"})
        assert [seq for seq, *_ in await home._read_run_updates("wf-2")] == [1]
