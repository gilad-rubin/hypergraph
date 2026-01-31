"""Tests for DiskCache persistence across runs."""

from __future__ import annotations

import pytest

from hypergraph import Graph, node, SyncRunner

diskcache = pytest.importorskip("diskcache", reason="diskcache not installed")

from hypergraph import DiskCache  # noqa: E402


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
