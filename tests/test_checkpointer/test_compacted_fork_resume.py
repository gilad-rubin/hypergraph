"""Fork/resume from a compacted run is refused at the boundary (#239).

Witness: a completed 3-node chain persisted under ``retention="windowed"``.
Compaction folds the upstream nodes' VALUES into the retention baseline, but
their EXECUTION identity goes with their step records. Before this ticket a
fork of that run restored the state, re-invoked the two upstream nodes for
real, and still reported COMPLETED — ``calls == {a: 2, b: 2, c: 1}`` with no
diagnostic anywhere.

State reconstruction and execution restoration are separate capabilities. The
boundary now refuses when compaction destroyed the second one for a node the
target would otherwise re-run, and says which nodes those are.

Assertion map (ticket acceptance items):
    RED witness, zero executions after      TestCompactedRestoreRefused
    names cause, nodes, and both fixes      TestCompactedRestoreRefused
    stable code + docs anchor               TestCompactedRestoreRefused
    capability: fork missing the producers  TestCompactedRestoreStaysCapable
    capability: resume off baseline values  TestCompactedRestoreStaysCapable
    matrix: latest never refuses            TestCompactedRestoreStaysCapable
    retry_from names 'retry', not 'fork'    TestCompactedRestoreRefused
    paused resume refused / admitted        TestWindowedInterruptResume
    nested GraphNode lineage                TestNestedCompactedLineage
    composes with the in-run nested guard   TestNestedCompactedLineage

#277 extends the same witness: the carrier now records WHICH nodes it folded,
so the refusal is per-producer instead of per-value-name, and the in-run guard
can answer instead of refusing.

    carrier records its folded producers    TestFoldedProducerProvenance
    provenance survives re-compaction       TestFoldedProducerProvenance
    memory/sqlite agree on the set          TestFoldedProducerProvenance
    recorded provenance decides the guard   TestNestedCompactedLineage
    only a legacy carrier still refuses     TestNestedCompactedLineage
"""

from __future__ import annotations

import inspect

import pytest

from hypergraph import CompactedRetentionError, Graph, SyncRunner, interrupt, node, route
from hypergraph.checkpointers import CheckpointPolicy, MemoryCheckpointer, SqliteCheckpointer
from hypergraph.diagnostics import DIAGNOSTIC_CODES
from hypergraph.runners._shared.results import RunStatus
from tests._interrupt_questions import StringQuestion

aiosqlite = pytest.importorskip("aiosqlite")

BACKENDS = ["sync-sqlite", "async-sqlite", "async-memory"]


# === Helpers ===


def retention_policy(retention: str, window: int | None = 1) -> CheckpointPolicy:
    if retention == "windowed":
        return CheckpointPolicy(durability="sync", retention="windowed", window=window)
    return CheckpointPolicy(durability="sync", retention=retention)


class Backend:
    """One (runner family, checkpointer) pair under one retention policy."""

    def __init__(self, kind: str, tmp_path, policy: CheckpointPolicy):
        from hypergraph import AsyncRunner

        self.kind = kind
        if kind == "async-memory":
            self.checkpointer = MemoryCheckpointer()
            # MemoryCheckpointer.__init__ takes no policy= — assign it.
            self.checkpointer.policy = policy
        else:
            self.checkpointer = SqliteCheckpointer(str(tmp_path / f"{kind}.db"), policy=policy)
        if kind == "sync-sqlite":
            self.checkpointer._sync_db()
            self.runner = SyncRunner(checkpointer=self.checkpointer)
        else:
            self.runner = AsyncRunner(checkpointer=self.checkpointer)

    async def run(self, *args, **kwargs):
        result = self.runner.run(*args, **kwargs)
        if inspect.iscoroutine(result):
            result = await result
        return result

    async def close(self) -> None:
        if isinstance(self.checkpointer, SqliteCheckpointer):
            if self.kind == "sync-sqlite":
                if self.checkpointer._sync_conn:
                    self.checkpointer._sync_conn.close()
            else:
                await self.checkpointer.close()


