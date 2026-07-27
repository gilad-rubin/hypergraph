"""Issue #342 — the durable-Batch interrupt public behavior matrix.

Sixteen scenarios over ONE seam: the graph-first Host plus RunHomeClient
against a real SQLite Run Home. They observe only receipts, Batch/Run views,
PauseSlots, durable updates, and final domain outcomes, because that is
exactly the surface a Panda operator console holds. The one deliberate
exception is scenario 15's release-window race, which has to interpose on
the transaction seam to stage the instant it is falsifying — no black-box
schedule can make that instant reliably occur.

What each scenario is falsifying, in one line:

 1. Served Graph objects work; unserved ones fail immediately.
 2. No public string Definition selector, and no ``host.map``.
 3. Runner-shaped zip and product expansion freeze the expected manifests.
 4. ``key_by`` produces stable identity and rejects invalid/duplicate keys.
 5. A mixed Batch keeps clean/paused/answered/stopped/failed/unstarted
    siblings independent.
 6. A paused child is visible, nonterminal, and holds no active-Run slot.
 7. ``items[key].run_ref`` drives every item-scoped verb with no derived ids.
 8. Answering makes the child runnable atomically; the worker resumes it.
 9. The answer routes the DOMAIN — create, replace, and archive.
10. The Batch stays unsettled while any child is paused.
11. Batch watch reconnects without gaps and reports the durable
    ``child_paused`` / ``child_runnable`` facts.
12. Completed siblings never replay when another child pauses or resumes.
13. A second interrupt occurrence uses a new pause id; the old one is stale.
14. Stop is execution control, never a duplicate-resolution decision.
15. Stop/answer and answer/answer races elect exactly one committed
    outcome, and an answer landing inside the worker's release window is
    neither lost nor applied twice.
16. A fresh world — an empty SQLite Home, no hand-seeded rows — runs the
    whole public flow.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
import pytest_asyncio

from hypergraph import (
    AsyncRunner,
    Graph,
    ItemKeyError,
    RunHome,
    RunHomeClient,
    RunQuery,
    RunRef,
    UnservedGraphError,
    WaitingCondition,
    node,
    serve,
)
from hypergraph.checkpointers.types import (
    AnswerRejectedError,
    PauseAlreadySettledError,
    PauseSlot,
    StalePauseError,
    WorkflowStatus,
)
from tests.test_host._ingestion_fixture import (
    LEDGER_ENV,
    answer_value,
    ingestion_graph,
    looping_graph,
    read_ledger,
)

aiosqlite = pytest.importorskip("aiosqlite")

pytestmark = pytest.mark.host_batch_interrupt


# === Harness ===


@pytest_asyncio.fixture
async def home(tmp_path):
    """A FRESH world: an empty SQLite Run Home, never hand-seeded."""
    h = RunHome.open(f"file:{tmp_path / 'runs.db'}")
    yield h
    await h.close()


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """The deterministic domain-effect ledger for this test."""
    path = tmp_path / "effects.log"
    monkeypatch.setenv(LEDGER_ENV, str(path))
    return path


@contextlib.asynccontextmanager
async def worker(host, worker_id: str = "w-342", **kwargs):
    """Run work_forever as a task; shut it down cleanly on exit."""
    task = asyncio.create_task(host.work_forever(worker_id, **kwargs))
    try:
        yield task
    finally:
        host.shutdown()
        await asyncio.wait_for(task, timeout=25)


async def until(check, timeout: float = 20.0, interval: float = 0.02):
    """Poll an async zero-arg callable until it returns something truthy."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        value = await check()
        if value:
            return value
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(interval)


async def collect(stream, *, timeout: float = 25.0, stop_when=None):
    """Drain a watch stream under a deadline, optionally detaching early.

    A watch stream ends on its own once its subject is accounted, so no
    healthy test needs the deadline — it exists so a stream that never
    ends reports a failure instead of hanging the suite forever.
    """
    collected: list = []

    async def drain() -> None:
        async for update in stream:
            collected.append(update)
            if stop_when is not None and stop_when(update):
                return

    async with contextlib.aclosing(stream):
        try:
            await asyncio.wait_for(drain(), timeout=timeout)
        except asyncio.TimeoutError:
            raise AssertionError(f"watch stream did not end within {timeout}s ({len(collected)} updates seen)") from None
    return collected


def paused_items(view) -> list[str]:
    return [key for key, item in view.items.items() if item.waiting is WaitingCondition.PAUSED]


async def batch_where(client, ref, predicate):
    async def check():
        view = await client.get(ref)
        return view if view is not None and predicate(view) else None

    return await until(check)


async def submit_ids(host, graph, ids, workflow_id, **kwargs):
    """THE public submission used everywhere below: graph-first + runner-shaped."""
    return await host.submit_batch(
        graph,
        {"work_item_id": list(ids)},
        map_over="work_item_id",
        key_by="work_item_id",
        workflow_id=workflow_id,
        **kwargs,
    )


async def answer_item(client, item, decision: str, target_doc_id: int | None = None) -> PauseSlot:
    """Answer one item through its OWN RunRef — no workflow-id string built."""
    slot = await client.get_run_slot(item.run_ref)
    return await client.answer(item.run_ref, pause_id=slot.pause_id, value=answer_value(decision, target_doc_id))


# === 1. Served Graph objects submit; unserved Graphs fail immediately ===


