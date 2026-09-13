"""``NodeContext`` — the framework value injected into a node's own signature.

This module is public because the type is: a user writes ``ctx: NodeContext``
in their node, so its import path is part of the API they read and type
against. It is exported from ``hypergraph`` and ``hypergraph.runners``; the
builder that fills one in per node execution stays runner-internal in
``runners._shared.node_context``.

NodeContext is the ONE sanctioned seam for runtime-injected per-node
capabilities a node cannot obtain any other way — a capability that needs
the live run (its stop signal, its event stream, its durable log) belongs
here rather than in a node's inputs or a module-level global.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hypergraph.nodes._input_extraction import register_injectable

if TYPE_CHECKING:
    from hypergraph.checkpointers.base import Checkpointer
    from hypergraph.runners._shared.stop import StopSignal


class NodeContext:
    """Framework context injected via type-hint detection.

    Three capabilities:

    * ``stop_requested`` — cooperative stop flag (read-only)
    * ``stream(chunk)``  — emit a ``StreamingChunkEvent`` for live UI preview
    * ``record(kind, payload)`` — append a DURABLE fact to the run's log

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
        "_checkpointer",
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
        checkpointer: Checkpointer | None = None,
    ) -> None:
        self._stop_signal = stop_signal
        self._emit_fn = emit_fn
        self._node_name = node_name
        self._run_id = run_id
        self._graph_name = graph_name
        self._workflow_id = workflow_id
        self._item_index = item_index
        self._parent_span_id = parent_span_id
        self._checkpointer = checkpointer

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

    def record(self, kind: str, payload: dict[str, Any]) -> int | None:
        """Append one DURABLE fact to this run's log; return its ``seq``.

        Where ``stream`` offers a best-effort preview that nothing keeps,
        this commits: the fact lands on the run's own gap-free sequence
        beside the host's ``step`` and ``status`` facts, so
        ``client.watch(ref, after=cursor)`` replays it in order and a
        watcher that connects after the node finished still sees it.

        ``kind`` is the node's own vocabulary — any string except the
        framework's own (see ``RESERVED_FACT_KINDS``), which raises
        ``ReservedFactKindError`` wherever the node runs. ``payload`` must
        be a JSON-safe dict.

        Returns ``None`` when this run has no durable log — a Tier-0 run,
        or any store that is not a Run Home. That is a no-op, not a raise:
        a node must run the same in-process as it does under a host, and
        "nobody is keeping a log" is a deployment fact, not a bug in the
        node.

        ::

            @node(output_name="answer")
            async def agent_turn(prompt: str, ctx: NodeContext) -> str:
                ctx.record("tool_call", {"name": "search", "args": {"q": prompt}})
                return await llm(prompt)
        """
        from hypergraph.checkpointers.types import RESERVED_FACT_KINDS, ReservedFactKindError

        if kind in RESERVED_FACT_KINDS:
            raise ReservedFactKindError(kind)
        if not isinstance(payload, dict):
            raise TypeError(
                f"record() payload must be a dict, got {type(payload).__name__}.\n\nHow to fix: wrap the value in a dict, e.g. ctx.record({kind!r}, {{'value': ...}}). A fact's payload is a JSON object every watcher reads by key."
            )
        if self._checkpointer is None or self._workflow_id is None:
            return None
        return self._checkpointer.append_run_fact_sync(self._workflow_id, kind, payload)


# Register NodeContext as a framework-injectable type.
# This causes extract_inputs() to exclude it from node.inputs
# and store it as node._context_param for executor injection.
register_injectable(NodeContext)
