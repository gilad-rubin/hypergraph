"""InterruptNode — declarative pause point for human-in-the-loop workflows."""

from __future__ import annotations

import hashlib
import keyword
from typing import Any, Callable

from hypergraph.nodes.base import HyperNode


class InterruptNode(HyperNode):
    """A declarative pause point that surfaces a value and waits for a response.

    InterruptNode has one input (the value shown to the user) and one output
    (where the user's response is written). It never caches results.

    Args:
        name: Node name
        input_param: Name of the input parameter (value shown to caller)
        output_param: Name of the output parameter (where response goes)
        response_type: Optional type annotation for the response value
        handler: Optional callable to auto-resolve the interrupt.
            Must accept a single positional argument (the input value)
            and return the response value. May be sync or async.
    """

    def __init__(
        self,
        name: str,
        *,
        input_param: str,
        output_param: str,
        response_type: type | None = None,
        handler: Callable[..., Any] | None = None,
    ) -> None:
        """Create an InterruptNode.

        The handler, if provided, must accept a single argument (the input
        value surfaced to the caller) and return the response value. It may
        be a sync function or an async coroutine.

        Raises:
            ValueError: If input_param or output_param is not a valid
                Python identifier or is a reserved keyword.
        """
        _validate_param_name(input_param, "input_param")
        _validate_param_name(output_param, "output_param")
        self.name = name
        self.inputs = (input_param,)
        self.outputs = (output_param,)
        self.response_type = response_type
        self.handler = handler
        self._rename_history = []

    @property
    def input_param(self) -> str:
        """The input parameter name."""
        return self.inputs[0]

    @property
    def output_param(self) -> str:
        """The output parameter name."""
        return self.outputs[0]

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
        """Return the response_type for the output parameter."""
        if param == self.output_param:
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


def _response_type_label(response_type: type | None) -> str:
    """Return a stable string label for a response type, handling union types."""
    if response_type is None:
        return "None"
    qualname = getattr(response_type, "__qualname__", None)
    if qualname is None:
        qualname = repr(response_type)
    module = getattr(response_type, "__module__", None)
    return f"{module}.{qualname}" if module else qualname


def _validate_param_name(name: str, label: str) -> None:
    """Validate that a parameter name is a valid Python identifier and not a keyword."""
    if not name.isidentifier():
        raise ValueError(f"{label} must be a valid Python identifier, got {name!r}")
    if keyword.iskeyword(name):
        raise ValueError(f"{label} must not be a Python keyword, got {name!r}")
