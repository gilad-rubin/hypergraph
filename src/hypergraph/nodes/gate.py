"""Gate nodes for control flow routing in graphs.

This module provides:
- END: Sentinel indicating execution should terminate
- GateNode: Abstract base for routing logic
- RouteNode: Concrete gate that routes to target nodes
- route: Decorator for creating RouteNode from a function
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, TypeVar

from hypergraph._utils import hash_definition
from hypergraph.nodes._rename import _apply_renames, build_reverse_rename_map
from hypergraph.nodes.base import HyperNode

# =============================================================================
# END Sentinel
# =============================================================================


class _ENDMeta(type):
    """Metaclass for END sentinel to prevent instantiation and provide clean repr."""

    def __repr__(cls) -> str:
        return "END"

    def __str__(cls) -> str:
        return "END"

    def __call__(cls, *args, **kwargs):
        raise TypeError("END cannot be instantiated. Use END directly as a sentinel.")


class END(metaclass=_ENDMeta):
    """Sentinel class indicating execution should terminate along this path.

    Use END in route targets to indicate a path terminates:

        @route(targets=["process", END])
        def decide(x):
            return END if x == 0 else "process"

    Note: END is a class, not an instance. Use it directly (END, not END()).
    """

    pass


# Type alias for targets parameter (list or dict with descriptions)
TargetsSpec = list[str | type[END]] | dict[str | type[END], str]


# =============================================================================
# GateNode Base Class
# =============================================================================


class GateNode(HyperNode):
    """Abstract base class for routing/control flow nodes.

    Gate nodes make routing decisions but do not produce data outputs.
    They control which downstream nodes execute via control edges.

    Subclasses must set these attributes in __init__:
    - name: str
    - inputs: tuple[str, ...]
    - outputs: tuple[str, ...] (always empty for gates)
    - targets: list[str | type[END]]
    - descriptions: dict[str | type[END], str]
    - _rename_history: list[RenameEntry]
    """

    targets: list[str | type[END]]
    descriptions: dict[str | type[END], str]
    _cache: bool

    @property
    def cache(self) -> bool:
        """Whether results should be cached. Default False for gates."""
        return self._cache


# =============================================================================
# RouteNode Implementation
# =============================================================================


_T = TypeVar("_T", bound="RouteNode")


class RouteNode(GateNode):
    """Routes execution to target nodes based on a routing function's return value.

    A RouteNode executes a function that returns the name of the target node(s)
    to activate. Targets not returned are not executed.

    Attributes:
        name: Node name (default: func.__name__)
        inputs: Input parameter names from function signature
        outputs: Always empty tuple (gates produce no data)
        targets: List of valid target names (or END)
        descriptions: Optional descriptions for visualization
        fallback: Default target if function returns None
        multi_target: If True, function returns list of targets to run in parallel

    Example:
        >>> @route(targets=["process_a", "process_b", END])
        ... def decide(x: int) -> str:
        ...     if x == 0:
        ...         return END
        ...     return "process_a" if x > 0 else "process_b"
    """

    func: Callable
    fallback: str | type[END] | None
    multi_target: bool
    _definition_hash: str

    def __init__(
        self,
        func: Callable[..., str | type[END] | list[str | type[END]] | None],
        targets: TargetsSpec,
        *,
        fallback: str | type[END] | None = None,
        multi_target: bool = False,
        cache: bool = False,
        name: str | None = None,
        rename_inputs: dict[str, str] | None = None,
    ) -> None:
        """Create a RouteNode from a routing function.

        Args:
            func: Function that returns target name(s) or END
            targets: Valid targets (list or dict with descriptions)
            fallback: Default target if func returns None (incompatible with multi_target)
            multi_target: If True, func returns list of targets to run in parallel
            cache: Whether to cache routing decisions (default: False)
            name: Node name (default: func.__name__)
            rename_inputs: Mapping to rename inputs {old: new}

        Raises:
            TypeError: If func is async or generator
            ValueError: If targets is empty
            ValueError: If fallback and multi_target are both set
        """
        # Validate routing function type
        if asyncio.iscoroutinefunction(func):
            raise TypeError(
                f"Routing function '{func.__name__}' cannot be async.\n\n"
                f"Routing decisions should be fast and based on already-computed values.\n\n"
                f"How to fix: Move async logic to a FunctionNode before the gate"
            )
        if inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(func):
            raise TypeError(
                f"Routing function '{func.__name__}' cannot be a generator.\n\n"
                f"Routing functions must return a single decision, not yield multiple.\n\n"
                f"How to fix: Return a single target name or list of targets"
            )

        # Normalize targets from list or dict
        if isinstance(targets, dict):
            target_list = list(targets.keys())
            descriptions = dict(targets)
        else:
            target_list = list(targets)
            descriptions = {}

        # Validate targets
        if not target_list:
            raise ValueError(
                f"RouteNode '{func.__name__}' must have at least one target.\n\n"
                f"How to fix: Provide a non-empty targets list"
            )

        # Deduplicate targets (preserve order)
        seen: set[str | type[END]] = set()
        unique_targets: list[str | type[END]] = []
        for t in target_list:
            if t not in seen:
                seen.add(t)
                unique_targets.append(t)
        target_list = unique_targets

        # Validate fallback compatibility
        if fallback is not None and multi_target:
            raise ValueError(
                f"RouteNode '{func.__name__}' cannot have both fallback and multi_target=True.\n\n"
                f"With multi_target=True, return an empty list [] instead of None.\n\n"
                f"How to fix: Remove fallback or set multi_target=False"
            )

        # Add fallback to targets if not already present
        if fallback is not None and fallback not in target_list:
            target_list.append(fallback)

        self.func = func
        self.targets = target_list
        self.descriptions = descriptions
        self.fallback = fallback
        self.multi_target = multi_target
        self._cache = cache
        self._definition_hash = hash_definition(func)

        # Core HyperNode attributes
        self.name = name or func.__name__
        self.outputs = ()  # Gates produce no data outputs

        inputs = tuple(inspect.signature(func).parameters.keys())
        self.inputs, self._rename_history = _apply_renames(
            inputs, rename_inputs, "inputs"
        )

    @property
    def definition_hash(self) -> str:
        """SHA256 hash of routing function source (cached at creation)."""
        return self._definition_hash

    @property
    def is_async(self) -> bool:
        """Route functions must be sync, so always False."""
        return False

    @property
    def is_generator(self) -> bool:
        """Route functions must not be generators, so always False."""
        return False

    def has_default_for(self, param: str) -> bool:
        """Check if this parameter has a default value."""
        sig = inspect.signature(self.func)
        # Handle renamed inputs
        original_param = self._get_original_param_name(param)
        if original_param in sig.parameters:
            return sig.parameters[original_param].default is not inspect.Parameter.empty
        return False

    def get_default_for(self, param: str) -> Any:
        """Get default value for a parameter."""
        sig = inspect.signature(self.func)
        original_param = self._get_original_param_name(param)
        if original_param in sig.parameters:
            default = sig.parameters[original_param].default
            if default is not inspect.Parameter.empty:
                return default
        raise KeyError(f"No default for '{param}'")

    def _get_original_param_name(self, current_name: str) -> str:
        """Map a current input name back to the original function parameter."""
        reverse_map = build_reverse_rename_map(self._rename_history, "inputs")
        return reverse_map.get(current_name, current_name)

    def map_inputs_to_params(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Map renamed input names back to original function parameter names.

        Args:
            inputs: Dict with current (potentially renamed) input names as keys

        Returns:
            Dict with original function parameter names as keys
        """
        reverse_map = build_reverse_rename_map(self._rename_history, "inputs")
        if not reverse_map:
            return inputs

        return {reverse_map.get(key, key): value for key, value in inputs.items()}

    def __call__(self, *args: Any, **kwargs: Any) -> str | type[END] | list | None:
        """Call the routing function directly."""
        return self.func(*args, **kwargs)

    def __repr__(self) -> str:
        """Informative string representation."""
        original = self.func.__name__
        targets_str = [str(t) if t is END else t for t in self.targets]
        if self.name == original:
            return f"RouteNode({self.name}, targets={targets_str})"
        return f"RouteNode({original} as '{self.name}', targets={targets_str})"


