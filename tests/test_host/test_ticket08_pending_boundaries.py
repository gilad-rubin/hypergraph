"""Durable Host V1 — ticket 08 / PRD 0013: pending node boundaries.

Covers: every runnable sibling is durably attributable *before* any sibling
in that superstep can cause external work; a real SIGKILL between sibling
boundaries keeps committed facts and leaves unfinished siblings visible;
nested-graph boundaries keep the parent-facing address across the kill; a
loop's interrupted iteration never leaks into the next iteration's identity;
sync and async runners (and the Memory and SQLite backends) expose the same
recovery result; existing checkpoint resume is unaffected.

Recorded-vs-committed granularity, proven by the kill tests below: a
StepRecord is written when its superstep finishes, so a process that dies
mid-superstep loses the whole superstep's journal entries. That is exactly
the gap PRD 0013 names — after this ticket the *boundaries* survive, so
recovery reads unfinished siblings instead of inferring them from silence.
Re-dispatching a repeat-safe sibling is explicitly tolerated ("this only
wastes effort"); PRD 0014 / ticket 09 makes the effectful case safe via the
``dispatched_at`` seam.
"""

import asyncio
import contextlib
import sqlite3
import subprocess
import sys
import time

import pytest
import pytest_asyncio

from hypergraph import (
    END,
    AsyncRunner,
    Graph,
    RunHome,
    RunRef,
    SyncRunner,
    node,
    route,
    serve,
)
from hypergraph.checkpointers import (
    BoundaryState,
    Checkpointer,
    CheckpointPolicy,
    MemoryCheckpointer,
    SqliteCheckpointer,
    WorkflowStatus,
    node_address,
    parse_node_address,
)
from hypergraph.checkpointers.protocols import PendingNodeProtocol, SyncPendingNodeProtocol

aiosqlite = pytest.importorskip("aiosqlite")


# === Helpers ===


def _home_uri(tmp_path, filename: str = "runs.db") -> str:
    return f"file:{tmp_path / filename}"


