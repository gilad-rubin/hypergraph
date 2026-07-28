"""Settled off-loop execution for synchronous work with side effects.

``asyncio.to_thread`` alone is not cancellation-correct for work that
mutates anything: cancelling the awaiting coroutine returns control to the
caller while the worker thread is still running — the caller observes
cancellation mid-mutation. The helper here shields the thread and DRAINS it
on cancellation, so cancellation never abandons in-flight threaded work.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")

__all__ = ["to_thread_settled"]


async def to_thread_settled(func: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run ``func(*args, **kwargs)`` in a worker thread; settle before raising.

    A worker thread cannot be interrupted, so on cancellation this waits for
    the thread to finish before re-raising ``CancelledError`` — the caller
    never observes cancellation while the threaded work is still running.
    The drain is a LOOP: a second cancellation arriving mid-drain restarts
    the wait instead of returning while the thread may still be live. The
    settled task's exception is retrieved so settling never logs as an
    unretrieved task exception.

    ``asyncio.to_thread`` copies the caller's context, so ContextVar reads
    made by ``func`` observe the caller's values; ContextVar writes made in
    the thread do not propagate back. Work runs on the loop's default
    executor, a pool shared with every other ``to_thread`` user.
    """
    task = asyncio.ensure_future(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait([task])
        if not task.cancelled():
            task.exception()  # retrieved: settling must not log as unretrieved
        raise