@pytest.fixture(params=BACKENDS)
def backend_kind(request) -> str:
    return request.param


def build_chain_graph(fail_c: list[bool] | None = None):
    """a -> b -> c, one superstep each, with invocation counters.

    ``fail_c`` is a one-element mutable flag: while it is truthy, ``c`` raises,
    so a caller can persist a FAILED source run and then repair it.
    """
    calls = {"a": 0, "b": 0, "c": 0}
    armed = fail_c if fail_c is not None else [False]

    @node(output_name="a_out")
    def a(seed: int) -> int:
        calls["a"] += 1
        return seed + 1

    @node(output_name="b_out")
    def b(a_out: int) -> int:
        calls["b"] += 1
        return a_out + 1

    @node(output_name="c_out")
    def c(b_out: int) -> int:
        calls["c"] += 1
        if armed[0]:
            raise RuntimeError("boom")
        return b_out + 1

    return Graph(nodes=[a, b, c], name="chain"), calls, (a, b, c)


def build_accumulator_graph(fail_first: list[bool]):
    """Cyclic accumulate -> gate -> finish; finish fails once, then succeeds.

    The loop re-executes ``accumulate`` every turn, so a window wide enough to
    keep the last turn leaves every folded value with a surviving producer row.
    """
    calls = {"accumulate": 0, "finish": 0}

    @node(output_name=("total", "snapshot"))
    def accumulate(total: int = 0, tick: int = 1) -> tuple[int, int]:
        calls["accumulate"] += 1
        return total + tick, total + tick

    @route(targets=["accumulate", "finish"])
    def gate(total: int = 0) -> str:
        return "finish" if total >= 3 else "accumulate"

    @node(output_name="report")
    def finish(snapshot: int) -> str:
        calls["finish"] += 1
        if fail_first[0]:
            raise RuntimeError("boom")
        return f"total={snapshot}"

    graph = Graph(nodes=[accumulate, gate, finish], name="loop", entrypoint="accumulate")
    return graph, calls


def assert_boundary_guidance(message: str) -> None:
    """The refusal names the cause, both supported fixes, and its precision."""
    assert "compacted" in message
    assert "retention='windowed'" in message
    assert "EXECUTION" in message
    assert "retention='full'" in message
    assert "retention='latest'" in message
    assert "new workflow_id" in message
    # #277: the names come from recorded provenance, not from value names.
    assert "the baseline recorded folding" in message


# === Tests ===


