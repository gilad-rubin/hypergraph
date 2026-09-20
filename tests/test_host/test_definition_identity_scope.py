"""Issue #408, guard 1 — a graph modifier that changes what runs is identity.

`select()` and `with_entrypoint()` used to be invisible to the identity a
submission pins: `host.submit(full.select("cheap"))` resolved to the served
*unselected* Definition and the worker then executed the served object, so
the narrowing was discarded without a word.

What this file falsifies:

1. A selected graph submitted against its unselected served twin is refused,
   and the refusal names the selection and the fix.
2. `with_entrypoint` travels the same path and is refused the same way.
3. Serving the narrowed graph is the fix: it is its own Definition, it
   accepts, and its entrypoint really does skip the upstream node.
4. A host that serves ONE narrowing and is handed another is told what it
   actually serves — "drop the select()" would be wrong advice there.
5. `submit_batch` and `fork` share `_require_definition`, so one guard covers
   all three new-work/migration verbs.
6. An unmodified graph's Definition hash is still its `structural_hash` —
   every already-stored submission keeps resolving.
7. The documented upgrade path for a host that serves a narrowed graph:
   already-stored work parks as VERSION_INCOMPATIBLE and drains under the
   unnarrowed Definition, whose identity never moved.
8. (#452) The other end of that backlog: a stored submission the served
   Definition REFUSES to start is retired as a `start_refused` dead letter
   instead of sitting claimed forever with no runs row.
9. (#452) And the door that let it in: `submit()` now compares `values` to
   the served Definition's boundary inputs, the check `submit_batch()` has
   always applied per item.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hypergraph import (
    AsyncRunner,
    DefinitionId,
    Graph,
    RunHomeReadModel,
    RunQuery,
    RunRef,
    SyncRunner,
    UnservedGraphError,
    node,
    serve,
)
from hypergraph.checkpointers.types import WorkflowStatus
from hypergraph.exceptions import GraphChangedError
from hypergraph.host import WaitingCondition, definition_struct_hash
from hypergraph.host.views import DEAD_LETTER_START_REFUSED
from tests.test_host._batch_interrupt import until, worker

aiosqlite = pytest.importorskip("aiosqlite")


def pipeline(ledger: list[str], name: str = "pipeline", *, runner: Any = None) -> Graph:
    """`cheap` -> `costly`: the issue's two-node probe graph."""

    @node(output_name="cheap")
    def cheap(x: int) -> int:
        ledger.append("cheap")
        return x + 1

    @node(output_name="costly")
    def costly(cheap: int) -> int:
        ledger.append("costly")
        return cheap * 10

    return Graph([cheap, costly], name=name).with_runner(runner or AsyncRunner())


class TestSelectionIsDefinitionIdentity:
    async def test_a_selected_graph_is_refused_against_its_unselected_twin(self, home):
        """The issue's probe: served whole, submitted as `full.select(...)`."""
        ledger: list[str] = []
        full = pipeline(ledger)
        host = serve(full, home=home, deployment_version="v1")

        with pytest.raises(UnservedGraphError) as excinfo:
            await host.submit(full.select("cheap"), {"x": 1})

        # Refused at the call site: nothing was accepted, nothing ran.
        assert await host.client.list(RunQuery()) == []
        assert ledger == []
        message = str(excinfo.value)
        assert "select('cheap')" in message, message
        assert "How to fix:" in message

    async def test_the_selected_graphs_identity_differs_from_the_served_one(self, home):
        ledger: list[str] = []
        full = pipeline(ledger)
        selected = full.select("cheap")

        # The Graph's own structural hash is topology only — unchanged.
        assert selected.structural_hash == full.structural_hash
        # The Definition identity a submission pins is not.
        assert definition_struct_hash(selected) != definition_struct_hash(full)
        assert definition_struct_hash(full) == full.structural_hash

    async def test_serving_the_selected_graph_accepts_it(self, home):
        """The fix the message names: the narrowed graph is its own Definition."""
        ledger: list[str] = []
        selected = pipeline(ledger).select("cheap")
        host = serve(selected, home=home, deployment_version="v1")

        receipt = await host.submit(selected, {"x": 1}, workflow_id="sel-1")
        async with worker(host):
            view = await host.client.follow(receipt.run_ref, deadline=30)
        assert view.status is WorkflowStatus.COMPLETED

        # ...and the unselected twin is now the stranger.
        with pytest.raises(UnservedGraphError, match="structural_hash"):
            await host.submit(pipeline(ledger), {"x": 1})

    async def test_a_reordered_selection_is_the_same_definition(self, home):
        """Selection is a set of outputs, not a spelling: order is not identity."""
        ledger: list[str] = []
        full = pipeline(ledger)
        host = serve(full.select("cheap", "costly"), home=home, deployment_version="v1")

        receipt = await host.submit(full.select("costly", "cheap"), {"x": 1}, workflow_id="reordered")
        assert receipt.duplicate is False


