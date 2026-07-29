"""Issue #342 — the durable Batch's public submission surface.

What this file falsifies:

1. A served Graph object submits; an unserved or drifted one is refused at
   the call site, before any store write.
2. The surface budget holds: two new-work verbs, no ``host.map``, and no
   Definition-name string anywhere.
3. Runner-shaped ``zip`` / ``product`` expansion freezes the expected
   manifest, and mutating the caller's collection afterwards cannot change
   it.
4. ``identity`` derives stable identity from an expanded input, and never
   falls back to a generated map index.
5. Every submission refusal names the input, the supplied value, and a
   literal fix — before anything is accepted.
6. An empty SQLite file, opened cold, runs the whole flow.
"""

from __future__ import annotations

import inspect
import json

import pytest

from hypergraph import (
    AsyncRunner,
    Graph,
    ItemKeyError,
    RunHome,
    RunQuery,
    UnservedGraphError,
    node,
    serve,
)
from tests.test_host._batch_interrupt import (
    batch_where,
    paused_items,
    submit_ids,
    worker,
)
from tests.test_host._ingestion_fixture import (
    answer_value,
    ingestion_graph,
    read_ledger,
)

aiosqlite = pytest.importorskip("aiosqlite")

pytestmark = pytest.mark.host_batch_interrupt


def expansion_graph() -> Graph:
    """A graph whose boundary declares every expansion-test input."""

    @node(output_name="expanded")
    def expand(work_item_id: str, tenant: str = "", shard: int = 0) -> str:
        return f"{tenant}:{work_item_id}:{shard}"

    return Graph([expand], name="expand").with_runner(AsyncRunner())


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
            await host.submit_batch("ingest", {"work_item_id": ["w"]}, map_over="work_item_id", identity="work_item_id", workflow_id="d")

    async def test_non_dict_values_say_what_a_values_dict_is(self, home):
        """`values` names graph inputs; a bare sequence cannot be matched to one."""
        host = serve(ingestion_graph(), home=home, deployment_version="v1")

        with pytest.raises(TypeError) as excinfo:
            await host.submit(ingestion_graph(), ["w"])  # type: ignore[arg-type]

        message = str(excinfo.value)
        assert "must be a dict" in message and "got list" in message
        assert "How to fix:" in message
        assert await host.client.list(RunQuery()) == []


# === 2. Public surface budget: two new-work verbs, no map, no selectors ===


