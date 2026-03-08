"""Integration tests for hierarchical checkpointing: map(), nested graphs, and cycles."""

import pytest

from hypergraph import END, AsyncRunner, Graph, SyncRunner, node, route
from hypergraph.checkpointers import CheckpointPolicy, SqliteCheckpointer

aiosqlite = pytest.importorskip("aiosqlite")


# --- Shared nodes ---


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled * 3


# --- Fixtures ---


@pytest.fixture
def async_cp(tmp_path):
    cp = SqliteCheckpointer(str(tmp_path / "test.db"))
    cp.policy = CheckpointPolicy(durability="sync", retention="full")
    yield cp


@pytest.fixture
def sync_cp(tmp_path):
    cp = SqliteCheckpointer(str(tmp_path / "test.db"))
    cp.policy = CheckpointPolicy(durability="sync", retention="full")
    db = cp._sync_db()
    from hypergraph.checkpointers._migrate import ensure_schema

    ensure_schema(db)
    yield cp
    db.close()


# --- AsyncRunner map() ---


class TestAsyncMapCheckpointing:
    async def test_map_creates_parent_and_children(self, async_cp):
        """map() creates a parent batch run with child runs."""
        runner = AsyncRunner(checkpointer=async_cp)
        graph = Graph([double])

        await runner.map(graph, {"x": [1, 2, 3]}, map_over="x", workflow_id="batch")

        runs = async_cp.runs()
        run_ids = {r.id for r in runs}
        assert "batch" in run_ids
        assert "batch/0" in run_ids
        assert "batch/1" in run_ids
        assert "batch/2" in run_ids

        # Children link to parent
        for r in runs:
            if r.id.startswith("batch/"):
                assert r.parent_run_id == "batch"

    async def test_map_parent_run_completed(self, async_cp):
        """Parent batch run is marked completed with correct stats."""
        runner = AsyncRunner(checkpointer=async_cp)
        graph = Graph([double])

        await runner.map(graph, {"x": [10, 20]}, map_over="x", workflow_id="batch-stats")

        run = async_cp.get_run("batch-stats")
        assert run.status.value == "completed"
        assert run.node_count == 2

    async def test_map_partial_failure(self, async_cp):
        """Partially failed batch still creates all child runs."""

        @node(output_name="result")
        def maybe_fail(x: int) -> int:
            if x == 2:
                raise ValueError("bad value")
            return x * 10

        runner = AsyncRunner(checkpointer=async_cp)
        graph = Graph([maybe_fail])

        await runner.map(graph, {"x": [1, 2, 3]}, map_over="x", workflow_id="batch-fail", error_handling="continue")

        runs = async_cp.runs()
        child_runs = {r.id: r for r in runs if r.parent_run_id == "batch-fail"}
        assert len(child_runs) == 3

        # Item 1 failed
        failed = async_cp.get_run("batch-fail/1")
        assert failed.status.value == "failed"

    async def test_top_level_only_filter(self, async_cp):
        """runs(parent_run_id=None) returns only top-level runs."""
        runner = AsyncRunner(checkpointer=async_cp)
        graph = Graph([double])

        await runner.map(graph, {"x": [1, 2]}, map_over="x", workflow_id="batch-filter")

        top_level = async_cp.runs(parent_run_id=None)
        assert len(top_level) == 1
        assert top_level[0].id == "batch-filter"

    async def test_children_of_filter(self, async_cp):
        """runs(parent_run_id='X') returns children of X."""
        runner = AsyncRunner(checkpointer=async_cp)
        graph = Graph([double])

        await runner.map(graph, {"x": [1, 2]}, map_over="x", workflow_id="batch-children")

        children = async_cp.runs(parent_run_id="batch-children")
        assert len(children) == 2
        assert {r.id for r in children} == {"batch-children/0", "batch-children/1"}

    async def test_workflow_id_with_slash_rejected_async(self):
        """User-provided workflow_id containing '/' is rejected."""
        runner = AsyncRunner()
        graph = Graph([double])

        with pytest.raises(ValueError, match="cannot contain '/'"):
            await runner.map(graph, {"x": [1]}, map_over="x", workflow_id="bad/id")

        with pytest.raises(ValueError, match="cannot contain '/'"):
            await runner.run(graph, {"x": 1}, workflow_id="also/bad")


