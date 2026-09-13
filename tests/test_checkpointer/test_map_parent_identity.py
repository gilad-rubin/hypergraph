"""Parent-boundary identity validation for a crash-resumed ``map()`` (#309).

``map()`` writes its parent batch row with an upsert, so before this ticket the
stored graph hash and retry-policy manifest were overwritten before anything
could compare them: re-running a batch under the same ``workflow_id`` with a
DIFFERENT graph returned the old graph's restored values, reported COMPLETED,
and left a row claiming the new graph had run it.

Assertion map (ticket acceptance items):
    a   changed graph rejects, no item runs         TestMapParentIdentityRejects
    b   changed policy rejects with field diff      TestMapParentIdentityRejects
    -   stored parent config survives rejection     TestMapParentIdentityRejects
    -   a fresh workflow_id adopts the change       TestMapForksStayFree
    -   identical graph+policy still resumes        TestLegitimateMapResume
    -   identity ONLY (completed batches top up)    TestMapKeepsNonIdentityAdmissions
    -   nested map behaves like a flat map          TestNestedMapParentIdentity
"""

from __future__ import annotations

import inspect
import json
from dataclasses import asdict

import pytest
import pytest_asyncio

from hypergraph import (
    AsyncRunner,
    Graph,
    RetryPolicy,
    RetryPolicyChangedError,
    SyncRunner,
    node,
)
from hypergraph.checkpointers import SqliteCheckpointer
from hypergraph.events import EventProcessor
from hypergraph.exceptions import GraphChangedError
from hypergraph.runners._shared.policy_manifest import RetryPolicyManifest
from hypergraph.runners._shared.results import RunStatus

aiosqlite = pytest.importorskip("aiosqlite")


# === Helpers ===


