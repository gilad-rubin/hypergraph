"""GraphNode - wrapper for using graphs as nodes."""

from typing import TYPE_CHECKING, Any, Literal, TypeVar

from hypergraph.nodes._rename import build_reverse_rename_map
from hypergraph.nodes.base import HyperNode, RenameEntry

# TypeVar for self-referential return types (Python 3.10 compatible)
_GN = TypeVar("_GN", bound="GraphNode")

# Duplicated from runners._shared.types to avoid circular import
# (graph_node -> runners -> graph -> nodes -> graph_node)
ErrorHandling = Literal["raise", "continue"]

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
        inputs: All graph inputs (required + optional + entry point params)
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
    _RESERVED_CHARS = frozenset("./")

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
            raise ValueError("GraphNode requires a name. Either set name on Graph(..., name='x') or pass name to as_node(name='x')")

        # Validate name doesn't contain reserved path separators
        for char in self._RESERVED_CHARS:
            if char in resolved_name:
                raise ValueError(f"GraphNode name cannot contain '{char}': {resolved_name!r}. Reserved characters: {set(self._RESERVED_CHARS)}")

        self._graph = graph
        self._rename_history: list[RenameEntry] = []

        # map_over configuration (None = no mapping)
        self._map_over: list[str] | None = None
        self._map_mode: Literal["zip", "product"] = "zip"
        self._error_handling: ErrorHandling = "raise"
        self._clone: bool | list[str] = False

        # Runner delegation (None = inherit from parent runner)
        self._runner_override: Any = None

        # When True, the inner graph finishes all remaining supersteps
        # after a stop signal instead of breaking immediately.
        self._complete_on_stop: bool = False

        # Core HyperNode attributes
        self.name = resolved_name
        self.inputs = graph.inputs.all
        self.outputs = self._derive_outputs(graph)

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
    def runner_override(self) -> Any:
        """Runner to delegate execution to, or None to inherit from parent."""
        return self._runner_override

    @property
    def nested_graph(self) -> "Graph":
        """Returns the nested Graph."""
        return self._graph

    @property
    def nx_attrs(self) -> dict[str, Any]:
        """Flattened attributes enriched with collapsed-view output metadata."""
        attrs = super().nx_attrs
        attrs["collapsed_outputs"] = self._derive_collapsed_outputs()
        return attrs

    def with_runner(self: _GN, runner: Any) -> _GN:
        """Return a new GraphNode that delegates execution to the given runner.

        The runner override does not affect graph structure or definition_hash.
        It is resolved at execution time by the parent runner's GraphNode executor.

        Args:
            runner: A runner instance (e.g., DaftRunner(), SyncRunner()).

        Returns:
            New GraphNode with runner_override set.

        Example:
            >>> sub = inner_graph.as_node(name="sub").with_runner(DaftRunner())
        """
        new = self._copy()
        new._runner_override = runner
        return new

    @property
    def map_config(self) -> tuple[list[str], Literal["zip", "product"], ErrorHandling] | None:
        """Map configuration if set, else None.

        Returns:
            Tuple of (params, mode, error_handling) if map_over was configured, else None.
        """
        if self._map_over:
            return (self._map_over, self._map_mode, self._error_handling)
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
        for node in self._graph.iter_nodes():
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
        for inner_node in self._graph.iter_nodes():
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

    def map_inputs_to_params(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Map renamed input names back to original inner graph parameter names.

        When a GraphNode's inputs are renamed (via with_inputs), this method
        maps the current/renamed names back to the original parameter names
        expected by the inner graph.

        Args:
            inputs: Dict with current (potentially renamed) input names as keys

        Returns:
            Dict with original inner graph parameter names as keys
        """
        reverse_map = build_reverse_rename_map(self._rename_history, "inputs")
        if not reverse_map:
            return inputs

        return {reverse_map.get(key, key): value for key, value in inputs.items()}

    def _original_map_params(self) -> list[str] | None:
        """Get map_over params translated to original inner graph names.

        Uses build_reverse_rename_map() to handle parallel renames correctly
        (e.g., with_inputs(x='y', y='x')), matching the semantics of
        map_inputs_to_params().

        Returns:
            List of original param names if map_over is set, else None.
        """
        if self._map_over is None:
            return None
        reverse_map = build_reverse_rename_map(self._rename_history, "inputs")
        if not reverse_map:
            return list(self._map_over)
        return [reverse_map.get(p, p) for p in self._map_over]

    def _original_clone(self) -> bool | list[str]:
        """Get clone config translated to original inner graph names.

        Mirrors _original_map_params() — user specifies clone in outer
        (renamed) namespace, but generate_map_inputs operates on original names.

        Returns:
            Clone config with param names in original inner graph namespace.
        """
        if not isinstance(self._clone, list):
            return self._clone
        reverse_map = build_reverse_rename_map(self._rename_history, "inputs")
        if not reverse_map:
            return list(self._clone)
        return [reverse_map.get(p, p) for p in self._clone]

    def map_outputs_from_original(self, outputs: dict[str, Any]) -> dict[str, Any]:
        """Map original inner graph output names to renamed external names.

        When a GraphNode's outputs are renamed (via with_outputs), this method
        maps the original names produced by the inner graph to the renamed names
        expected by the outer graph.

        Args:
            outputs: Dict with original inner graph output names as keys

        Returns:
            Dict with renamed (external) output names as keys
        """
        reverse_map = build_reverse_rename_map(self._rename_history, "outputs")
        if not reverse_map:
            return outputs

        # Build forward map (original -> renamed) by inverting reverse map
        forward_map = {v: k for k, v in reverse_map.items()}
        return {forward_map.get(key, key): value for key, value in outputs.items()}

    def map_output_name_from_original(self, output_name: str) -> str:
        """Map a single inner output name to its external renamed name."""
        reverse_map = build_reverse_rename_map(self._rename_history, "outputs")
        if not reverse_map:
            return output_name
        forward_map = {v: k for k, v in reverse_map.items()}
        return forward_map.get(output_name, output_name)

    def resolve_original_output_name(self, output_name: str) -> str:
        """Resolve an external output name back to the inner graph's name."""
        reverse_map = build_reverse_rename_map(self._rename_history, "outputs")
        return reverse_map.get(output_name, output_name)

    def map_input_name_from_original(self, input_name: str) -> str:
        """Map a single inner input name to its external renamed name."""
        reverse_map = build_reverse_rename_map(self._rename_history, "inputs")
        if not reverse_map:
            return input_name
        forward_map = {v: k for k, v in reverse_map.items()}
        return forward_map.get(input_name, input_name)

    def map_resume_key_from_original(self, resume_key: str) -> str:
        """Map a nested resume key from inner names to external names.

        Only the first path component can be renamed by the GraphNode boundary.
        For example, ``decision`` may become ``verdict`` while
        ``review.decision`` remains unchanged because ``review`` is internal.
        """
        head, sep, tail = resume_key.partition(".")
        mapped_head = self.map_output_name_from_original(head)
        if not sep:
            return mapped_head
        return f"{mapped_head}.{tail}"

    def iter_active_inner_nodes(self) -> tuple[HyperNode, ...]:
        """Return the inner graph nodes visible through this GraphNode boundary."""
        from hypergraph.graph.input_spec import _compute_active_scope

        active_nodes, _ = _compute_active_scope(
            self._graph._nodes,
            self._graph._nx_graph,
            entrypoints=self._graph.entrypoints_config,
            selected=self._graph.selected,
        )
        return tuple(active_nodes.values())

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

        # Check if bound anywhere in the visible inner graph scope
        if original_param in self._graph.inputs.bound:
            return True
        # Check if any inner node has a default
        inner_nodes_with_param = [n for n in self._graph.iter_nodes() if original_param in n.inputs]
        if not inner_nodes_with_param:
            return False
        return all(inner_node.has_default_for(original_param) for inner_node in inner_nodes_with_param)

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
        for inner_node in self._graph.iter_nodes():
            if original_param in inner_node.inputs and inner_node.has_default_for(original_param):
                return inner_node.get_default_for(original_param)
        raise KeyError(f"No default value for parameter '{param}'")

    def has_signature_default_for(self, param: str) -> bool:
        """Check if a parameter has consistent signature defaults in all inner nodes.

        This only checks actual function signature defaults, NOT bound values.
        Used for validation to ensure consistent defaults across nodes.

        Args:
            param: Input parameter name (may be a renamed external name)

        Returns:
            True if all inner nodes using this parameter have a signature default.
            False if parameter is bound or if any inner node lacks a signature default.
        """
        if param not in self.inputs:
            return False

        # Resolve to original name for inner graph lookup
        original_param = self._resolve_original_input_name(param)

        # Bound parameters don't have signature defaults (they're configuration)
        if original_param in self._graph.inputs.bound:
            return False

        # Check if ALL inner nodes using this param have signature defaults
        inner_nodes_with_param = [n for n in self._graph.iter_nodes() if original_param in n.inputs]

        if not inner_nodes_with_param:
            return False

        return all(n.has_signature_default_for(original_param) for n in inner_nodes_with_param)

    def get_signature_default_for(self, param: str) -> Any:
        """Get the signature default value for a parameter.

        Returns ONLY signature defaults from inner nodes, NOT bound values.
        Used for validation to compare actual default values.

        Args:
            param: Input parameter name (may be a renamed external name)

        Returns:
            The signature default value from an inner node.

        Raises:
            KeyError: If no signature default exists (bound values don't count).
        """
        # Resolve to original name for inner graph lookup
        original_param = self._resolve_original_input_name(param)

        # Bound parameters don't have signature defaults
        if original_param in self._graph.inputs.bound:
            raise KeyError(f"Parameter '{param}' is bound, not a signature default")

        # Get signature default from inner nodes (not bound values)
        for inner_node in self._graph.iter_nodes():
            if original_param in inner_node.inputs and inner_node.has_signature_default_for(original_param):
                return inner_node.get_signature_default_for(original_param)

        raise KeyError(f"No signature default for parameter '{param}'")

    def map_over(
        self: _GN,
        *params: str,
        mode: Literal["zip", "product"] = "zip",
        error_handling: ErrorHandling = "raise",
        clone: bool | list[str] = False,
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
            error_handling: How to handle failures during map execution:
                - "raise": Stop on first failure and raise the error (default).
                - "continue": Collect all results; failed items become None
                  placeholders to preserve list length.
            clone: Control deep-copying of broadcast (non-mapped) values per
                iteration. Useful when nodes mutate broadcast inputs.
                - False (default): share by reference (efficient, backward compatible)
                - True: deep-copy ALL broadcast values per iteration
                - list[str]: deep-copy only the named params per iteration

                Broadcast Sharing:
                    Non-mapped inputs ("broadcast values") are shared by reference
                    across all iterations. This is efficient and correct for:
                    - API clients (OpenAI, httpx) — designed for concurrent use
                    - Immutable values (strings, ints, tuples) — can't be mutated
                    - Frozen objects — mutation raises immediately

                    If a node mutates a broadcast value, all iterations see the change.
                    In async execution, this is a race condition.

                    To protect against accidental mutation, either:

                    1. Use clone= to deep-copy per iteration:
                       inner.map_over("items", clone=["config"])

                    2. Use immutable types for broadcast values:
                       - dict → types.MappingProxyType(config)
                       - list → tuple(items)
                       - @dataclass → @dataclass(frozen=True)
                       - Pydantic → model_config = ConfigDict(frozen=True)

                    3. Use .bind() on the inner graph for shared resources:
                       inner = Graph([...]).bind(client=openai_client)
                       # bind values are resolved inside the inner graph
                       # and never go through the clone path

                    Performance note: clone=True deep-copies every broadcast value
                    on every iteration. For large product-mode maps this can be
                    expensive. Prefer clone=["specific_params"] when possible.

        Returns:
            New GraphNode instance with map_over configuration

        Raises:
            ValueError: If no parameters specified
            ValueError: If any parameter is not in this node's inputs
            TypeError: If clone is not bool or list[str]
            ValueError: If clone list contains a mapped parameter

        Example:
            >>> inner = Graph([double], name="inner")
            >>> # Execute double() for each x in [1, 2, 3]
            >>> gn = inner.as_node().map_over("x")
            >>> # In outer graph, doubled output will be [2, 4, 6]

            >>> # Zip mode: process pairs (x=1,y=10), (x=2,y=20)
            >>> gn = inner.as_node().map_over("x", "y", mode="zip")

            >>> # Product mode: process all combinations
            >>> gn = inner.as_node().map_over("x", "y", mode="product")

            >>> # Clone broadcast values to prevent mutation leaking across iterations
            >>> gn = inner.as_node().map_over("items", clone=["config"])
        """
        if not params:
            raise ValueError("map_over requires at least one parameter")

        # Validate all params exist in inputs
        for param in params:
            if param not in self.inputs:
                raise ValueError(f"Parameter '{param}' is not an input of this GraphNode. Available inputs: {self.inputs}")

        # Validate clone parameter
        _validate_clone(clone, params, self.inputs)

        # Create copy with map_over configuration
        new = self._copy()
        new._map_over = list(params)
        new._map_mode = mode
        new._error_handling = error_handling
        new._clone = list(clone) if isinstance(clone, list) else clone
        return new

    def _copy(self: _GN) -> _GN:
        """Create a shallow copy preserving map_over and runner_override."""
        import copy

        new = copy.copy(self)
        new._rename_history = list(self._rename_history)
        # Preserve map_over as a new list if set
        if self._map_over is not None:
            new._map_over = list(self._map_over)
        # Preserve clone as a new list if it's a list
        if isinstance(self._clone, list):
            new._clone = list(self._clone)
        # runner_override is preserved as-is (runner instances are shared)
        return new

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
        renamed = self._with_renamed("inputs", combined)

        # Update map_over if any mapped params were renamed
        if renamed._map_over is not None:
            renamed._map_over = [combined.get(p, p) for p in renamed._map_over]

        # Update clone list if any cloned params were renamed
        if isinstance(renamed._clone, list):
            renamed._clone = [combined.get(p, p) for p in renamed._clone]

        return renamed

    @staticmethod
    def _derive_outputs(graph: "Graph") -> tuple[str, ...]:
        """Resolve the GraphNode's visible outputs for the current graph scope."""
        if graph.selected is not None:
            return graph.selected

        if graph.entrypoints_config is None:
            return graph.outputs

        from hypergraph.graph.input_spec import _compute_active_scope

        active_nodes, _ = _compute_active_scope(
            graph._nodes,
            graph._nx_graph,
            entrypoints=graph.entrypoints_config,
            selected=None,
        )
        return tuple(dict.fromkeys(output for node in active_nodes.values() for output in node.outputs))

    def _derive_collapsed_outputs(self) -> tuple[str, ...]:
        """Resolve outputs a collapsed GraphNode should advertise.

        By default this is the set of leaf outputs visible through the graph
        boundary. For select-scoped graphs, preserve the explicitly selected
        outputs even when they are not leaves.
        """
        if self._graph.selected is not None:
            return tuple(self.map_output_name_from_original(output) for output in self._graph.selected)

        active_nodes = self.iter_active_inner_nodes()
        active_names = {node.name for node in active_nodes}
        nx_graph = self._graph.nx_graph
        internally_consumed_outputs: set[str] = set()

        for source, target, edge_data in nx_graph.edges(data=True):
            if source not in active_names or target not in active_names:
                continue
            if edge_data.get("edge_type", "data") != "data":
                continue
            internally_consumed_outputs.update(edge_data.get("value_names", ()))

        leaf_outputs: list[str] = []
        for node in active_nodes:
            for output in node.outputs:
                if output in internally_consumed_outputs:
                    continue
                mapped_output = self.map_output_name_from_original(output)
                if mapped_output not in leaf_outputs:
                    leaf_outputs.append(mapped_output)

        return tuple(leaf_outputs)


def _validate_clone(
    clone: bool | list[str],
    map_params: tuple[str, ...],
    node_inputs: tuple[str, ...],
) -> None:
    """Validate the clone parameter for map_over().

    Raises:
        TypeError: If clone is not bool or list of strings
        ValueError: If clone list contains a mapped parameter
        ValueError: If clone list contains a param not in node inputs
    """
    if not isinstance(clone, (bool, list)):
        raise TypeError(f"clone must be bool or list[str], got {type(clone).__name__}")

    if isinstance(clone, list):
        for entry in clone:
            if not isinstance(entry, str):
                raise TypeError(f"clone list entries must be strings, got {type(entry).__name__}: {entry!r}")

        mapped_set = set(map_params)
        inputs_set = set(node_inputs)
        for name in clone:
            if name in mapped_set:
                raise ValueError(f"Cannot clone mapped parameter '{name}' — mapped params are already per-iteration")
            if name not in inputs_set:
                raise ValueError(f"Parameter '{name}' in clone is not an input of this GraphNode. Available inputs: {node_inputs}")