def _read_boundaries_from_disk(db_path: str, run_id: str) -> list[tuple[int, str]]:
    """Read committed boundary rows over a FRESH connection.

    A fresh connection can only see what actually reached the database file,
    so this is durability evidence rather than in-process bookkeeping.
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT superstep, node_name FROM pending_nodes WHERE run_id = ? ORDER BY superstep, node_name",
            (run_id,),
        ).fetchall()
    finally:
        conn.close()
    return [(int(row[0]), row[1]) for row in rows]


def _addresses(boundaries) -> list[str]:
    return [b.address for b in boundaries]


def _states(boundaries) -> dict[str, BoundaryState]:
    return {b.address: b.state for b in boundaries}


async def _wait_for(check, timeout: float = 20.0, interval: float = 0.02):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await check()
        if value:
            return value
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(interval)


async def _terminal_view(client, ref):
    view = await client.get(ref)
    if view is not None and view.status in {
        WorkflowStatus.COMPLETED,
        WorkflowStatus.FAILED,
        WorkflowStatus.PARTIAL,
        WorkflowStatus.STOPPED,
    }:
        return view
    return None


@contextlib.asynccontextmanager
async def _worker(host, worker_id: str = "w-test", **kwargs):
    task = asyncio.create_task(host.work_forever(worker_id, **kwargs))
    try:
        yield task
    finally:
        host.shutdown()
        await asyncio.wait_for(task, timeout=30)


@pytest_asyncio.fixture
async def home(tmp_path):
    h = RunHome.open(_home_uri(tmp_path))
    yield h
    await h.close()


@pytest_asyncio.fixture
async def sqlite_cp(tmp_path):
    cp = SqliteCheckpointer(str(tmp_path / "cp.db"))
    yield cp
    await cp.close()


# === 1. Boundaries persist before any sibling can cause external work ===


class TestBoundariesPrecedeSiblingEffects:
    """Checkbox 1: every runnable sibling is durably attributable first."""

    @staticmethod
    def _sibling_graph(observed: dict, db_path: str, run_id: str, *, async_nodes: bool):
        """seed (superstep 0) then three siblings (superstep 1).

        Every sibling records what the DATABASE FILE already held when it
        started, so the assertion is "all boundaries were durable before ANY
        sibling ran", not "before the last one ran".
        """

        @node(output_name="seeded")
        def seed(x: int) -> int:
            return x

        def _observe(name: str) -> None:
            observed[name] = _read_boundaries_from_disk(db_path, run_id)

        if async_nodes:

            @node(output_name="a_out")
            async def alpha(seeded: int) -> int:
                _observe("alpha")
                await asyncio.sleep(0)
                return seeded + 1

            @node(output_name="b_out")
            async def beta(seeded: int) -> int:
                _observe("beta")
                await asyncio.sleep(0)
                return seeded + 2

            @node(output_name="c_out")
            async def gamma(seeded: int) -> int:
                _observe("gamma")
                await asyncio.sleep(0)
                return seeded + 3

        else:

            @node(output_name="a_out")
            def alpha(seeded: int) -> int:
                _observe("alpha")
                return seeded + 1

            @node(output_name="b_out")
            def beta(seeded: int) -> int:
                _observe("beta")
                return seeded + 2

            @node(output_name="c_out")
            def gamma(seeded: int) -> int:
                _observe("gamma")
                return seeded + 3

        return Graph([seed, alpha, beta, gamma], name="sibs")

    def test_sync_siblings_all_see_every_boundary_already_durable(self, tmp_path):
        db_path = str(tmp_path / "sync.db")
        observed: dict = {}
        cp = SqliteCheckpointer(db_path)
        graph = self._sibling_graph(observed, db_path, "wf-sync", async_nodes=False)
        try:
            result = SyncRunner(checkpointer=cp).run(graph, {"x": 1}, workflow_id="wf-sync")
        finally:
            asyncio.run(cp.close())

        assert result["c_out"] == 4
        expected = [(1, "alpha"), (1, "beta"), (1, "gamma")]
        assert set(observed) == {"alpha", "beta", "gamma"}
        for name, rows in observed.items():
            # seed's own boundary (superstep 0) is there too; the siblings are
            # what matters — all three were durable before the first one ran.
            assert [row for row in rows if row[0] == 1] == expected, name
            assert (0, "seed") in rows, name

    async def test_async_siblings_all_see_every_boundary_already_durable(self, tmp_path):
        db_path = str(tmp_path / "async.db")
        observed: dict = {}
        cp = SqliteCheckpointer(db_path)
        graph = self._sibling_graph(observed, db_path, "wf-async", async_nodes=True)
        try:
            result = await AsyncRunner(checkpointer=cp).run(graph, {"x": 1}, workflow_id="wf-async")
        finally:
            await cp.close()

        assert result["c_out"] == 4
        expected = [(1, "alpha"), (1, "beta"), (1, "gamma")]
        assert set(observed) == {"alpha", "beta", "gamma"}
        for name, rows in observed.items():
            assert [row for row in rows if row[0] == 1] == expected, name

    async def test_memory_backend_records_the_same_boundaries(self):
        seen: dict = {}
        cp = MemoryCheckpointer()

        @node(output_name="seeded")
        async def seed(x: int) -> int:
            return x

        @node(output_name="a_out")
        async def alpha(seeded: int) -> int:
            seen["alpha"] = _addresses(await cp.get_node_boundaries("wf-mem"))
            return seeded + 1

        @node(output_name="b_out")
        async def beta(seeded: int) -> int:
            seen["beta"] = _addresses(await cp.get_node_boundaries("wf-mem"))
            return seeded + 2

        graph = Graph([seed, alpha, beta], name="sibs")
        await AsyncRunner(checkpointer=cp).run(graph, {"x": 1}, workflow_id="wf-mem")

        for name, addresses in seen.items():
            assert "wf-mem:1:alpha" in addresses, name
            assert "wf-mem:1:beta" in addresses, name
        assert _states(await cp.get_node_boundaries("wf-mem")) == {
            "wf-mem:0:seed": BoundaryState.COMMITTED,
            "wf-mem:1:alpha": BoundaryState.COMMITTED,
            "wf-mem:1:beta": BoundaryState.COMMITTED,
        }

    async def test_every_boundary_address_matches_its_step_address(self, sqlite_cp):
        @node(output_name="seeded")
        def seed(x: int) -> int:
            return x

        @node(output_name="a_out")
        def alpha(seeded: int) -> int:
            return seeded

        graph = Graph([seed, alpha], name="match")
        await AsyncRunner(checkpointer=sqlite_cp).run(graph, {"x": 1}, workflow_id="wf-match")

        steps = await sqlite_cp.get_steps("wf-match")
        step_addresses = {node_address(s.run_id, s.superstep, s.node_name) for s in steps}
        boundaries = await sqlite_cp.get_node_boundaries("wf-match")
        assert {b.address for b in boundaries} == step_addresses
        assert all(b.state is BoundaryState.COMMITTED for b in boundaries)
        assert all(b.dispatched_at is None for b in boundaries)
        # The sync mirror reads the same rows.
        assert _addresses(sqlite_cp.node_boundaries("wf-match")) == _addresses(boundaries)

    async def test_exit_durability_records_no_boundaries(self, tmp_path):
        """``durability="exit"`` opted out of mid-run persistence entirely."""
        cp = SqliteCheckpointer(str(tmp_path / "exit.db"), policy=CheckpointPolicy(durability="exit", retention="latest"))

        @node(output_name="out")
        def compute(x: int) -> int:
            return x + 1

        try:
            await AsyncRunner(checkpointer=cp).run(Graph([compute], name="exitdef"), {"x": 1}, workflow_id="wf-exit")
            assert await cp.get_node_boundaries("wf-exit") == []
        finally:
            await cp.close()

    async def test_checkpointer_without_the_seam_still_runs(self):
        """A third-party checkpointer lacking the seam must not hard-fail."""

        class BareCheckpointer(Checkpointer):
            def __init__(self):
                super().__init__()
                self.steps: list = []
                self.runs: dict = {}

            async def save_step(self, record):
                self.steps.append(record)

            async def create_run(self, run_id, **kwargs):
                from hypergraph.checkpointers import Run

                run = Run(id=run_id, status=WorkflowStatus.ACTIVE)
                self.runs[run_id] = run
                return run

            async def update_run_status(self, run_id, status, **kwargs):
                self.runs[run_id].status = status

            async def get_state(self, run_id, *, superstep=None):
                return {}

            async def get_steps(self, run_id, *, superstep=None, show_internal=False):
                return []

            async def get_run_async(self, run_id):
                return self.runs.get(run_id)

            async def list_runs(self, **kwargs):
                return list(self.runs.values())

        bare = BareCheckpointer()
        assert not isinstance(bare, PendingNodeProtocol)

        @node(output_name="out")
        def compute(x: int) -> int:
            return x + 1

        result = await AsyncRunner(checkpointer=bare).run(Graph([compute], name="bare"), {"x": 1}, workflow_id="wf-bare")
        assert result["out"] == 2
        assert [s.node_name for s in bare.steps] == ["compute"]

    def test_backends_advertise_the_seam(self, tmp_path):
        assert isinstance(MemoryCheckpointer(), PendingNodeProtocol)
        cp = SqliteCheckpointer(str(tmp_path / "probe.db"))
        try:
            assert isinstance(cp, PendingNodeProtocol)
            assert isinstance(cp, SyncPendingNodeProtocol)
        finally:
            asyncio.run(cp.close())


# === 2. Node addressing shared with the pause slot (PRD 0010) ===


class TestNodeAddressing:
    """``<run_id>:<superstep>:<node_name>`` — PRD 0010's pause_id shape."""

    def test_round_trips_including_nested_and_batch_run_ids(self):
        cases = [
            ("refund-c-42", 8, "approval"),
            ("wf-1/nested", 0, "inner"),
            ("wf-batch:item-7", 3, "charge_card"),
            ("wf-batch:item-7/nested", 12, "notify"),
        ]
        for run_id, superstep, name in cases:
            address = node_address(run_id, superstep, name)
            assert parse_node_address(address) == (run_id, superstep, name)
        assert node_address("refund-c-42", 8, "approval") == "refund-c-42:8:approval"

    def test_malformed_address_raises(self):
        for bad in ["", "wf-1", "wf-1:node", "wf-1:notanint:node"]:
            with pytest.raises(ValueError, match="Malformed node address"):
                parse_node_address(bad)


