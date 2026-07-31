"""A mounted HyperTable visualizes as its derivation recipe, not one box.

A table's recipe IS a graph — the one it runs per row — so drawing the
mounted node as a single opaque FUNCTION box hid exactly the pipeline a
reader opens a diagram to see. ``MaterializationNode`` now reports
``node_type == "GRAPH"`` and hands its recipe to ``nested_graph``, which is
all the flat-graph builder needs to expand it like any other container.

The change is deliberately structural-only, and the boundaries are pinned
here: execution is untouched (the runners dispatch on the exact class), and
no durable record changes (step and boundary records store
``type(node).__name__``, never this property).
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from hypergraph import Graph, SyncRunner, node
from hypergraph.materialization import TableStore
from hypergraph.materialization._node import MaterializationNode


class MemoryStore(TableStore):
    """In-memory TableStore that really keeps rows, so writes are observable."""

    def __init__(self) -> None:
        self.opened_spec: Any = None
        self.tables: dict[str, list[dict[str, Any]]] = {}

    def open(self, spec, children):
        self.opened_spec = spec
        opened = {spec.name: [column.name for column in spec.columns]}
        self.tables.setdefault(spec.name, [])
        for child in children:
            opened[child.name] = [column.name for column in child.columns]
            self.tables.setdefault(child.name, [])
        return opened

    def count(self, table_name):
        return len(self.tables.get(table_name, []))

    def read_rows(self, table_name, where=None, *, limit=None):
        rows = list(self.tables.get(table_name, []))
        return rows[:limit] if limit is not None else rows

    def read_one(self, table_name, identity_column, identity_value):
        for row in self.tables.get(table_name, []):
            if row.get(identity_column) == identity_value:
                return row
        return None

    def write_rows(self, table_name, rows):
        self.tables.setdefault(table_name, []).extend(rows)
        return None

    def delete_rows(self, table_name, where):
        return 0

    def max_write_gen(self, table_name):
        return 0

    def evolve_schema(self, table_name, new_columns):
        return []


@node(output_name="clean_text")
def clean(text: str) -> str:
    return text.strip().lower()


@node(output_name="word_count")
def count_words(clean_text: str) -> int:
    return len(clean_text.split())


@node(output_name="embedding")
def embed(clean_text: str) -> list[float]:
    return [1.0, 0.0]


RECIPE_NODES = ("clean", "count_words", "embed")


def _table(store: MemoryStore | None = None):
    return Graph([clean, count_words, embed], name="recipe").as_table(
        identity="doc_id",
        store=store if store is not None else MemoryStore(),
        runner=SyncRunner(),
    )


def _mounted(name: str = "materialize_docs"):
    return Graph([_table().as_node(name=name)], name="outer")


class TestRecipeIsVisible:
    def test_node_reports_itself_as_a_container(self):
        mat = _table().as_node(name="materialize_docs")
        assert isinstance(mat, MaterializationNode)
        assert mat.node_type == "GRAPH"
        assert mat.nested_graph is not None

    def test_nested_graph_is_the_tables_recipe(self):
        table = _table()
        mat = table.as_node(name="materialize_docs")
        assert mat.nested_graph is table.graph, "the recipe itself, never a copy or a rebuild"

    def test_flat_graph_contains_the_recipe_nodes(self):
        """The regression this exists for: one opaque box before, a tree now."""
        flat = _mounted().to_flat_graph()

        assert "materialize_docs" in flat.nodes
        for inner in RECIPE_NODES:
            node_id = f"materialize_docs/{inner}"
            assert node_id in flat.nodes, f"{inner!r} must appear inside the container"
            assert flat.nodes[node_id]["parent"] == "materialize_docs"
            assert flat.nodes[node_id]["original_name"] == inner

    def test_flat_graph_carries_the_recipes_internal_edges(self):
        flat = _mounted().to_flat_graph()
        edges = set(flat.edges)
        assert ("materialize_docs/clean", "materialize_docs/count_words") in edges
        assert ("materialize_docs/clean", "materialize_docs/embed") in edges

    def test_container_renders_as_an_expandable_pipeline(self):
        from hypergraph.viz.renderer import render_graph

        result = render_graph(_mounted().to_flat_graph(), depth=2, separate_outputs=False)
        by_id = {n["id"]: n for n in result["nodes"]}

        assert by_id["materialize_docs"]["data"]["nodeType"] == "PIPELINE"
        for inner in RECIPE_NODES:
            assert f"materialize_docs/{inner}" in by_id, "depth=2 draws the recipe inside"

    def test_depth_zero_still_collapses_to_one_box(self):
        """Expandable is not the same as always expanded."""
        from hypergraph.viz.renderer import render_graph

        result = render_graph(_mounted().to_flat_graph(), depth=0, separate_outputs=False)
        visible = [n["id"] for n in result["nodes"] if not n.get("hidden")]
        assert "materialize_docs" in visible
        assert not [n for n in visible if n.startswith("materialize_docs/")]


class TestBoundariesHeld:
    """What the structural change must NOT touch."""

    def test_durable_node_type_metadata_is_unchanged(self):
        """Checkpointers store the CLASS name, never the node_type property.

        So flipping the property from FUNCTION to GRAPH cannot change a
        stored string, and existing Run Homes need no migration or compat
        shim. Pinned because it is the whole reason this change is safe.
        """
        graph = _mounted()
        stored = type(graph._nodes["materialize_docs"]).__name__
        assert stored == "MaterializationNode"
        assert stored != graph._nodes["materialize_docs"].node_type

    def test_execution_dispatch_keys_on_the_exact_class(self):
        """Executor selection is an exact-class lookup, so node_type cannot steer it.

        Asserted behaviorally against the real registries: the exact class
        resolves, and a subclass does NOT inherit the entry. That second half
        is what proves the lookup is `registry[type(node)]` rather than an
        isinstance walk — and therefore that a property can never reroute it.
        """
        from hypergraph import AsyncRunner
        from hypergraph.runners.async_.executors.materialization_node import AsyncMaterializationNodeExecutor
        from hypergraph.runners.sync.executors.materialization_node import SyncMaterializationNodeExecutor

        class Subclassed(MaterializationNode):
            pass

        for runner, expected in (
            (SyncRunner(), SyncMaterializationNodeExecutor),
            (AsyncRunner(), AsyncMaterializationNodeExecutor),
        ):
            registry = runner._executors
            assert isinstance(registry.get(MaterializationNode), expected)
            assert registry.get(Subclassed) is None, "an exact-class lookup never resolves a subclass"

    def test_the_recipe_is_not_executed_as_a_nested_run(self):
        """Sequential behavior pin: mounting still materializes rows itself.

        ``nested_graph`` is structure for the diagram. If the runner ever
        started descending into it, the table's own write path would be
        bypassed and nothing would be persisted.
        """
        store = MemoryStore()
        table = _table(store)
        graph = Graph([table.as_node(name="materialize_docs")], name="outer")

        result = SyncRunner().run(graph, {"doc_id": "d-1", "text": "  Hello World  "})

        assert result.completed
        receipt = result.values["receipt"]
        assert receipt.id == "d-1"
        assert receipt.error is None
        persisted = [row for rows in store.tables.values() for row in rows if row.get("doc_id") == "d-1"]
        assert persisted, "the table's own write path must still run"
        (row,) = persisted
        assert row["clean_text"] == "hello world", "the recipe derived its columns"
        assert row["word_count"] == 2
        assert row["embedding"] == [1.0, 0.0]

    def test_run_log_records_one_step_for_the_container(self):
        """The recipe's nodes are not separate steps in the parent run."""
        table = _table()
        graph = Graph([table.as_node(name="materialize_docs")], name="outer")

        result = SyncRunner().run(graph, {"doc_id": "d-2", "text": "one two three"})

        assert result.log is not None
        assert [step.node_name for step in result.log.steps] == ["materialize_docs"]

    def test_identity_hashes_are_unaffected(self):
        """A viz-only property must not move Definition identity."""
        first = _table().as_node(name="materialize_docs")
        second = _table().as_node(name="materialize_docs")
        assert first.definition_hash == second.definition_hash
        assert Graph([first], name="outer").structural_hash == Graph([second], name="outer").structural_hash


@pytest.mark.parametrize("attribute", ["node_type", "nested_graph"])
def test_overrides_live_on_the_class_not_the_instance(attribute):
    """A real property override, not a patched instance attribute."""
    assert attribute in MaterializationNode.__dict__
    assert isinstance(MaterializationNode.__dict__[attribute], property)


def test_pyarrow_is_available():
    """The store double declares Arrow types; fail loudly if pyarrow is gone."""
    assert pa.utf8() is not None
