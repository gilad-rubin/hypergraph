"""Gate nodes for control flow routing in graphs.

This module provides:
- GateNode: Abstract base for routing logic
- RouteNode: Concrete gate that routes to target nodes by name
- IfElseNode: Binary gate that routes based on boolean decision
- route: Decorator for creating RouteNode from a function
- ifelse: Decorator for creating IfElseNode from a function
- END: Sentinel indicating execution should terminate
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, TypeVar

from hypergraph._utils import hash_definition
from hypergraph.nodes._rename import _apply_renames, build_reverse_rename_map
from hypergraph.nodes.base import HyperNode


# =============================================================================
# Validation Helpers
# =============================================================================


def _validate_routing_func(func: Callable, node_type: str) -> None:
    """Validate routing function is sync and not generator.

    Args:
        func: The routing function to validate
        node_type: Name of the node type for error messages

    Raises:
        TypeError: If func is async or generator
    """
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


def _validate_not_string_end(target: str | type[END], func_name: str) -> None:
    """Reject string 'END' as target (easily confused with END sentinel).

    Args:
        target: The target to validate
        func_name: Function name for error messages

    Raises:
        ValueError: If target is the string "END"
    """
    if target == "END":
        raise ValueError(
            f"Gate '{func_name}' has 'END' as a string target.\n\n"
            f"The string 'END' is not allowed because it's easily confused "
            f"with the END sentinel.\n\n"
            f"How to fix: Use 'from hypergraph import END' and use END directly, "
            f"or rename your target to something else."
        )


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
    - descriptions: dict[...key..., str] (key type varies by subclass)
    - func: Callable (the routing function)
    - _definition_hash: str
    - _rename_history: list[RenameEntry]
    """

    targets: list[str | type[END]]
    descriptions: dict[str | type[END] | bool, str]
    func: Callable
    _definition_hash: str
    _cache: bool

    @property
    def cache(self) -> bool:
        """Whether results should be cached. Default False for gates."""
        return self._cache

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

    fallback: str | type[END] | None
    multi_target: bool

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
        _validate_routing_func(func, "RouteNode")

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

        # Reject "END" string as target
        for t in target_list:
            _validate_not_string_end(t, func.__name__)

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


# =============================================================================
# IfElseNode Implementation
# =============================================================================


class IfElseNode(GateNode):
    """Binary gate that routes based on boolean decision.

    An IfElseNode executes a function that returns True or False,
    routing to `when_true` or `when_false` targets accordingly.

    This is syntactic sugar for the common if/else branching pattern.
    For more complex routing (multiple targets, fallback, etc.), use RouteNode.

    Attributes:
        name: Node name (default: func.__name__)
        inputs: Input parameter names from function signature
        outputs: Always empty tuple (gates produce no data)
        targets: [when_true, when_false] targets
        descriptions: Fixed {True: "True", False: "False"}
        when_true: Target when function returns True
        when_false: Target when function returns False

    Example:
        >>> @ifelse(when_true="process", when_false="skip")
        ... def is_valid(data: dict) -> bool:
        ...     return data.get("valid", False)
    """

    when_true: str | type[END]
    when_false: str | type[END]

    def __init__(
        self,
        func: Callable[..., bool],
        when_true: str | type[END],
        when_false: str | type[END],
        *,
        cache: bool = False,
        name: str | None = None,
        rename_inputs: dict[str, str] | None = None,
    ) -> None:
        """Create an IfElseNode from a boolean function.

        Args:
            func: Function that returns True or False
            when_true: Target to activate when func returns True
            when_false: Target to activate when func returns False
            cache: Whether to cache routing decisions (default: False)
            name: Node name (default: func.__name__)
            rename_inputs: Mapping to rename inputs {old: new}

        Raises:
            TypeError: If func is async or generator
            ValueError: If when_true == when_false
            ValueError: If when_true or when_false is string "END"
        """
        _validate_routing_func(func, "IfElseNode")

        # Reject string "END" as target
        _validate_not_string_end(when_true, func.__name__)
        _validate_not_string_end(when_false, func.__name__)

        # Validate targets are different
        if when_true == when_false:
            raise ValueError(
                f"IfElseNode '{func.__name__}' has the same target for both branches.\n\n"
                f"when_true={when_true!r} == when_false={when_false!r}\n\n"
                f"How to fix: Use different targets for True and False branches, "
                f"or use a regular FunctionNode if no branching is needed"
            )

        self.func = func
        self.when_true = when_true
        self.when_false = when_false
        self.targets = [when_true, when_false]
        self.descriptions = {True: "True", False: "False"}
        self._cache = cache
        self._definition_hash = hash_definition(func)

        # Core HyperNode attributes
        self.name = name or func.__name__
        self.outputs = ()  # Gates produce no data outputs

        inputs = tuple(inspect.signature(func).parameters.keys())
        self.inputs, self._rename_history = _apply_renames(
            inputs, rename_inputs, "inputs"
        )

    def __call__(self, *args: Any, **kwargs: Any) -> bool:
        """Call the routing function directly."""
        return self.func(*args, **kwargs)

    def __repr__(self) -> str:
        """Informative string representation."""
        original = self.func.__name__
        true_str = str(self.when_true) if self.when_true is END else self.when_true
        false_str = str(self.when_false) if self.when_false is END else self.when_false
        if self.name == original:
            return f"IfElseNode({self.name}, true={true_str}, false={false_str})"
        return f"IfElseNode({original} as '{self.name}', true={true_str}, false={false_str})"


# =============================================================================
# @ifelse Decorator
# =============================================================================


def ifelse(
    when_true: str | type[END],
    when_false: str | type[END],
    *,
    cache: bool = False,
    name: str | None = None,
    rename_inputs: dict[str, str] | None = None,
) -> Callable[[Callable[..., bool]], IfElseNode]:
    """Decorator to create an IfElseNode from a boolean function.

    The decorated function should return True or False. Based on the result:
    - True: Routes to `when_true` target
    - False: Routes to `when_false` target

    For more complex routing (multiple targets, fallback, etc.), use @route.

    Args:
        when_true: Target to activate when function returns True
        when_false: Target to activate when function returns False
        cache: Whether to cache routing decisions
        name: Node name (default: func.__name__)
        rename_inputs: Mapping to rename inputs {old: new}

    Returns:
        Decorator that creates an IfElseNode

    Example:
        >>> @ifelse(when_true="process", when_false="skip")
        ... def is_valid(data: dict) -> bool:
        ...     return data.get("valid", False)

        >>> @ifelse(when_true="continue", when_false=END)
        ... def should_continue(count: int) -> bool:
        ...     return count < 10
    """

    def decorator(func: Callable[..., bool]) -> IfElseNode:
        return IfElseNode(
            func,
            when_true=when_true,
            when_false=when_false,
            cache=cache,
            name=name,
            rename_inputs=rename_inputs,
        )

    return decorator


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
