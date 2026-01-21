"""GraphNode - wrapper for using graphs as nodes."""

from typing import Any, Literal, TYPE_CHECKING, TypeVar

from hypergraph.nodes.base import HyperNode, RenameEntry

# TypeVar for self-referential return types (Python 3.10 compatible)
_GN = TypeVar("_GN", bound="GraphNode")

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

    # Reserved characters that cannot appear in GraphNode names
    # These are used as path separators in nested graph output access
    _RESERVED_CHARS = frozenset('./')

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
            ValueError: If name contains reserved characters ('.' or '/').
        """
        resolved_name = name or graph.name
        if resolved_name is None:
            raise ValueError(
                "GraphNode requires a name. Either set name on Graph(..., name='x') "
                "or pass name to as_node(name='x')"
            )

        # Validate name doesn't contain reserved path separators
        for char in self._RESERVED_CHARS:
            if char in resolved_name:
                raise ValueError(
                    f"GraphNode name cannot contain '{char}': {resolved_name!r}. "
                    f"Reserved characters: {set(self._RESERVED_CHARS)}"
                )

        self._graph = graph
        self._rename_history: list[RenameEntry] = []

        # map_over configuration (None = no mapping)
        self._map_over: list[str] | None = None
        self._map_mode: Literal["zip", "product"] = "zip"

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
    def node_type(self) -> str:
        """Node type for NetworkX representation."""
        return "GRAPH"

    @property
    def nested_graph(self) -> "Graph":
        """Returns the nested Graph."""
        return self._graph

    @property
    def map_config(self) -> tuple[list[str], Literal["zip", "product"]] | None:
        """Map configuration if set, else None.

        Returns:
            Tuple of (params, mode) if map_over was configured, else None.
        """
        if self._map_over:
            return (self._map_over, self._map_mode)
        return None

    @property
    def output_annotation(self) -> dict[str, Any]:
        """Type annotations for output values from the inner graph.

        Returns:
            dict mapping output names to their type annotations.
            For each output, finds the node in the inner graph that produces it
            and gets that node's type annotation for that specific output.
            Returns empty dict entries for outputs without type annotations.

            When map_over is configured, output types are wrapped in list[T]
            since mapped execution produces lists of results.

        Example:
            >>> @node(output_name="x")
            ... def inner_func(a: int) -> str: return "hello"
            >>> inner_graph = Graph([inner_func], name="inner")
            >>> gn = inner_graph.as_node()
            >>> gn.output_annotation
            {'x': str}
            >>> gn.map_over("a").output_annotation
            {'x': list[str]}
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
                result[output_name] = self._wrap_type_for_map_over(None)
                continue

            # Use universal get_output_type method
            output_type = source_node.get_output_type(output_name)
            result[output_name] = self._wrap_type_for_map_over(output_type)

        return result

    def _wrap_type_for_map_over(self, inner_type: type | None) -> type | None:
        """Wrap type in list[] if map_over is configured.

        Args:
            inner_type: The inner type to potentially wrap

        Returns:
            list[inner_type] if map_over is set, otherwise inner_type unchanged.
            If inner_type is None and map_over is set, returns bare list.
        """
        if not self._map_over:
            return inner_type
        if inner_type is None:
            return list
        return list[inner_type]

    def get_output_type(self, output: str) -> type | None:
        """Get type annotation for an output from the inner graph.

        Args:
            output: Output value name

        Returns:
            The type annotation, or None if not annotated.
        """
        return self.output_annotation.get(output)

    def get_input_type(self, param: str) -> type | None:
        """Get expected type for an input parameter from the inner graph.

        Derives the type from whichever node in the inner graph declares
        this parameter as an input.

        Args:
            param: Input parameter name (may be a renamed external name)

        Returns:
            The type annotation, or None if not annotated.
        """
        # Resolve param back to original name if renamed
        original_param = self._resolve_original_input_name(param)

        # Find which node in inner graph has this as an input
        for inner_node in self._graph._nodes.values():
            if original_param in inner_node.inputs:
                return inner_node.get_input_type(original_param)
        return None

    def _resolve_original_input_name(self, param: str) -> str:
        """Resolve a possibly-renamed input name back to the original.

        Traces back through rename history to find what the input was
        originally called in the inner graph.

        Args:
            param: Current input parameter name

        Returns:
            Original name used in the inner graph.
        """
        current = param
        # Walk rename history in reverse to find original
        for entry in reversed(self._rename_history):
            if entry.kind == "inputs" and entry.new == current:
                current = entry.old
        return current

    def has_default_for(self, param: str) -> bool:
        """Check if a parameter has a default or bound value in the inner graph.

        Returns True if the parameter is bound in the inner graph, or if any
        inner node has a default for it.

        Args:
            param: Input parameter name (may be a renamed external name)

        Returns:
            True if param is bound or any inner node has a default.
        """
        if param not in self.inputs:
            return False

        # Resolve to original name for inner graph lookup
        original_param = self._resolve_original_input_name(param)

        # Check if bound in inner graph
        if original_param in self._graph.inputs.bound:
            return True
        # Check if any inner node has a default
        for inner_node in self._graph._nodes.values():
            if original_param in inner_node.inputs and inner_node.has_default_for(original_param):
                return True
        return False

    def get_default_for(self, param: str) -> Any:
        """Get the default or bound value for a parameter from the inner graph.

        Returns the bound value if the parameter is bound, otherwise returns
        the default value from an inner node.

        Args:
            param: Input parameter name (may be a renamed external name)

        Returns:
            The bound or default value.

        Raises:
            KeyError: If no default or bound value exists for this parameter.
        """
        # Resolve to original name for inner graph lookup
        original_param = self._resolve_original_input_name(param)

        # Check if bound in inner graph first
        if original_param in self._graph.inputs.bound:
            return self._graph.inputs.bound[original_param]
        # Check inner nodes for defaults
        for inner_node in self._graph._nodes.values():
            if original_param in inner_node.inputs and inner_node.has_default_for(original_param):
                return inner_node.get_default_for(original_param)
        raise KeyError(f"No default value for parameter '{param}'")

    def map_over(
        self: _GN,
        *params: str,
        mode: Literal["zip", "product"] = "zip",
    ) -> _GN:
        """Configure this GraphNode for iteration over input parameters.

        When a GraphNode is configured with map_over, the runner will execute
        the inner graph multiple times, once for each combination of values
        in the mapped parameters. Outputs become lists of results.

        Args:
            *params: Input parameter names to iterate over. These parameters
                should receive list values at runtime.
            mode: How to combine multiple parameters:
                - "zip": Parallel iteration (default). Parameters must have
                  equal-length lists. First values together, second together, etc.
                - "product": Cartesian product. All combinations of values.

        Returns:
            New GraphNode instance with map_over configuration

        Raises:
            ValueError: If no parameters specified
            ValueError: If any parameter is not in this node's inputs

        Example:
            >>> inner = Graph([double], name="inner")
            >>> # Execute double() for each x in [1, 2, 3]
            >>> gn = inner.as_node().map_over("x")
            >>> # In outer graph, doubled output will be [2, 4, 6]

            >>> # Zip mode: process pairs (x=1,y=10), (x=2,y=20)
            >>> gn = inner.as_node().map_over("x", "y", mode="zip")

            >>> # Product mode: process all combinations
            >>> gn = inner.as_node().map_over("x", "y", mode="product")
        """
        if not params:
            raise ValueError("map_over requires at least one parameter")

        # Validate all params exist in inputs
        for param in params:
            if param not in self.inputs:
                raise ValueError(
                    f"Parameter '{param}' is not an input of this GraphNode. "
                    f"Available inputs: {self.inputs}"
                )

        # Create copy with map_over configuration
        clone = self._copy()
        clone._map_over = list(params)
        clone._map_mode = mode
        return clone

    def _copy(self: _GN) -> _GN:
        """Create a shallow copy preserving map_over configuration."""
        import copy

        clone = copy.copy(self)
        clone._rename_history = list(self._rename_history)
        # Preserve map_over as a new list if set
        if self._map_over is not None:
            clone._map_over = list(self._map_over)
        return clone

    def with_inputs(
        self: _GN,
        mapping: dict[str, str] | None = None,
        /,
        **kwargs: str,
    ) -> _GN:
        """Return new node with renamed inputs, updating map_over if needed.

        Overrides base class to also update _map_over when inputs are renamed.
        """
        combined = {**(mapping or {}), **kwargs}
        if not combined:
            return self._copy()

        # Call base class rename
        clone = self._with_renamed("inputs", combined)

        # Update map_over if any mapped params were renamed
        if clone._map_over is not None:
            clone._map_over = [
                combined.get(p, p) for p in clone._map_over
            ]

        return clone
