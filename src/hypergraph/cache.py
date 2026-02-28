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

    Uses O_CREAT|O_EXCL for atomic creation so concurrent processes
    racing to initialize the same directory converge on one key.
    """
    key_path = os.path.join(cache_dir, _HMAC_KEY_FILENAME)
    try:
        with open(key_path, "rb") as f:
            key = f.read()
        if len(key) == 32:
            return key
        logger.warning(
            "HMAC key file has invalid length (%d bytes), regenerating",
            len(key),
        )
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("Failed to read HMAC key file (%s), regenerating", exc)

    os.makedirs(cache_dir, exist_ok=True)
    key = secrets.token_bytes(32)

    try:
        # O_EXCL fails if the file already exists — prevents race conditions
        # where two processes both try to create the key simultaneously.
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        return key
    except FileExistsError:
        # Another process created the file first — read their key
        with open(key_path, "rb") as f:
            key = f.read()
        if len(key) == 32:
            return key
        # Fall through to overwrite if the winner wrote a bad key
    except OSError:
        pass

    # Fallback: overwrite (covers invalid-length key regeneration)
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)

    return key


def _compute_hmac_bytes(hmac_key: bytes, cache_key: str, raw_bytes: bytes) -> str:
    """Compute HMAC-SHA256 over the cache key and raw serialized bytes."""
    msg = cache_key.encode() + raw_bytes
    return hmac.new(hmac_key, msg, hashlib.sha256).hexdigest()


class DiskCache:
    """Persistent disk-based cache using diskcache.

    Requires ``pip install hypergraph[cache]`` (installs diskcache).

    Values are serialized to bytes with pickle at write time. The raw bytes
    and an HMAC-SHA256 signature are stored together. On read, the HMAC is
    verified *before* deserialization, so tampered payloads are never
    unpickled. A per-directory secret key is generated on first use and
    stored in the cache directory.

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
            raise ImportError("diskcache is required for DiskCache. Install it with: pip install 'hypergraph[cache]'") from None

        expanded = os.path.expanduser(cache_dir)
        self._cache = diskcache.Cache(expanded, **kwargs)
        self._hmac_key = _load_or_create_hmac_key(expanded)

    def get(self, key: str) -> tuple[bool, Any]:
        """Return (hit, value) from disk cache.

        Verifies HMAC integrity *before* deserializing. Tampered or
        unsigned entries are never unpickled — they are evicted and
        treated as cache misses.
        """
        sentinel = object()
        # Raw bytes — diskcache stores bytes as-is (binary mode), no pickle.load
        raw_bytes = self._cache.get(key, default=sentinel)
        if raw_bytes is sentinel:
            return False, None

        if not isinstance(raw_bytes, bytes):
            # Legacy entry written before HMAC migration — evict
            logger.warning("Cache entry is not raw bytes for key %s — evicting", key)
            self._cache.delete(key)
            return False, None

        stored_hmac = self._cache.get(key + self._HMAC_SUFFIX, default=None)
        if stored_hmac is None:
            logger.warning("Cache entry missing HMAC for key %s — evicting", key)
            self._cache.delete(key)
            return False, None

        expected_hmac = _compute_hmac_bytes(self._hmac_key, key, raw_bytes)

        if not isinstance(stored_hmac, str):
            logger.warning("Cache HMAC has invalid type for key %s — evicting", key)
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

        # HMAC verified — safe to deserialize
        try:
            value = pickle.loads(raw_bytes)  # noqa: S301
        except Exception:
            logger.warning("Cache deserialization failed for key %s — evicting", key)
            self._cache.delete(key)
            self._cache.delete(key + self._HMAC_SUFFIX)
            return False, None

        return True, value

    def set(self, key: str, value: Any) -> None:
        """Store a value to disk cache with HMAC signature.

        Serializes to bytes first, computes HMAC over the raw bytes,
        then stores both. Skips silently if value is not picklable.
        """
        try:
            raw_bytes = pickle.dumps(value)
        except (pickle.PicklingError, TypeError, AttributeError):
            logger.warning("Cache write skipped: output not picklable for key %s", key)
            return

        value_hmac = _compute_hmac_bytes(self._hmac_key, key, raw_bytes)
        # Store raw bytes — diskcache keeps bytes in binary mode, no extra pickling
        self._cache.set(key, raw_bytes)
        self._cache.set(key + self._HMAC_SUFFIX, value_hmac)


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