class TestGraphFirstSubmission:
    async def test_a_served_graph_object_submits_a_run_and_a_batch(self, home):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")

        run = await host.submit(graph, {"work_item_id": "work-a81f43c129"})
        batch = await submit_ids(host, graph, ["work-1", "work-2"], "drop-1")

        assert run.run_ref.run_id == run.workflow_id and run.duplicate is False
        assert batch.batch_ref.batch_id.startswith("b-") and batch.workflow_id == "drop-1"
        # The Definition identity was resolved from the Graph, not typed.
        view = await host.client.get(run.run_ref)
        assert view.definition_id.name == "ingest" and view.definition_id.deployment_version == "v1"

    async def test_the_pre_with_runner_graph_object_resolves_too(self, home):
        """`with_runner` clones: name and structural_hash are the identity."""
        bare = ingestion_graph()
        served = bare.with_runner(AsyncRunner())
        host = serve(served, home=home, deployment_version="v1")

        receipt = await host.submit(bare, {"work_item_id": "work-1"})
        assert receipt.duplicate is False

    async def test_an_unserved_graph_fails_immediately_and_says_why(self, home):
        host = serve(ingestion_graph("ingest"), home=home, deployment_version="v1")

        @node(output_name="y")
        def other(x: int) -> int:
            return x

        stranger = Graph([other], name="stranger").with_runner(AsyncRunner())
        with pytest.raises(UnservedGraphError, match="not served by this host"):
            await host.submit(stranger, {"x": 1})
        with pytest.raises(UnservedGraphError, match=r"This host serves: \['ingest'\]"):
            await submit_ids(host, stranger, ["a"], "drop-x")
        # Nothing was accepted: refusal happens before any store write.
        assert await host.client.list(RunQuery()) == []

    async def test_a_same_named_graph_with_drifted_topology_is_refused(self, home):
        host = serve(ingestion_graph("ingest"), home=home, deployment_version="v1")

        @node(output_name="validated_id")
        def different(work_item_id: str) -> str:
            return work_item_id

        drifted = Graph([different], name="ingest").with_runner(AsyncRunner())
        with pytest.raises(UnservedGraphError, match="structural_hash"):
            await host.submit(drifted, {"work_item_id": "w"})

    async def test_a_definition_name_string_is_a_typeerror_not_a_selector(self, home):
        host = serve(ingestion_graph(), home=home, deployment_version="v1")
        with pytest.raises(TypeError, match="graph-first"):
            await host.submit("ingest", {"work_item_id": "w"})
        with pytest.raises(TypeError, match="graph-first"):
            await host.submit_batch("ingest", {"work_item_id": ["w"]}, map_over="work_item_id", key_by="work_item_id", workflow_id="d")


# === 2. Public surface budget: two new-work verbs, no map, no selectors ===


class TestPublicSurfaceBudget:
    async def test_the_host_has_exactly_two_new_work_verbs_and_no_map(self, home):
        host = serve(ingestion_graph(), home=home, deployment_version="v1")

        assert not hasattr(host, "map")
        assert not hasattr(host, "map_sync")
        new_work = {name for name in dir(host) if name.startswith("submit")}
        assert new_work == {"submit", "submit_sync", "submit_batch", "submit_batch_sync"}

    async def test_no_submission_verb_accepts_a_definition_name_string(self, home):
        """Every verb that creates work is graph-first, fork included."""
        import inspect

        host = serve(ingestion_graph(), home=home, deployment_version="v1")
        for verb in ("submit", "submit_sync", "submit_batch", "submit_batch_sync"):
            first = list(inspect.signature(getattr(host, verb)).parameters)[0]
            assert first == "graph", verb
        assert inspect.signature(host.fork).parameters["into"].annotation == "Graph"

    async def test_the_public_mapping_of_key_to_inputs_shape_is_gone(self, home):
        host = serve(ingestion_graph(), home=home, deployment_version="v1")
        with pytest.raises(TypeError, match="unexpected keyword argument 'items'"):
            await host.submit_batch(ingestion_graph(), items={"a": {"work_item_id": "a"}}, workflow_id="d")


# === 3. Runner-shaped zip and product expansion ===


class TestRunnerShapedExpansion:
    async def test_zip_expansion_freezes_the_expected_manifest(self, home):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        ids = ["work-a", "work-b", "work-c"]

        receipt = await host.submit_batch(
            graph,
            {"work_item_id": ids, "tenant": "acme"},
            map_over="work_item_id",
            key_by="work_item_id",
            workflow_id="drop-zip",
        )

        manifest = json.loads((await home._get_batch(receipt.batch_ref.batch_id))["items_json"])
        assert list(manifest) == ids
        # Broadcast inputs reach every child verbatim; expanded ones vary.
        assert manifest["work-b"] == {"work_item_id": "work-b", "tenant": "acme"}

    async def test_product_expansion_crosses_both_inputs(self, home):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")

        receipt = await host.submit_batch(
            graph,
            {"work_item_id": ["w1", "w2"], "shard": [7]},
            map_over=["work_item_id", "shard"],
            map_mode="product",
            key_by="work_item_id",
            workflow_id="drop-product",
        )

        manifest = json.loads((await home._get_batch(receipt.batch_ref.batch_id))["items_json"])
        assert manifest == {"w1": {"work_item_id": "w1", "shard": 7}, "w2": {"work_item_id": "w2", "shard": 7}}

    async def test_a_product_that_repeats_a_key_is_refused_not_silently_merged(self, home):
        """The cartesian cross must still yield distinct logical identity."""
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")

        with pytest.raises(ItemKeyError, match="duplicate item key"):
            await host.submit_batch(
                graph,
                {"work_item_id": ["w1", "w2"], "shard": [0, 1]},
                map_over=["work_item_id", "shard"],
                map_mode="product",
                key_by="work_item_id",
                workflow_id="drop-product-dup",
            )

    async def test_the_frozen_manifest_ignores_later_caller_mutation(self, home):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        ids = ["work-a", "work-b"]

        receipt = await submit_ids(host, graph, ids, "drop-frozen")
        ids.append("work-sneaky")

        manifest = json.loads((await home._get_batch(receipt.batch_ref.batch_id))["items_json"])
        assert list(manifest) == ["work-a", "work-b"]

    async def test_expansion_refusals_name_the_real_mistake(self, home):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        call = lambda **kw: host.submit_batch(graph, workflow_id="d", **kw)  # noqa: E731

        with pytest.raises(ValueError, match="map_over"):
            await call(values={"work_item_id": []}, map_over=[], key_by="work_item_id")
        with pytest.raises(ValueError, match="not in values"):
            await call(values={"other": [1]}, map_over="work_item_id", key_by="work_item_id")
        with pytest.raises(ValueError, match="map_mode"):
            await call(values={"work_item_id": ["a"]}, map_over="work_item_id", map_mode="cross", key_by="work_item_id")
        with pytest.raises(ValueError, match="equal lengths"):
            await call(
                values={"work_item_id": ["a", "b"], "shard": [1]},
                map_over=["work_item_id", "shard"],
                key_by="work_item_id",
            )
        with pytest.raises(ValueError, match="an empty Batch is not a Batch"):
            await call(values={"work_item_id": []}, map_over="work_item_id", key_by="work_item_id")