# === 3. Real SIGKILL between sibling boundaries (flat + nested) ===

_SIBLING_KILL_SCRIPT = """
import asyncio
import time

from hypergraph import AsyncRunner, Graph, RunHome, SyncRunner, node, serve

MARKER = {marker!r}


def mark(name):
    with open(MARKER, "a") as f:
        f.write(name + "\\n")


@node(output_name="seeded")
def seed(x: int) -> int:
    mark("seed")
    return x


@node(output_name="alpha_out")
def alpha(seeded: int) -> int:
    mark("alpha")
    return seeded + 1


@node(output_name="inner_fast_out")
def inner_fast(seeded: int) -> int:
    mark("inner_fast")
    return seeded


@node(output_name="inner_slow_out")
{async_kw}def inner_slow(inner_fast_out: int) -> int:
    mark("inner_slow")
    {sleep_stmt}
    return inner_fast_out


inner = Graph([inner_fast, inner_slow], name="inner")
graph = Graph([seed, alpha, inner.as_node(name="nested")], name="killdef").with_runner({runner})
home = RunHome.open({uri!r})
host = serve(graph, home=home, deployment_version="v1")
host.submit_sync("killdef", {{"x": 1}}, workflow_id="wf-kill")
asyncio.run(host.work_forever("w-child", poll_interval=0.02))
"""


