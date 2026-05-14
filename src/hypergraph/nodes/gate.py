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
from collections.abc import Callable
from typing import Any, TypeVar

from hypergraph._utils import ensure_tuple, hash_definition
from hypergraph.nodes._callable import CallableMixin
from hypergraph.nodes._input_extraction import extract_inputs
from hypergraph.nodes._rename import _apply_renames
from hypergraph.nodes.base import HyperNode, _validate_emit_wait_for

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


def _validate_not_string_end(target: str, func_name: str) -> None:
    """Reject reserved END-like strings as targets.

    Two values are reserved: the human-friendly form "END" (rejected because
    it's easily confused with the END sentinel) and END's hidden underlying
    value (rejected because raw strings that match it would otherwise pass
    set-membership checks while failing identity checks). The actual END
    singleton always passes through unchanged.

    Args:
        target: The target to validate
        func_name: Function name for error messages

    Raises:
        ValueError: If target is the string "END" or the reserved underlying
            value, but is not the END sentinel itself
    """
    if target is END:
        return
    if target == "END" or target == _END_VALUE:
        raise ValueError(
            f"Gate '{func_name}' uses a reserved END-like string target ({target!r}).\n\n"
            f"This string is reserved because it would be confused with the "
            f"END sentinel at runtime.\n\n"
            f"How to fix: Use 'from hypergraph import END' and pass END directly, "
            f"or rename your target to something else."
        )


# =============================================================================
# GateNode Base Class
# =============================================================================


