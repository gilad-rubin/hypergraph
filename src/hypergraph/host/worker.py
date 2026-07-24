"""Worker internals for the durable host: exclusive lock and bounded drain.

One exclusive worker per Run Home, enforced by an OS-level lock on
``<db path>.lock`` acquired at ``work_forever()`` startup and released on
exit (normal, drained, or cancelled). POSIX uses ``fcntl.flock``; Windows
uses ``msvcrt.locking`` on a best-effort basis. In-memory Homes use a
process-local guard since they cannot be shared across processes anyway.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import TYPE_CHECKING

from hypergraph.host.errors import WorkerLockError

if TYPE_CHECKING:
    from hypergraph.host.home import RunHome

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows only
    fcntl = None  # type: ignore[assignment]

# Process-local fallback registry for :memory: Homes (keyed by home identity).
_memory_locks: set[int] = set()
_memory_locks_guard = threading.Lock()


class _WorkerLock:
    """One exclusive-worker claim on a Run Home."""

    def __init__(self, lock_path: str | None, memory_key: int | None) -> None:
        self._lock_path = lock_path
        self._memory_key = memory_key
        self._fd: int | None = None
        self._acquired = False

    @classmethod
    def for_home(cls, home: RunHome) -> _WorkerLock:
        if home._is_memory:
            return cls(None, id(home))
        path = home.path
        if path.startswith("file:"):
            path = path[len("file:") :]
        return cls(f"{path}.lock", None)

    def acquire(self) -> None:
        """Take the lock; raise WorkerLockError immediately if held."""
        if self._lock_path is None:
            with _memory_locks_guard:
                if self._memory_key in _memory_locks:
                    raise WorkerLockError(":memory:", "This in-memory Run Home already has an active worker.")
                _memory_locks.add(self._memory_key)  # type: ignore[arg-type]
            self._acquired = True
            return

        self._fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # pragma: no cover - Windows only
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                if os.fstat(self._fd).st_size == 0:
                    os.write(self._fd, b"\0")
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise WorkerLockError(self._lock_path) from None
        self._acquired = True

    def release(self) -> None:
        """Release the lock (idempotent)."""
        if not self._acquired:
            return
        self._acquired = False
        if self._lock_path is None:
            with _memory_locks_guard:
                _memory_locks.discard(self._memory_key)  # type: ignore[arg-type]
            return
        if self._fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                else:  # pragma: no cover - Windows only
                    import msvcrt

                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(self._fd)
                self._fd = None


async def _drain(tasks: set[asyncio.Task], drain_timeout: float) -> None:
    """Bounded drain: await active runs, then cancel what outlives the bound."""
    pending = [task for task in tasks if not task.done()]
    if not pending:
        return
    _, still_pending = await asyncio.wait(pending, timeout=drain_timeout)
    for task in still_pending:
        task.cancel()
    if still_pending:
        await asyncio.gather(*still_pending, return_exceptions=True)