class TestTheRefusalNamesBothNarrowings:
    async def test_a_differently_narrowed_graph_is_told_what_the_host_serves(self, home):
        """ "Drop the select()" would be wrong: the served one is narrowed too."""
        ledger: list[str] = []
        full = pipeline(ledger)
        host = serve(full.select("cheap"), home=home, deployment_version="v1")

        with pytest.raises(UnservedGraphError) as excinfo:
            await host.submit(full.select("costly"), {"x": 1})

        message = str(excinfo.value)
        assert "narrowed by select('costly')" in message, message
        assert "served one is narrowed by select('cheap')" in message, message
        assert "graph.select('cheap')" in message, "the fix names the narrowing this host serves"
        assert "Dropping the select('costly') will not match" in message

    async def test_an_unnarrowed_graph_against_a_narrowed_host_says_so(self, home):
        ledger: list[str] = []
        full = pipeline(ledger)
        host = serve(full.with_entrypoint("costly"), home=home, deployment_version="v1")

        with pytest.raises(UnservedGraphError) as excinfo:
            await host.submit(full, {"x": 1})

        message = str(excinfo.value)
        assert "this Graph is not narrowed and the served Definition is narrowed by with_entrypoint('costly')" in message, message
        assert "graph.with_entrypoint('costly')" in message

    async def test_a_drifted_topology_still_reads_as_drift_not_as_narrowing(self, home):
        """Narrowing is only named when the topology matches: no half-truths."""
        ledger: list[str] = []
        host = serve(pipeline(ledger), home=home, deployment_version="v1")

        @node(output_name="cheap")
        def other(x: int) -> int:
            return x

        drifted = Graph([other], name="pipeline").with_runner(AsyncRunner())
        with pytest.raises(UnservedGraphError) as excinfo:
            await host.submit(drifted.select("cheap"), {"x": 1})

        message = str(excinfo.value)
        assert "narrowed by" not in message, message
        assert "A changed topology is a new Definition" in message


class TestEntrypointIsDefinitionIdentity:
    async def test_an_entrypoint_graph_is_refused_against_its_unrestricted_twin(self, home):
        ledger: list[str] = []
        full = pipeline(ledger)
        host = serve(full, home=home, deployment_version="v1")

        with pytest.raises(UnservedGraphError) as excinfo:
            await host.submit(full.with_entrypoint("costly"), {"cheap": 2})

        assert ledger == []
        assert "with_entrypoint('costly')" in str(excinfo.value)

    async def test_serving_the_entrypoint_graph_really_skips_the_upstream_node(self, home):
        """What the silent path cost: the served narrowing actually narrows."""
        ledger: list[str] = []
        narrowed = pipeline(ledger).with_entrypoint("costly")
        host = serve(narrowed, home=home, deployment_version="v1")

        receipt = await host.submit(narrowed, {"cheap": 2}, workflow_id="entry-1")
        async with worker(host):
            view = await host.client.follow(receipt.run_ref, deadline=30)

        assert view.status is WorkflowStatus.COMPLETED
        assert ledger == ["costly"], "the entrypoint skipped `cheap`"


