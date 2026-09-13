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
    nested GraphNode lineage                TestNestedCompactedLineage
    composes with the in-run nested guard   TestNestedCompactedLineage
"""

from __future__ import annotations

import inspect

import pytest

from hypergraph import CompactedRetentionError, Graph, SyncRunner, node, route
from hypergraph.checkpointers import CheckpointPolicy, MemoryCheckpointer, SqliteCheckpointer
from hypergraph.diagnostics import DIAGNOSTIC_CODES
from hypergraph.runners._shared.results import RunStatus

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


def build_chain_graph():
    """a -> b -> c, one superstep each, with invocation counters."""
    calls = {"a": 0, "b": 0, "c": 0}

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
    """The refusal names the cause, both supported fixes, and the descope."""
    assert "compacted" in message
    assert "retention='windowed'" in message
    assert "EXECUTION" in message
    assert "retention='full'" in message
    assert "retention='latest'" in message
    assert "new workflow_id" in message
    assert "#277" in message


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
        that lands mid-run. Both refuse as ``CompactedRetentionError``.
        """
        from hypergraph.checkpointers.types import StepRecord, StepStatus, _utcnow
        from hypergraph.runners._shared.state_restore import has_prior_completion_evidence

        child = Graph(nodes=[_noop_node()], name="child")
        graph_node = child.as_node(name="child_wf")

        baseline = StepRecord(
            run_id="wf",
            superstep=0,
            node_name="__retained_state__",
            index=0,
            status=StepStatus.COMPLETED,
            input_versions={},
            values={"doubled": 4},
            created_at=_utcnow(),
            node_type="RetentionBaseline",
        )
        with pytest.raises(CompactedRetentionError) as error:
            has_prior_completion_evidence([baseline], graph_node)

        assert error.value.node_name == "child_wf"
        assert error.value.pruned_nodes == ()
        assert error.value.code == "HG_COMPACTED_RETENTION"
        message = str(error.value)
        assert "retention='full'" in message
        assert "retention='latest'" in message
        assert "#277" in message


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
