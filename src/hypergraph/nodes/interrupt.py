"""InterruptNode — declarative pause point for human-in-the-loop workflows."""

from __future__ import annotations

import functools
import hashlib
import inspect
import keyword
from typing import Any, Callable, get_type_hints

from hypergraph._utils import ensure_tuple, hash_definition
from hypergraph.nodes._callable import CallableMixin
from hypergraph.nodes._rename import (
    RenameEntry,
    _apply_renames,
)
from hypergraph.nodes.base import HyperNode, _validate_emit_wait_for


class InterruptNode(CallableMixin, HyperNode):
    """A declarative pause point that surfaces a value and waits for a response.

    InterruptNode has one or more inputs (values shown to the user) and one or
    more outputs (where the user's responses are written). It never caches results.

    Can be created two ways:

    1. Via the ``@interrupt`` decorator or constructor with a source function
       (preferred for handler-backed nodes)::

        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            return "auto-approved"      # returns value -> auto-resolve
            # return None               # returns None -> pause

        # Or equivalently via constructor:
        approval = InterruptNode(my_func, output_name="decision")

    2. Via the legacy class constructor (for handler-less pause points)::

        approval = InterruptNode(
            name="approval",
            input_param="draft",
            output_param="decision",
        )

    When created with a source function, the function IS the handler:
    - Returning a value -> auto-resolves the interrupt
    - Returning None -> pauses for human input
    - The function signature defines inputs; ``output_name`` defines outputs
    - Type annotations are used for input/output types
    - Function defaults work as node defaults
    """

    func: Callable | None
    _hide: bool
    _emit: tuple[str, ...]
    _wait_for: tuple[str, ...]
    _is_async: bool
    _is_generator: bool

    def __init__(
        self,
        source: Callable | str | None = None,
        name: str | None = None,
        output_name: str | tuple[str, ...] | None = None,
        *,
        rename_inputs: dict[str, str] | None = None,
        emit: str | tuple[str, ...] | None = None,
        wait_for: str | tuple[str, ...] | None = None,
        hide: bool = False,
        response_type: type | dict[str, type] | None = None,
        handler: Callable[..., Any] | None = None,
        # Legacy constructor params
        input_param: str | tuple[str, ...] | None = None,
        output_param: str | tuple[str, ...] | None = None,
    ) -> None:
        """Create an InterruptNode.

        Two construction modes:

        **Source function mode** (like FunctionNode)::

            InterruptNode(my_func, output_name="decision")
            InterruptNode(my_func, name="review", output_name="decision",
                         emit="done", wait_for="ready")

        **Legacy mode** (handler-less pause points)::

            InterruptNode(name="approval", input_param="draft",
                         output_param="decision")

        Args:
            source: Function to wrap as handler, or node name (legacy).
            name: Public node name (default: func.__name__ for source mode).
            output_name: Name(s) for output value(s) (source mode).
            rename_inputs: Mapping to rename inputs {old: new}.
            emit: Ordering-only output name(s). Auto-produced with sentinel.
            wait_for: Ordering-only input name(s). Node waits for freshness.
            hide: Whether to hide from visualization (default: False).
            response_type: Type annotation for response (legacy mode).
            handler: Handler callable (legacy mode).
            input_param: Input parameter name(s) (legacy mode).
            output_param: Output parameter name(s) (legacy mode).
        """
        is_legacy = input_param is not None or output_param is not None

        if is_legacy:
            self._init_legacy(
                source if isinstance(source, str) else name or source,
                input_param=input_param,
                output_param=output_param,
                response_type=response_type,
                handler=handler,
            )
        else:
            func = source if callable(source) else None
            if func is not None:
                self._init_from_func(
                    func,
                    name=name,
                    output_name=output_name,
                    rename_inputs=rename_inputs,
                    emit=emit,
                    wait_for=wait_for,
                    hide=hide,
                )
            else:
                # Called as InterruptNode(name="x", input_param=..., output_param=...)
                # but without input_param/output_param — invalid
                raise TypeError(
                    "InterruptNode requires either a source function "
                    "(e.g., InterruptNode(my_func, output_name='x')) or "
                    "legacy params (input_param=..., output_param=...)"
                )

    def _init_legacy(
        self,
        name: str | None,
        *,
        input_param: str | tuple[str, ...] | None,
        output_param: str | tuple[str, ...] | None,
        response_type: type | dict[str, type] | None,
        handler: Callable[..., Any] | None,
    ) -> None:
        """Initialize from legacy constructor (input_param/output_param)."""
        if input_param is None or output_param is None:
            raise TypeError("Legacy constructor requires both input_param and output_param")
        if name is None:
            raise TypeError("Legacy constructor requires name")

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
        self._use_kwargs = False
        self._hide = False
        self._emit = ()
        self._wait_for = ()
        self._is_async = False
        self._is_generator = False
        self._definition_hash = ""  # computed lazily for legacy

    def _init_from_func(
        self,
        func: Callable,
        *,
        name: str | None,
        output_name: str | tuple[str, ...] | None,
        rename_inputs: dict[str, str] | None,
        emit: str | tuple[str, ...] | None,
        wait_for: str | tuple[str, ...] | None,
        hide: bool,
    ) -> None:
        """Initialize from a source function (like FunctionNode)."""
        self.func = func
        self._use_kwargs = True
        self._hide = hide
        self._definition_hash = hash_definition(func)
        self._emit = ensure_tuple(emit) if emit else ()
        self._wait_for = ensure_tuple(wait_for) if wait_for else ()

        # Name
        self.name = name or func.__name__

        # Outputs = data outputs + emit outputs
        if output_name is None:
            raise TypeError(
                "InterruptNode requires output_name when created with a source function"
            )
        data_outputs = ensure_tuple(output_name)
        for p in data_outputs:
            _validate_param_name(p, "output_name")
        self.outputs = data_outputs + self._emit

        # Inputs from function signature
        sig_inputs = tuple(inspect.signature(func).parameters.keys())
        self.inputs, self._rename_history = _apply_renames(
            sig_inputs, rename_inputs, "inputs"
        )

        # Validate emit/wait_for
        _validate_emit_wait_for(
            self.name, self._emit, self._wait_for, data_outputs, self.inputs,
        )

        # Derive response_type from return annotation
        try:
            hints = get_type_hints(func)
            self.response_type = hints.get("return")
        except Exception:
            self.response_type = None

        # Detect execution mode
        self._is_async = inspect.iscoroutinefunction(
            func
        ) or inspect.isasyncgenfunction(func)
        self._is_generator = inspect.isgeneratorfunction(
            func
        ) or inspect.isasyncgenfunction(func)

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
        return self.data_outputs[0] if self.data_outputs else self.outputs[0]

    @property
    def is_multi_input(self) -> bool:
        """Whether this node has multiple inputs."""
        return len(self.inputs) > 1

    @property
    def is_multi_output(self) -> bool:
        """Whether this node has multiple data outputs."""
        return len(self.data_outputs) > 1

    # ── Capabilities ──

    @property
    def cache(self) -> bool:
        """InterruptNodes never cache."""
        return False

    @property
    def is_async(self) -> bool:
        """True if handler requires await."""
        return self._is_async

    @property
    def is_generator(self) -> bool:
        """True if handler yields multiple values."""
        return self._is_generator

    @property
    def hide(self) -> bool:
        """Whether this node is hidden from visualization."""
        return self._hide

    @property
    def wait_for(self) -> tuple[str, ...]:
        """Ordering-only inputs this node waits for."""
        return self._wait_for

    @property
    def data_outputs(self) -> tuple[str, ...]:
        """Outputs that carry data (excludes emit-only outputs)."""
        if not self._emit:
            return self.outputs
        return self.outputs[:len(self.outputs) - len(self._emit)]

    @property
    def definition_hash(self) -> str:
        """Hash based on function source (func mode) or metadata (legacy mode)."""
        if self._use_kwargs and self.func is not None:
            return self._definition_hash
        rt = _response_type_label(self.response_type)
        content = f"InterruptNode:{self.name}:{self.inputs}:{self.outputs}:{rt}"
        return hashlib.sha256(content.encode()).hexdigest()

    # ── Legacy overrides for CallableMixin ──
    # CallableMixin assumes self.func is always a callable. For legacy nodes
    # (func=None), we short-circuit to empty dicts.

    @functools.cached_property
    def defaults(self) -> dict[str, Any]:
        """Default values for input parameters."""
        if not self._use_kwargs or self.func is None:
            return {}
        # Delegate to CallableMixin logic
        from hypergraph.nodes._callable import _build_forward_rename_map
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
        if not self._use_kwargs or self.func is None:
            return {}
        # Delegate to CallableMixin logic
        from hypergraph.nodes._callable import _build_forward_rename_map
        try:
            hints = get_type_hints(self.func)
        except Exception:
            return {}
        sig = inspect.signature(self.func)
        rename_map = _build_forward_rename_map(self._rename_history)
        return {
            rename_map.get(orig, orig): hints[orig]
            for orig in sig.parameters
            if orig in hints
        }

    def map_inputs_to_params(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Map renamed input names back to original function parameter names."""
        if not self._use_kwargs:
            return inputs
        return super().map_inputs_to_params(inputs)

    # ── Type introspection ──

    def get_output_type(self, param: str) -> type | None:
        """Return the type for an output parameter.

        For func-based nodes: uses return annotation.
        For legacy nodes: uses response_type.
        """
        if param not in self.data_outputs:
            return None
        # Func-based: use return annotation
        if self._use_kwargs and self.func is not None:
            return self._output_annotation.get(param)
        # Legacy: use response_type
        if isinstance(self.response_type, dict):
            return self.response_type.get(param)
        if param == self.data_outputs[0]:
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
        data_outs = self.data_outputs
        if len(data_outs) == 1:
            return {data_outs[0]: return_hint}
        # Multi-output: try tuple element types
        from typing import get_args, get_origin

        if get_origin(return_hint) is tuple:
            args = get_args(return_hint)
            if len(args) == len(data_outs):
                return dict(zip(data_outs, args))
        return {}

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
    emit: str | tuple[str, ...] | None = None,
    wait_for: str | tuple[str, ...] | None = None,
    hide: bool = False,
) -> Callable[[Callable], InterruptNode]:
    """Decorator to create an InterruptNode from a function.

    The function IS the handler:
    - Returning a value -> auto-resolves the interrupt
    - Returning None -> pauses for human input

    Inputs come from the function signature. Outputs from ``output_name``.
    Types from annotations.

    Args:
        output_name: Name(s) for output value(s).
        rename_inputs: Mapping to rename inputs {old: new}
        emit: Ordering-only output name(s).
        wait_for: Ordering-only input name(s).
        hide: Whether to hide from visualization (default: False).

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

        # With ordering
        @interrupt(output_name="decision", emit="approved", wait_for="ready")
        def approval(draft: str) -> str:
            ...

        # Test the handler directly
        assert approval.func("my draft") == "auto-approved"
        assert approval("my draft") == "auto-approved"
    """

    def decorator(func: Callable) -> InterruptNode:
        return InterruptNode(
            source=func,
            output_name=output_name,
            rename_inputs=rename_inputs,
            emit=emit,
            wait_for=wait_for,
            hide=hide,
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