class _EventNameCollector(EventProcessor):
    """Records the type name of every event the runner dispatches."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def on_event(self, event) -> None:
        self.names.append(type(event).__name__)


def _rows_snapshot(cp: SqliteCheckpointer) -> str:
    """Serialize every persisted run row for a literal before/after compare."""
    return json.dumps(
        [asdict(run) for run in sorted(cp.runs(), key=lambda run: run.id)],
        sort_keys=True,
        default=str,
    )


def _make_runner(family: str, **kwargs):
    return SyncRunner(**kwargs) if family == "sync" else AsyncRunner(**kwargs)


async def _map(runner, *args, **kwargs):
    result = runner.map(*args, **kwargs)
    if inspect.iscoroutine(result):
        result = await result
    return result


async def _run(runner, *args, **kwargs):
    result = runner.run(*args, **kwargs)
    if inspect.iscoroutine(result):
        result = await result
    return result


@pytest.fixture(params=["sync", "async"])
def family(request) -> str:
    return request.param


@pytest_asyncio.fixture
async def make_sqlite(tmp_path):
    created: list[SqliteCheckpointer] = []

    def factory(name: str = "map-identity.db") -> SqliteCheckpointer:
        cp = SqliteCheckpointer(str(tmp_path / name))
        created.append(cp)
        return cp

    yield factory
    for cp in created:
        await cp.close()


def _scaling_graph(calls: list[int], factor: int) -> Graph:
    """One node per factor. The node NAME differs, so the structure differs."""

    @node(output_name="out")
    def double(x: int) -> int:
        calls.append(x)
        return x * 2

    @node(output_name="out")
    def triple(x: int) -> int:
        calls.append(x)
        return x * 3

    @node(output_name="out")
    def septuple(x: int) -> int:
        calls.append(x)
        return x * 7

    return Graph([{2: double, 3: triple, 7: septuple}[factor]], name="scaler")


def _policy_graph(calls: list[int], *, max_attempts: int) -> Graph:
    """One node with a fixed name and a tunable retry budget.

    The node name and signature never change, so the structural hash is stable
    and only the policy manifest moves.
    """

    @node(
        output_name="out",
        retry=RetryPolicy(
            max_attempts=max_attempts,
            retry_on=(ConnectionError,),
            initial_delay=0.001,
            jitter="none",
        ),
    )
    def work(x: int) -> int:
        calls.append(x)
        return x * 2

    return Graph([work], name="worker")


# === Red-green items (a) and (b): the parent boundary rejects ===


class TestMapParentIdentityRejects:
    async def test_changed_graph_rejects_before_any_item_executes(self, family, make_sqlite):
        cp = make_sqlite()
        calls: list[int] = []
        runner = _make_runner(family, checkpointer=cp)
        doubling = _scaling_graph(calls, 2)

        first = await _map(runner, doubling, {"x": [1, 2]}, map_over="x", workflow_id="batch")
        assert first.status == RunStatus.COMPLETED
        assert [item["out"] for item in first.results] == [2, 4]
        assert sorted(calls) == [1, 2]

        calls.clear()
        tripling = _scaling_graph(calls, 3)
        assert tripling.structural_hash != doubling.structural_hash

        with pytest.raises(GraphChangedError) as exc_info:
            await _map(runner, tripling, {"x": [1, 2]}, map_over="x", workflow_id="batch")

        assert "batch" in str(exc_info.value)
        # The sentinel: rejection precedes every item AND the persisted world.
        assert calls == [], "no item may execute on a rejected batch resume"
        stored = cp.get_run("batch")
        assert stored.config["graph_struct_hash"] == doubling.structural_hash, "the parent config must not be overwritten by a rejected resume"

    async def test_changed_retry_policy_rejects_with_the_run_paths_diagnostic(self, family, make_sqlite):
        cp = make_sqlite()
        calls: list[int] = []
        runner = _make_runner(family, checkpointer=cp)
        budget_three = _policy_graph(calls, max_attempts=3)

        first = await _map(runner, budget_three, {"x": [1, 2]}, map_over="x", workflow_id="policy-batch")
        assert first.status == RunStatus.COMPLETED
        assert sorted(calls) == [1, 2]

        calls.clear()
        budget_nine = _policy_graph(calls, max_attempts=9)
        assert budget_nine.structural_hash == budget_three.structural_hash, "the policy half must not be masked by a structural change"

        with pytest.raises(RetryPolicyChangedError) as exc_info:
            await _map(runner, budget_nine, {"x": [1, 2]}, map_over="x", workflow_id="policy-batch")

        error = exc_info.value
        assert error.code == "HG_RETRY_POLICY_CHANGED"
        assert error.workflow_id == "policy-batch"
        assert [(c.node_name, c.field, c.stored, c.current) for c in error.changes] == [("work", "max_attempts", 3, 9)]
        assert "max_attempts" in str(error)
        assert "HG_RETRY_POLICY_CHANGED" in str(error)

        assert calls == [], "no item may execute on a rejected batch resume"
        stored_manifest = RetryPolicyManifest.from_config(cp.get_run("policy-batch").config)
        assert stored_manifest is not None
        assert stored_manifest.entries[0].max_attempts == 3, "the stored manifest must not be overwritten by a rejected resume"

    async def test_a_rejected_resume_emits_nothing_and_writes_nothing(self, family, make_sqlite):
        """The gate sits BEFORE the run-start event, not just before create_run.

        Placed one step later — immediately before `create_run_sync`, where the
        ticket originally proposed it — this batch emits `['RunStartEvent']`
        with no matching `RunEndEvent`: a dangling event and OTel span for every
        rejected resume. Zero events is the assertion that pins the placement.
        """
        cp = make_sqlite()
        calls: list[int] = []
        runner = _make_runner(family, checkpointer=cp)

        await _map(runner, _scaling_graph(calls, 2), {"x": [1, 2]}, map_over="x", workflow_id="silent")
        before = _rows_snapshot(cp)

        calls.clear()
        collector = _EventNameCollector()
        with pytest.raises(GraphChangedError):
            await _map(
                runner,
                _scaling_graph(calls, 3),
                {"x": [1, 2]},
                map_over="x",
                workflow_id="silent",
                event_processors=[collector],
            )

        assert collector.names == [], "a rejected resume must not dispatch a single event"
        assert calls == []
        assert _rows_snapshot(cp) == before, "a rejected resume must not touch any persisted row"

    async def test_graph_change_takes_precedence_over_policy_change(self, family, make_sqlite):
        """Both changed at once reports the coarser fact, like the run path."""
        cp = make_sqlite()
        calls: list[int] = []
        runner = _make_runner(family, checkpointer=cp)

        await _map(runner, _policy_graph(calls, max_attempts=3), {"x": [1]}, map_over="x", workflow_id="both")

        with pytest.raises(GraphChangedError):
            await _map(runner, _scaling_graph(calls, 7), {"x": [1]}, map_over="x", workflow_id="both")

    async def test_legacy_parent_config_without_a_manifest_still_resumes(self, family, make_sqlite):
        """Rows written before manifests existed skip policy validation."""
        cp = make_sqlite()
        calls: list[int] = []
        runner = _make_runner(family, checkpointer=cp)
        graph = _policy_graph(calls, max_attempts=3)

        await _map(runner, graph, {"x": [1]}, map_over="x", workflow_id="legacy-policy")
        cp.create_run_sync(
            "legacy-policy",
            graph_name=graph.name,
            config={"graph_struct_hash": graph.structural_hash},
        )

        calls.clear()
        result = await _map(
            runner,
            _policy_graph(calls, max_attempts=9),
            {"x": [1]},
            map_over="x",
            workflow_id="legacy-policy",
        )
        assert result.status == RunStatus.COMPLETED
        assert calls == [], "the completed child is still restored"


# === Forks stay free ===


class TestMapForksStayFree:
    async def test_a_fresh_workflow_id_adopts_the_changed_graph_and_policy(self, family, make_sqlite):
        cp = make_sqlite()
        calls: list[int] = []
        runner = _make_runner(family, checkpointer=cp)

        await _map(runner, _scaling_graph(calls, 2), {"x": [1, 2]}, map_over="x", workflow_id="batch-v1")

        calls.clear()
        forked = await _map(runner, _scaling_graph(calls, 3), {"x": [1, 2]}, map_over="x", workflow_id="batch-v2")
        assert forked.status == RunStatus.COMPLETED
        assert [item["out"] for item in forked.results] == [3, 6]
        assert sorted(calls) == [1, 2]

        calls.clear()
        policy_fork = await _map(runner, _policy_graph(calls, max_attempts=9), {"x": [1]}, map_over="x", workflow_id="policy-v2")
        assert policy_fork.status == RunStatus.COMPLETED

    async def test_an_unpersisted_batch_never_consults_a_parent_row(self, family, make_sqlite):
        """Without a workflow_id there is no parent boundary to validate."""
        calls: list[int] = []
        runner = _make_runner(family, checkpointer=make_sqlite())

        await _map(runner, _scaling_graph(calls, 2), {"x": [1]}, map_over="x")
        calls.clear()
        result = await _map(runner, _scaling_graph(calls, 3), {"x": [1]}, map_over="x")
        assert [item["out"] for item in result.results] == [3]

    async def test_map_has_no_override_workflow_shortcut(self, family, make_sqlite):
        """map() rejects override_workflow= explicitly; a new id is the fork."""
        runner = _make_runner(family, checkpointer=make_sqlite())
        calls: list[int] = []

        with pytest.raises(ValueError, match="override_workflow"):
            await _map(
                runner,
                _scaling_graph(calls, 2),
                {"x": [1]},
                map_over="x",
                workflow_id="no-override",
                override_workflow=True,
            )


# === A legitimate crash resume is untouched ===


class TestLegitimateMapResume:
    async def test_identical_graph_and_policy_restores_completed_and_reruns_the_rest(self, family, make_sqlite):
        cp = make_sqlite()
        seen: list[int] = []
        already_failed: list[int] = []

        @node(output_name="out")
        def flaky(x: int) -> int:
            seen.append(x)
            if x == 20 and 20 not in already_failed:
                already_failed.append(20)
                raise ValueError("transient")
            return x * 2

        graph = Graph([flaky], name="flaky-batch")
        runner = _make_runner(family, checkpointer=cp)

        first = await _map(
            runner,
            graph,
            {"x": [10, 20, 30]},
            map_over="x",
            workflow_id="resume-batch",
            error_handling="continue",
        )
        assert [r.status for r in first.results] == [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.COMPLETED]

        seen.clear()
        second = await _map(
            runner,
            graph,
            {"x": [10, 20, 30]},
            map_over="x",
            workflow_id="resume-batch",
            error_handling="continue",
        )
        assert second.status == RunStatus.COMPLETED
        assert seen == [20], "only the unfinished item re-executes"
        assert [item.restored for item in second.results] == [True, False, True]
        assert [item["out"] for item in second.results] == [20, 40, 60]


# === Identity ONLY: the run path's other rejections stay out (#309 decision) ===


class TestMapKeepsNonIdentityAdmissions:
    async def test_a_completed_batch_is_still_re_admitted_for_a_top_up(self, family, make_sqlite):
        """map() must not import WorkflowAlreadyCompletedError from run()."""
        cp = make_sqlite()
        calls: list[int] = []
        runner = _make_runner(family, checkpointer=cp)
        graph = _scaling_graph(calls, 2)

        await _map(runner, graph, {"x": [1, 2]}, map_over="x", workflow_id="topup")
        assert sorted(calls) == [1, 2]

        calls.clear()
        topped_up = await _map(runner, graph, {"x": [1, 2, 3]}, map_over="x", workflow_id="topup")
        assert topped_up.status == RunStatus.COMPLETED
        assert calls == [3], "only the new item runs; the old two restore"
        assert [item["out"] for item in topped_up.results] == [2, 4, 6]

    async def test_new_runtime_values_do_not_require_a_fork(self, family, make_sqlite):
        """map() must not import InputOverrideRequiresForkError from run()."""
        cp = make_sqlite()
        calls: list[int] = []
        runner = _make_runner(family, checkpointer=cp)

        @node(output_name="out")
        def scale(x: int, factor: int = 2) -> int:
            calls.append(x)
            return x * factor

        graph = Graph([scale], name="override-batch")
        await _map(runner, graph, {"x": [1]}, map_over="x", workflow_id="override")

        calls.clear()
        again = await _map(runner, graph, {"x": [1], "factor": 5}, map_over="x", workflow_id="override")
        assert again.status == RunStatus.COMPLETED
        assert calls == [1], "the changed item signature re-executes rather than erroring"
        assert [item["out"] for item in again.results] == [5]


# === Nested map behaves like a flat map ===


class TestNestedMapParentIdentity:
    def _graphs(self, seen: list[int], already_failed: list[int]) -> tuple[Graph, Graph]:
        @node(output_name="out")
        def item(x: int) -> int:
            seen.append(x)
            if x == 2 and 2 not in already_failed:
                already_failed.append(2)
                raise ValueError("transient")
            return x * 10

        inner = Graph([item], name="inner")
        return inner, Graph([inner.as_node(name="embed").map_over("x")], name="outer")

    async def test_a_crashed_nested_batch_still_resumes_through_the_new_gate(self, family, make_sqlite):
        """The nested parent row carries the INNER graph's identity.

        This is the parity risk the new boundary introduces: if the nested map
        compared the wrong graph, every legitimate nested crash resume would
        start raising GraphChangedError.
        """
        cp = make_sqlite()
        seen: list[int] = []
        already_failed: list[int] = []
        runner = _make_runner(family, checkpointer=cp)

        inner, outer = self._graphs(seen, already_failed)
        first = await _run(runner, outer, {"x": [1, 2]}, workflow_id="nested", error_handling="continue")
        assert first.status == RunStatus.FAILED
        nested_parent = cp.get_run("nested/embed")
        assert nested_parent is not None
        assert nested_parent.config["graph_struct_hash"] == inner.structural_hash

        seen.clear()
        _, outer_again = self._graphs(seen, already_failed)
        resumed = await _run(runner, outer_again, workflow_id="nested")
        assert resumed.status == RunStatus.COMPLETED
        assert resumed["out"] == [10, 20]
        assert seen == [2], "item 1 restores; only the crashed item re-runs"

    async def test_the_nested_boundary_rejects_a_changed_inner_graph(self, family, make_sqlite):
        """Driven through the same private child call a GraphNode executor makes.

        A user cannot address ``parent/child`` directly (``validate_workflow_id``
        reserves ``/``), and an end-to-end changed nested graph is already
        rejected one level up by ``run()``. This exercises the nested map
        boundary itself.
        """
        cp = make_sqlite()
        calls: list[int] = []
        runner = _make_runner(family, checkpointer=cp)

        inner = _scaling_graph(calls, 2)
        outer = Graph([inner.as_node(name="embed").map_over("x")], name="outer")
        await _run(runner, outer, {"x": [1, 2]}, workflow_id="nested-change")
        assert sorted(calls) == [1, 2]

        calls.clear()
        with pytest.raises(GraphChangedError) as exc_info:
            await _map(
                runner,
                _scaling_graph(calls, 3),
                {"x": [1, 2]},
                map_over="x",
                workflow_id="nested-change/embed",
                _parent_run_id="nested-change",
            )
        assert "nested-change/embed" in str(exc_info.value)
        assert calls == []
        assert cp.get_run("nested-change/embed").config["graph_struct_hash"] == inner.structural_hash

    async def test_a_changed_nested_graph_is_rejected_end_to_end(self, family, make_sqlite):
        """Outer identity covers the inner graph, so run() rejects first."""
        cp = make_sqlite()
        calls: list[int] = []
        runner = _make_runner(family, checkpointer=cp)

        inner = _scaling_graph(calls, 2)
        await _run(runner, Graph([inner.as_node(name="embed").map_over("x")], name="outer"), {"x": [1, 2]}, workflow_id="nested-e2e")

        calls.clear()
        changed = Graph([_scaling_graph(calls, 3).as_node(name="embed").map_over("x")], name="outer")
        with pytest.raises(GraphChangedError):
            await _run(runner, changed, {"x": [1, 2]}, workflow_id="nested-e2e")
        assert calls == []
        assert cp.get_run("nested-e2e/embed").config["graph_struct_hash"] == inner.structural_hash
