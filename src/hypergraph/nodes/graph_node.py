"""GraphNode - wrapper for using graphs as nodes."""

from typing import TYPE_CHECKING

from hypergraph.nodes.base import HyperNode, RenameEntry

if TYPE_CHECKING:
    from hypergraph.graph import Graph


class GraphNode(HyperNode):
    """Wrap a Graph for use as a node in another graph.

    Enables hierarchical composition: a graph can contain other graphs as nodes.
    The wrapped graph's inputs become the node's inputs, and its outputs become
    the node's outputs.

    Create via Graph.as_node() rather than directly:

        >>> inner = Graph([...], name="preprocess")
        >>> outer = Graph([inner.as_node(), ...])

    Attributes:
        name: Node name (from graph.name or explicit override)
        inputs: All graph inputs (required + optional + seeds)
        outputs: All graph outputs
        graph: The wrapped Graph instance

    Properties:
        definition_hash: Hash of the nested graph (delegates to graph.definition_hash)

    Example:
        >>> @node(output_name="y")
        ... def double(x: int) -> int:
        ...     return x * 2
        >>> inner = Graph([double], name="doubler")
        >>> gn = inner.as_node()
        >>> gn.name
        'doubler'
        >>> gn.inputs
        ('x',)
        >>> gn.outputs
        ('y',)
    """

    def __init__(
        self,
        graph: "Graph",
        name: str | None = None,
    ):
        """Wrap a graph as a node.

        Args:
            graph: The graph to wrap
            name: Node name (default: use graph.name if set)

        Raises:
            ValueError: If name not provided and graph has no name.
        """
        resolved_name = name or graph.name
        if resolved_name is None:
            raise ValueError(
                "GraphNode requires a name. Either set name on Graph(..., name='x') "
                "or pass name to as_node(name='x')"
            )

        self._graph = graph
        self._rename_history: list[RenameEntry] = []

        # Core HyperNode attributes
        self.name = resolved_name
        self.inputs = graph.inputs.all
        self.outputs = graph.outputs

    @property
    def graph(self) -> "Graph":
        """The wrapped graph."""
        return self._graph

    @property
    def definition_hash(self) -> str:
        """Hash of the nested graph."""
        return self._graph.definition_hash
