"""Tests for DiskCache persistence across runs."""

from __future__ import annotations

import os

import pytest

from hypergraph import Graph, SyncRunner, node

diskcache = pytest.importorskip("diskcache", reason="diskcache not installed")

from hypergraph import DiskCache  # noqa: E402
from hypergraph.cache import _HMAC_KEY_FILENAME  # noqa: E402


class TestDiskCachePersistence:
    """DiskCache persists results across separate runner instances."""

    def test_cross_runner_cache_hit(self, tmp_path):
        """Second runner with same DiskCache directory skips execution."""
        counter = {"n": 0}

        @node(output_name="result", cache=True)
        def expensive(x: int) -> int:
            counter["n"] += 1
            return x * 2

        graph = Graph([expensive])
        cache_dir = str(tmp_path / "cache")

        # First run
        r1 = SyncRunner(cache=DiskCache(cache_dir))
        result1 = r1.run(graph, {"x": 5})
        assert result1["result"] == 10
        assert counter["n"] == 1

        # Second run with fresh runner but same disk dir
        r2 = SyncRunner(cache=DiskCache(cache_dir))
        result2 = r2.run(graph, {"x": 5})
        assert result2["result"] == 10
        assert counter["n"] == 1  # Not executed again

    def test_different_inputs_not_cached(self, tmp_path):
        """Different inputs produce different cache keys on disk."""
        counter = {"n": 0}

        @node(output_name="result", cache=True)
        def expensive(x: int) -> int:
            counter["n"] += 1
            return x * 2

        graph = Graph([expensive])
        cache = DiskCache(str(tmp_path / "cache"))
        runner = SyncRunner(cache=cache)

        runner.run(graph, {"x": 1})
        runner.run(graph, {"x": 2})
        assert counter["n"] == 2


class TestDiskCacheHMACIntegrity:
    """HMAC verification prevents deserialization of tampered cache entries."""

    def test_hmac_key_created_on_init(self, tmp_path):
        """HMAC key file is created in the cache directory."""
        cache_dir = str(tmp_path / "cache")
        DiskCache(cache_dir)
        key_path = os.path.join(cache_dir, _HMAC_KEY_FILENAME)
        assert os.path.exists(key_path)
        with open(key_path, "rb") as f:
            assert len(f.read()) == 32

    def test_hmac_key_reused_across_instances(self, tmp_path):
        """Same HMAC key is loaded when reopening the same cache directory."""
        cache_dir = str(tmp_path / "cache")
        c1 = DiskCache(cache_dir)
        c2 = DiskCache(cache_dir)
        assert c1._hmac_key == c2._hmac_key

    def test_valid_entry_passes_hmac_check(self, tmp_path):
        """Normal set/get round-trip succeeds with HMAC verification."""
        cache = DiskCache(str(tmp_path / "cache"))
        cache.set("k1", {"result": 42})
        hit, value = cache.get("k1")
        assert hit is True
        assert value == {"result": 42}

    def test_tampered_value_rejected(self, tmp_path):
        """Directly modifying the cached value causes HMAC mismatch."""
        cache_dir = str(tmp_path / "cache")
        cache = DiskCache(cache_dir)
        cache.set("k1", {"result": 42})

        # Tamper: overwrite the value directly in the underlying diskcache
        cache._cache.set("k1", {"result": 9999})

        hit, value = cache.get("k1")
        assert hit is False
        assert value is None

    def test_missing_hmac_entry_rejected(self, tmp_path):
        """Entry without a corresponding HMAC is treated as a miss."""
        cache_dir = str(tmp_path / "cache")
        cache = DiskCache(cache_dir)

        # Write value directly to underlying cache (no HMAC)
        cache._cache.set("sneaky", {"payload": "evil"})

        hit, value = cache.get("sneaky")
        assert hit is False
        assert value is None

    def test_different_hmac_key_rejects_entries(self, tmp_path):
        """Entries written with one key are rejected by a different key."""
        cache_dir = str(tmp_path / "cache")
        cache = DiskCache(cache_dir)
        cache.set("k1", {"result": 42})

        # Overwrite the HMAC key file with a different key
        key_path = os.path.join(cache_dir, _HMAC_KEY_FILENAME)
        with open(key_path, "wb") as f:
            f.write(b"\x00" * 32)

        # New instance loads the different key
        cache2 = DiskCache(cache_dir)
        hit, value = cache2.get("k1")
        assert hit is False
        assert value is None

    def test_tampered_hmac_rejected(self, tmp_path):
        """Modifying the stored HMAC causes rejection."""
        cache_dir = str(tmp_path / "cache")
        cache = DiskCache(cache_dir)
        cache.set("k1", {"result": 42})

        # Tamper with the HMAC entry
        cache._cache.set("k1" + DiskCache._HMAC_SUFFIX, "bogus_hmac")

        hit, value = cache.get("k1")
        assert hit is False
        assert value is None

    def test_hmac_key_file_permissions(self, tmp_path):
        """HMAC key file has restrictive permissions (owner-only)."""
        cache_dir = str(tmp_path / "cache")
        DiskCache(cache_dir)
        key_path = os.path.join(cache_dir, _HMAC_KEY_FILENAME)
        mode = os.stat(key_path).st_mode & 0o777
        assert mode == 0o600