class TestOneGuardCoversEveryVerb:
    async def test_submit_batch_is_refused_too(self, home):
        ledger: list[str] = []
        full = pipeline(ledger)
        host = serve(full, home=home, deployment_version="v1")

        with pytest.raises(UnservedGraphError, match=r"select\('cheap'\)"):
            await host.submit_batch(full.select("cheap"), {"x": [1, 2]}, map_over=["x"], identity="x", workflow_id="b-sel")
        assert await host.client.list(RunQuery()) == []

    async def test_fork_into_a_selected_graph_is_refused_too(self, home):
        ledger: list[str] = []
        full = pipeline(ledger)
        host = serve(full, home=home, deployment_version="v1")
        receipt = await host.submit(full, {"x": 1}, workflow_id="fork-source")

        with pytest.raises(UnservedGraphError, match=r"select\('cheap'\)"):
            await host.fork(receipt.run_ref, into=full.select("cheap"), reason="narrow it")

    async def test_the_sync_mirrors_refuse_identically(self, home):
        ledger: list[str] = []
        full = pipeline(ledger)
        host = serve(full, home=home, deployment_version="v1")
        receipt = host.submit_sync(full, {"x": 1}, workflow_id="sync-source")

        with pytest.raises(UnservedGraphError, match=r"select\('cheap'\)"):
            host.submit_sync(full.select("cheap"), {"x": 1})
        with pytest.raises(UnservedGraphError, match=r"select\('cheap'\)"):
            host.submit_batch_sync(full.select("cheap"), {"x": [1]}, map_over=["x"], identity="x", workflow_id="b-sync")
        with pytest.raises(UnservedGraphError, match=r"select\('cheap'\)"):
            host.fork_sync(receipt.run_ref, into=full.select("cheap"), reason="narrow it")


class TestUnmodifiedGraphsKeepTheirIdentity:
    async def test_an_unmodified_graph_pins_its_structural_hash_verbatim(self, home):
        """Stored submissions were pinned on `structural_hash`: nothing moves."""
        ledger: list[str] = []
        full = pipeline(ledger)
        host = serve(full, home=home, deployment_version="v1")

        receipt = await host.submit(full, {"x": 1}, workflow_id="unmodified")
        view = await host.client.get(receipt.run_ref)
        assert view.definition_id.structural_hash == full.structural_hash


class TestComputingThePinnedHash:
    async def test_the_public_helper_is_what_a_submission_pins(self, home):
        ledger: list[str] = []
        narrowed = pipeline(ledger).with_entrypoint("costly")
        host = serve(narrowed, home=home, deployment_version="v1")

        receipt = await host.submit(narrowed, {"cheap": 2}, workflow_id="pinned")
        view = await host.client.get(receipt.run_ref)
        assert view.definition_id.structural_hash == definition_struct_hash(narrowed)
        assert view.definition_id.structural_hash != narrowed.structural_hash

    async def test_accepts_takes_the_definition_hash_and_says_so_when_it_does_not(self, home):
        ledger: list[str] = []
        narrowed = pipeline(ledger).with_entrypoint("costly")

        # The documented way to build a prior identity.
        prior = DefinitionId("pipeline", "v0", definition_struct_hash(narrowed))
        serve(narrowed, home=home, deployment_version="v1", accepts=(prior,))

        # The unnarrowed hash is the mistake this now names.
        with pytest.raises(ValueError, match=r"definition_struct_hash\(graph\)") as excinfo:
            serve(narrowed, home=home, deployment_version="v1", accepts=(DefinitionId("pipeline", "v0", narrowed.structural_hash),))
        assert "narrowing is part of Definition identity" in str(excinfo.value)