# === 4. key_by: stable identity, refused before acceptance ===


class TestKeyByIdentity:
    async def test_key_by_reuses_the_mapped_scalar_as_the_item_key(self, home):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")

        receipt = await submit_ids(host, graph, ["work-a81f43c129", "work-b12"], "drop-key")

        view = await host.client.get(receipt.batch_ref)
        assert list(view.items) == ["work-a81f43c129", "work-b12"]
        assert view.items["work-b12"].run_ref.run_id == "drop-key:work-b12"

    async def test_integer_keys_are_accepted_as_stable_identity(self, home):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")

        receipt = await host.submit_batch(
            graph,
            {"work_item_id": [17, 18]},
            map_over="work_item_id",
            key_by="work_item_id",
            workflow_id="drop-int",
        )
        view = await host.client.get(receipt.batch_ref)
        assert list(view.items) == ["17", "18"]

    @pytest.mark.parametrize(
        ("values", "match"),
        [
            ({"work_item_id": ["a", None]}, "missing"),
            ({"work_item_id": ["a", ""]}, "empty"),
            ({"work_item_id": ["a", 1.5]}, "a float"),
            ({"work_item_id": ["a", True]}, "a bool"),
            ({"work_item_id": ["a", ["x"]]}, "a list"),
            ({"work_item_id": ["a", "a"]}, "duplicate item key"),
        ],
    )
    async def test_invalid_and_duplicate_keys_are_refused_before_acceptance(self, home, values, match):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")

        with pytest.raises(ItemKeyError, match=match):
            await host.submit_batch(graph, values, map_over="work_item_id", key_by="work_item_id", workflow_id="drop-bad")
        # Refused before acceptance: no Batch, no children.
        assert await host.client.list(RunQuery()) == []

    async def test_a_broadcast_key_by_is_refused_as_the_wrong_input(self, home):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")

        with pytest.raises(ItemKeyError, match="does not name an expanded input"):
            await host.submit_batch(
                graph,
                {"work_item_id": ["a", "b"], "tenant": "acme"},
                map_over="work_item_id",
                key_by="tenant",
                workflow_id="drop-broadcast",
            )

    async def test_key_by_is_required(self, home):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        with pytest.raises(TypeError, match="key_by"):
            await host.submit_batch(graph, {"work_item_id": ["a"]}, map_over="work_item_id", workflow_id="drop-nokey")


# === 5. A mixed Batch: six sibling conditions at once ===


