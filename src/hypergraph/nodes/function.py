"""FunctionNode - wraps Python functions as graph nodes."""

from __future__ import annotations

import inspect
import warnings
from typing import Any, Callable, get_type_hints

from hypergraph._utils import ensure_tuple, hash_definition
from hypergraph.nodes._rename import _apply_renames
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
    if output_name:
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
    cache: bool
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
        self.cache = cache
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