class TestUpgradingAHostThatServesANarrowedGraph:
    async def test_a_selected_hosts_stored_work_parks_and_drains_unnarrowed(self, home):
        """The migration paragraph in docs/06-api-reference/host.md, run.

        The legacy row carries the inputs the SELECTED Definition takes — the
        same ones the whole graph takes, which is exactly why this narrowing
        has a drain and `with_entrypoint` does not (the test below).
        """
        ledger: list[str] = []
        legacy_inputs = {"x": 1}
        assert pipeline(ledger).select("cheap").inputs.required == ("x",), "the selected surface IS these inputs"

        legacy_host = serve(pipeline(ledger), home=home, deployment_version="v1")
        receipt = await legacy_host.submit(pipeline(ledger), legacy_inputs, workflow_id="legacy-1")
        assert (await legacy_host.client.get(receipt.run_ref)).definition_id.structural_hash == pipeline(ledger).structural_hash

        # Upgrade: the same deployment now serves the selected graph.
        upgraded = serve(pipeline(ledger).select("cheap"), home=home, deployment_version="v1")

        async def parked_view():
            view = await upgraded.client.get(receipt.run_ref)
            return view if view.waiting is WaitingCondition.VERSION_INCOMPATIBLE else None

        async with worker(upgraded, "w-upgraded"):
            parked = await until(parked_view)
        assert parked.waiting is WaitingCondition.VERSION_INCOMPATIBLE
        assert parked.status is None and ledger == [], "parked, never lost, never executed"

        # The documented drain: the unnarrowed Definition's identity never moved.
        drain_host = serve(pipeline(ledger), home=home, deployment_version="v1")
        async with worker(drain_host, "w-drain"):
            drained = await drain_host.client.follow(receipt.run_ref, deadline=30)
        assert drained.status is WorkflowStatus.COMPLETED
        assert ledger == ["cheap", "costly"], "it runs unnarrowed — what the old identity could not say"

    async def test_an_entrypoint_hosts_backlog_has_no_unnarrowed_drain(self, home):
        """Why the drain bullet is scoped to `select()`.

        A `with_entrypoint`-narrowed host's submissions carry MID-GRAPH
        values. The unnarrowed Definition refuses those inputs outright, so
        serving it is not a migration path for that backlog — stop and
        resubmit is.
        """
        ledger: list[str] = []
        narrowed = pipeline(ledger).with_entrypoint("costly")
        assert narrowed.inputs.required == ("cheap",), "the entrypoint surface is a mid-graph value"

        host = serve(narrowed, home=home, deployment_version="v1")
        receipt = await host.submit(narrowed, {"cheap": 2}, workflow_id="entry-legacy")
        async with worker(host):
            assert (await host.client.follow(receipt.run_ref, deadline=30)).status is WorkflowStatus.COMPLETED
        assert ledger == ["costly"]

        # The same stored inputs, handed to the unnarrowed Definition:
        with pytest.raises(ValueError, match=r"internal parameters: \['cheap'\]"):
            await AsyncRunner().run(pipeline(ledger), {"cheap": 2})


# === 8. #452: a Definition that refuses to start retires its submission ===


async def _plant(home, graph: Graph, workflow_id: str, values: dict[str, Any], *, deployment_version: str = "v1") -> RunRef:
    """Accept a submission through the Home's own door, inputs unchecked.

    `submit()` refuses these inputs at the call site, so a row shaped like
    this is one the store ALREADY holds: accepted before that check
    existed, or pinned against a Definition a worker rebuilds from a
    builder address. Either way the worker has to settle it.
    """
    created, _row = await home._submit(
        workflow_id,
        graph.name,
        deployment_version,
        definition_struct_hash(graph),
        json.dumps(values),
        None,
        None,
        fingerprint=f"fp-{workflow_id}",
    )
    assert created is True, "the plant must be the row under test, not a dedup hit"
    return RunRef(home=home.uri, run_id=workflow_id)


def _updates(home, workflow_id: str) -> list[tuple[str, dict[str, Any]]]:
    """Every durable run update for one submission, in order."""
    rows = home._sync_db().execute("SELECT kind, payload FROM run_updates WHERE run_id = ? ORDER BY seq", (workflow_id,)).fetchall()
    return [(kind, json.loads(payload)) for kind, payload in rows]