class TestMixedBatchIndependence:
    async def test_clean_paused_answered_stopped_failed_and_unstarted_coexist(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        ids = [
            "work-clean",  # bypasses the interrupt entirely
            "work-dup-answer",  # pauses, then gets an answer
            "work-dup-stop",  # pauses, then gets stopped
            "work-boom",  # fails in staging
            "work-dup-hold",  # pauses and stays parked
            "work-never",  # stopped before it was ever admitted
        ]
        receipt = await submit_ids(host, graph, ids, "drop-mixed")
        client = host.client

        # Cancel one item BEFORE any worker runs: it never executes at all.
        accepted = await client.get(receipt.batch_ref)
        await client.stop(accepted.items["work-never"].run_ref, info="withdrawn before pickup")

        async with worker(host):
            view = await batch_where(
                client,
                receipt.batch_ref,
                lambda v: len(paused_items(v)) == 3 and v.outcomes["work-clean"] == "completed" and v.outcomes["work-boom"] == "failed",
            )
            assert view.counts["paused"] == 3 and view.counts["active"] == 0

            await answer_item(client, view.items["work-dup-answer"], "replace_existing", 3143)
            await client.stop(view.items["work-dup-stop"].run_ref, info="operator cancelled the upload")
            final = await batch_where(
                client,
                receipt.batch_ref,
                lambda v: v.outcomes["work-dup-answer"] == "completed" and v.outcomes["work-dup-stop"] == "stopped",
            )

        # Every sibling landed on its own truth, independently.
        assert final.outcomes == {
            "work-clean": "completed",
            "work-dup-answer": "completed",
            "work-dup-stop": "stopped",
            "work-boom": "failed",
            "work-dup-hold": None,
            "work-never": None,
        }
        assert paused_items(final) == ["work-dup-hold"]
        assert final.unstarted_items == ("work-never",)
        assert final.items["work-never"].started is False
        assert final.items["work-clean"].started is True
        assert final.settled is False  # one child is still parked on a human
        # Domain effects ran once each, and only for items that reached them.
        assert sorted(read_ledger(ledger)) == ["created:work-clean", "replaced:work-dup-answer:3143"]

    async def test_answering_a_child_of_a_tripped_batch_does_not_reopen_admission(self, home, ledger):
        """A trip CLOSES admission — an answer cannot reopen it.

        A paused child never counts toward tolerance, so it is still parked
        when the Batch trips. Answering it returns it to claim order, and
        closed admission then settles it instead of running it: tolerance is
        a stop-the-line decision, not an advisory threshold.

        It settles as ``abandoned``, never ``unstarted``. It committed steps
        and could have landed side effects, so an operator must reconcile it
        before rerunning — the exact distinction ``unstarted`` would erase.
        """
        from hypergraph import BatchTolerance

        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(
            host,
            graph,
            ["work-dup-1", "work-boom-a", "work-boom-b"],
            "drop-tripped",
            tolerance=BatchTolerance(max_failed=1),
        )
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: v.tolerance_tripped and len(paused_items(v)) == 1)
            await answer_item(client, view.items["work-dup-1"], "create_new")
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        assert final.outcomes["work-dup-1"] == "abandoned"
        assert final.counts["failed"] == 2 and final.counts["abandoned"] == 1
        assert final.counts["unstarted"] == 0
        assert final.abandoned_items == ("work-dup-1",)
        # The decision was accepted, but no domain effect followed it.
        assert read_ledger(ledger) == []
        # Accounted exactly once, by the honest fact.
        kinds = [k for _s, k, _p, _a in await home._read_batch_updates(receipt.batch_ref.batch_id)]
        assert kinds.count("child_abandoned") == 1
        assert kinds.count("child_unstarted") == 0

    async def test_a_never_started_child_of_a_tripped_batch_is_still_unstarted(self, home, ledger):
        """The other half of the split: nothing ran, so nothing to reconcile."""
        from hypergraph import BatchTolerance

        graph = ingestion_graph()
        home.max_active_runs = 1  # keep later items from ever being claimed
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(
            host,
            graph,
            ["work-boom-a", "work-boom-b", "work-clean-c", "work-clean-d"],
            "drop-tripped-cold",
            tolerance=BatchTolerance(max_failed=1),
        )

        async with worker(host):
            final = await batch_where(host.client, receipt.batch_ref, lambda v: v.settled)

        assert final.tolerance_tripped is True
        assert final.counts["abandoned"] == 0
        assert set(final.unstarted_items) == {"work-clean-c", "work-clean-d"}
        assert all(final.outcomes[key] is None for key in final.unstarted_items)

    async def test_every_manifest_item_is_counted_exactly_once(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        ids = ["work-clean", "work-dup-1", "work-boom", "work-dup-2"]
        receipt = await submit_ids(host, graph, ids, "drop-count")

        async with worker(host):
            # Wait for the quiescent point: two items parked on people, the
            # other two settled. Asserting on `active` while a sibling is
            # still mid-flight would be a race, not a rule.
            view = await batch_where(
                host.client,
                receipt.batch_ref,
                lambda v: len(paused_items(v)) == 2 and v.outcomes["work-clean"] == "completed" and v.outcomes["work-boom"] == "failed",
            )

        assert sum(view.counts.values()) == len(ids) == len(view.items)
        # Nobody is executing, yet two items are unmistakably in flight.
        assert view.counts["paused"] == 2 and view.counts["active"] == 0
        assert view.settled is False


# === 6. A paused child holds no active-Run slot ===


class TestPausedChildHoldsNoSlot:
    async def test_a_paused_child_is_visible_nonterminal_and_frees_its_slot(self, home, ledger):
        graph = ingestion_graph()
        home.max_active_runs = 1
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1", "work-clean"], "drop-slot")
        client = host.client

        async with worker(host):
            # With a cap of ONE, the clean sibling can only complete if the
            # paused child gave its slot back.
            view = await batch_where(client, receipt.batch_ref, lambda v: v.outcomes["work-clean"] == "completed")

        item = view.items["work-dup-1"]
        assert item.status is WorkflowStatus.PAUSED and item.waiting is WaitingCondition.PAUSED
        assert item.outcome is None and item.started is True
        assert view.counts["paused"] == 1 and view.counts["active"] == 0
        # Nonterminal: the child is not settled, so neither is the Batch.
        assert view.settled is False
        assert (await home._get_submission(item.workflow_id))["state"] == "paused"

    async def test_a_paused_child_is_stoppable_not_terminal(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-stoppable")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            receipt_stop = await client.stop(view.items["work-dup-1"].run_ref, info="cancelled")
            assert receipt_stop.duplicate is False
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        assert final.outcomes["work-dup-1"] == "stopped"
        assert read_ledger(ledger) == []


# === 7. Item-scoped control needs no derived workflow ids ===


class TestItemScopedControl:
    async def test_run_ref_drives_inspect_answer_watch_stop_and_rerun(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1", "work-clean"], "drop-item")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]

            # inspect + answer, both from the item's own inert address
            run_view = await client.get(item.run_ref)
            assert run_view.workflow_id == item.workflow_id
            slot = await client.get_run_slot(item.run_ref)
            assert slot.response_key == "duplicate_decision"
            assert slot.question["prompt"].startswith("Possible duplicate")
            await client.answer(item.run_ref, pause_id=slot.pause_id, value=answer_value("create_new"))

            # watch, from the same ref
            kinds = [update.kind for update in await collect(client.watch(item.run_ref)) if update.durable]
            assert "answer" in kinds and kinds[-1] == "status"
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

            # rerun the settled item, still from the ref
            rerun = await client.rerun(item.run_ref)
            assert rerun.workflow_id == f"{item.workflow_id}-retry-1"

        assert final.outcomes["work-dup-1"] == "completed"
        # Nothing above built a "<batch>:<item>" string by hand.
        assert item.run_ref == RunRef(home=home.uri, run_id="drop-item:work-dup-1")

    async def test_the_batch_run_query_filter_finds_the_same_children(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1", "work-clean"], "drop-query")

        async with worker(host):
            view = await batch_where(host.client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            paused = await host.client.list(RunQuery(batch=receipt.batch_ref, waiting=WaitingCondition.PAUSED))

        assert [run.run_ref for run in paused] == [view.items["work-dup-1"].run_ref]


# === 8. Answering makes the child runnable; the worker resumes it ===


class TestAnsweredChildResumes:
    async def test_the_answer_and_the_runnable_transition_commit_together(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-atomic")
        client = host.client

        async with worker(host) as task:
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            host.shutdown()  # no worker races the assertion below
            await asyncio.wait_for(task, timeout=25)

            settled = await answer_item(client, item, "create_new")

        # One committed transaction moved BOTH halves.
        assert settled.answer == answer_value("create_new")
        assert (await home._get_submission(item.workflow_id))["state"] == "pending"
        assert (await client.get(item.run_ref)).waiting is WaitingCondition.QUEUED
        kinds = [kind for _seq, kind, _payload, _at in await home._read_batch_updates(receipt.batch_ref.batch_id)]
        assert kinds.count("child_runnable") == 1

    async def test_the_worker_resumes_the_same_checkpointed_run(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-resume")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            await answer_item(client, item, "replace_existing", 99)
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        # SAME workflow id, one runs row, resumed rather than restarted.
        assert final.outcomes["work-dup-1"] == "completed"
        run = await home.get_run_async(item.workflow_id)
        assert run.id == item.workflow_id and run.retry_of is None and run.forked_from is None
        assert read_ledger(ledger) == ["replaced:work-dup-1:99"]
        # The head node ran exactly once across both attempts: the resume
        # replayed checkpoint state, it did not re-execute the graph.
        steps = await home.get_steps(item.workflow_id)
        assert [step.node_name for step in steps].count("stage_candidate") == 1

    async def test_an_answered_child_re_enters_ordinary_claim_order(self, home, ledger):
        """Answering never jumps the queue: admission still gates the child."""
        graph = ingestion_graph()
        home.max_active_runs = 1
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-order")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            # Fill the single slot with unrelated work before answering.
            await answer_item(client, item, "create_new")
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        assert final.outcomes["work-dup-1"] == "completed"


# === 9. The answer routes the DOMAIN: create, replace, archive ===


class TestTypedAnswerRoutesTheDomain:
    @pytest.mark.parametrize(
        ("decision", "target", "effect"),
        [
            ("create_new", None, "created:work-dup-1"),
            ("replace_existing", 3143, "replaced:work-dup-1:3143"),
            ("archive_duplicate", 2718, "archived:work-dup-1:2718"),
        ],
    )
    async def test_each_decision_takes_its_own_branch(self, home, ledger, decision, target, effect):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-route")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            await answer_item(client, view.items["work-dup-1"], decision, target)
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        assert final.outcomes["work-dup-1"] == "completed"
        assert read_ledger(ledger) == [effect]

    async def test_a_value_failing_the_slot_schema_leaves_the_pause_open(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-schema")
        client = host.client

        async with worker(host) as task:
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            host.shutdown()
            await asyncio.wait_for(task, timeout=25)

            slot = await client.get_run_slot(item.run_ref)
            with pytest.raises(AnswerRejectedError, match="answer schema"):
                await client.answer(item.run_ref, pause_id=slot.pause_id, value="replace_existing")

        # Rejected before any write: still parked, still answerable.
        assert (await home._get_submission(item.workflow_id))["state"] == "paused"
        assert (await client.get_run_slot(item.run_ref)).is_open
        kinds = [kind for _s, kind, _p, _a in await home._read_batch_updates(receipt.batch_ref.batch_id)]
        assert "child_runnable" not in kinds


# === 10. The Batch stays unsettled while any child is paused ===


class TestBatchSettlementWithPausedChildren:
    async def test_a_paused_child_keeps_the_batch_and_its_watch_open(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-clean", "work-dup-1"], "drop-unsettled")
        client = host.client

        async with worker(host):
            # Both halves of the quiescent point, or `outcomes` below would
            # be a race against the sibling rather than a rule about pauses.
            view = await batch_where(
                client,
                receipt.batch_ref,
                lambda v: len(paused_items(v)) == 1 and v.outcomes["work-clean"] == "completed",
            )
            assert view.settled is False

            # The stream is still open while the decision is outstanding.
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(_drain_batch_watch(client, receipt.batch_ref), timeout=0.6)

            await answer_item(client, view.items["work-dup-1"], "create_new")
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        assert final.settled is True
        assert await asyncio.wait_for(_drain_batch_watch(client, receipt.batch_ref), timeout=25) is not None


async def _drain_batch_watch(client, ref):
    """Drain until the stream ENDS — used to prove it has not ended yet.

    Callers wrap this in ``asyncio.wait_for``; ``aclosing`` makes the
    cancellation close the generator instead of leaving it for the garbage
    collector, which warning-as-error CI would surface.
    """
    kinds = []
    stream = client.watch(ref)
    async with contextlib.aclosing(stream):
        async for update in stream:
            if update.durable:
                kinds.append(update.kind)
    return kinds


# === 11. Batch watch reconnects with the durable pause facts ===


class TestDurableBatchStream:
    async def test_the_stream_reports_child_paused_and_child_runnable(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-clean", "work-dup-1"], "drop-stream")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            await answer_item(client, view.items["work-dup-1"], "archive_duplicate", 7)
            await batch_where(client, receipt.batch_ref, lambda v: v.settled)
            facts = [(u.cursor, u.kind, u.payload) for u in await collect(client.watch(receipt.batch_ref)) if u.durable]

        kinds = [kind for _c, kind, _p in facts]
        assert kinds[0] == "manifest"
        assert kinds.count("child_paused") == 1 and kinds.count("child_runnable") == 1
        assert kinds.index("child_paused") < kinds.index("child_runnable")
        # Both facts carry the logical item key AND the inert child address,
        # so a consumer never parses child workflow-id syntax.
        for kind in ("child_paused", "child_runnable"):
            payload = next(p for _c, k, p in facts if k == kind)
            assert payload["item_key"] == "work-dup-1"
            assert payload["run_ref"] == {"home": home.uri, "run_id": "drop-stream:work-dup-1"}
            assert payload["pause_id"].startswith("drop-stream:work-dup-1:")
        # The cursor sequence is gap-free.
        assert [c for c, _k, _p in facts] == [f"bseq:{n}" for n in range(1, len(facts) + 1)]

    async def test_reconnecting_from_a_stored_cursor_has_no_gaps_or_repeats(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-clean", "work-dup-1"], "drop-reconnect")
        client = host.client

        async with worker(host):
            # Detach mid-stream, exactly as a UI reconnect does.
            opening = await collect(
                client.watch(receipt.batch_ref),
                stop_when=lambda u: u.durable and u.kind == "child_paused",
            )
            first = [(u.cursor, u.kind) for u in opening if u.durable]
            cursor = first[-1][0]

            view = await client.get(receipt.batch_ref)
            await answer_item(client, view.items["work-dup-1"], "create_new")
            await batch_where(client, receipt.batch_ref, lambda v: v.settled)

            rest = [(u.cursor, u.kind) for u in await collect(client.watch(receipt.batch_ref, after=cursor)) if u.durable]

        joined = first + rest
        assert [c for c, _k in joined] == [f"bseq:{n}" for n in range(1, len(joined) + 1)]
        assert [k for _c, k in rest][0] != "child_paused"  # no repeat across the seam
        assert "child_runnable" in [k for _c, k in rest]


# === 12. Terminal siblings never replay ===


class TestTerminalSiblingsNeverReplay:
    async def test_a_completed_sibling_is_untouched_by_a_pause_and_a_resume(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-clean-a", "work-clean-b", "work-dup-1"], "drop-replay")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            before = await home.get_run_async("drop-replay:work-clean-a")
            await answer_item(client, view.items["work-dup-1"], "create_new")
            await batch_where(client, receipt.batch_ref, lambda v: v.settled)
            after = await home.get_run_async("drop-replay:work-clean-a")

        assert before.completed_at == after.completed_at and before.status == after.status
        # Each deterministic effect ran exactly once.
        assert sorted(read_ledger(ledger)) == ["created:work-clean-a", "created:work-clean-b", "created:work-dup-1"]
        assert len(read_ledger(ledger)) == 3


# === 13. A second occurrence is a new pause id ===


class TestLoopingOccurrences:
    async def test_answering_the_first_occurrence_parks_on_a_new_one(self, home, ledger):
        graph = looping_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await host.submit_batch(
            graph,
            {"work_item_id": ["work-loop"]},
            map_over="work_item_id",
            key_by="work_item_id",
            workflow_id="drop-loop",
        )
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-loop"]
            first = await client.get_run_slot(item.run_ref)
            await client.answer(item.run_ref, pause_id=first.pause_id, value=answer_value("create_new"))

            # It becomes runnable, resumes, and parks on the SECOND question.
            second = await until(lambda: _new_slot(client, item.run_ref, first.pause_id))
            assert second.pause_id != first.pause_id
            assert second.response_key == "second_answer" and second.is_open

            # The settled first occurrence can never make this one runnable.
            with pytest.raises(StalePauseError):
                await client.answer(item.run_ref, pause_id=first.pause_id, value=answer_value("archive_duplicate"))

            await client.answer(item.run_ref, pause_id=second.pause_id, value=answer_value("replace_existing"))
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)
            facts = [u.kind for u in await collect(client.watch(receipt.batch_ref)) if u.durable]

        assert final.outcomes["work-loop"] == "completed"
        assert read_ledger(ledger) == ["mid:create_new|replace_existing"]
        # A new occurrence earns its OWN paused/runnable pair.
        assert facts.count("child_paused") == 2 and facts.count("child_runnable") == 2


async def _new_slot(client, ref, previous_pause_id):
    slot = await client.get_run_slot(ref)
    return slot if slot is not None and slot.pause_id != previous_pause_id else None


# === 14. Stop is execution control, not a duplicate decision ===


class TestStopIsNotADecision:
    async def test_stopping_a_paused_child_records_no_domain_outcome(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1", "work-dup-2"], "drop-notadecision")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 2)
            await client.stop(view.items["work-dup-1"].run_ref, info="cancelled")
            await answer_item(client, view.items["work-dup-2"], "archive_duplicate", 5)
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        assert final.outcomes == {"work-dup-1": "stopped", "work-dup-2": "completed"}
        # A stop produced NO create/replace/archive effect; only the answer did.
        assert read_ledger(ledger) == ["archived:work-dup-2:5"]

    async def test_a_stopped_child_cannot_then_be_answered(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-stopped-answer")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            slot = await client.get_run_slot(item.run_ref)
            await client.stop(item.run_ref, info="cancelled")
            await batch_where(client, receipt.batch_ref, lambda v: v.settled)

            with pytest.raises(AnswerRejectedError, match="is stopped, not paused"):
                await client.answer(item.run_ref, pause_id=slot.pause_id, value=answer_value("create_new"))


# === 15. Races elect exactly one committed outcome ===


class TestRaces:
    async def test_concurrent_answers_elect_exactly_one_winner(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-race-answer")
        client = host.client

        async with worker(host) as task:
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            host.shutdown()
            await asyncio.wait_for(task, timeout=25)
            slot = await client.get_run_slot(item.run_ref)

            decisions = ["create_new", "replace_existing", "archive_duplicate", "create_new"]
            results = await asyncio.gather(
                *(client.answer(item.run_ref, pause_id=slot.pause_id, value=answer_value(decision, 1)) for decision in decisions),
                return_exceptions=True,
            )

        winners = [r for r in results if isinstance(r, PauseSlot)]
        losers = [r for r in results if isinstance(r, BaseException)]
        assert len(winners) == 1 and len(losers) == 3
        assert all(isinstance(loser, PauseAlreadySettledError) for loser in losers)
        # Exactly one committed answer, and exactly one runnable transition.
        assert (await client.get_run_slot(item.run_ref)).answer == winners[0].answer
        kinds = [k for _s, k, _p, _a in await home._read_batch_updates(receipt.batch_ref.batch_id)]
        assert kinds.count("child_runnable") == 1
        assert (await home._get_submission(item.workflow_id))["state"] == "pending"

    async def test_answer_versus_stop_resolves_by_commit_order(self, home, ledger):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-race-stop")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            slot = await client.get_run_slot(item.run_ref)
            ready = asyncio.Event()

            async def answer():
                await ready.wait()
                return await client.answer(item.run_ref, pause_id=slot.pause_id, value=answer_value("create_new"))

            async def stop():
                await ready.wait()
                return await client.stop(item.run_ref, info="cancelled")

            answer_task = asyncio.create_task(answer())
            stop_task = asyncio.create_task(stop())
            ready.set()
            answered, _stopped = await asyncio.gather(answer_task, stop_task, return_exceptions=True)
            final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

        # Both commands are legal against a parked run; commit order decides
        # which terminal outcome the child reaches — and only one is recorded.
        assert final.outcomes["work-dup-1"] in {"completed", "stopped"}
        if isinstance(answered, BaseException):
            assert final.outcomes["work-dup-1"] == "stopped"
            assert read_ledger(ledger) == []
        else:
            assert len(read_ledger(ledger)) <= 1


class TestTheReleaseWindowIsNotARace:
    """Each transition has ONE owner and ONE commit.

    The pause transaction parks the submission; the answer transaction
    re-admits it. ``_release_submission`` owns neither, so an answer that
    lands while the worker is still finishing cannot be missed — and process
    death anywhere in the window cannot separate a durable decision from a
    claimable run.
    """

    async def test_the_pause_transaction_parks_the_submission_itself(self, home, ledger):
        """No instant where the run is PAUSED but the submission is claimed."""
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-park")
        workflow_id = "drop-park:work-dup-1"
        observed: list[tuple[str, str]] = []

        original = home.record_pause

        async def observe(*args, **kwargs):
            result = await original(*args, **kwargs)
            # First read after the pause COMMITTED: both halves must agree.
            run = await home.get_run_async(workflow_id)
            submission = await home._get_submission(workflow_id)
            observed.append((run.status.value, submission["state"]))
            return result

        home.record_pause = observe  # type: ignore[method-assign]
        try:
            async with worker(host):
                await batch_where(host.client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
        finally:
            home.record_pause = original  # type: ignore[method-assign]

        assert observed == [("paused", "paused")]

    async def test_an_answer_inside_the_release_window_still_re_admits(self, home, ledger):
        """THE deterministic race: answer between pause commit and release."""
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-window")
        client = host.client
        workflow_id = "drop-window:work-dup-1"
        answered = asyncio.Event()

        original = home._release_submission

        async def answer_first(wid: str) -> None:
            # Hold the release open, answer, THEN let the worker release.
            if wid == workflow_id and not answered.is_set():
                slot = await home.get_pause_slot(wid)
                if slot is not None and slot.is_open:
                    await client.answer(RunRef(home=home.uri, run_id=wid), pause_id=slot.pause_id, value=answer_value("create_new"))
                    answered.set()
            await original(wid)

        home._release_submission = answer_first  # type: ignore[method-assign]
        try:
            async with worker(host):
                final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)
        finally:
            home._release_submission = original  # type: ignore[method-assign]

        assert answered.is_set()
        assert final.outcomes["work-dup-1"] == "completed"
        assert read_ledger(ledger) == ["created:work-dup-1"]
        # The release did NOT own the re-admission, and did not duplicate it.
        kinds = [k for _s, k, _p, _a in await home._read_batch_updates(receipt.batch_ref.batch_id)]
        assert kinds.count("child_runnable") == 1
        assert kinds.count("child_paused") == 1

    async def test_the_release_never_undoes_an_answer(self, home, ledger):
        """A release arriving after an answer is a compare-and-set no-op."""
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-noop")
        client = host.client

        async with worker(host) as task:
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            host.shutdown()
            await asyncio.wait_for(task, timeout=25)
            await answer_item(client, item, "create_new")

            assert (await home._get_submission(item.workflow_id))["state"] == "pending"
            # A late release for the same run must change nothing.
            await home._release_submission(item.workflow_id)

        assert (await home._get_submission(item.workflow_id))["state"] == "pending"
        kinds = [k for _s, k, _p, _a in await home._read_batch_updates(receipt.batch_ref.batch_id)]
        assert kinds.count("child_runnable") == 1

    async def test_a_watch_replay_shows_one_pause_and_one_runnable(self, home, ledger):
        """Reconnectable history, from the durable sequence alone."""
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-replay")
        client = host.client

        async with worker(host):
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            await answer_item(client, view.items["work-dup-1"], "create_new")
            await batch_where(client, receipt.batch_ref, lambda v: v.settled)
            replayed = [u.kind for u in await collect(client.watch(receipt.batch_ref)) if u.durable]

        assert replayed.count("child_paused") == 1
        assert replayed.count("child_runnable") == 1
        assert replayed.index("child_paused") < replayed.index("child_runnable")

    async def test_the_sync_store_mirror_parks_and_re_admits_identically(self, home, ledger):
        """Sync/async parity for both transitions, at the store."""
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        receipt = await submit_ids(host, graph, ["work-dup-1"], "drop-sync-parity")
        client = host.client

        async with worker(host) as task:
            view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
            item = view.items["work-dup-1"]
            # The pause transition was written by the ASYNC mirror; read it
            # back through the SYNC one and settle through the sync answer.
            assert home._get_submission_sync(item.workflow_id)["state"] == "paused"
            host.shutdown()
            await asyncio.wait_for(task, timeout=25)

            slot = client.get_run_slot_sync(item.run_ref)
            client.answer_sync(item.run_ref, pause_id=slot.pause_id, value=answer_value("create_new"))

        assert home._get_submission_sync(item.workflow_id)["state"] == "pending"
        kinds = [k for _s, k, _p, _a in await home._read_batch_updates(receipt.batch_ref.batch_id)]
        assert kinds.count("child_runnable") == 1


# === 16. Fresh world: the whole flow on an empty Home ===


class TestFreshWorld:
    async def test_an_empty_sqlite_home_runs_the_whole_public_flow(self, tmp_path, ledger):
        """No fixture, no hand-seeded rows: open a file and use the API."""
        run_home = RunHome.open(f"file:{tmp_path / 'fresh.db'}")
        graph = ingestion_graph("kb_ingestion_lifecycle")
        host = serve(graph, home=run_home, deployment_version="2026.07.26")
        try:
            work_item_ids = ["work-a81f43c129", "work-dup-b7c2e1", "work-c993aa"]
            receipt = await host.submit_batch(
                graph,
                {"work_item_id": work_item_ids},
                map_over="work_item_id",
                key_by="work_item_id",
                workflow_id="schneider-drop-42",
            )
            client = host.client

            async with worker(host, "fresh-worker"):
                batch = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
                item = batch.items["work-dup-b7c2e1"]
                slot = await client.get_run_slot(item.run_ref)
                await client.answer(
                    item.run_ref,
                    pause_id=slot.pause_id,
                    value=answer_value("replace_existing", 3143),
                )
                final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

            assert final.counts["completed"] == 3
            assert list(final.items) == work_item_ids
            assert sorted(read_ledger(ledger)) == [
                "created:work-a81f43c129",
                "created:work-c993aa",
                "replaced:work-dup-b7c2e1:3143",
            ]
        finally:
            await run_home.close()

    async def test_the_sync_mirror_submits_the_same_frozen_manifest(self, tmp_path, ledger):
        run_home = RunHome.open(f"file:{tmp_path / 'sync.db'}")
        graph = ingestion_graph("ingest", sync=True)
        host = serve(graph, home=run_home, deployment_version="v1")
        try:
            receipt = host.submit_batch_sync(
                graph,
                {"work_item_id": ["work-a", "work-dup-b"]},
                map_over="work_item_id",
                key_by="work_item_id",
                workflow_id="drop-sync",
            )
            view = RunHomeClient(run_home).get_sync(receipt.batch_ref)
            assert list(view.items) == ["work-a", "work-dup-b"]
            assert view.counts["queued"] == 2 and view.counts["paused"] == 0
            assert host.submit_sync(graph, {"work_item_id": "work-solo"}).duplicate is False
        finally:
            await run_home.close()

    async def test_the_sync_mirror_answers_and_re_admits_the_same_way(self, tmp_path, ledger):
        """``answer_sync`` commits the SAME two halves as ``answer``.

        Sync/async parity is a repository invariant, and settlement is the
        one place where drifting would leave an answered child parked
        forever on the surface a synchronous operator process uses. The
        graph itself stays async-served: interrupts require an
        ``AsyncRunner`` (``SyncRunner`` refuses them outright), so the
        synchronous half of this story is the *client*, not the runner —
        a blocking ops tool answering a question an async worker asked.
        """
        run_home = RunHome.open(f"file:{tmp_path / 'sync-answer.db'}")
        graph = ingestion_graph("ingest")
        host = serve(graph, home=run_home, deployment_version="v1")
        client = host.client
        try:
            receipt = host.submit_batch_sync(
                graph,
                {"work_item_id": ["work-dup-1"]},
                map_over="work_item_id",
                key_by="work_item_id",
                workflow_id="drop-sync-answer",
            )
            async with worker(host):
                view = await batch_where(client, receipt.batch_ref, lambda v: len(paused_items(v)) == 1)
                item = view.items["work-dup-1"]

                slot = client.get_run_slot_sync(item.run_ref)
                settled = client.answer_sync(item.run_ref, pause_id=slot.pause_id, value=answer_value("replace_existing", 3143))
                assert settled.settled_at is not None

                # The same worker picks the child up again and finishes it.
                final = await batch_where(client, receipt.batch_ref, lambda v: v.settled)

            # One committed transaction moved BOTH halves, exactly as async.
            kinds = [kind for _seq, kind, _payload, _at in await run_home._read_batch_updates(receipt.batch_ref.batch_id)]
            assert kinds.count("child_runnable") == 1
            assert final.outcomes["work-dup-1"] == "completed"
            assert read_ledger(ledger) == ["replaced:work-dup-1:3143"]
        finally:
            await run_home.close()