def _run_until_marker(script: str, marker, needle: str, *, settle: float = 0.4) -> None:
    """Start the child, wait for ``needle``, then SIGKILL it (no cleanup)."""
    proc = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            if marker.exists() and needle in marker.read_text():
                break
            if proc.poll() is not None:
                raise AssertionError("child worker exited before reaching the marker")
            time.sleep(0.05)
        else:
            raise AssertionError(f"child never wrote marker {needle!r}")
        time.sleep(settle)
        proc.kill()
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()


def _parent_kill_graph(marker_path: str, runner):
    @node(output_name="seeded")
    def seed(x: int) -> int:
        with open(marker_path, "a") as f:
            f.write("seed\n")
        return x

    @node(output_name="alpha_out")
    def alpha(seeded: int) -> int:
        with open(marker_path, "a") as f:
            f.write("alpha\n")
        return seeded + 1

    @node(output_name="inner_fast_out")
    def inner_fast(seeded: int) -> int:
        with open(marker_path, "a") as f:
            f.write("inner_fast\n")
        return seeded

    @node(output_name="inner_slow_out")
    def inner_slow(inner_fast_out: int) -> int:
        with open(marker_path, "a") as f:
            f.write("inner_slow\n")
        return inner_fast_out

    inner = Graph([inner_fast, inner_slow], name="inner")
    return Graph([seed, alpha, inner.as_node(name="nested")], name="killdef").with_runner(runner)