class GateNode(CallableMixin, HyperNode):
    """Abstract base class for routing/control flow nodes.

    Gate nodes make routing decisions but do not produce data outputs.
    They control which downstream nodes execute via control edges.

    Subclasses must set these attributes in __init__:
    - name: str
    - inputs: tuple[str, ...]
    - outputs: tuple[str, ...] (empty or emit-only for gates)
    - targets: list[str]
    - descriptions: dict[...key..., str] (key type varies by subclass)
    - func: Callable (the routing function)
    - _definition_hash: str
    - _rename_history: list[RenameEntry]
    """

    targets: list[str]
    descriptions: dict[str | bool, str]
    func: Callable
    _definition_hash: str
    _cache: bool
    _hide: bool
    _wait_for: tuple[str, ...]
    _emit: tuple[str, ...]
    default_open: bool

    @property
    def cache(self) -> bool:
        """Whether routing function results should be cached."""
        return self._cache

    @property
    def hide(self) -> bool:
        """Whether this node is hidden from visualization."""
        return self._hide

    @property
    def wait_for(self) -> tuple[str, ...]:
        """Ordering-only graph-scope addresses this gate waits for."""
        return self._wait_for

    @property
    def data_outputs(self) -> tuple[str, ...]:
        """Internal routing value output used for checkpoint/resume reconstruction."""
        return (f"_{self.name}",)

    @property
    def is_gate(self) -> bool:
        """Gate nodes route execution flow."""
        return True

    @property
    def is_async(self) -> bool:
        """Route functions must be sync, so always False."""
        return False

    @property
    def is_generator(self) -> bool:
        """Route functions must not be generators, so always False."""
        return False

    @property
    def node_type(self) -> str:
        """Node type for NetworkX representation."""
        return "BRANCH"

    @property
    def branch_data(self) -> dict[str, Any] | None:
        """Branch-specific routing data. Override in subclasses."""
        return None

    @property
    def nx_attrs(self) -> dict[str, Any]:
        """Flattened attributes including branch_data."""
        attrs = super().nx_attrs
        attrs["branch_data"] = self.branch_data
        return attrs


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

    fallback: str | None
    multi_target: bool

    def __init__(
        self,
        func: Callable[..., str | list[str] | None],
        targets: TargetsSpec,
        *,
        fallback: str | None = None,
        multi_target: bool = False,
        cache: bool = False,
        hide: bool = False,
        default_open: bool = True,
        name: str | None = None,
        rename_inputs: dict[str, str] | None = None,
        emit: str | tuple[str, ...] | None = None,
        wait_for: str | tuple[str, ...] | None = None,
    ) -> None:
        """Create a RouteNode from a routing function.

        Args:
            func: Function that returns target name(s) or END
            targets: Valid targets (list or dict with descriptions)
            fallback: Default target if func returns None (incompatible with multi_target)
            multi_target: If True, func returns list of targets to run in parallel
            cache: Whether to cache routing decisions (default: False)
            hide: Whether to hide from visualization (default: False)
            name: Node name (default: func.__name__)
            rename_inputs: Mapping to rename inputs {old: new}
            emit: Ordering-only local output name(s). Auto-produced when gate runs.
            wait_for: Ordering-only graph-scope output/emit address(es). Gate
                      won't run until these values exist and are fresh.

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
            raise ValueError(f"RouteNode '{func.__name__}' must have at least one target.\n\nHow to fix: Provide a non-empty targets list")

        # Reject "END" string as target
        for t in target_list:
            _validate_not_string_end(t, func.__name__)

        # Deduplicate targets (preserve order)
        seen: set[str] = set()
        unique_targets: list[str] = []
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

        # Validate and add fallback to targets if not already present
        if fallback is not None:
            _validate_not_string_end(fallback, func.__name__)
            if fallback not in target_list:
                target_list.append(fallback)

        resolved_name = name or func.__name__
        self.name = resolved_name
        self.func = func
        self.targets = target_list
        self.descriptions = descriptions
        self.fallback = fallback
        self.multi_target = multi_target
        self._cache = cache
        self._hide = hide
        self._emit = ensure_tuple(emit) if emit else ()
        self._wait_for = ensure_tuple(wait_for) if wait_for else ()
        self.default_open = default_open
        self._definition_hash = hash_definition(func)

        # Core HyperNode attributes
        self.outputs = (f"_{self.name}", *self._emit)

        inputs, self._context_param = extract_inputs(func)
        self.inputs, self._rename_history = _apply_renames(inputs, rename_inputs, "inputs")

        _validate_emit_wait_for(
            self.name,
            self._emit,
            self._wait_for,
            (),
            self.inputs,
        )

    def __call__(self, *args: Any, **kwargs: Any) -> str | list | None:
        """Call the routing function directly."""
        return self.func(*args, **kwargs)

    def __repr__(self) -> str:
        """Informative string representation."""
        original = self.func.__name__
        targets_str = [str(t) if t is END else t for t in self.targets]
        if self.name == original:
            return f"RouteNode({self.name}, targets={targets_str})"
        return f"RouteNode({original} as '{self.name}', targets={targets_str})"

    @property
    def branch_data(self) -> dict[str, Any]:
        """Branch-specific data for visualization."""
        return {
            "targets": ["END" if t is END else t for t in self.targets],
            "multi_target": self.multi_target,
        }


# =============================================================================
# @route Decorator
# =============================================================================


def route(
    targets: TargetsSpec,
    *,
    fallback: str | None = None,
    multi_target: bool = False,
    cache: bool = False,
    hide: bool = False,
    default_open: bool = True,
    name: str | None = None,
    rename_inputs: dict[str, str] | None = None,
    emit: str | tuple[str, ...] | None = None,
    wait_for: str | tuple[str, ...] | None = None,
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
        cache: Cache the routing function's return value. On cache hit,
            the runner restores the routing decision without calling the
            function. Requires a cache backend on the runner.
        hide: Whether to hide from visualization (default: False)
        default_open: If True, targets may execute before the gate runs the
            first time. If False, targets are blocked until the gate executes
            and records a decision.
        name: Node name (default: func.__name__)
        rename_inputs: Mapping to rename inputs {old: new}
        emit: Ordering-only local output name(s). Auto-produced when gate runs.
        wait_for: Ordering-only graph-scope output/emit address(es). Gate
                  won't run until these values exist and are fresh.

    Returns:
        Decorator that creates a RouteNode

    Example:
        >>> @route(targets=["process", END])
        ... def decide(x: int) -> str:
        ...     return END if x == 0 else "process"

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
            hide=hide,
            default_open=default_open,
            name=name,
            rename_inputs=rename_inputs,
            emit=emit,
            wait_for=wait_for,
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

    when_true: str
    when_false: str

    def __init__(
        self,
        func: Callable[..., bool],
        when_true: str,
        when_false: str,
        *,
        cache: bool = False,
        hide: bool = False,
        default_open: bool = True,
        name: str | None = None,
        rename_inputs: dict[str, str] | None = None,
        emit: str | tuple[str, ...] | None = None,
        wait_for: str | tuple[str, ...] | None = None,
    ) -> None:
        """Create an IfElseNode from a boolean function.

        Args:
            func: Function that returns True or False
            when_true: Target to activate when func returns True
            when_false: Target to activate when func returns False
            cache: Whether to cache routing decisions (default: False)
            hide: Whether to hide from visualization (default: False)
            default_open: If True, targets may execute before the gate runs
                the first time. If False, targets are blocked until the gate
                executes and records a decision.
            name: Node name (default: func.__name__)
            rename_inputs: Mapping to rename inputs {old: new}
            emit: Ordering-only local output name(s). Auto-produced when gate runs.
            wait_for: Ordering-only graph-scope output/emit address(es). Gate
                      won't run until these values exist and are fresh.

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

        resolved_name = name or func.__name__
        self.name = resolved_name
        self.func = func
        self.when_true = when_true
        self.when_false = when_false
        self.targets = [when_true, when_false]
        self.descriptions = {True: "True", False: "False"}
        self._cache = cache
        self._hide = hide
        self._emit = ensure_tuple(emit) if emit else ()
        self._wait_for = ensure_tuple(wait_for) if wait_for else ()
        self.default_open = default_open
        self._definition_hash = hash_definition(func)

        # Core HyperNode attributes
        self.outputs = (f"_{self.name}", *self._emit)

        inputs, self._context_param = extract_inputs(func)
        self.inputs, self._rename_history = _apply_renames(inputs, rename_inputs, "inputs")

        _validate_emit_wait_for(
            self.name,
            self._emit,
            self._wait_for,
            (),
            self.inputs,
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

    @property
    def branch_data(self) -> dict[str, Any]:
        """Branch-specific data for visualization."""
        return {
            "when_true": "END" if self.when_true is END else self.when_true,
            "when_false": "END" if self.when_false is END else self.when_false,
        }


# =============================================================================
# @ifelse Decorator
# =============================================================================


def ifelse(
    when_true: str,
    when_false: str,
    *,
    cache: bool = False,
    hide: bool = False,
    default_open: bool = True,
    name: str | None = None,
    rename_inputs: dict[str, str] | None = None,
    emit: str | tuple[str, ...] | None = None,
    wait_for: str | tuple[str, ...] | None = None,
) -> Callable[[Callable[..., bool]], IfElseNode]:
    """Decorator to create an IfElseNode from a boolean function.

    The decorated function should return True or False. Based on the result:
    - True: Routes to `when_true` target
    - False: Routes to `when_false` target

    For more complex routing (multiple targets, fallback, etc.), use @route.

    Args:
        when_true: Target to activate when function returns True
        when_false: Target to activate when function returns False
        cache: Cache the routing function's return value. On cache hit,
            the runner restores the routing decision without calling the
            function. Requires a cache backend on the runner.
        hide: Whether to hide from visualization (default: False)
        default_open: If True, targets may execute before the gate runs the
            first time. If False, targets are blocked until the gate executes
            and records a decision.
        name: Node name (default: func.__name__)
        rename_inputs: Mapping to rename inputs {old: new}
        emit: Ordering-only local output name(s). Auto-produced when gate runs.
        wait_for: Ordering-only graph-scope output/emit address(es). Gate
                  won't run until these values exist and are fresh.

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
            hide=hide,
            default_open=default_open,
            name=name,
            rename_inputs=rename_inputs,
            emit=emit,
            wait_for=wait_for,
        )

    return decorator


# =============================================================================
# END Sentinel
# =============================================================================

# Hidden underlying value. Obscure enough that accidental collisions from
# external sources (LLM output, config, deserialization) are extremely unlikely.
_END_VALUE = "__hg_end__"


class _End(str):
    """Singleton str subclass used as the END sentinel.

    END is an instance of this class (a string with value `__hg_end__`) so
    routing functions annotated `-> str` accept `return END` without typing
    pain, while `target is END` still uniquely identifies the sentinel.
    """

    __slots__ = ()

    def __new__(cls) -> _End:
        return super().__new__(cls, _END_VALUE)

    def __repr__(self) -> str:
        return "END"

    def __str__(self) -> str:
        return "END"

    def __reduce__(self) -> tuple:
        return (_resolve_end_singleton, ())


def _resolve_end_singleton() -> _End:
    """Reconstruct the END singleton during unpickling.

    Without this, pickle would route through the default str-subclass path
    (calling `_End("__hg_end__")`), but `_End.__new__` takes no arguments
    so it raises TypeError. Returning the module-level singleton also
    preserves identity (`restored is END`) across pickle / deepcopy /
    multiprocessing round-trips, which gate caching and checkpointing
    rely on for `decision is END` checks to remain correct.
    """
    return END


END: _End = _End()
"""Sentinel indicating execution should terminate along a routing path.

    @route(targets=["process", END])
    def decide(x) -> str:
        return END if x == 0 else "process"

END is a singleton `str` subclass. Use `is END` to test for it; equality
against arbitrary strings is False (its underlying value is intentionally
obscure to prevent collisions).
"""


def _check_no_end_collision(value: Any, gate_name: str) -> None:
    """Reject external strings that string-equal END but aren't the singleton.

    Catches the rare case where an LLM, config file, or deserialized object
    emits the reserved underlying value. Without this, such a value would
    silently terminate the path.
    """
    if isinstance(value, list):
        for item in value:
            _check_no_end_collision(item, gate_name)
        return
    if isinstance(value, str) and value == END and value is not END:
        raise ValueError(
            f"Gate '{gate_name}' returned the string {value!r}, "
            f"but it is not the END sentinel.\n\n"
            f"This usually means an external source (LLM output, config, "
            f"deserialization) emitted the reserved value.\n\n"
            f"How to fix: Use 'from hypergraph import END' and return END directly."
        )


# Type alias for targets parameter (list or dict with descriptions)
TargetsSpec = list[str] | dict[str, str]
