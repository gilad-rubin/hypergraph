"""NodeContext — injected into nodes that declare ``ctx: NodeContext``.

Read-only view of the stop signal and a streaming side-channel.
The user never creates this; the framework builds one per node execution.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hypergraph.nodes._input_extraction import register_injectable

if TYPE_CHECKING:
    from hypergraph.runners._shared.stop import StopSignal


class NodeContext:
    """Framework context injected via type-hint detection.

    Two capabilities:

    * ``stop_requested`` — cooperative stop flag (read-only)
    * ``stream(chunk)``  — emit a ``StreamingChunkEvent`` for live UI preview

    Usage::

        @node(output_name="response")
        async def llm(messages: list, ctx: NodeContext) -> str:
            response = ""
            async for chunk in llm.stream(messages):
                if ctx.stop_requested:
                    break
                response += chunk
                ctx.stream(chunk)
            return response

    Testing::

        from unittest.mock import MagicMock
        ctx = MagicMock(spec=NodeContext)
        ctx.stop_requested = False
        result = llm(messages=["hi"], ctx=ctx)
    """

    __slots__ = (
        "_stop_signal",
        "_emit_fn",
        "_node_name",
        "_run_id",
        "_graph_name",
        "_workflow_id",
        "_item_index",
        "_parent_span_id",
    )

    def __init__(
        self,
        stop_signal: StopSignal,
        emit_fn: Callable[[Any], None],
        node_name: str,
        run_id: str = "",
        *,
        graph_name: str = "",
        workflow_id: str | None = None,
        item_index: int | None = None,
        parent_span_id: str | None = None,
    ) -> None:
        self._stop_signal = stop_signal
        self._emit_fn = emit_fn
        self._node_name = node_name
        self._run_id = run_id
        self._graph_name = graph_name
        self._workflow_id = workflow_id
        self._item_index = item_index
        self._parent_span_id = parent_span_id

    @property
    def stop_requested(self) -> bool:
        """``True`` when ``runner.stop()`` has been called."""
        return self._stop_signal.is_set

    def stream(self, chunk: Any) -> None:
        """Emit a ``StreamingChunkEvent`` for live UI preview.

        No-op when ``stop_requested`` is ``True``.
        Does **not** affect the node's return value.
        """
        if not self.stop_requested:
            from hypergraph.events.types import StreamingChunkEvent

            self._emit_fn(
                StreamingChunkEvent(
                    run_id=self._run_id,
                    parent_span_id=self._parent_span_id,
                    workflow_id=self._workflow_id,
                    item_index=self._item_index,
                    chunk=chunk,
                    node_name=self._node_name,
                    graph_name=self._graph_name,
                )
            )


# Register NodeContext as a framework-injectable type.
# This causes extract_inputs() to exclude it from node.inputs
# and store it as node._context_param for executor injection.
register_injectable(NodeContext)


def _noop_emit(event: Any) -> None:
    """Fallback emit function when no dispatcher is configured."""


def build_node_context(
    node_name: str,
    emit_fn: Callable[[Any], None] | None,
    run_id: str = "",
    *,
    graph_name: str = "",
    workflow_id: str | None = None,
    item_index: int | None = None,
    parent_span_id: str | None = None,
) -> NodeContext:
    """Build a NodeContext for executor injection.

    Reads the StopSignal from the contextvar (set by the runner).
    Falls back to an unset signal if none is active. The correlation
    fields (graph_name, workflow_id, item_index, parent_span_id) are
    stamped onto every ``StreamingChunkEvent`` the node emits.
    """
    from hypergraph.runners._shared.stop import StopSignal, get_stop_signal

    signal = get_stop_signal() or StopSignal()
    return NodeContext(
        signal,
        emit_fn or _noop_emit,
        node_name,
        run_id=run_id,
        graph_name=graph_name,
        workflow_id=workflow_id,
        item_index=item_index,
        parent_span_id=parent_span_id,
    )
