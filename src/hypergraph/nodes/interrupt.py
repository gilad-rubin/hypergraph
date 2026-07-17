"""InterruptNode — declarative pause point for human-in-the-loop workflows."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, get_type_hints

from hypergraph.nodes.function import FunctionNode


class InterruptNode(FunctionNode):
    """Pause point for human-in-the-loop workflows.

    The handler returns the question shown to a human. ``answer_name`` is the
    node's single output port where the eventual answer enters dataflow.

    Created via the ``@interrupt`` decorator or ``InterruptNode()`` constructor::

        @interrupt(answer_name="decision")
        def approval(draft: str) -> Confirm:
            return Confirm(prompt="Publish this draft?", evidence=(draft,))

        # Or equivalently via constructor:
        approval = InterruptNode(my_func, answer_name="decision")
    """

    def __init__(
        self,
        source: Callable | FunctionNode,
        name: str | None = None,
        *,
        answer_name: str,
        rename_inputs: dict[str, str] | None = None,
        cache: bool = False,
        hide: bool = False,
        emit: str | tuple[str, ...] | None = None,
        wait_for: str | tuple[str, ...] | None = None,
        **unsupported: Any,
    ) -> None:
        _validate_answer_name("InterruptNode", answer_name, unsupported)
        super().__init__(
            source,
            name=name,
            output_name=answer_name,
            rename_inputs=rename_inputs,
            cache=cache,
            hide=hide,
            emit=emit,
            wait_for=wait_for,
        )

    @property
    def is_interrupt(self) -> bool:
        return True

    @property
    def answer_name(self) -> str:
        """Current local output port where the human answer enters dataflow."""
        return self.data_outputs[0]

    @property
    def ask_annotation(self) -> Any | None:
        """Resolved return annotation for the structural ask payload."""
        try:
            return get_type_hints(self.func, include_extras=True).get("return")
        except Exception:
            return None

    @property
    def output_annotation(self) -> dict[str, Any]:
        """Map the answer port to the question annotation's answer type."""
        annotation = self.ask_annotation
        if annotation is None or not hasattr(annotation, "answer_type"):
            return {}
        return {self.answer_name: annotation.answer_type}

    def get_output_type(self, output: str) -> Any | None:
        """Return the ask annotation's declared answer type for the answer port."""
        return self.output_annotation.get(output)

    def __repr__(self) -> str:
        original = self.func.__name__
        if self.name == original:
            return f"InterruptNode({self.name}, outputs={self.outputs})"
        return f"InterruptNode({original} as '{self.name}', outputs={self.outputs})"


def interrupt(
    *,
    answer_name: str,
    rename_inputs: dict[str, str] | None = None,
    cache: bool = False,
    emit: str | tuple[str, ...] | None = None,
    wait_for: str | tuple[str, ...] | None = None,
    hide: bool = False,
    **unsupported: Any,
) -> Callable[[Callable], InterruptNode]:
    """Decorator to create an InterruptNode from a function.

    The function return is the question payload. ``answer_name`` is the
    node's single output port and the response key used to resume the run.

    Args:
        answer_name: Name of the single answer output port.
        rename_inputs: Mapping to rename inputs {old: new}.
        cache: Whether to cache results (default: False).
        emit: Ordering-only local output name(s).
        wait_for: Ordering-only graph-scope output/emit address(es).
        hide: Whether to hide from visualization (default: False).

    Examples::

        @interrupt(answer_name="decision")
        def approval(draft: str) -> Confirm:
            return Confirm(prompt="Publish this draft?", evidence=(draft,))

        # Test the handler directly
        assert approval("my draft").prompt == "Publish this draft?"
    """

    _validate_answer_name("interrupt()", answer_name, unsupported)

    def decorator(func: Callable) -> InterruptNode:
        int_node = InterruptNode(
            source=func,
            answer_name=answer_name,
            rename_inputs=rename_inputs,
            cache=cache,
            emit=emit,
            wait_for=wait_for,
            hide=hide,
        )
        int_node.__wrapped__ = func  # type: ignore[attr-defined]
        return int_node

    return decorator


def _validate_answer_name(owner: str, answer_name: Any, unsupported: dict[str, Any]) -> None:
    """Reject legacy, tuple, and otherwise invalid answer slots."""
    if unsupported:
        names = ", ".join(sorted(unsupported))
        raise TypeError(f"{owner} received unsupported argument(s): {names}\n\nHow to fix: Provide one keyword-only answer_name string.")
    if not isinstance(answer_name, str):
        raise TypeError(
            f"{owner} answer_name must be a str, got {type(answer_name).__name__}\n\nHow to fix: Provide one keyword-only answer_name string."
        )
