"""GraphNode - wrapper for using graphs as nodes."""

from typing import Any, TYPE_CHECKING

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

    @property
    def is_async(self) -> bool:
        """Does this nested graph contain any async nodes?

        Delegates to the inner graph's has_async_nodes property.
        """
        return self._graph.has_async_nodes

    @property
    def output_annotation(self) -> dict[str, Any]:
        """Type annotations for output values from the inner graph.

        Returns:
            dict mapping output names to their type annotations.
            For each output, finds the node in the inner graph that produces it
            and gets that node's type annotation for that specific output.
            Returns empty dict entries for outputs without type annotations.

        Example:
            >>> @node(output_name="x")
            ... def inner_func(a: int) -> str: return "hello"
            >>> inner_graph = Graph([inner_func], name="inner")
            >>> gn = inner_graph.as_node()
            >>> gn.output_annotation
            {'x': str}
        """
        result: dict[str, Any] = {}

        # Build mapping: output_name -> source_node
        output_to_node: dict[str, HyperNode] = {}
        for node in self._graph._nodes.values():
            for output in node.outputs:
                output_to_node[output] = node

        # For each output of this GraphNode, get type from source node
        for output_name in self.outputs:
            source_node = output_to_node.get(output_name)
            if source_node is None:
                continue

            # Use universal get_output_type method
            output_type = source_node.get_output_type(output_name)
            if output_type is not None:
                result[output_name] = output_type

        return result

    def get_output_type(self, output: str) -> type | None:
        """Get type annotation for an output from the inner graph.

        Args:
            output: Output value name

        Returns:
            The type annotation, or None if not annotated.
        """
        return self.output_annotation.get(output)