class TestCompactedRestoreRefused:
    """The boundary refuses before a single node executes."""

    async def test_windowed_fork_refuses_and_runs_nothing(self, tmp_path, backend_kind):
        """The ticket witness: today {a: 2, b: 2}; after, a typed refusal."""
        backend = Backend(backend_kind, tmp_path, retention_policy("windowed"))
        try:
            graph, calls, _ = build_chain_graph()
            first = await backend.run(graph, {"seed": 1}, workflow_id="src")
            assert first.status is RunStatus.COMPLETED
            assert first.values == {"a_out": 2, "b_out": 3, "c_out": 4}
            assert calls == {"a": 1, "b": 1, "c": 1}

            with pytest.raises(CompactedRetentionError) as error:
                await backend.run(graph, {}, workflow_id="forked", fork_from="src")

            # Nothing executed: the refusal is a boundary decision, not a
            # post-mortem on side effects that already happened.
            assert calls == {"a": 1, "b": 1, "c": 1}
            assert error.value.pruned_nodes == ("a", "b")
            assert error.value.workflow_id == "forked"
            assert error.value.source_run_id == "src"
            message = str(error.value)
            assert "'a', 'b'" in message
            assert_boundary_guidance(message)
        finally:
            await backend.close()

    async def test_windowed_same_lineage_resume_refuses(self, tmp_path, backend_kind):
        """A resume of the same compacted lineage is refused the same way."""
        backend = Backend(backend_kind, tmp_path, retention_policy("windowed"))
        fail_first = [True]
        try:
            graph, calls = build_accumulator_graph(fail_first)
            with pytest.raises(RuntimeError, match="boom"):
                await backend.run(graph, {}, workflow_id="loop")
            assert calls == {"accumulate": 3, "finish": 1}

            fail_first[0] = False
            with pytest.raises(CompactedRetentionError) as error:
                await backend.run(graph, workflow_id="loop")

            assert calls == {"accumulate": 3, "finish": 1}
            assert error.value.pruned_nodes == ("accumulate", "gate")
            assert "Cannot resume 'loop'" in str(error.value)
        finally:
            await backend.close()

    async def test_retry_from_refusal_names_the_retry(self, tmp_path, backend_kind):
        """The message names the operation the caller asked for, not 'fork'."""
        backend = Backend(backend_kind, tmp_path, retention_policy("windowed"))
        try:
            fail_c = [True]
            graph, calls, _ = build_chain_graph(fail_c)
            with pytest.raises(RuntimeError, match="boom"):
                await backend.run(graph, {"seed": 1}, workflow_id="src")
            assert calls == {"a": 1, "b": 1, "c": 1}

            fail_c[0] = False
            with pytest.raises(CompactedRetentionError) as error:
                await backend.run(graph, {}, workflow_id="rt", retry_from="src")

            assert calls == {"a": 1, "b": 1, "c": 1}
            assert error.value.is_retry is True
            assert error.value.pruned_nodes == ("a", "b")
            assert "Cannot retry 'rt' from 'src'" in str(error.value)
            assert "fork 'rt'" not in str(error.value)
        finally:
            await backend.close()

    async def test_refusal_carries_the_registered_diagnostic_code(self, tmp_path):
        backend = Backend("sync-sqlite", tmp_path, retention_policy("windowed"))
        try:
            graph, _, _ = build_chain_graph()
            await backend.run(graph, {"seed": 1}, workflow_id="src")
            with pytest.raises(CompactedRetentionError) as error:
                await backend.run(graph, {}, workflow_id="forked", fork_from="src")

            assert error.value.code == "HG_COMPACTED_RETENTION"
            assert DIAGNOSTIC_CODES["HG_COMPACTED_RETENTION"] == "docs/06-api-reference/errors.md#hg-compacted-retention"
        finally:
            await backend.close()

    async def test_already_completed_still_outranks_the_compaction_refusal(self, tmp_path):
        """Precedence: 'this lineage is finished' is the coarser fact."""
        from hypergraph.exceptions import WorkflowAlreadyCompletedError

        backend = Backend("sync-sqlite", tmp_path, retention_policy("windowed"))
        try:
            graph, _, _ = build_chain_graph()
            await backend.run(graph, {"seed": 1}, workflow_id="src")
            with pytest.raises(WorkflowAlreadyCompletedError):
                await backend.run(graph, workflow_id="src")
        finally:
            await backend.close()


