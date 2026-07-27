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
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from hypergraph.host.errors import WorkerLockError

if TYPE_CHECKING:
    from hypergraph.host.home import RunHome

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows only
    fcntl = None  # type: ignore[assignment]

# Process-local fallback registry for :memory: Homes, keyed by the token each
# Home mints for itself at construction (never by `id()`, which a freed Home
# hands straight to the next allocation).
_memory_locks: set[int] = set()
_memory_locks_guard = threading.Lock()


def lock_path_for(uri: str) -> str:
    """THE lock file for a database, whatever URI spelling names it.

    The lock has to identify the FILE, not the string. SQLite accepts
    several spellings of one database — ``/x/runs.db``, ``file:/x/runs.db``,
    ``file:/x/runs.db?mode=rwc``, a relative path from a different working
    directory — and keying the lock on the raw string admitted two exclusive
    workers to the same database, which is the one thing the lock exists to
    prevent. So the query and fragment (SQLite parameters, not part of the
    filename), percent-encoding, and relative/symlinked spellings are all
    normalized away first.
    """
    path = uri
    if path.startswith("file:"):
        parts = urlsplit(path)
        path = unquote(parts.path)
    return f"{Path(path).resolve()}.lock"


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
            return cls(None, home._memory_lock_token)
        return cls(lock_path_for(home.path), None)

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