async def _settled(home, workflow_id: str):
    """The submission once it reached `dead_letter`, else None."""
    submission = await home._get_submission(workflow_id)
    return submission if submission is not None and submission["state"] == "dead_letter" else None


class _RefusesOnRestore(AsyncRunner):
    """A served Definition whose restore rejects the run before it starts.

    Not every deterministic pre-start refusal is about input names: the
    restore guards (`GraphChangedError`, `CompactedRetentionError`,
    `CheckpointCoercionError`, `InputOverrideRequiresForkError`) refuse
    rather than replay ambiguous history, and refuse identically every
    time — so they land on the same reason.
    """

    async def run(self, graph, inputs=None, **kwargs):  # type: ignore[override]
        raise GraphChangedError(kwargs.get("workflow_id", "?"))


class _CrashesAfterTheRun(AsyncRunner):
    """A runner that commits real work and THEN dies: the recovery case."""

    async def run(self, graph, inputs=None, **kwargs):  # type: ignore[override]
        await super().run(graph, inputs, **kwargs)
        raise RuntimeError("the worker died after the run committed")


def _transient_runner(calls: list[str]) -> AsyncRunner:
    """A runner whose FIRST call dies the way a busy store does.

    Fails before any runs row, exactly like a refusal — and unlike a
    refusal, succeeds on the next attempt. `calls` is a closure, so it
    survives the `copy.copy` every `serve()` makes.
    """

    class _TransientlyUnavailable(AsyncRunner):
        async def run(self, graph, inputs=None, **kwargs):  # type: ignore[override]
            calls.append(kwargs.get("workflow_id", "?"))
            if len(calls) == 1:
                raise OSError("database is locked")
            return await super().run(graph, inputs, **kwargs)

    return _TransientlyUnavailable()