class TestCompactedRestoreStaysCapable:
    """Capability-based, not blanket: supported restores keep working."""

    async def test_fork_reaching_no_pruned_producer_succeeds(self, tmp_path, backend_kind):
        """A target whose active scope excludes the pruned producers is admitted."""
        backend = Backend(backend_kind, tmp_path, retention_policy("windowed"))
        try:
            graph, calls, (_a, _b, c) = build_chain_graph()
            await backend.run(graph, {"seed": 1}, workflow_id="src")
            assert calls == {"a": 1, "b": 1, "c": 1}

            calls["d"] = 0

            @node(output_name="d_out")
            def d(c_out: int) -> int:
                calls["d"] += 1
                return c_out * 10

            tail = Graph(nodes=[c, d], name="tail")
            forked = await backend.run(tail, {}, workflow_id="tail", fork_from="src")

            assert forked.status is RunStatus.COMPLETED
            assert forked.values == {"c_out": 4, "d_out": 40}
            # 'd' did the new work off restored state; nothing re-executed.
            assert calls == {"a": 1, "b": 1, "c": 1, "d": 1}
        finally:
            await backend.close()

    async def test_resume_needing_only_folded_baseline_values_succeeds(self, tmp_path, backend_kind):
        """Every folded value still has a surviving producer row — so resume."""
        backend = Backend(backend_kind, tmp_path, retention_policy("windowed", window=3))
        fail_first = [True]
        try:
            graph, calls = build_accumulator_graph(fail_first)
            with pytest.raises(RuntimeError, match="boom"):
                await backend.run(graph, {}, workflow_id="loop")
            assert calls == {"accumulate": 3, "finish": 1}

            raw = await _raw_steps(backend, "loop")
            baseline = next(step for step in raw if step.node_type == "RetentionBaseline")
            assert set(baseline.values or {}) == {"total", "snapshot", "_gate"}

            fail_first[0] = False
            resumed = await backend.run(graph, workflow_id="loop")

            assert resumed.status is RunStatus.COMPLETED
            assert resumed.values["report"] == "total=3"
            # The loop did not turn again: only the failed node re-ran.
            assert calls == {"accumulate": 3, "finish": 2}
        finally:
            await backend.close()

    async def test_latest_retention_fork_never_refuses(self, tmp_path, backend_kind):
        """Matrix arm: retention='latest' keeps one row per node, so it is safe."""
        backend = Backend(backend_kind, tmp_path, retention_policy("latest"))
        try:
            graph, calls, _ = build_chain_graph()
            await backend.run(graph, {"seed": 1}, workflow_id="src")
            assert calls == {"a": 1, "b": 1, "c": 1}

            forked = await backend.run(graph, {}, workflow_id="forked", fork_from="src")

            assert forked.status is RunStatus.COMPLETED
            assert forked.values == {"a_out": 2, "b_out": 3, "c_out": 4}
            assert calls == {"a": 1, "b": 1, "c": 1}
        finally:
            await backend.close()


class TestWindowedInterruptResume:
    """Multi-turn interrupts and windowed retention do not mix.

    A pause is just a run that stopped mid-graph, so the boundary gate sees it
    like any other resume: whichever upstream producers compaction pruned are
    the ones a resume would re-invoke. That makes ``retention="windowed"``
    unusable for paused workflows unless the interrupt is the entrypoint, or
    the window is wide enough to keep every upstream producer's row.
    """

    @pytest.fixture(params=["async-sqlite", "async-memory"])
    def async_backend_kind(self, request) -> str:
        return request.param

    async def test_resume_after_pause_refuses_when_upstream_was_pruned(self, tmp_path, async_backend_kind):
        """prep -> mid -> ask -> post with window=1: 'mid' and 'prep' are gone."""
        calls = {"prep": 0, "mid": 0, "post": 0}

        @node(output_name="prepped")
        def prep(seed: int) -> int:
            calls["prep"] += 1
            return seed + 1

        @node(output_name="middled")
        def mid(prepped: int) -> int:
            calls["mid"] += 1
            return prepped * 2

        @interrupt(answer_name="answer")
        def ask(middled: int) -> StringQuestion:
            return StringQuestion(prompt=f"ok with {middled}?")

        @node(output_name="posted")
        def post(answer: str) -> str:
            calls["post"] += 1
            return f"posted:{answer}"

        graph = Graph(nodes=[prep, mid, ask, post], name="turn")
        backend = Backend(async_backend_kind, tmp_path, retention_policy("windowed"))
        try:
            paused = await backend.run(graph, {"seed": 1}, workflow_id="wf")
            assert paused.status is RunStatus.PAUSED
            assert calls == {"prep": 1, "mid": 1, "post": 0}

            # The interrupt itself keeps a PAUSED row — it is still on record.
            raw = await _raw_steps(backend, "wf")
            assert "ask" in {step.node_name for step in raw}

            with pytest.raises(CompactedRetentionError) as error:
                await backend.run(graph, {"answer": "yes"}, workflow_id="wf")

            assert calls == {"prep": 1, "mid": 1, "post": 0}
            assert error.value.pruned_nodes == ("mid", "prep")
            assert "Cannot resume 'wf'" in str(error.value)
            assert_boundary_guidance(str(error.value))
        finally:
            await backend.close()

    async def test_resume_after_pause_succeeds_when_the_interrupt_is_the_entrypoint(self, tmp_path, async_backend_kind):
        """Nothing upstream to prune, so windowed retention stays usable."""
        calls = {"post": 0}

        @interrupt(answer_name="answer")
        def ask(prompt: str = "next?") -> StringQuestion:
            return StringQuestion(prompt=prompt)

        @node(output_name="posted")
        def post(answer: str) -> str:
            calls["post"] += 1
            return f"posted:{answer}"

        graph = Graph(nodes=[ask, post], name="turn", entrypoint="ask")
        backend = Backend(async_backend_kind, tmp_path, retention_policy("windowed"))
        try:
            paused = await backend.run(graph, {}, workflow_id="wf")
            assert paused.status is RunStatus.PAUSED
            assert calls == {"post": 0}

            resumed = await backend.run(graph, {"answer": "yes"}, workflow_id="wf")

            assert resumed.status is RunStatus.COMPLETED
            assert resumed.values["posted"] == "posted:yes"
            assert calls == {"post": 1}
        finally:
            await backend.close()


