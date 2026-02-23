"""InterruptNode — declarative pause point for human-in-the-loop workflows."""

from __future__ import annotations

from collections.abc import Callable

from hypergraph.nodes.function import FunctionNode


class InterruptNode(FunctionNode):
    """Pause point for human-in-the-loop workflows.

    Identical to FunctionNode except:
    - ``output_name`` is required (must define where responses go)
    - ``is_interrupt`` is ``True`` (enables pause/resume in runners)
    - Handler returning ``None`` -> pauses for human input
    - Handler returning a value -> auto-resolves

    Created via the ``@interrupt`` decorator or ``InterruptNode()`` constructor::

        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            return "auto-approved"      # returns value -> auto-resolve
            # return None               # returns None -> pause

        # Or equivalently via constructor:
        approval = InterruptNode(my_func, output_name="decision")
    """

    def __init__(
        self,
        source: Callable | FunctionNode,
        name: str | None = None,
        output_name: str | tuple[str, ...] | None = None,
        *,
        rename_inputs: dict[str, str] | None = None,
        cache: bool = False,
        hide: bool = False,
        emit: str | tuple[str, ...] | None = None,
        wait_for: str | tuple[str, ...] | None = None,
    ) -> None:
        if output_name is None:
            raise TypeError(
                "InterruptNode requires output_name "
                "(defines where human responses are written)"
            )
        super().__init__(
            source,
            name=name,
            output_name=output_name,
            rename_inputs=rename_inputs,
            cache=cache,
            hide=hide,
            emit=emit,
            wait_for=wait_for,
        )

    @property
    def is_interrupt(self) -> bool:
        return True

    def __repr__(self) -> str:
        original = self.func.__name__
        if self.name == original:
            return f"InterruptNode({self.name}, outputs={self.outputs})"
        return f"InterruptNode({original} as '{self.name}', outputs={self.outputs})"


def interrupt(
    output_name: str | tuple[str, ...],
    *,
    rename_inputs: dict[str, str] | None = None,
    cache: bool = False,
    emit: str | tuple[str, ...] | None = None,
    wait_for: str | tuple[str, ...] | None = None,
    hide: bool = False,
) -> Callable[[Callable], InterruptNode]:
    """Decorator to create an InterruptNode from a function.

    The function IS the handler:
    - Returning a value -> auto-resolves the interrupt
    - Returning ``None`` -> pauses for human input

    Inputs come from the function signature. Outputs from ``output_name``.
    Types from annotations.

    Args:
        output_name: Name(s) for output value(s).
        rename_inputs: Mapping to rename inputs {old: new}.
        cache: Whether to cache results (default: False).
        emit: Ordering-only output name(s).
        wait_for: Ordering-only input name(s).
        hide: Whether to hide from visualization (default: False).

    Examples::

        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            return "auto-approved"

        @interrupt(output_name="decision")
        def approval(draft: str) -> str:
            ...  # returns None -> pause

        # Test the handler directly
        assert approval("my draft") == "auto-approved"
    """

    def decorator(func: Callable) -> InterruptNode:
        int_node = InterruptNode(
            source=func,
            output_name=output_name,
            rename_inputs=rename_inputs,
            cache=cache,
            emit=emit,
            wait_for=wait_for,
            hide=hide,
        )
        int_node.__wrapped__ = func
        return int_node

    return decorator
