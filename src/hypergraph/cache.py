"""Cache backends for node result caching.

Provides a protocol for cache backends and two implementations:
- InMemoryCache: dict-based, lives for the runner's lifetime
- DiskCache: wraps diskcache.Cache (optional dependency), persists across runs
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import pickle
import secrets
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


_HMAC_KEY_FILENAME = ".hypergraph_hmac_key"


def _load_or_create_hmac_key(cache_dir: str) -> bytes:
    """Load or generate a 32-byte HMAC key for the given cache directory.

    The key is stored as a file inside the cache directory. If the file
    doesn't exist, a new random key is generated. File permissions are
    restricted to owner-only on Unix systems.
    """
    key_path = os.path.join(cache_dir, _HMAC_KEY_FILENAME)
    try:
        with open(key_path, "rb") as f:
            key = f.read()
        if len(key) == 32:
            return key
    except FileNotFoundError:
        pass

    os.makedirs(cache_dir, exist_ok=True)
    key = secrets.token_bytes(32)

    # Write with restrictive permissions (owner-only)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)

    return key


def _compute_hmac(hmac_key: bytes, cache_key: str, value: Any) -> str:
    """Compute HMAC-SHA256 over the cache key and pickled value."""
    value_bytes = pickle.dumps(value)
    msg = cache_key.encode() + value_bytes
    return hmac.new(hmac_key, msg, hashlib.sha256).hexdigest()


class DiskCache:
    """Persistent disk-based cache using diskcache.

    Requires ``pip install hypergraph[cache]`` (installs diskcache).

    Each cached value is stored with an HMAC-SHA256 signature to detect
    tampering. A per-directory secret key is generated on first use and
    stored in the cache directory. If the HMAC check fails on read, the
    entry is treated as a cache miss and evicted.

    Args:
        cache_dir: Path to the cache directory.
        **kwargs: Additional arguments passed to ``diskcache.Cache``.

    Example:
        >>> cache = DiskCache("/tmp/hg-cache")
        >>> cache.set("key", "value")
        >>> cache.get("key")
        (True, 'value')
    """

    _HMAC_SUFFIX = ":hmac"

    def __init__(self, cache_dir: str = "~/.cache/hypergraph", **kwargs: Any) -> None:
        try:
            import diskcache
        except ImportError:
            raise ImportError(
                "diskcache is required for DiskCache. "
                "Install it with: pip install 'hypergraph[cache]'"
            ) from None

        expanded = os.path.expanduser(cache_dir)
        self._cache = diskcache.Cache(expanded, **kwargs)
        self._hmac_key = _load_or_create_hmac_key(expanded)

    def get(self, key: str) -> tuple[bool, Any]:
        """Return (hit, value) from disk cache.

        Verifies HMAC integrity before returning. Tampered or unsigned
        entries are treated as cache misses and evicted.
        """
        sentinel = object()
        value = self._cache.get(key, default=sentinel)
        if value is sentinel:
            return False, None

        stored_hmac = self._cache.get(key + self._HMAC_SUFFIX, default=None)
        if stored_hmac is None:
            logger.warning("Cache entry missing HMAC for key %s — evicting", key)
            self._cache.delete(key)
            return False, None

        try:
            expected_hmac = _compute_hmac(self._hmac_key, key, value)
        except (pickle.PicklingError, TypeError, AttributeError):
            logger.warning("Cache HMAC computation failed for key %s — evicting", key)
            self._cache.delete(key)
            self._cache.delete(key + self._HMAC_SUFFIX)
            return False, None

        if not hmac.compare_digest(stored_hmac, expected_hmac):
            logger.warning(
                "Cache HMAC mismatch for key %s — possible tampering, evicting",
                key,
            )
            self._cache.delete(key)
            self._cache.delete(key + self._HMAC_SUFFIX)
            return False, None

        return True, value

    def set(self, key: str, value: Any) -> None:
        """Store a value to disk cache with HMAC signature.

        Skips silently if value is not picklable.
        """
        try:
            value_hmac = _compute_hmac(self._hmac_key, key, value)
            self._cache.set(key, value)
            self._cache.set(key + self._HMAC_SUFFIX, value_hmac)
        except (pickle.PicklingError, TypeError, AttributeError):
            logger.warning("Cache write skipped: output not picklable for key %s", key)


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
