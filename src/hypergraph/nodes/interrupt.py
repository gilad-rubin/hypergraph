"""InterruptNode — declarative pause point for human-in-the-loop workflows."""

from __future__ import annotations

import functools
import hashlib
import inspect
import keyword
from typing import Any, Callable, get_type_hints

from hypergraph._utils import ensure_tuple, hash_definition
from hypergraph.nodes._callable import _build_forward_rename_map
from hypergraph.nodes._rename import (
    RenameEntry,
    _apply_renames,
    build_reverse_rename_map,
)
from hypergraph.nodes.base import HyperNode


class InterruptNode(HyperNode):
    """A declarative pause point that surfaces a value and waits for a response.

    InterruptNode has one or more inputs (values shown to the user) and one or
    more outputs (where the user's responses are written). It never caches results.

    Can be created two ways:

    1. Via the ``@interrupt`` decorator (preferred for handler-backed nodes)::

        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            return "auto-approved"      # returns value → auto-resolve
            # return None               # returns None → pause

    2. Via the class constructor (for handler-less pause points)::

        approval = InterruptNode(
            name="approval",
            input_param="draft",
            output_param="decision",
        )

    When created via decorator, the function IS the handler:
    - Returning a value → auto-resolves the interrupt
    - Returning None → pauses for human input
    - The function signature defines inputs; ``output_name`` defines outputs
    - Type annotations are used for input/output types
    - Function defaults work as node defaults

    Args:
        name: Node name
        input_param: Name(s) of the input parameter(s). Can be a single string
            or a tuple of strings for multiple inputs.
        output_param: Name(s) of the output parameter(s). Can be a single string
            or a tuple of strings for multiple outputs.
        response_type: Optional type annotation for the response value(s).
            For single output: a type. For multiple outputs: a dict mapping
            output names to types. Ignored when created via decorator (uses
            return annotation instead).
        handler: Optional callable to auto-resolve the interrupt.
            For single input: accepts a single positional argument (the input value).
            For multiple inputs: accepts a dict mapping input names to values.
            Returns the response value (single output) or dict of values (multi output).
            May be sync or async.
    """

    def __init__(
        self,
        name: str,
        *,
        input_param: str | tuple[str, ...],
        output_param: str | tuple[str, ...],
        response_type: type | dict[str, type] | None = None,
        handler: Callable[..., Any] | None = None,
    ) -> None:
        """Create an InterruptNode via the class constructor.

        The handler, if provided, must accept either a single value (for single
        input) or a dict of values (for multiple inputs). It may be sync or async.

        Raises:
            ValueError: If any input_param or output_param is not a valid
                Python identifier or is a reserved keyword.
        """
        self.inputs = ensure_tuple(input_param)
        self.outputs = ensure_tuple(output_param)

        for p in self.inputs:
            _validate_param_name(p, "input_param")
        for p in self.outputs:
            _validate_param_name(p, "output_param")

        self.name = name
        self.response_type = response_type
        self.func = handler
        self._rename_history: list[RenameEntry] = []
        # Class-constructor nodes use the legacy positional calling convention
        self._use_kwargs = False

    # ── Decorator-based construction ──

    @classmethod
    def _from_func(
        cls,
        func: Callable,
        output_name: str | tuple[str, ...],
        rename_inputs: dict[str, str] | None = None,
    ) -> InterruptNode:
        """Create an InterruptNode from a decorated function.

        The function IS the handler. Inputs come from the function signature,
        outputs from ``output_name``, and types from annotations.
        """
        self = object.__new__(cls)

        self.func = func
        self._use_kwargs = True

        # Derive outputs
        self.outputs = ensure_tuple(output_name)
        for p in self.outputs:
            _validate_param_name(p, "output_name")

        # Derive inputs from function signature
        sig_inputs = tuple(inspect.signature(func).parameters.keys())
        self.inputs, self._rename_history = _apply_renames(
            sig_inputs, rename_inputs, "inputs"
        )

        self.name = func.__name__

        # Derive response_type from return annotation
        try:
            hints = get_type_hints(func)
            self.response_type = hints.get("return")
        except Exception:
            self.response_type = None

        return self

    # ── Backward-compat properties ──

    @property
    def handler(self) -> Callable[..., Any] | None:
        """The handler callable (backward compat for ``self.func``)."""
        return self.func

    @handler.setter
    def handler(self, value: Callable[..., Any] | None) -> None:
        self.func = value

    @property
    def input_param(self) -> str:
        """The first input parameter name (backward compat)."""
        return self.inputs[0]

    @property
    def output_param(self) -> str:
        """The first output parameter name (backward compat)."""
        return self.outputs[0]

    @property
    def is_multi_input(self) -> bool:
        """Whether this node has multiple inputs."""
        return len(self.inputs) > 1

    @property
    def is_multi_output(self) -> bool:
        """Whether this node has multiple outputs."""
        return len(self.outputs) > 1

    # ── Capabilities ──

    @property
    def cache(self) -> bool:
        """InterruptNodes never cache."""
        return False

    @property
    def definition_hash(self) -> str:
        """Hash based on function source (decorator) or metadata (class constructor)."""
        if self._use_kwargs and self.func is not None:
            return hash_definition(self.func)
        rt = _response_type_label(self.response_type)
        content = f"InterruptNode:{self.name}:{self.inputs}:{self.outputs}:{rt}"
        return hashlib.sha256(content.encode()).hexdigest()

    # ── Defaults and type introspection (for decorator-created nodes) ──

    @functools.cached_property
    def defaults(self) -> dict[str, Any]:
        """Default values for input parameters (using current/renamed names)."""
        if self.func is None or not self._use_kwargs:
            return {}
        sig = inspect.signature(self.func)
        rename_map = _build_forward_rename_map(self._rename_history)
        return {
            rename_map.get(name, name): param.default
            for name, param in sig.parameters.items()
            if param.default is not inspect.Parameter.empty
        }

    @functools.cached_property
    def parameter_annotations(self) -> dict[str, Any]:
        """Type annotations for input parameters."""
        if self.func is None or not self._use_kwargs:
            return {}
        try:
            hints = get_type_hints(self.func)
        except Exception:
            return {}
        sig = inspect.signature(self.func)
        rename_map = _build_forward_rename_map(self._rename_history)
        return {
            rename_map.get(name, name): hints[name]
            for name in sig.parameters
            if name in hints
        }

    def has_default_for(self, param: str) -> bool:
        """Check if this parameter has a default value."""
        return param in self.defaults

    def get_default_for(self, param: str) -> Any:
        """Get default value for a parameter."""
        defaults = self.defaults
        if param not in defaults:
            raise KeyError(f"No default for '{param}'")
        return defaults[param]

    def get_input_type(self, param: str) -> type | None:
        """Get type annotation for an input parameter."""
        return self.parameter_annotations.get(param)

    def get_output_type(self, param: str) -> type | None:
        """Return the type for an output parameter.

        For decorator-created nodes: uses return annotation.
        For class-constructor nodes: uses response_type.
        """
        if param not in self.outputs:
            return None
        # Decorator-created: use return annotation
        if self._use_kwargs and self.func is not None:
            return self._output_annotation.get(param)
        # Class-constructor: use response_type
        if isinstance(self.response_type, dict):
            return self.response_type.get(param)
        if param == self.outputs[0]:
            return self.response_type
        return None

    @functools.cached_property
    def _output_annotation(self) -> dict[str, Any]:
        """Output type annotations derived from function return hint."""
        if self.func is None:
            return {}
        try:
            hints = get_type_hints(self.func)
        except Exception:
            return {}
        return_hint = hints.get("return")
        if return_hint is None:
            return {}
        if len(self.outputs) == 1:
            return {self.outputs[0]: return_hint}
        # Multi-output: try tuple element types
        from typing import get_args, get_origin

        if get_origin(return_hint) is tuple:
            args = get_args(return_hint)
            if len(args) == len(self.outputs):
                return dict(zip(self.outputs, args))
        return {}

    def map_inputs_to_params(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Map renamed input names back to original function parameter names."""
        if not self._use_kwargs:
            return inputs
        reverse_map = build_reverse_rename_map(self._rename_history, "inputs")
        if not reverse_map:
            return inputs
        return {reverse_map.get(key, key): value for key, value in inputs.items()}

    # ── Calling ──

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Call the handler function directly (for testing)."""
        if self.func is None:
            raise TypeError(
                f"InterruptNode '{self.name}' has no handler function. "
                f"Use @interrupt decorator or pass handler= to constructor."
            )
        return self.func(*args, **kwargs)

    # ── Immutable transformations ──

    def with_handler(self, handler: Callable[..., Any]) -> InterruptNode:
        """Return a new InterruptNode with the given handler (immutable).

        Args:
            handler: Callable that resolves the interrupt automatically.

        Returns:
            A new InterruptNode instance with the handler attached.
        """
        clone = self._copy()
        clone.func = handler
        return clone

    def __repr__(self) -> str:
        if self.func is not None and self._use_kwargs:
            original = self.func.__name__
            if self.name == original:
                return f"InterruptNode({self.name}, outputs={self.outputs})"
            return f"InterruptNode({original} as '{self.name}', outputs={self.outputs})"
        return f"InterruptNode('{self.name}', inputs={self.inputs}, outputs={self.outputs})"


def interrupt(
    output_name: str | tuple[str, ...],
    *,
    rename_inputs: dict[str, str] | None = None,
) -> Callable[[Callable], InterruptNode]:
    """Decorator to create an InterruptNode from a function.

    The function IS the handler:
    - Returning a value → auto-resolves the interrupt
    - Returning None → pauses for human input

    Inputs come from the function signature. Outputs from ``output_name``.
    Types from annotations.

    Args:
        output_name: Name(s) for output value(s).
        rename_inputs: Mapping to rename inputs {old: new}

    Returns:
        Decorator that creates an InterruptNode.

    Examples::

        # Auto-resolving handler
        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            return "auto-approved"

        # Pause point (returns None)
        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            ...

        # Conditional: sometimes resolve, sometimes pause
        @interrupt(output_name="decision")
        def approval(draft: str) -> str | None:
            if "LGTM" in draft:
                return "auto-approved"
            return None  # pause for human review

        # Test the handler directly
        assert approval.func("my draft") == "auto-approved"
        assert approval("my draft") == "auto-approved"
    """

    def decorator(func: Callable) -> InterruptNode:
        return InterruptNode._from_func(
            func,
            output_name=output_name,
            rename_inputs=rename_inputs,
        )

    return decorator


# ── Helpers ──


def _response_type_label(response_type: type | dict[str, type] | None) -> str:
    """Return a stable string label for a response type, handling union types and dicts."""
    if response_type is None:
        return "None"
    if isinstance(response_type, dict):
        # Sort by key for stable ordering
        items = sorted(response_type.items())
        return "{" + ",".join(f"{k}:{_single_type_label(v)}" for k, v in items) + "}"
    return _single_type_label(response_type)


def _single_type_label(t: type) -> str:
    """Return a stable string label for a single type."""
    qualname = getattr(t, "__qualname__", None)
    if qualname is None:
        qualname = repr(t)
    module = getattr(t, "__module__", None)
    return f"{module}.{qualname}" if module else qualname


def _validate_param_name(name: str, label: str) -> None:
    """Validate that a parameter name is a valid Python identifier and not a keyword."""
    if not name.isidentifier():
        raise ValueError(f"{label} must be a valid Python identifier, got {name!r}")
    if keyword.iskeyword(name):
        raise ValueError(f"{label} must not be a Python keyword, got {name!r}")