@pytest.mark.parametrize(
    ("kind", "runner_expr", "async_kw", "sleep_stmt"),
    [
        ("sync", "SyncRunner()", "", "time.sleep(30)"),
        ("async", "AsyncRunner()", "async ", "await asyncio.sleep(30)"),
    ],
)
class TestRealKillBetweenSiblingBoundaries:
    """Checkboxes 2 + 3 + 4, proven by a real process boundary."""

    async def test_unfinished_siblings_stay_visible_and_nesting_keeps_its_address(self, tmp_path, home, kind, runner_expr, async_kw, sleep_stmt):
        marker = tmp_path / f"marker-{kind}.txt"
        script = _SIBLING_KILL_SCRIPT.format(
            marker=str(marker),
            uri=_home_uri(tmp_path),
            runner=runner_expr,
            async_kw=async_kw,
            sleep_stmt=sleep_stmt,
        )
        _run_until_marker(script, marker, "inner_slow")
        before_restart = marker.read_text().splitlines()

        # --- what a fresh process reads from the same Run Home ---
        parent = home.node_boundaries("wf-kill")
        parent_states = _states(parent)
        assert parent_states == {
            "wf-kill:0:seed": BoundaryState.COMMITTED,
            "wf-kill:1:alpha": BoundaryState.PENDING,
            "wf-kill:1:nested": BoundaryState.PENDING,
        }
        # Nothing was inferred from silence: both unfinished siblings of the
        # interrupted superstep are named, and neither is an unknown effect.
        assert all(b.state is not BoundaryState.UNKNOWN_EFFECT for b in parent)
        assert {b.node_type for b in parent if b.node_name == "nested"} == {"GraphNode"}

        # The nested boundary carries the PARENT-facing address — the same one
        # the parent's StepRecord for that GraphNode uses.
        (nested_boundary,) = [b for b in parent if b.node_name == "nested"]
        assert nested_boundary.address == node_address("wf-kill", 1, "nested")
        assert parse_node_address(nested_boundary.address) == ("wf-kill", 1, "nested")

        # The child run keeps its own addresses under the child workflow id.
        child = home.node_boundaries("wf-kill/nested")
        assert _states(child) == {
            "wf-kill/nested:0:inner_fast": BoundaryState.COMMITTED,
            "wf-kill/nested:1:inner_slow": BoundaryState.PENDING,
        }
        # The committed fact survived the kill exactly as committed.
        (committed_child_step,) = await home.get_steps("wf-kill/nested")
        assert committed_child_step.node_name == "inner_fast"

        # --- restart: a new process-shaped worker drains the same Home ---
        runner = SyncRunner() if kind == "sync" else AsyncRunner()
        host = serve(_parent_kill_graph(str(marker), runner), home=home, deployment_version="v1")
        ref = RunRef(home=home.uri, run_id="wf-kill")
        async with _worker(host, "w-parent"):
            view = await _wait_for(lambda: _terminal_view(host.client, ref), timeout=40)
        assert view.status == WorkflowStatus.COMPLETED

        after_restart = marker.read_text().splitlines()[len(before_restart) :]
        # The committed inner boundary is not re-executed; the pending ones
        # dispatch exactly once.
        assert after_restart.count("inner_fast") == 0
        assert after_restart.count("inner_slow") == 1
        assert after_restart.count("alpha") == 1

        # Parent-facing addresses match across the kill, and every boundary
        # now settles against the journal.
        final_parent = home.node_boundaries("wf-kill")
        assert node_address("wf-kill", 1, "nested") in _addresses(final_parent)
        assert all(b.state is BoundaryState.COMMITTED for b in final_parent if b.superstep == 0)
        assert not [b for b in final_parent if b.state is BoundaryState.UNKNOWN_EFFECT]


# === 4. Loop: an interrupted iteration never leaks into the next identity ===

_LOOP_KILL_SCRIPT = """
import asyncio
import time

from hypergraph import END, AsyncRunner, Graph, RunHome, SyncRunner, node, route, serve

MARKER = {marker!r}


def mark(name):
    with open(MARKER, "a") as f:
        f.write(name + "\\n")


@node(output_name="count")
{async_kw}def worker(count: int) -> int:
    mark("worker-" + str(count))
    if count == 1:
        {sleep_stmt}
    return count + 1


@route(targets=["worker", END])
def keep_going(count: int) -> str:
    mark("gate-" + str(count))
    return END if count >= 3 else "worker"


graph = Graph([worker, keep_going], name="loopdef", entrypoint="worker").with_runner({runner})
home = RunHome.open({uri!r})
host = serve(graph, home=home, deployment_version="v1")
host.submit_sync("loopdef", {{"count": 0}}, workflow_id="wf-loop")
asyncio.run(host.work_forever("w-child", poll_interval=0.02))
"""


def _parent_loop_graph(marker_path: str, runner):
    def mark(name: str) -> None:
        with open(marker_path, "a") as f:
            f.write(name + "\n")

    @node(output_name="count")
    def worker(count: int) -> int:
        mark(f"worker-{count}")
        return count + 1

    @route(targets=["worker", END])
    def keep_going(count: int) -> str:
        mark(f"gate-{count}")
        return END if count >= 3 else "worker"

    return Graph([worker, keep_going], name="loopdef", entrypoint="worker").with_runner(runner)