# --- Nested graph checkpointing ---


class TestNestedGraphCheckpointing:
    async def test_nested_graph_creates_child_run(self, async_cp):
        """GraphNode creates a child run with parent linkage."""
        runner = AsyncRunner(checkpointer=async_cp)
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node(name="embed"), triple], name="outer")

        result = await runner.run(outer, {"x": 5}, workflow_id="nested")

        assert result["tripled"] == 30

        # Should have parent and child runs
        runs = async_cp.runs()
        run_map = {r.id: r for r in runs}
        assert "nested" in run_map
        assert "nested/embed" in run_map
        assert run_map["nested/embed"].parent_run_id == "nested"

    async def test_nested_step_has_child_run_id(self, async_cp):
        """StepRecord for GraphNode has child_run_id populated."""
        runner = AsyncRunner(checkpointer=async_cp)
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node(name="embed"), triple], name="outer")

        await runner.run(outer, {"x": 5}, workflow_id="nested-step")

        steps = async_cp.steps("nested-step")
        embed_step = next(s for s in steps if s.node_name == "embed")
        assert embed_step.child_run_id == "nested-step/embed"

        triple_step = next(s for s in steps if s.node_name == "triple")
        assert triple_step.child_run_id is None

    async def test_nested_map_creates_hierarchy(self, async_cp):
        """GraphNode with map_over creates nested batch hierarchy."""
        runner = AsyncRunner(checkpointer=async_cp)
        inner = Graph([double], name="inner")
        embed = inner.as_node(name="embed").map_over("x")
        outer = Graph([embed], name="outer")

        await runner.run(outer, {"x": [1, 2]}, workflow_id="nested-map")

        runs = async_cp.runs()
        run_ids = {r.id for r in runs}

        # outer → embed (batch) → embed/0, embed/1
        assert "nested-map" in run_ids
        assert "nested-map/embed" in run_ids
        assert "nested-map/embed/0" in run_ids
        assert "nested-map/embed/1" in run_ids

    async def test_deep_nesting(self, async_cp):
        """Three-level nesting: A → B → C."""
        runner = AsyncRunner(checkpointer=async_cp)

        @node(output_name="result")
        def add_one(x: int) -> int:
            return x + 1

        inner = Graph([add_one], name="C")
        mid_node = inner.as_node(name="B")
        mid = Graph([mid_node], name="mid")
        outer_node = mid.as_node(name="A")
        outer = Graph([outer_node], name="outer")

        result = await runner.run(outer, {"x": 5}, workflow_id="deep")
        assert result["result"] == 6

        runs = async_cp.runs()
        run_ids = {r.id for r in runs}
        assert "deep" in run_ids
        assert "deep/A" in run_ids
        assert "deep/A/B" in run_ids

    async def test_no_workflow_id_auto_generates_nested_checkpointing(self, async_cp):
        """Without workflow_id, parent/child runs use an auto-generated id."""
        runner = AsyncRunner(checkpointer=async_cp)
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node(name="embed"), triple], name="outer")

        result = await runner.run(outer, {"x": 5})
        assert result["tripled"] == 30
        assert result.workflow_id is not None

        runs = async_cp.runs()
        run_ids = {r.id for r in runs}
        assert result.workflow_id in run_ids
        assert f"{result.workflow_id}/embed" in run_ids

    async def test_nested_workflow_can_be_forked_with_lineage(self, async_cp):
        """Forking an outer workflow preserves lineage and nested child structure."""
        runner = AsyncRunner(checkpointer=async_cp)
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node(name="embed"), triple], name="outer")

        await runner.run(outer, {"x": 5}, workflow_id="nested-root")
        fork_id, fork_cp = async_cp.fork_workflow("nested-root", workflow_id="nested-root-fork")
        await runner.run(outer, {"x": 10}, checkpoint=fork_cp, workflow_id=fork_id)

        fork_run = async_cp.get_run("nested-root-fork")
        assert fork_run is not None
        assert fork_run.forked_from == "nested-root"
        assert fork_run.retry_of is None

        child = next((run for run in async_cp.runs(parent_run_id="nested-root-fork") if run.id.startswith("nested-root-fork/embed")), None)
        assert child is not None
        assert child.parent_run_id == "nested-root-fork"

    async def test_nested_graph_in_outer_cycle_uses_distinct_child_run_ids(self, async_cp):
        """Checkpointed outer cycles should not collide on repeated nested executions."""

        @node(output_name="count")
        def increment(count: int, limit: int = 3) -> int:
            return count + 1

        inner = Graph([increment], name="inner", entrypoint="increment")

        @route(targets=["inner", END])
        def decide(count: int, limit: int = 3) -> str:
            return END if count >= limit else "inner"

        runner = AsyncRunner(checkpointer=async_cp)
        outer = Graph([inner.as_node(), decide], entrypoint="inner")

        result = await runner.run(outer, {"count": 0, "limit": 3}, workflow_id="nested-cycle")
        assert result["count"] == 3

        run_ids = {run.id for run in async_cp.runs()}
        inner_run_ids = {run_id for run_id in run_ids if run_id.startswith("nested-cycle/inner")}
        assert "nested-cycle/inner" in inner_run_ids
        assert len(inner_run_ids) == 3

    async def test_nested_graph_in_outer_cycle_uses_distinct_child_ids_when_output_repeats(self, async_cp):
        """Repeated nested executions should still get new child ids if outputs stay equal."""

        @node(output_name="tick")
        def tick(tick: int) -> int:
            return tick + 1

        @node(output_name="stable")
        def constant(tick: int) -> int:
            return 1

        inner = Graph([tick, constant], name="inner", entrypoint="tick")

        @route(targets=["inner", END])
        def decide(tick: int) -> str:
            return END if tick >= 3 else "inner"

        runner = AsyncRunner(checkpointer=async_cp)
        outer = Graph([inner.as_node(), decide], entrypoint="inner")

        result = await runner.run(outer, {"tick": 0}, workflow_id="nested-stable")
        assert result["tick"] == 3
        assert result["stable"] == 1

        run_ids = {run.id for run in async_cp.runs()}
        inner_run_ids = {run_id for run_id in run_ids if run_id.startswith("nested-stable/inner")}
        assert "nested-stable/inner" in inner_run_ids
        assert len(inner_run_ids) == 3


