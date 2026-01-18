"""FunctionNode - wraps Python functions as graph nodes."""

from __future__ import annotations

import inspect
import warnings
from typing import Any, Callable, get_type_hints

from hypergraph._utils import ensure_tuple, hash_definition
from hypergraph.nodes._rename import _apply_renames, build_reverse_rename_map
from hypergraph.nodes.base import HyperNode


def _resolve_outputs(
    func: Callable,
    output_name: str | tuple[str, ...] | None,
) -> tuple[str, ...]:
    """Resolve output names, warning if return annotation exists without output_name.

    Args:
        func: The wrapped function
        output_name: User-provided output name(s), or None for side-effect only

    Returns:
        Tuple of output names (empty for side-effect only nodes)
    """
    if output_name is not None:
        return ensure_tuple(output_name)

    # No output_name → side-effect only, but warn if function has return annotation
    _warn_if_has_return_annotation(func)
    return ()


def _warn_if_has_return_annotation(func: Callable) -> None:
    """Emit warning if function has a non-None return type annotation."""
    try:
        hints = get_type_hints(func)
    except Exception:
        # get_type_hints can fail on some edge cases, skip warning
        return

    return_hint = hints.get("return")
    if return_hint is None or return_hint is type(None):
        return

    warnings.warn(
        f"Function '{func.__name__}' has return type '{return_hint}' but no output_name. "
        f"If you want to capture the return value, use @node(output_name='...'). "
        f"Otherwise, ignore this warning for side-effect only nodes.",
        UserWarning,
        stacklevel=4,  # Caller → _resolve_outputs → _warn_if_has_return_annotation
    )


def _build_forward_rename_map(rename_history: list) -> dict[str, str]:
    """Build a forward rename map: original_param -> current_name.

    Handles chained renames correctly:
    - Sequential calls: a->x then x->z → {a: z}
    - Parallel renames (same batch): x->y, y->z → {x: y, y: z}

    Args:
        rename_history: List of RenameEntry objects

    Returns:
        Dict mapping original names to their final current names
    """
    input_entries = [e for e in rename_history if e.kind == "inputs"]
    if not input_entries:
        return {}

    # Group entries by batch_id
    batches: dict[int | None, list] = {}
    for entry in input_entries:
        batches.setdefault(entry.batch_id, []).append(entry)

    rename_map: dict[str, str] = {}

    # Process batches in order (by first occurrence in history)
    for batch_id in dict.fromkeys(e.batch_id for e in input_entries):
        batch_entries = batches[batch_id]
        # For parallel renames (same batch), compute using map state BEFORE this batch
        batch_updates = {}
        for entry in batch_entries:
            # Find original: look for existing mapping where value == entry.old
            original = next(
                (k for k, v in rename_map.items() if v == entry.old),
                entry.old,
            )
            batch_updates[original] = entry.new
        # Apply all updates from this batch at once
        rename_map.update(batch_updates)

    return rename_map


