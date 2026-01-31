"""Cache backends for node result caching.

Provides a protocol for cache backends and two implementations:
- InMemoryCache: dict-based, lives for the runner's lifetime
- DiskCache: wraps diskcache.Cache (optional dependency), persists across runs
"""

from __future__ import annotations

import hashlib
import logging
import pickle
from collections import OrderedDict
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class CacheBackend(Protocol):
    """Protocol for cache backends.

    Implementations must provide get() and set() methods.
    """

    def get(self, key: str) -> tuple[bool, Any]:
        """Return (hit, value). hit=False means cache miss."""
        ...

    def set(self, key: str, value: Any) -> None:
        """Store a value."""
        ...


class InMemoryCache:
    """Dict-based in-memory cache with optional LRU eviction.

    Args:
        max_size: Maximum number of entries. None means unlimited.

    Example:
        >>> cache = InMemoryCache(max_size=100)
        >>> cache.set("key", "value")
        >>> cache.get("key")
        (True, 'value')
    """

    def __init__(self, max_size: int | None = None) -> None:
        self._max_size = max_size
        self._data: OrderedDict[str, Any] = OrderedDict()

    def get(self, key: str) -> tuple[bool, Any]:
        """Return (hit, value). Moves key to end for LRU tracking."""
        if key not in self._data:
            return False, None
        self._data.move_to_end(key)
        return True, self._data[key]

    def set(self, key: str, value: Any) -> None:
        """Store a value. Evicts least-recently-used entry if at capacity."""
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = value
        if self._max_size is not None and len(self._data) > self._max_size:
            self._data.popitem(last=False)


class DiskCache:
    """Persistent disk-based cache using diskcache.

    Requires ``pip install hypergraph[cache]`` (installs diskcache).

    Args:
        directory: Path to the cache directory.
        **kwargs: Additional arguments passed to ``diskcache.Cache``.

    Example:
        >>> cache = DiskCache("/tmp/hg-cache")
        >>> cache.set("key", "value")
        >>> cache.get("key")
        (True, 'value')
    """

    def __init__(self, directory: str, **kwargs: Any) -> None:
        try:
            import diskcache
        except ImportError:
            raise ImportError(
                "diskcache is required for DiskCache. "
                "Install it with: pip install 'hypergraph[cache]'"
            ) from None
        self._cache = diskcache.Cache(directory, **kwargs)

    def get(self, key: str) -> tuple[bool, Any]:
        """Return (hit, value) from disk cache."""
        sentinel = object()
        value = self._cache.get(key, default=sentinel)
        if value is sentinel:
            return False, None
        return True, value

    def set(self, key: str, value: Any) -> None:
        """Store a value to disk cache. Skips silently if value is not picklable."""
        try:
            self._cache.set(key, value)
        except (pickle.PicklingError, TypeError, AttributeError):
            logger.warning(
                "Cache write skipped: output not picklable for key %s", key
            )


def compute_cache_key(definition_hash: str, inputs: dict[str, Any]) -> str:
    """Compute a cache key from node identity and input values.

    Args:
        definition_hash: The node's definition hash (from node.definition_hash).
        inputs: Resolved input values for the node.

    Returns:
        SHA256 hex digest string, or empty string if inputs are not picklable.
    """
    try:
        sorted_items = sorted(inputs.items())
        inputs_bytes = pickle.dumps(sorted_items)
    except (pickle.PicklingError, TypeError, AttributeError) as exc:
        logger.warning("Cache miss: inputs not picklable (%s)", exc)
        return ""
    content = definition_hash.encode() + inputs_bytes
    return hashlib.sha256(content).hexdigest()