# --- SyncRunner nested + map ---


class TestSyncNestedCheckpointing:
    def test_sync_nested_graph(self, sync_cp):
        """SyncRunner: nested GraphNode creates child run."""
        runner = SyncRunner(checkpointer=sync_cp)
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node(name="embed"), triple], name="outer")

        result = runner.run(outer, {"x": 5}, workflow_id="sync-nested")
        assert result["tripled"] == 30

        db = sync_cp._sync_db()
        runs = db.execute("SELECT id, parent_run_id FROM runs ORDER BY id").fetchall()
        run_map = {r[0]: r[1] for r in runs}
        assert "sync-nested" in run_map
        assert "sync-nested/embed" in run_map
        assert run_map["sync-nested/embed"] == "sync-nested"

    def test_sync_nested_step_child_run_id(self, sync_cp):
        """SyncRunner: StepRecord for GraphNode has child_run_id."""
        runner = SyncRunner(checkpointer=sync_cp)
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node(name="embed"), triple], name="outer")

        runner.run(outer, {"x": 5}, workflow_id="sync-step")

        db = sync_cp._sync_db()
        step = db.execute(
            "SELECT child_run_id FROM steps WHERE run_id = ? AND node_name = 'embed'",
            ("sync-step",),
        ).fetchone()
        assert step[0] == "sync-step/embed"

    def test_sync_map_with_nested(self, sync_cp):
        """SyncRunner: map + nested graph creates full hierarchy."""
        runner = SyncRunner(checkpointer=sync_cp)
        inner = Graph([double], name="inner")
        embed = inner.as_node(name="embed").map_over("x")
        outer = Graph([embed], name="outer")

        runner.run(outer, {"x": [1, 2]}, workflow_id="sync-map-nest")

        db = sync_cp._sync_db()
        runs = db.execute("SELECT id FROM runs ORDER BY id").fetchall()
        run_ids = {r[0] for r in runs}

        assert "sync-map-nest" in run_ids
        assert "sync-map-nest/embed" in run_ids
        assert "sync-map-nest/embed/0" in run_ids
        assert "sync-map-nest/embed/1" in run_ids