class FunctionNode(HyperNode):
    """Wraps a Python function as a graph node.

    Created via the @node decorator or FunctionNode() constructor.
    Supports all four execution modes: sync, async, sync generator,
    and async generator.

    Attributes:
        name: Public node name (default: func.__name__)
        inputs: Input parameter names from function signature
        outputs: Output value names (empty tuple if no output_name)
        func: The wrapped function
        cache: Whether to cache results (default: False)

    Properties:
        definition_hash: SHA256 hash of function source (cached)
        is_async: True if async def or async generator
        is_generator: True if yields values

    Example:
        >>> @node(output_name="doubled")
        ... def double(x: int) -> int:
        ...     return x * 2
        >>> double.inputs
        ('x',)
        >>> double.outputs
        ('doubled',)
        >>> double(5)
        10

        >>> @node  # Side-effect only, no output
        ... def log(msg: str) -> None:
        ...     print(msg)
        >>> log.outputs
        ()
    """

    func: Callable
    _cache: bool
    _definition_hash: str
    _is_async: bool
    _is_generator: bool

    def __init__(
        self,
        source: Callable | FunctionNode,
        name: str | None = None,
        output_name: str | tuple[str, ...] | None = None,
        *,
        rename_inputs: dict[str, str] | None = None,
        cache: bool = False,
    ) -> None:
        """Wrap a function as a node.

        Args:
            source: Function to wrap, or existing FunctionNode (extracts .func)
            name: Public node name (default: func.__name__)
            output_name: Name(s) for output value(s). If None, outputs = ()
                         (side-effect only node).
            rename_inputs: Mapping to rename inputs {old: new}
            cache: Whether to cache results (default: False)

        Warning:
            If the function has a return type annotation but no output_name
            is provided, a warning is emitted. This helps catch cases where
            the user forgot to specify output_name for a function that
            returns a value.

        Note:
            When source is a FunctionNode, only source.func is extracted.
            All other configuration (name, outputs, renames, cache) from
            the source node is ignored - the new node is built fresh.
        """
        # Extract func if source is FunctionNode
        func = source.func if isinstance(source, FunctionNode) else source

        self.func = func
        self._cache = cache
        self._definition_hash = hash_definition(func)

        # Core HyperNode attributes
        self.name = name or func.__name__
        self.outputs = _resolve_outputs(func, output_name)

        inputs = tuple(inspect.signature(func).parameters.keys())
        self.inputs, self._rename_history = _apply_renames(
            inputs, rename_inputs, "inputs"
        )

        # Auto-detect execution mode
        self._is_async = inspect.iscoroutinefunction(
            func
        ) or inspect.isasyncgenfunction(func)
        self._is_generator = inspect.isgeneratorfunction(
            func
        ) or inspect.isasyncgenfunction(func)

    @property
    def definition_hash(self) -> str:
        """SHA256 hash of function source (cached at creation)."""
        return self._definition_hash

    @property
    def is_async(self) -> bool:
        """True if requires await (async def or async generator)."""
        return self._is_async

    @property
    def is_generator(self) -> bool:
        """True if yields multiple values (sync or async generator)."""
        return self._is_generator

    @property
    def cache(self) -> bool:
        """Whether results should be cached."""
        return self._cache

    @property
    def defaults(self) -> dict[str, Any]:
        """Default values for input parameters (using current/renamed names).

        Returns dict mapping current input names to their default values.
        If inputs have been renamed, uses the renamed names as keys.
        """
        sig = inspect.signature(self.func)

        # Build rename map with batch-aware chaining
        rename_map = _build_forward_rename_map(self._rename_history)

        return {
            rename_map.get(name, name): param.default
            for name, param in sig.parameters.items()
            if param.default is not inspect.Parameter.empty
        }

    @property
    def parameter_annotations(self) -> dict[str, Any]:
        """Type annotations for input parameters.

        Returns:
            dict mapping parameter names (using current/renamed input names) to their
            type annotations. Only includes parameters that have annotations.
            Returns empty dict if get_type_hints fails (e.g., forward references).

        Example:
            >>> @node(output_name="result")
            ... def add(x: int, y: str) -> float: return 0.0
            >>> add.parameter_annotations
            {'x': int, 'y': str}
        """
        try:
            hints = get_type_hints(self.func)
        except Exception:
            # get_type_hints can fail on forward references, etc.
            return {}

        # Get original parameter names from function signature
        sig = inspect.signature(self.func)
        original_params = list(sig.parameters.keys())

        # Build transitive rename mapping with batch-aware chaining
        rename_map = _build_forward_rename_map(self._rename_history)

        result: dict[str, Any] = {}
        for orig_param in original_params:
            if orig_param in hints:
                # Use renamed name if it exists, otherwise original
                final_name = rename_map.get(orig_param, orig_param)
                result[final_name] = hints[orig_param]

        return result

    @property
    def output_annotation(self) -> dict[str, Any]:
        """Type annotations for output values.

        Returns:
            dict mapping output names to their type annotations.
            - For single output: maps output_name to return type
            - For multiple outputs with tuple return: maps each output to
              corresponding tuple element type (using typing.get_args)
            - Returns empty dict if no outputs or no return annotation

        Example:
            >>> @node(output_name="result")
            ... def add(x: int, y: int) -> float: return 0.0
            >>> add.output_annotation
            {'result': float}

            >>> @node(output_name=("a", "b"))
            ... def split(x: str) -> tuple[int, str]: return (0, "")
            >>> split.output_annotation
            {'a': int, 'b': str}
        """
        if not self.outputs:
            return {}

        try:
            hints = get_type_hints(self.func)
        except Exception:
            return {}

        return_hint = hints.get("return")
        if return_hint is None:
            return {}

        # Single output case
        if len(self.outputs) == 1:
            return {self.outputs[0]: return_hint}

        # Multiple outputs - try to extract tuple element types
        from typing import get_args, get_origin

        origin = get_origin(return_hint)
        if origin is tuple:
            args = get_args(return_hint)
            if len(args) == len(self.outputs):
                return dict(zip(self.outputs, args))

        # Can't map tuple elements to outputs - return empty
        return {}

    # === Override base class capability methods ===

    def has_default_for(self, param: str) -> bool:
        """Check if this parameter has a default value.

        Args:
            param: Input parameter name (using current/renamed name)

        Returns:
            True if parameter has a default value.
        """
        return param in self.defaults

    def get_default_for(self, param: str) -> Any:
        """Get default value for a parameter.

        Args:
            param: Input parameter name (using current/renamed name)

        Returns:
            The default value.

        Raises:
            KeyError: If parameter has no default.
        """
        defaults = self.defaults
        if param not in defaults:
            raise KeyError(f"No default for '{param}'")
        return defaults[param]

    def get_input_type(self, param: str) -> type | None:
        """Get type annotation for an input parameter.

        Args:
            param: Input parameter name (using current/renamed name)

        Returns:
            The type annotation, or None if not annotated.
        """
        return self.parameter_annotations.get(param)

    def get_output_type(self, output: str) -> type | None:
        """Get type annotation for an output.

        Args:
            output: Output value name

        Returns:
            The type annotation, or None if not annotated.
        """
        return self.output_annotation.get(output)

    def map_inputs_to_params(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Map renamed input names back to original function parameter names.

        Handles chained renames: if a->x->z (via separate calls), z maps to a.
        Handles parallel renames: if x->y, y->z (same call), they don't chain.

        Args:
            inputs: Dict with current (potentially renamed) input names as keys

        Returns:
            Dict with original function parameter names as keys
        """
        reverse_map = build_reverse_rename_map(self._rename_history, "inputs")
        if not reverse_map:
            return inputs

        return {reverse_map.get(key, key): value for key, value in inputs.items()}

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Call the wrapped function directly.

        Delegates to self.func(*args, **kwargs).
        """
        return self.func(*args, **kwargs)

    def __repr__(self) -> str:
        """Informative string representation.

        Shows original function name and current node configuration.
        If renamed, shows "original as 'new_name'".

        Examples:
            FunctionNode(process, outputs=('result',))
            FunctionNode(process as 'preprocessor', outputs=('result',))
        """
        original = self.func.__name__

        if self.name == original:
            return f"FunctionNode({self.name}, outputs={self.outputs})"
        else:
            return f"FunctionNode({original} as '{self.name}', outputs={self.outputs})"


def node(
    source: Callable | None = None,
    output_name: str | tuple[str, ...] | None = None,
    *,
    rename_inputs: dict[str, str] | None = None,
    cache: bool = False,
) -> FunctionNode | Callable[[Callable], FunctionNode]:
    """Decorator to wrap a function as a FunctionNode.

    Can be used with or without parentheses:

        @node  # Side-effect only node, outputs = ()
        def log(x): ...

        @node(output_name="result")  # Node with output
        def process(x): ...

    Args:
        source: The function to wrap (when used without parens)
        output_name: Name(s) for output value(s). If None, outputs = ()
                     (side-effect only node).
        rename_inputs: Mapping to rename inputs {old: new}
        cache: Whether to cache results (default: False)

    Returns:
        FunctionNode if source provided, else decorator function.

    Note:
        The decorator always uses func.__name__ as the node name.
        To customize the name, use FunctionNode() constructor directly.

    Warning:
        If the function has a return type annotation but no output_name
        is provided, a warning is emitted to help catch mistakes.
    """

    def decorator(func: Callable) -> FunctionNode:
        # Delegates to FunctionNode - warning logic is in FunctionNode.__init__
        return FunctionNode(
            source=func,
            name=None,  # Always use func.__name__ (handled by FunctionNode)
            output_name=output_name,
            rename_inputs=rename_inputs,
            cache=cache,
        )

    if source is not None:
        # Used without parentheses: @node
        return decorator(source)
    # Used with parentheses: @node(...)
    return decorator
