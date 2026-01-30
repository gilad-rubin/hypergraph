"""InterruptNode — declarative pause point for human-in-the-loop workflows."""

from __future__ import annotations

import hashlib
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
        handler: Optional callable to auto-resolve the interrupt
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
        rt = self.response_type.__qualname__ if self.response_type else "None"
        content = f"InterruptNode:{self.name}:{self.inputs}:{self.outputs}:{rt}"
        return hashlib.sha256(content.encode()).hexdigest()

    def with_handler(self, handler: Callable[..., Any]) -> "InterruptNode":
        """Return a new InterruptNode with the given handler (immutable)."""
        clone = self._copy()
        clone.handler = handler
        return clone