class TestNestedCompactedLineage:
    """Nested GraphNode lineage behaves like the flat one."""

    async def test_windowed_fork_names_the_pruned_graphnode(self, tmp_path, backend_kind):
        calls = {"prepare": 0, "double": 0, "consume": 0}

        @node(output_name="prepared")
        def prepare(seed: int) -> int:
            calls["prepare"] += 1
            return seed + 1

        @node(output_name="doubled")
        def double(prepared: int) -> int:
            calls["double"] += 1
            return prepared * 2

        @node(output_name="final")
        def consume(doubled: int) -> int:
            calls["consume"] += 1
            return doubled + 1

        child = Graph(nodes=[double], name="child")
        parent = Graph(nodes=[prepare, child.as_node(name="child_wf"), consume], name="parent")

        backend = Backend(backend_kind, tmp_path, retention_policy("windowed"))
        try:
            first = await backend.run(parent, {"seed": 1}, workflow_id="src")
            assert first.status is RunStatus.COMPLETED
            assert calls == {"prepare": 1, "double": 1, "consume": 1}

            with pytest.raises(CompactedRetentionError) as error:
                await backend.run(parent, {}, workflow_id="forked", fork_from="src")

            assert calls == {"prepare": 1, "double": 1, "consume": 1}
            assert error.value.pruned_nodes == ("child_wf", "prepare")
            assert_boundary_guidance(str(error.value))
        finally:
            await backend.close()

    def test_in_run_nested_guard_speaks_the_same_vocabulary(self):
        """The two guards compose: same type, same fixes, different witness.

        ``has_prior_completion_evidence`` is the in-run last-mile check for a
        GraphNode crash window; the boundary gate above cannot see compaction
        that lands mid-run. Both refuse as ``CompactedRetentionError`` — and
        since #277 the in-run one refuses only what it genuinely cannot
        decide: a carrier written before provenance existed.
        """
        from hypergraph.runners._shared.state_restore import has_prior_completion_evidence

        child = Graph(nodes=[_noop_node()], name="child")
        graph_node = child.as_node(name="child_wf")

        legacy = _carrier(values={"doubled": 4}, folded_producers=None)
        with pytest.raises(CompactedRetentionError) as error:
            has_prior_completion_evidence([legacy], graph_node)

        assert error.value.node_name == "child_wf"
        assert error.value.pruned_nodes == ()
        assert error.value.code == "HG_COMPACTED_RETENTION"
        message = str(error.value)
        assert "retention='full'" in message
        assert "retention='latest'" in message
        assert "predates producer provenance" in message

    def test_recorded_provenance_decides_the_in_run_guard(self):
        """A carrier that names the node is evidence; one that does not is not.

        Both carriers hold a value called ``doubled`` — the GraphNode's own
        output name. Before #277 that alone was enough to make the restore
        ambiguous; now only the recorded producer answers the question.
        """
        from hypergraph.runners._shared.state_restore import has_prior_completion_evidence

        child = Graph(nodes=[_noop_node()], name="child")
        graph_node = child.as_node(name="child_wf")

        folded_this_node = _carrier(values={"doubled": 4}, folded_producers=("prepare", "child_wf"))
        assert has_prior_completion_evidence([folded_this_node], graph_node) is True

        same_name_other_producer = _carrier(values={"doubled": 4}, folded_producers=("prepare",))
        assert has_prior_completion_evidence([same_name_other_producer], graph_node) is False