class TestADefinitionThatRefusesToStartIsADeadLetter:
    """#452: the executor is present, and it refuses THIS submission.

    Every other dead-letter reason says "nothing alive can run this". These
    rows were claimed by the Definition that owns them and never started —
    the stored inputs are not its boundary inputs, or a restore-time check
    rejected them. The stored inputs and the pinned identity are immutable,
    so a retry is provably identical: the worker retires the submission
    instead of renewing its lease over nothing.

    "Provably" is the whole rule. A pre-run failure that a retry might
    survive keeps the recovery path it has always had — the last test in
    this class is the one that holds the line.
    """

    @pytest.mark.parametrize("runner", [AsyncRunner, SyncRunner], ids=["async", "sync"])
    async def test_stored_inputs_the_definition_refuses_settle_as_start_refused(self, home, runner):
        """Probe A, both runner families: mid-graph values, no runs row."""
        ledger: list[str] = []
        graph = pipeline(ledger, runner=runner())
        host = serve(graph, home=home, deployment_version="v1")
        ref = await _plant(home, graph, "wf-bad", {"x": 1, "cheap": 99})

        async with worker(host, "w-drain"):
            submission = await until(lambda: _settled(home, "wf-bad"))

        assert submission["finished_at"] is not None, "settled, not merely flagged"
        assert await home.get_run_async("wf-bad") is None, "it never started: no runs row to recover from"
        assert ledger == [], "and no node ran"

        kinds = [kind for kind, _payload in _updates(home, "wf-bad")]
        assert kinds == ["submitted", "dead_lettered"]
        payload = _updates(home, "wf-bad")[-1][1]
        assert payload["reason"] == DEAD_LETTER_START_REFUSED
        assert payload["definition_id"] == DefinitionId("pipeline", "v1", definition_struct_hash(graph)).to_dict()
        assert payload["error"] == "ValueError", "the exception type is on the durable fact"

        view = await host.client.get(ref)
        assert (view.waiting, view.status) == (WaitingCondition.DEAD_LETTER, None)
        read = await RunHomeReadModel(host.client).get_run(ref)
        assert (read.status, read.condition, read.dead_letter_reason) == ("failed", "dead_letter", DEAD_LETTER_START_REFUSED)

        # Handled, not crashed: the worker reports no error of its own.
        assert host.worker_errors == []

        # Revivable exactly like a builder_failed dead letter.
        repeat = await host.client.rerun(ref)
        assert repeat.workflow_id == "wf-bad-retry-1"

    async def test_a_batch_child_refused_this_way_settles_its_parent(self, home):
        """Blast radius F: `claimed` is in no settled set, so a watch hung.

        `submit_batch` refuses bad item fields at its own door, so a Batch
        reaches a start refusal only through a cause that door cannot
        foresee — here a restore-time one.
        """
        ledger: list[str] = []
        graph = pipeline(ledger, "batched", runner=_RefusesOnRestore())
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await host.submit_batch(graph, {"x": [1, 2]}, map_over="x", identity="x", workflow_id="drop-refused")

        async def settled_batch():
            view = await host.client.get(receipt.batch_ref)
            return view if view.settled else None

        async with worker(host, "w-batch"):
            view = await until(settled_batch)

        assert view.counts["dead_letter"] == 2
        assert sum(view.counts.values()) == 2, "every manifest item accounted exactly once"
        assert view.outcomes == {"1": "dead_letter", "2": "dead_letter"}
        assert view.tolerance_tripped is False, "dead letters of every reason stay out of a trip, deliberately"
        assert host.worker_errors == []

    # The runner warns about the unknown key before it raises for the
    # missing one. Under CI's `-W error` that warning would BE the refusal,
    # and this test is about which reason the refusal settles on, not about
    # which of the two arrives first.
    @pytest.mark.filterwarnings("ignore::UserWarning")
    async def test_an_entrypoint_backlog_that_cannot_drain_is_retired_not_stalled(self, home):
        """Probe B: the narrowed host's own backlog, submitted pre-narrowing.

        `test_an_entrypoint_hosts_backlog_has_no_unnarrowed_drain` says stop
        and resubmit is the migration. This is what the rows do meanwhile.
        """
        ledger: list[str] = []
        narrowed = pipeline(ledger, "narrow").with_entrypoint("costly")
        host = serve(narrowed, home=home, deployment_version="v1")
        # `x` was the boundary input before the narrowing; now `cheap` is.
        await _plant(home, narrowed, "wf-entry", {"x": 1})

        async with worker(host, "w-narrow"):
            await until(lambda: _settled(home, "wf-entry"))

        payload = _updates(home, "wf-entry")[-1][1]
        assert (payload["reason"], payload["error"]) == (DEAD_LETTER_START_REFUSED, "MissingInputError")
        assert await home.get_run_async("wf-entry") is None
        assert host.worker_errors == []

    async def test_a_restore_time_refusal_lands_on_the_same_reason(self, home):
        """The reason is not an input-error reason: it is a repeatable one."""
        ledger: list[str] = []
        graph = pipeline(ledger, "restores", runner=_RefusesOnRestore())
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await host.submit(graph, {"x": 1}, workflow_id="wf-restore")

        async with worker(host, "w-restore"):
            await until(lambda: _settled(home, "wf-restore"))

        payload = _updates(home, "wf-restore")[-1][1]
        assert (payload["reason"], payload["error"]) == (DEAD_LETTER_START_REFUSED, "GraphChangedError")
        assert (await host.client.get(receipt.run_ref)).waiting is WaitingCondition.DEAD_LETTER
        assert host.worker_errors == []

    async def test_a_graph_its_bound_runner_cannot_execute_is_refused_the_same_way(self, home):
        """A pinned PAIRING can be the refusal, not only a pinned value.

        `serve()` accepts an async node under a `SyncRunner`; the check that
        refuses it lives in the runner, one call later and before any runs
        row. Graph and runner are both pinned by the Definition, so the
        refusal is as immutable as a bad input — and without this the
        submission is the original #452 zombie.
        """

        @node(output_name="out")
        async def only_async(x: int) -> int:
            return x + 1

        graph = Graph([only_async], name="mismatch").with_runner(SyncRunner())
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await host.submit(graph, {"x": 1}, workflow_id="wf-mismatch")

        async with worker(host, "w-mismatch"):
            await until(lambda: _settled(home, "wf-mismatch"))

        payload = _updates(home, "wf-mismatch")[-1][1]
        assert (payload["reason"], payload["error"]) == (DEAD_LETTER_START_REFUSED, "IncompatibleRunnerError")
        assert await home.get_run_async("wf-mismatch") is None
        assert (await host.client.get(receipt.run_ref)).waiting is WaitingCondition.DEAD_LETTER
        assert host.worker_errors == []

    async def test_the_same_submission_with_the_right_inputs_just_runs(self, home):
        """Falsifier (probe C): the handler retires refusals, not work."""
        ledger: list[str] = []
        graph = pipeline(ledger)
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await host.submit(graph, {"x": 1}, workflow_id="wf-good")

        async with worker(host, "w-good"):
            view = await host.client.follow(receipt.run_ref, deadline=30)

        assert view.status is WorkflowStatus.COMPLETED
        assert ledger == ["cheap", "costly"]
        assert (await home._get_submission("wf-good"))["state"] == "finished"
        assert [kind for kind, _payload in _updates(home, "wf-good")].count("dead_lettered") == 0
        assert host.worker_errors == []

    async def test_a_crash_after_the_run_committed_is_recovered_not_retired(self, home):
        """The at-least-once contract the new handler must not eat.

        A runs row means the attempt changed something, so the submission
        stays claimed for the reclaim scan — the lease, not a dead letter,
        is what settles it.
        """
        ledger: list[str] = []
        graph = pipeline(ledger, "crashes", runner=_CrashesAfterTheRun())
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await host.submit(graph, {"x": 1}, workflow_id="wf-crash")

        async def crashed():
            return host.worker_errors or None

        async with worker(host, "w-crash"):
            await until(crashed)
            row = await home._get_submission("wf-crash")
            assert row["state"] == "claimed", "left for the reclaim scan, exactly as before"
            assert await home.get_run_async("wf-crash") is not None
            assert [kind for kind, _payload in _updates(home, "wf-crash")].count("dead_lettered") == 0
            # The dead-letter door is fenced on the claim either way: a
            # stale claimant cannot retire the claim a newer one holds.
            assert await home._dead_letter("wf-crash", DEAD_LETTER_START_REFUSED, claim_seq=row["claim_seq"] - 1) is False
            assert (await home._get_submission("wf-crash"))["state"] == "claimed"

        # Re-adoption still settles it: the recovery path is untouched.
        healthy = serve(pipeline(ledger, "crashes"), home=home, deployment_version="v1")
        async with worker(healthy, "w-readopt"):
            view = await healthy.client.follow(receipt.run_ref, deadline=30)
        assert view.status is WorkflowStatus.COMPLETED

    async def test_a_transient_failure_before_the_first_run_row_is_retried(self, home):
        """ "No runs row" is not the whole test: the refusal must be PROVEN.

        A locked store fails in exactly the shape a refusal does — before
        anything is recorded — and says nothing about the stored inputs.
        Retiring it after one call would spend durable work on a blip and
        spend none of the recovery budget that exists for it. Only the
        deterministic class is retired; this keeps the at-least-once path.
        """
        ledger: list[str] = []
        calls: list[str] = []
        graph = pipeline(ledger, "flaky", runner=_transient_runner(calls))
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await host.submit(graph, {"x": 1}, workflow_id="wf-transient")

        async def raised():
            return host.worker_errors or None

        async with worker(host, "w-first"):
            await until(raised)
            assert (await home._get_submission("wf-transient"))["state"] == "claimed"

        assert isinstance(host.worker_errors[0], OSError), "recorded as a worker error, not swallowed"
        assert await home.get_run_async("wf-transient") is None, "it really did fail before the first runs row"
        assert [kind for kind, _payload in _updates(home, "wf-transient")] == ["submitted"], "nothing was retired"
        assert home._dead_letter_reasons_sync(["wf-transient"]) == {}

        # The lease was surrendered on shutdown, so the next worker re-adopts
        # it — and the second attempt works, which a dead letter would have
        # made unreachable without a human rerun.
        retry = serve(graph, home=home, deployment_version="v1")
        async with worker(retry, "w-second"):
            view = await retry.client.follow(receipt.run_ref, deadline=30)
        assert view.status is WorkflowStatus.COMPLETED
        assert calls == ["wf-transient", "wf-transient"], "one attempt lost, the submission kept"
        assert ledger == ["cheap", "costly"]


