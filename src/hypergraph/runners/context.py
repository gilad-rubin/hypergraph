"""``NodeContext`` — the framework value injected into a node's own signature.

This module is public because the type is: a user writes ``ctx: NodeContext``
in their node, so its import path is part of the API they read and type
against. It is exported from ``hypergraph`` and ``hypergraph.runners``; the
builder that fills one in per node execution stays runner-internal in
``runners._shared.node_context``.
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
