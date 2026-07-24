"""RunHomeClient — backend-neutral inspection of existing work.

A client is constructed from a Run Home alone — no Definition code needed.
It owns ``get`` and ``watch`` for existing runs; submission stays with the
Host. The same surface runs against SQLite now and other backends later.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from hypergraph.host._bus import _bus_for, _PreviewBus
from hypergraph.host.refs import RunRef
from hypergraph.host.views import TERMINAL_WORKFLOW_STATUSES, RunUpdate, RunView, WaitingCondition

if TYPE_CHECKING:
    from hypergraph.checkpointers.types import Run
    from hypergraph.host.home import RunHome


def _parse_cursor(after: str | int | None) -> int:
    """Normalize a watch cursor to a durable sequence number."""
    if after is None:
        return 0
    if isinstance(after, int):
        return after
    if isinstance(after, str) and after.startswith("seq:"):
        try:
            return int(after[len("seq:") :])
        except ValueError:
            pass
    raise ValueError(f"Invalid watch cursor {after!r}. Expected None, an int seq, or a 'seq:N' cursor string from a durable RunUpdate.")


def _build_view(home_uri: str, run_id: str, submission: dict[str, Any] | None, run: Run | None) -> RunView | None:
    if submission is None and run is None:
        return None
    waiting = None
    if submission is not None and submission["state"] in ("pending", "claimed") and run is None:
        waiting = WaitingCondition.QUEUED
    if submission is not None:
        definition_name = submission["definition_name"]
    elif run is not None:
        definition_name = run.graph_name or ""
    else:  # pragma: no cover - guarded by the None check above
        definition_name = ""
    return RunView(
        run_ref=RunRef(home=home_uri, run_id=run_id),
        workflow_id=run_id,
        definition_name=definition_name,
        status=run.status if run is not None else None,
        waiting=waiting,
    )


class RunHomeClient:
    """Inspect existing runs in a Run Home — no graph code required.

    Construct directly from a ``RunHome``::

        client = RunHomeClient(RunHome.open("file:./runs.db"))

    ``list``/``stop``/``rerun``/``answer`` arrive with later host tickets.
    """

    def __init__(self, home: RunHome, *, _bus: _PreviewBus | None = None) -> None:
        from hypergraph.host.home import RunHome as _RunHome

        if not isinstance(home, _RunHome):
            raise TypeError(f"RunHomeClient expects a RunHome, got {type(home).__name__}. Open one with RunHome.open(uri).")
        self._home = home
        # An explicit bus wins; otherwise pick up the bus serve() registered
        # for this Home URI when a worker lives in the same process.
        self._bus = _bus if _bus is not None else _bus_for(home.uri)

    async def get(self, ref: RunRef) -> RunView | None:
        """Return persisted facts for ``ref``, or None if unknown."""
        submission = await self._home._get_submission(ref.run_id)
        run = await self._home.get_run_async(ref.run_id)
        return _build_view(self._home.uri, ref.run_id, submission, run)

    def get_sync(self, ref: RunRef) -> RunView | None:
        """Sync mirror of ``get``."""
        submission = self._home._get_submission_sync(ref.run_id)
        run = self._home.get_run(ref.run_id)
        return _build_view(self._home.uri, ref.run_id, submission, run)

    async def watch(self, ref: RunRef, *, after: str | int | None = None, poll_interval: float = 0.05) -> AsyncIterator[RunUpdate]:
        """Replay durable facts after ``after``, then tail live previews.

        Durable updates carry ``durable=True`` and a cursor that advances
        monotonically (``seq:N``); replay from a stored cursor has no gaps
        and no repeats. Live previews (only when a worker runs in this
        process) carry ``durable=False`` and repeat the last durable cursor
        — they never advance it. Store cursors from durable updates only.
        The generator ends once the run reaches a terminal status and every
        committed fact has been delivered.
        """
        cursor_seq = _parse_cursor(after)
        queue: asyncio.Queue | None = None
        if self._bus is not None:
            queue = self._bus.subscribe(ref.run_id)
        terminal = False
        try:
            while True:
                rows = await self._home._read_run_updates(ref.run_id, cursor_seq)
                for seq, kind, payload, created_at in rows:
                    cursor_seq = seq
                    yield RunUpdate(
                        cursor=f"seq:{seq}",
                        durable=True,
                        kind=kind,
                        payload=json.loads(payload),
                        timestamp=created_at,
                    )
                if queue is not None:
                    while True:
                        try:
                            kind, payload = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        yield RunUpdate(
                            cursor=f"seq:{cursor_seq}",
                            durable=False,
                            kind=kind,
                            payload=payload,
                            timestamp=datetime.now(timezone.utc).isoformat(),
                        )
                if rows:
                    continue
                if terminal:
                    return
                run = await self._home.get_run_async(ref.run_id)
                terminal = run is not None and run.status in TERMINAL_WORKFLOW_STATUSES
                if not terminal:
                    await asyncio.sleep(poll_interval)
        finally:
            if queue is not None and self._bus is not None:
                self._bus.unsubscribe(ref.run_id, queue)