@pytest.mark.parametrize(
    ("kind", "runner_expr", "async_kw", "sleep_stmt"),
    [
        ("sync", "SyncRunner()", "", "time.sleep(30)"),
        ("async", "AsyncRunner()", "async ", "await asyncio.sleep(30)"),
    ],
)
class TestLoopIterationIdentity:
    """Checkbox 3 (loops): iteration identity is per-superstep, never shared."""

    async def test_interrupted_iteration_does_not_leak_into_the_next(self, tmp_path, home, kind, runner_expr, async_kw, sleep_stmt):
        marker = tmp_path / f"loop-{kind}.txt"
        script = _LOOP_KILL_SCRIPT.format(
            marker=str(marker),
            uri=_home_uri(tmp_path),
            runner=runner_expr,
            async_kw=async_kw,
            sleep_stmt=sleep_stmt,
        )
        _run_until_marker(script, marker, "worker-1")

        # Iteration 1 committed at superstep 0; the interrupted iteration's
        # boundary is a DIFFERENT address at superstep 2 and stays pending.
        killed = home.node_boundaries("wf-loop")
        assert _states(killed) == {
            "wf-loop:0:worker": BoundaryState.COMMITTED,
            "wf-loop:1:keep_going": BoundaryState.COMMITTED,
            "wf-loop:2:worker": BoundaryState.PENDING,
        }
        worker_boundaries = [b for b in killed if b.node_name == "worker"]
        assert len({b.superstep for b in worker_boundaries}) == len(worker_boundaries)

        # --- restart: the loop continues on fresh superstep identities ---
        runner = SyncRunner() if kind == "sync" else AsyncRunner()
        host = serve(_parent_loop_graph(str(marker), runner), home=home, deployment_version="v1")
        ref = RunRef(home=home.uri, run_id="wf-loop")
        async with _worker(host, "w-parent"):
            view = await _wait_for(lambda: _terminal_view(host.client, ref), timeout=40)
        assert view.status == WorkflowStatus.COMPLETED

        final = home.node_boundaries("wf-loop")
        final_states = _states(final)
        # Recovery re-adopted the interrupted iteration AT ITS OWN ADDRESS
        # (resume offsets supersteps to the first uncommitted one), so the
        # pending record settles where it was made — it never migrates onto
        # the next iteration's identity.
        assert final_states["wf-loop:2:worker"] is BoundaryState.COMMITTED
        # Every iteration keeps a distinct address; the later iteration is a
        # different superstep, not a reuse of the interrupted one.
        worker_supersteps = sorted(b.superstep for b in final if b.node_name == "worker")
        assert worker_supersteps == [0, 2, 4]
        assert len({b.address for b in final}) == len(final)
        assert all(b.state is BoundaryState.COMMITTED for b in final)
        # The whole loop replay is visible: three worker iterations, and the
        # interrupted one is the only node that ran twice.
        executions = marker.read_text().splitlines()
        assert executions.count("worker-1") == 2
        assert executions.count("worker-0") == 1
        assert executions.count("worker-2") == 1


# === 5. Sync/async and Memory/SQLite produce the same recovery result ===


class TestRunnerAndBackendParity:
    """Checkbox 4, verified by comparison rather than asserted twice."""

    @staticmethod
    def _graph():
        @node(output_name="seeded")
        def seed(x: int) -> int:
            return x

        @node(output_name="a_out")
        def alpha(seeded: int) -> int:
            return seeded + 1

        @node(output_name="b_out")
        def beta(seeded: int) -> int:
            return seeded + 2

        inner = Graph([alpha, beta], name="inner")

        @node(output_name="tail")
        def tail(a_out: int, b_out: int) -> int:
            return a_out + b_out

        return Graph([seed, inner.as_node(name="nested"), tail], name="parity")

    async def test_sync_and_async_agree_on_every_boundary(self, tmp_path):
        cp = SqliteCheckpointer(str(tmp_path / "parity.db"))
        graph = self._graph()
        try:
            SyncRunner(checkpointer=cp).run(graph, {"x": 1}, workflow_id="wf-s")
            await AsyncRunner(checkpointer=cp).run(graph, {"x": 1}, workflow_id="wf-a")

            def shape(run_id: str, boundaries):
                return sorted((b.superstep, b.node_name, b.node_type, b.state) for b in boundaries)

            sync_parent = shape("wf-s", cp.node_boundaries("wf-s"))
            async_parent = shape("wf-a", await cp.get_node_boundaries("wf-a"))
            assert sync_parent == async_parent
            sync_child = shape("wf-s/nested", cp.node_boundaries("wf-s/nested"))
            async_child = shape("wf-a/nested", await cp.get_node_boundaries("wf-a/nested"))
            assert sync_child == async_child
            assert sync_child, "nested child boundaries must be recorded too"
        finally:
            await cp.close()

    async def test_memory_and_sqlite_agree_on_every_boundary(self, tmp_path):
        cp = SqliteCheckpointer(str(tmp_path / "backend.db"))
        memory = MemoryCheckpointer()
        graph = self._graph()
        try:
            await AsyncRunner(checkpointer=cp).run(graph, {"x": 1}, workflow_id="wf-x")
            await AsyncRunner(checkpointer=memory).run(graph, {"x": 1}, workflow_id="wf-x")

            def shape(boundaries):
                return sorted((b.superstep, b.node_name, b.node_type, b.state) for b in boundaries)

            assert shape(await cp.get_node_boundaries("wf-x")) == shape(await memory.get_node_boundaries("wf-x"))
            assert shape(await cp.get_node_boundaries("wf-x/nested")) == shape(await memory.get_node_boundaries("wf-x/nested"))
        finally:
            await cp.close()


