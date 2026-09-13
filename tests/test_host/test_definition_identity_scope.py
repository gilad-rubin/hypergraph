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
4. `submit_batch` and `fork` share `_require_definition`, so one guard covers
   all three new-work/migration verbs.
5. An unmodified graph's Definition hash is still its `structural_hash` —
   every already-stored submission keeps resolving.
"""

from __future__ import annotations

import pytest

from hypergraph import (
    AsyncRunner,
    Graph,
    RunQuery,
    UnservedGraphError,
    node,
    serve,
)
from hypergraph.checkpointers.types import WorkflowStatus
from hypergraph.host.definition import definition_struct_hash
from tests.test_host._batch_interrupt import worker

aiosqlite = pytest.importorskip("aiosqlite")


def pipeline(ledger: list[str], name: str = "pipeline") -> Graph:
    """`cheap` -> `costly`: the issue's two-node probe graph."""

    @node(output_name="cheap")
    def cheap(x: int) -> int:
        ledger.append("cheap")
        return x + 1

    @node(output_name="costly")
    def costly(cheap: int) -> int:
        ledger.append("costly")
        return cheap * 10

    return Graph([cheap, costly], name=name).with_runner(AsyncRunner())


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
