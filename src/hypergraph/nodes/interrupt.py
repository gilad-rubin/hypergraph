"""InterruptNode — declarative pause point for human-in-the-loop workflows."""

from __future__ import annotations

import hashlib
import keyword
from typing import Any, Callable

from hypergraph._utils import ensure_tuple
from hypergraph.nodes.base import HyperNode


class InterruptNode(HyperNode):
    """A declarative pause point that surfaces a value and waits for a response.

    InterruptNode has one or more inputs (values shown to the user) and one or
    more outputs (where the user's responses are written). It never caches results.

    Args:
        name: Node name
        input_param: Name(s) of the input parameter(s). Can be a single string
            or a tuple of strings for multiple inputs.
        output_param: Name(s) of the output parameter(s). Can be a single string
            or a tuple of strings for multiple outputs.
        response_type: Optional type annotation for the response value(s).
            For single output: a type. For multiple outputs: a dict mapping
            output names to types.
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
        """Create an InterruptNode.

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
        self.handler = handler
        self._rename_history = []

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

    @property
    def cache(self) -> bool:
        """InterruptNodes never cache."""
        return False

    @property
    def definition_hash(self) -> str:
        """Hash includes response_type but excludes handler."""
        rt = _response_type_label(self.response_type)
        content = f"InterruptNode:{self.name}:{self.inputs}:{self.outputs}:{rt}"
        return hashlib.sha256(content.encode()).hexdigest()

    def get_output_type(self, param: str) -> type | None:
        """Return the response_type for the output parameter.

        For single output: returns response_type if param matches.
        For multiple outputs: returns response_type[param] if response_type is a dict.
        """
        if param not in self.outputs:
            return None
        if isinstance(self.response_type, dict):
            return self.response_type.get(param)
        # Single output or legacy: return response_type for first output only
        if param == self.outputs[0]:
            return self.response_type
        return None

    def with_handler(self, handler: Callable[..., Any]) -> "InterruptNode":
        """Return a new InterruptNode with the given handler (immutable).

        The handler must accept a single positional argument (the input value)
        and return the response value. It may be sync or async.

        Args:
            handler: Callable that resolves the interrupt automatically.

        Returns:
            A new InterruptNode instance with the handler attached.
        """
        clone = self._copy()
        clone.handler = handler
        return clone


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