class TestFoldedProducerProvenance:
    """The carrier records WHOSE steps it folded, identically on every backend."""

    async def test_carrier_names_its_folded_producers(self, tmp_path, backend_kind):
        """Memory and SQLite agree: same graph, same retention, same set.

        Two compaction passes happen here (once after 'b', once after 'c'), so
        this also witnesses provenance surviving a carrier being re-folded:
        'a' is only reachable through the first carrier's own record.
        """
        backend = Backend(backend_kind, tmp_path, retention_policy("windowed"))
        try:
            graph, _calls, _ = build_chain_graph()
            await backend.run(graph, {"seed": 1}, workflow_id="src")

            raw = await _raw_steps(backend, "src")
            carrier = next(step for step in raw if step.node_type == "RetentionBaseline")
            assert carrier.folded_producers == ("a", "b")
            assert set(carrier.values or {}) == {"a_out", "b_out"}
            # Provenance is a carrier-only fact: ordinary rows record none.
            assert [step.folded_producers for step in raw if step.node_type != "RetentionBaseline"] == [None]
        finally:
            await backend.close()

    async def test_a_surviving_producer_is_not_named_by_the_refusal(self, tmp_path, backend_kind):
        """retention='latest' folds a loop's older rows and refuses nothing."""
        backend = Backend(backend_kind, tmp_path, retention_policy("latest"))
        fail_first = [True]
        try:
            graph, calls = build_accumulator_graph(fail_first)
            with pytest.raises(RuntimeError, match="boom"):
                await backend.run(graph, {}, workflow_id="loop")

            raw = await _raw_steps(backend, "loop")
            carrier = next(step for step in raw if step.node_type == "RetentionBaseline")
            assert carrier.folded_producers == ("accumulate", "gate")

            fail_first[0] = False
            resumed = await backend.run(graph, workflow_id="loop")
            assert resumed.status is RunStatus.COMPLETED
            assert calls == {"accumulate": 3, "finish": 2}
        finally:
            await backend.close()


def _carrier(*, values: dict, folded_producers: tuple[str, ...] | None):
    """A retention carrier row exactly as compaction writes it."""
    from hypergraph.checkpointers.types import StepRecord, StepStatus, _utcnow

    return StepRecord(
        run_id="wf",
        superstep=0,
        node_name="__retained_state__",
        index=0,
        status=StepStatus.COMPLETED,
        input_versions={},
        values=values,
        created_at=_utcnow(),
        node_type="RetentionBaseline",
        folded_producers=folded_producers,
    )


def _noop_node():
    @node(output_name="doubled")
    def double(prepared: int = 1) -> int:
        return prepared * 2

    return double


async def _raw_steps(backend: Backend, run_id: str):
    """Raw history including the retention carrier, per backend family."""
    if backend.kind == "sync-sqlite":
        return backend.checkpointer.steps(run_id, show_internal=True)
    return await backend.checkpointer.get_steps(run_id, show_internal=True)