# =============================================================================
# @route Decorator
# =============================================================================


def route(
    targets: TargetsSpec,
    *,
    fallback: str | type[END] | None = None,
    multi_target: bool = False,
    cache: bool = False,
    name: str | None = None,
    rename_inputs: dict[str, str] | None = None,
) -> Callable[[Callable], RouteNode]:
    """Decorator to create a RouteNode from a routing function.

    The decorated function should return:
    - A target name (str) to activate that node
    - END to terminate execution along this path
    - None to use fallback (if set) or do nothing
    - A list of targets if multi_target=True

    Args:
        targets: Valid target names (list or dict with descriptions)
        fallback: Default target if function returns None
        multi_target: If True, function returns list of targets
        cache: Whether to cache routing decisions
        name: Node name (default: func.__name__)
        rename_inputs: Mapping to rename inputs {old: new}

    Returns:
        Decorator that creates a RouteNode

    Example:
        >>> @route(targets=["process", END])
        ... def decide(x: int) -> str:
        ...     return "process" if x > 0 else END

        >>> @route(targets={"fast": "Quick path", "slow": "Thorough path"})
        ... def choose_path(complexity: int) -> str:
        ...     return "fast" if complexity < 5 else "slow"
    """

    def decorator(func: Callable) -> RouteNode:
        return RouteNode(
            func,
            targets=targets,
            fallback=fallback,
            multi_target=multi_target,
            cache=cache,
            name=name,
            rename_inputs=rename_inputs,
        )

    return decorator
