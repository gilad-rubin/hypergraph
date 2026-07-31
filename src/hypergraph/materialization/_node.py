"""HyperTable's insert-and-derive graph node."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from hypergraph.materialization._types import MaterializationReceipt
from hypergraph.nodes._rename import build_reverse_rename_map
from hypergraph.nodes.base import HyperNode

if TYPE_CHECKING:
    from hypergraph.materialization._hypertable import HyperTable


class MaterializationNode(HyperNode):
    """Insert one source row into a HyperTable and derive it."""

    def __init__(
        self,
        table: HyperTable,
        *,
        name: str | None = None,
        output_name: str = "receipt",
    ) -> None:
        table._ensure_analyzed()
        spec = table._spec
        assert spec is not None

        self.table = table
        self.name = name or spec.name
        self._local_inputs = (spec.identity, *(column.name for column in spec.columns if column.role == "source"))
        self.inputs = self._local_inputs
        self.outputs = (output_name,)
        self._rename_history = []

    @property
    def is_async(self) -> bool:
        return self.table._is_async_runner()

    @property
    def node_type(self) -> str:
        """A mounted table IS its derivation recipe, structurally.

        The recipe is a real graph the table runs per row, so a diagram that
        draws it as one opaque box hides the pipeline the reader came to see.
        Reporting ``"GRAPH"`` (with :attr:`nested_graph` below) lets the
        flat-graph builder expand it like any other container.

        This property is a VISUALIZATION concern only. It never reaches the
        checkpointer: step and boundary records store ``type(node).__name__``
        ("MaterializationNode"), not this string, so no durable record changes.
        Nor does it steer execution — the runners dispatch on
        ``executors.get(type(node))``, an exact class lookup.
        """
        return "GRAPH"

    @property
    def nested_graph(self) -> Any:
        """The derivation recipe, so viz can expand this node's interior.

        Structure only: the runner never descends into it. Execution stays
        with the MaterializationNode executor, which drives the table's own
        write path (insert, derive, persist) rather than running this graph
        as a nested run.
        """
        return self.table.graph

    @property
    def definition_hash(self) -> str:
        spec = self.table._spec
        assert spec is not None
        content = repr(
            (
                super().definition_hash,
                self.table._source_graph.definition_hash,
                spec.name,
                spec.identity,
                self.table._on_error,
            )
        )
        return hashlib.sha256(content.encode()).hexdigest()

    def _structural_signature_parts(self) -> list[str]:
        spec = self.table._spec
        assert spec is not None
        return [
            *super()._structural_signature_parts(),
            f"table={spec.name}",
            f"identity={spec.identity}",
            f"recipe_struct={self.table._source_graph.structural_hash}",
        ]

    def get_output_type(self, output: str) -> type | None:
        if output in self.outputs:
            return MaterializationReceipt
        return None

    def map_inputs_to_params(self, inputs: dict[str, Any]) -> dict[str, Any]:
        reverse_map = build_reverse_rename_map(self._rename_history, "inputs")
        return {reverse_map.get(key, key): value for key, value in inputs.items()}