# === 6. Compatibility: resume, failures, and retention stay truthful ===


class TestCompatibility:
    """Checkbox 5 plus the two ways a derived state could start lying."""

    async def test_checkpoint_resume_is_unchanged_and_boundaries_are_additive(self, sqlite_cp):
        calls: list[str] = []

        @node(output_name="seeded")
        def seed(x: int) -> int:
            calls.append("seed")
            return x

        @node(output_name="out")
        def tail(seeded: int) -> int:
            calls.append("tail")
            if len(calls) < 3:
                raise RuntimeError("boom")
            return seeded + 1

        graph = Graph([seed, tail], name="resumedef")
        runner = AsyncRunner(checkpointer=sqlite_cp)
        with pytest.raises(RuntimeError):
            await runner.run(graph, {"x": 1}, workflow_id="wf-res")

        # The failed step is a witnessed settlement, so its boundary is
        # committed — a pending record never claims a node ran, and a
        # committed one never invents an outcome.
        states = _states(await sqlite_cp.get_node_boundaries("wf-res"))
        assert states["wf-res:0:seed"] is BoundaryState.COMMITTED
        assert states["wf-res:1:tail"] is BoundaryState.COMMITTED

        result = await runner.run(graph, workflow_id="wf-res")
        assert result["out"] == 2
        assert calls == ["seed", "tail", "tail"]  # resume did not re-run seed
        # The resumed attempt owns a NEW address; the old one is untouched.
        final = _states(await sqlite_cp.get_node_boundaries("wf-res"))
        assert "wf-res:2:tail" in final
        assert final["wf-res:1:tail"] is BoundaryState.COMMITTED

    async def test_retention_prunes_boundaries_with_their_steps(self, tmp_path):
        """A pruned step must take its boundary, or COMMITTED would decay."""

        @node(output_name="count")
        def worker(count: int) -> int:
            return count + 1

        @route(targets=["worker", END])
        def keep_going(count: int) -> str:
            return END if count >= 4 else "worker"

        graph = Graph([worker, keep_going], name="retdef", entrypoint="worker")

        for label, cp in (
            ("sqlite", SqliteCheckpointer(str(tmp_path / "ret.db"), policy=CheckpointPolicy(retention="windowed", window=1))),
            ("memory", MemoryCheckpointer()),
        ):
            if label == "memory":
                cp.policy = CheckpointPolicy(retention="windowed", window=1)
            try:
                await AsyncRunner(checkpointer=cp).run(graph, {"count": 0}, workflow_id="wf-ret")
                boundaries = await cp.get_node_boundaries("wf-ret")
                steps = await cp.get_steps("wf-ret", show_internal=True)
                kept = {node_address(s.run_id, s.superstep, s.node_name) for s in steps}
                # Nothing survives without its step, so nothing decays to
                # PENDING after the fact.
                assert all(b.address in kept for b in boundaries), label
                assert all(b.state is BoundaryState.COMMITTED for b in boundaries), label
            finally:
                if label == "sqlite":
                    await cp.close()