class TestSubmitChecksBoundaryInputsAtAcceptTime:
    """#452 part 1: the asymmetry that let the bad submission in.

    `submit_batch()` has always compared each item to `graph.inputs`;
    `submit()` compared nothing. Once accepted, stored values are immutable
    — so the same shape `submit_batch()` refuses at the call site became a
    `start_refused` dead letter with nobody left to correct it. Both doors
    now apply the one check.
    """

    async def test_a_mid_graph_value_is_refused_and_nothing_is_accepted(self, home):
        """Probe A, at the door it should never have passed."""
        ledger: list[str] = []
        graph = pipeline(ledger)
        host = serve(graph, home=home, deployment_version="v1")

        with pytest.raises(ValueError) as excinfo:
            await host.submit(graph, {"x": 1, "cheap": 99}, workflow_id="wf-bad")

        message = str(excinfo.value)
        assert "submit() has unknown graph input field(s): ['cheap']" in message, message
        assert "Expected fields: ['x']" in message, message
        assert "How to fix:" in message
        assert await home._get_submission("wf-bad") is None, "the refusal wrote nothing"
        assert await host.client.list(RunQuery()) == []

    def test_the_sync_mirror_refuses_the_same_values(self, home):
        ledger: list[str] = []
        graph = pipeline(ledger, runner=SyncRunner())
        host = serve(graph, home=home, deployment_version="v1")

        with pytest.raises(ValueError, match=r"unknown graph input field\(s\): \['cheap'\]"):
            host.submit_sync(graph, {"x": 1, "cheap": 99}, workflow_id="wf-bad")
        assert home._get_submission_sync("wf-bad") is None

    async def test_a_missing_required_input_is_refused_too(self, home):
        """Probe B's shape: the narrowed Definition's boundary moved."""
        ledger: list[str] = []
        narrowed = pipeline(ledger, "narrow").with_entrypoint("costly")
        host = serve(narrowed, home=home, deployment_version="v1")

        with pytest.raises(ValueError) as excinfo:
            await host.submit(narrowed, {}, workflow_id="wf-entry")

        assert "missing required graph input field(s): ['cheap']" in str(excinfo.value)
        assert await home._get_submission("wf-entry") is None

    async def test_the_right_boundary_inputs_are_still_accepted(self, home):
        """The falsifier: the check refuses shapes, not submissions."""
        ledger: list[str] = []
        graph = pipeline(ledger)
        host = serve(graph, home=home, deployment_version="v1")

        receipt = await host.submit(graph, {"x": 1}, workflow_id="wf-good")
        async with worker(host, "w-good"):
            view = await host.client.follow(receipt.run_ref, deadline=30)
        assert view.status is WorkflowStatus.COMPLETED

    async def test_submit_batch_still_names_the_offending_item(self, home):
        """The shared check keeps each caller's own subject in the message."""
        ledger: list[str] = []
        graph = pipeline(ledger)
        host = serve(graph, home=home, deployment_version="v1")

        with pytest.raises(ValueError) as excinfo:
            await host.submit_batch(
                graph,
                [{"x": 1}, {"x": 2, "cheap": 99}],
                identity="x",
                workflow_id="wf-batch",
            )
        assert "submit_batch() item 1 has unknown graph input field(s): ['cheap']" in str(excinfo.value)