class TestPublicSurfaceBudget:
    async def test_batch_signature_has_only_optional_admission_cost(self, home):
        host = serve(ingestion_graph(), home=home, deployment_version="v1")
        signature = inspect.signature(host.submit_batch)

        assert "admission_cost" in signature.parameters
        assert signature.parameters["admission_cost"].default is None
        assert "schema" not in signature.parameters
        assert "exclusive_by" not in signature.parameters
        with pytest.raises(TypeError, match="unexpected keyword argument 'schema'"):
            await host.submit_batch(
                ingestion_graph(),
                [{"work_item_id": "a"}],
                identity="work_item_id",
                workflow_id="schema-refused",
                schema=dict,
            )
        with pytest.raises(TypeError, match="unexpected keyword argument 'exclusive_by'"):
            host.submit_batch_sync(
                ingestion_graph(),
                [{"work_item_id": "a"}],
                identity="work_item_id",
                workflow_id="exclusive-refused",
                exclusive_by="work_item_id",
            )

    async def test_structural_item_mappings_are_validated_without_a_schema(self, home):
        graph = expansion_graph()
        host = serve(graph, home=home, deployment_version="v1")

        for workflow_id, items, match in (
            ("missing-input", [{"tenant": "acme"}], "missing required graph input"),
            ("unknown-input", [{"work_item_id": "a", "surprise": True}], "unknown graph input"),
            ("non-json", [{"work_item_id": "a", "tenant": object()}], "JSON-serializable"),
        ):
            with pytest.raises((TypeError, ValueError), match=match):
                await host.submit_batch(graph, items, identity="work_item_id", workflow_id=workflow_id)

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
        graph = expansion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        ids = ["work-a", "work-b", "work-c"]

        receipt = await host.submit_batch(
            graph,
            {"work_item_id": ids, "tenant": "acme"},
            map_over="work_item_id",
            identity="work_item_id",
            workflow_id="drop-zip",
        )

        manifest = json.loads((await home._get_batch(receipt.batch_ref.batch_id))["items_json"])
        assert list(manifest) == ids
        # Broadcast inputs reach every child verbatim; expanded ones vary.
        assert manifest["work-b"] == {"work_item_id": "work-b", "tenant": "acme"}

    async def test_product_expansion_crosses_both_inputs(self, home):
        graph = expansion_graph()
        host = serve(graph, home=home, deployment_version="v1")

        receipt = await host.submit_batch(
            graph,
            {"work_item_id": ["w1", "w2"], "shard": [7]},
            map_over=["work_item_id", "shard"],
            map_mode="product",
            identity="work_item_id",
            workflow_id="drop-product",
        )

        manifest = json.loads((await home._get_batch(receipt.batch_ref.batch_id))["items_json"])
        assert manifest == {"w1": {"work_item_id": "w1", "shard": 7}, "w2": {"work_item_id": "w2", "shard": 7}}

    async def test_a_product_that_repeats_a_key_is_refused_not_silently_merged(self, home):
        """The cartesian cross must still yield distinct logical identity."""
        graph = expansion_graph()
        host = serve(graph, home=home, deployment_version="v1")

        with pytest.raises(ItemKeyError, match="duplicate item key"):
            await host.submit_batch(
                graph,
                {"work_item_id": ["w1", "w2"], "shard": [0, 1]},
                map_over=["work_item_id", "shard"],
                map_mode="product",
                identity="work_item_id",
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
            await call(values={"work_item_id": []}, map_over=[], identity="work_item_id")
        with pytest.raises(ValueError, match="not in values"):
            await call(values={"other": [1]}, map_over="work_item_id", identity="work_item_id")
        with pytest.raises(ValueError, match="map_mode"):
            await call(values={"work_item_id": ["a"]}, map_over="work_item_id", map_mode="cross", identity="work_item_id")
        with pytest.raises(ValueError, match="equal lengths"):
            await call(
                values={"work_item_id": ["a", "b"], "shard": [1]},
                map_over=["work_item_id", "shard"],
                identity="work_item_id",
            )
        with pytest.raises(ValueError, match="an empty Batch is not a Batch"):
            await call(values={"work_item_id": []}, map_over="work_item_id", identity="work_item_id")


# === 4. identity: stable item keys, refused before acceptance ===


class TestIdentity:
    async def test_identity_reuses_the_mapped_scalar_as_the_item_key(self, home):
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
            identity="work_item_id",
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
            await host.submit_batch(graph, values, map_over="work_item_id", identity="work_item_id", workflow_id="drop-bad")
        # Refused before acceptance: no Batch, no children.
        assert await host.client.list(RunQuery()) == []

    async def test_a_broadcast_identity_is_refused_as_the_wrong_input(self, home):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")

        with pytest.raises(ItemKeyError, match="does not name an expanded input"):
            await host.submit_batch(
                graph,
                {"work_item_id": ["a", "b"], "tenant": "acme"},
                map_over="work_item_id",
                identity="tenant",
                workflow_id="drop-broadcast",
            )

    async def test_identity_is_required(self, home):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")
        with pytest.raises(TypeError, match="identity"):
            await host.submit_batch(graph, {"work_item_id": ["a"]}, map_over="work_item_id", workflow_id="drop-nokey")


# === 5. Every submission refusal is actionable ===


class TestRefusalsAreActionable:
    """A refusal a caller cannot act on is a bug report addressed to us.

    Each case below is a mistake someone makes at 2am against a durable
    store, so each message must carry three things: WHAT failed, what was
    SUPPLIED against what was expected, and a literal ``How to fix:``.
    """

    #: (kwargs that fail, exception type, a phrase naming the supplied value).
    REFUSALS = [
        ({"values": {"work_item_id": ["a"]}, "map_over": 7, "identity": "work_item_id"}, TypeError, "int"),
        ({"values": {"work_item_id": ["a"]}, "map_over": {"work_item_id": ["a"]}, "identity": "work_item_id"}, TypeError, "dict"),
        ({"values": {"work_item_id": ["a"]}, "map_over": [], "identity": "work_item_id"}, ValueError, "empty sequence"),
        ({"values": {"work_item_id": ["a"]}, "map_over": ["work_item_id", ""], "identity": "work_item_id"}, ValueError, "''"),
        ({"values": {"work_item_id": ["a"]}, "map_over": ["work_item_id", "work_item_id"], "identity": "work_item_id"}, ValueError, "more than once"),
        ({"values": [("work_item_id", ["a"])], "map_over": "work_item_id", "identity": "work_item_id"}, TypeError, "list"),
        ({"values": {"work_item_id": ["a"]}, "map_over": "work_item_id", "map_mode": "cross", "identity": "work_item_id"}, ValueError, "'cross'"),
        ({"values": {"other": [1]}, "map_over": "work_item_id", "identity": "work_item_id"}, ValueError, "not in values"),
        ({"values": {"work_item_id": []}, "map_over": "work_item_id", "identity": "work_item_id"}, ValueError, "zero items"),
        ({"values": {"work_item_id": ["a"], "blob": object()}, "map_over": "work_item_id", "identity": "work_item_id"}, ValueError, "blob"),
        ({"values": {"work_item_id": ["a"], "t": "x"}, "map_over": "work_item_id", "identity": "t"}, ItemKeyError, "does not name an expanded input"),
        ({"values": {"work_item_id": ["a", "a"]}, "map_over": "work_item_id", "identity": "work_item_id"}, ItemKeyError, "duplicate item key"),
    ]

    @pytest.mark.parametrize(("kwargs", "error", "supplied"), REFUSALS, ids=[str(i) for i in range(len(REFUSALS))])
    async def test_a_batch_refusal_names_the_input_the_value_and_the_fix(self, home, kwargs, error, supplied):
        graph = ingestion_graph()
        host = serve(graph, home=home, deployment_version="v1")

        with pytest.raises(error) as excinfo:
            await host.submit_batch(graph, workflow_id="drop-refused", **kwargs)

        message = str(excinfo.value)
        assert "submit_batch()" in message, message  # WHAT failed, by verb
        assert supplied in message, message  # what was SUPPLIED
        assert "How to fix:" in message, message  # what to do instead
        assert await host.client.list(RunQuery()) == []

    async def test_a_non_graph_submission_names_what_is_served_and_the_fix(self, home):
        host = serve(ingestion_graph(), home=home, deployment_version="v1")

        with pytest.raises(TypeError) as excinfo:
            await host.submit("ingest", {"work_item_id": "w"})

        message = str(excinfo.value)
        assert "graph-first" in message and "str" in message
        assert "This host serves: ['ingest']" in message
        assert "How to fix:" in message

    async def test_an_unserved_graph_names_both_identities_and_the_fix(self, home):
        host = serve(ingestion_graph("ingest"), home=home, deployment_version="v1")

        @node(output_name="validated_id")
        def different(work_item_id: str) -> str:
            return work_item_id

        drifted = Graph([different], name="ingest").with_runner(AsyncRunner())
        with pytest.raises(UnservedGraphError) as excinfo:
            await host.submit(drifted, {"work_item_id": "w"})

        message = str(excinfo.value)
        assert drifted.structural_hash in message  # what was SUPPLIED
        assert ingestion_graph("ingest").structural_hash in message  # what was EXPECTED
        assert "How to fix:" in message


# === 6. A fresh world: an empty Home runs the whole public flow ===


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
                identity="work_item_id",
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
