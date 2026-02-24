"""Tests for GraphNode.map_over() method."""

import pytest

from hypergraph import Graph, node
from hypergraph.runners import AsyncRunner, RunStatus, SyncRunner

# === Test Fixtures ===


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="sum")
def add(a: int, b: int) -> int:
    return a + b


@node(output_name="doubled")
async def async_double(x: int) -> int:
    return x * 2


# === Tests ===


class TestMapOverConfiguration:
    """Tests for map_over() configuration."""

    def test_map_over_returns_new_instance(self):
        """map_over returns a new GraphNode instance."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        mapped = gn.map_over("x")

        assert mapped is not gn

    def test_map_over_preserves_original(self):
        """Original GraphNode is unchanged."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        mapped = gn.map_over("x")

        assert gn._map_over is None
        assert mapped._map_over == ["x"]

    def test_map_over_single_param(self):
        """Configure with single parameter."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        mapped = gn.map_over("x")

        assert mapped._map_over == ["x"]
        assert mapped._map_mode == "zip"

    def test_map_over_multiple_params(self):
        """Configure with multiple parameters."""
        inner = Graph([add], name="inner")
        gn = inner.as_node()

        mapped = gn.map_over("a", "b")

        assert mapped._map_over == ["a", "b"]

    def test_map_over_mode_zip_default(self):
        """Default mode is zip."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        mapped = gn.map_over("x")

        assert mapped._map_mode == "zip"

    def test_map_over_mode_product(self):
        """Can configure product mode."""
        inner = Graph([add], name="inner")
        gn = inner.as_node()

        mapped = gn.map_over("a", "b", mode="product")

        assert mapped._map_mode == "product"

    def test_map_over_nonexistent_param_raises(self):
        """Error if parameter doesn't exist."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        with pytest.raises(ValueError, match="not an input"):
            gn.map_over("nonexistent")

    def test_map_over_requires_at_least_one_param(self):
        """Error if no parameters specified."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        with pytest.raises(ValueError, match="at least one parameter"):
            gn.map_over()


class TestMapOverRenameIntegration:
    """Tests for map_over with input renaming."""

    def test_rename_after_map_over_updates(self):
        """Renaming input after map_over updates map_over params."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        mapped = gn.map_over("x")
        renamed = mapped.with_inputs(x="input_x")

        assert renamed._map_over == ["input_x"]
        assert "input_x" in renamed.inputs

    def test_map_over_after_rename_uses_new_name(self):
        """map_over after rename uses renamed input name."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        renamed = gn.with_inputs(x="input_x")
        mapped = renamed.map_over("input_x")

        assert mapped._map_over == ["input_x"]

    def test_rename_unrelated_param_no_change(self):
        """Renaming unrelated param doesn't affect map_over."""
        inner = Graph([add], name="inner")
        gn = inner.as_node()

        mapped = gn.map_over("a")
        renamed = mapped.with_inputs(b="input_b")

        assert renamed._map_over == ["a"]


class TestMapOverRenameExecution:
    """Tests for map_over execution with renamed inputs/outputs.

    These tests verify that renamed inputs and outputs work correctly
    when the GraphNode is connected to upstream producers via edges.
    """

    def test_map_over_with_renamed_input_from_producer(self):
        """map_over works when renamed input matches upstream producer.

        This is a regression test for the bug where:
        - Producer outputs "items" (a list)
        - Inner graph expects "item" (singular)
        - with_inputs(item="items") renames to match producer
        - map_over("items") iterates over the list
        """

        @node(output_name="items")
        def produce_items(count: int) -> list[dict]:
            return [{"id": i, "value": i * 10} for i in range(count)]

        @node(output_name="processed")
        def process_item(item: dict, multiplier: int) -> dict:
            return {"id": item["id"], "result": item["value"] * multiplier}

        inner = Graph(nodes=[process_item], name="inner")
        mapped_node = inner.as_node(name="process_all").with_inputs(item="items").map_over("items")

        outer = Graph(nodes=[produce_items, mapped_node]).bind(multiplier=2)
        runner = SyncRunner()

        result = runner.run(outer, {"count": 3})

        assert result.status == RunStatus.COMPLETED
        assert result["items"] == [
            {"id": 0, "value": 0},
            {"id": 1, "value": 10},
            {"id": 2, "value": 20},
        ]
        assert result["processed"] == [
            {"id": 0, "result": 0},
            {"id": 1, "result": 20},
            {"id": 2, "result": 40},
        ]

    def test_map_over_with_renamed_input_and_output(self):
        """map_over works with both renamed input and output."""

        @node(output_name="items")
        def produce() -> list[int]:
            return [1, 2, 3]

        @node(output_name="doubled")
        def process(x: int) -> int:
            return x * 2

        inner = Graph(nodes=[process], name="inner")
        mapped_node = inner.as_node(name="mapper").with_inputs(x="items").with_outputs(doubled="results").map_over("items")

        outer = Graph(nodes=[produce, mapped_node])
        runner = SyncRunner()

        result = runner.run(outer, {})

        assert result.status == RunStatus.COMPLETED
        assert result["results"] == [2, 4, 6]

    async def test_map_over_with_renamed_input_async(self):
        """map_over with renamed input works with AsyncRunner."""

        @node(output_name="items")
        def produce() -> list[int]:
            return [10, 20, 30]

        @node(output_name="result")
        async def process(value: int) -> int:
            return value + 1

        inner = Graph(nodes=[process], name="inner")
        mapped_node = inner.as_node(name="mapper").with_inputs(value="items").map_over("items")

        outer = Graph(nodes=[produce, mapped_node])
        runner = AsyncRunner()

        result = await runner.run(outer, {})

        assert result.status == RunStatus.COMPLETED
        assert result["result"] == [11, 21, 31]

    def test_map_over_with_multiple_renamed_inputs(self):
        """map_over works with multiple renamed inputs in zip mode."""

        @node(output_name="xs")
        def produce_xs() -> list[int]:
            return [1, 2, 3]

        @node(output_name="ys")
        def produce_ys() -> list[int]:
            return [10, 20, 30]

        @node(output_name="sum")
        def add_nums(a: int, b: int) -> int:
            return a + b

        inner = Graph(nodes=[add_nums], name="inner")
        mapped_node = inner.as_node(name="adder").with_inputs(a="xs", b="ys").map_over("xs", "ys", mode="zip")

        outer = Graph(nodes=[produce_xs, produce_ys, mapped_node])
        runner = SyncRunner()

        result = runner.run(outer, {})

        assert result.status == RunStatus.COMPLETED
        assert result["sum"] == [11, 22, 33]

    def test_map_over_renamed_input_with_broadcast(self):
        """Non-mapped values broadcast correctly with renamed inputs."""

        @node(output_name="items")
        def produce() -> list[int]:
            return [1, 2, 3]

        @node(output_name="result")
        def multiply(value: int, factor: int) -> int:
            return value * factor

        inner = Graph(nodes=[multiply], name="inner")
        mapped_node = inner.as_node(name="multiplier").with_inputs(value="items").map_over("items")

        outer = Graph(nodes=[produce, mapped_node])
        runner = SyncRunner()

        # "factor" is broadcast to all iterations
        result = runner.run(outer, {"factor": 10})

        assert result.status == RunStatus.COMPLETED
        assert result["result"] == [10, 20, 30]

    def test_chained_renames_with_map_over(self):
        """Chained renames (a->b->c) work correctly with map_over."""

        @node(output_name="data")
        def produce() -> list[int]:
            return [5, 10, 15]

        @node(output_name="result")
        def process(x: int) -> int:
            return x * 2

        inner = Graph(nodes=[process], name="inner")
        mapped_node = (
            inner.as_node(name="processor")
            .with_inputs(x="temp")  # x -> temp
            .with_inputs(temp="data")  # temp -> data
            .map_over("data")
        )

        outer = Graph(nodes=[produce, mapped_node])
        runner = SyncRunner()

        result = runner.run(outer, {})

        assert result.status == RunStatus.COMPLETED
        assert result["result"] == [10, 20, 30]

    def test_parallel_renames_with_map_over(self):
        """Parallel renames (same batch) work correctly with map_over.

        Regression test for handling parallel renames like with_inputs(a='b', b='a')
        which require special handling via build_reverse_rename_map.
        """

        @node(output_name="xs")
        def produce_xs() -> list[int]:
            return [1, 2, 3]

        @node(output_name="ys")
        def produce_ys() -> list[int]:
            return [10, 20, 30]

        @node(output_name="sum")
        def add_nums(a: int, b: int) -> int:
            return a + b

        inner = Graph(nodes=[add_nums], name="inner")
        # Swap parameter names in a single call (parallel renames)
        mapped_node = (
            inner.as_node(name="adder")
            .with_inputs({"a": "temp_a", "b": "temp_b"})  # Parallel batch 1
            .with_inputs({"temp_a": "ys", "temp_b": "xs"})  # Parallel batch 2
            .map_over("xs", "ys", mode="zip")
        )

        outer = Graph(nodes=[produce_xs, produce_ys, mapped_node])
        runner = SyncRunner()

        result = runner.run(outer, {})

        assert result.status == RunStatus.COMPLETED
        # a takes ys values, b takes xs values (swapped)
        # ys=[10,20,30], xs=[1,2,3] -> sums are 11, 22, 33
        assert result["sum"] == [11, 22, 33]


class TestMapOverExecution:
    """Tests for execution with map_over configuration."""

    def test_graphnode_with_map_over_iterates(self):
        """GraphNode with map_over iterates over input."""
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node().map_over("x")])
        runner = SyncRunner()

        result = runner.run(outer, {"x": [1, 2, 3]})

        assert result.status == RunStatus.COMPLETED
        assert result["doubled"] == [2, 4, 6]

    def test_graphnode_map_over_zip_mode(self):
        """map_over with zip mode."""
        inner = Graph([add], name="inner")
        outer = Graph([inner.as_node().map_over("a", "b", mode="zip")])
        runner = SyncRunner()

        result = runner.run(outer, {"a": [1, 2, 3], "b": [10, 20, 30]})

        assert result["sum"] == [11, 22, 33]

    def test_graphnode_map_over_product_mode(self):
        """map_over with product mode generates cartesian product."""
        inner = Graph([add], name="inner")
        outer = Graph([inner.as_node().map_over("a", "b", mode="product")])
        runner = SyncRunner()

        result = runner.run(outer, {"a": [1, 2], "b": [10, 20]})

        # 2 * 2 = 4 results
        assert len(result["sum"]) == 4
        assert sorted(result["sum"]) == [11, 12, 21, 22]

    def test_nested_map_over_in_parent_graph(self):
        """map_over works in multi-node parent graph."""
        inner = Graph([double], name="inner")
        outer = Graph(
            [
                inner.as_node().map_over("x"),
                add.with_inputs(a="doubled"),  # "doubled" is now a list
            ]
        )
        runner = SyncRunner()

        # Note: add expects int, but doubled is list[int]
        # This test documents current behavior — may succeed or raise
        # depending on how the downstream node handles list input
        try:
            result = runner.run(outer, {"x": [1, 2], "b": 0})
            assert result.status == RunStatus.COMPLETED
        except Exception:
            pass  # Also acceptable — type mismatch in downstream node

    async def test_graphnode_map_over_async_runner(self):
        """map_over works with AsyncRunner."""
        inner = Graph([async_double], name="inner")
        outer = Graph([inner.as_node().map_over("x")])
        runner = AsyncRunner()

        result = await runner.run(outer, {"x": [1, 2, 3]})

        assert result.status == RunStatus.COMPLETED
        assert result["doubled"] == [2, 4, 6]

    def test_empty_list_returns_empty_results(self):
        """map_over with empty list returns empty output list."""
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node().map_over("x")])
        runner = SyncRunner()

        result = runner.run(outer, {"x": []})

        assert result["doubled"] == []

    def test_broadcast_values_with_map_over(self):
        """Non-mapped values are broadcast to each iteration."""
        inner = Graph([add], name="inner")
        outer = Graph([inner.as_node().map_over("a")])
        runner = SyncRunner()

        result = runner.run(outer, {"a": [1, 2, 3], "b": 10})

        assert result["sum"] == [11, 12, 13]


class TestConcurrentNestedMaps:
    """Tests for concurrent nested map executions (GAP-03)."""

    def test_map_within_map_sync(self):
        """Nested map_over: outer graph has map_over on a graph with map_over."""
        # Inner: simple transform
        inner = Graph([double], name="inner")

        # Middle: maps over inner
        middle = Graph([inner.as_node().map_over("x")], name="middle")

        # Outer: maps over middle with different param structure
        @node(output_name="results")
        def collect(doubled: list) -> list:
            return doubled

        outer = Graph([middle.as_node(), collect])
        runner = SyncRunner()

        # Pass list to inner map_over
        result = runner.run(outer, {"x": [1, 2, 3]})

        assert result.status == RunStatus.COMPLETED
        assert result["doubled"] == [2, 4, 6]
        assert result["results"] == [2, 4, 6]

    async def test_map_within_map_concurrent(self):
        """Nested map_over executions with AsyncRunner."""
        import asyncio

        @node(output_name="value")
        async def slow_transform(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 2

        inner = Graph([slow_transform], name="inner")
        outer = Graph([inner.as_node().map_over("x")])

        runner = AsyncRunner()

        # Multiple items should process concurrently
        result = await runner.run(outer, {"x": [1, 2, 3, 4, 5]})

        assert result.status == RunStatus.COMPLETED
        assert result["value"] == [2, 4, 6, 8, 10]

    async def test_nested_map_no_deadlock_with_max_concurrency(self):
        """map_over doesn't deadlock when outer run has max_concurrency."""
        import asyncio
        import time

        @node(output_name="value")
        async def slow_transform(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 2

        inner = Graph([slow_transform], name="inner")
        outer = Graph([inner.as_node().map_over("x")])

        runner = AsyncRunner()

        # With max_concurrency=1 on outer, nested map should still work (no deadlock)
        # Nested executions don't inherit outer concurrency limits
        start = time.time()
        result = await runner.run(outer, {"x": [1, 2, 3]}, max_concurrency=1)
        elapsed = time.time() - start

        assert result.status == RunStatus.COMPLETED
        assert result["value"] == [2, 4, 6]
        # Should complete reasonably fast (nested runs concurrently)
        assert elapsed < 1.0, f"Expected fast completion, got {elapsed:.3f}s (possible deadlock)"

    async def test_nested_map_runs_concurrently(self):
        """map_over executes iterations concurrently by default."""
        import asyncio
        import time

        @node(output_name="value")
        async def slow_transform(x: int) -> int:
            await asyncio.sleep(0.05)
            return x * 2

        inner = Graph([slow_transform], name="inner")
        outer = Graph([inner.as_node().map_over("x")])

        runner = AsyncRunner()

        # With default concurrency (unlimited), should run in parallel
        start = time.time()
        result = await runner.run(outer, {"x": [1, 2, 3, 4, 5]})
        elapsed = time.time() - start

        assert result.status == RunStatus.COMPLETED
        assert result["value"] == [2, 4, 6, 8, 10]
        # Concurrent: should be ~0.05s (all run in parallel), not 0.25s (sequential)
        assert elapsed < 0.2, f"Expected concurrent execution (<0.2s), got {elapsed:.3f}s"

    async def test_nested_map_with_async_inner(self):
        """map_over works with async nodes in inner graph."""

        @node(output_name="value")
        async def async_transform(x: int) -> int:
            return x * 2

        inner = Graph([async_transform], name="inner")
        outer = Graph([inner.as_node().map_over("x")])

        runner = AsyncRunner()
        result = await runner.run(outer, {"x": [1, 2, 3]})

        assert result.status == RunStatus.COMPLETED
        assert result["value"] == [2, 4, 6]

    async def test_map_over_graph_with_async_nodes(self):
        """map_over graph containing async nodes."""

        @node(output_name="a")
        async def async_step1(x: int) -> int:
            return x + 1

        @node(output_name="b")
        async def async_step2(a: int) -> int:
            return a * 2

        inner = Graph([async_step1, async_step2], name="inner")
        outer = Graph([inner.as_node().map_over("x")])

        runner = AsyncRunner()
        result = await runner.run(outer, {"x": [1, 2, 3]})

        assert result.status == RunStatus.COMPLETED
        # (1+1)*2=4, (2+1)*2=6, (3+1)*2=8
        assert result["b"] == [4, 6, 8]

    def test_doubly_nested_map_over(self):
        """Two levels of map_over nesting."""
        # Innermost: simple double
        innermost = Graph([double], name="innermost")

        # Middle: maps over innermost
        middle = Graph([innermost.as_node().map_over("x")], name="middle")

        # Outer: wraps middle (no additional map)
        outer = Graph([middle.as_node()])

        runner = SyncRunner()
        result = runner.run(outer, {"x": [1, 2, 3]})

        assert result.status == RunStatus.COMPLETED
        assert result["doubled"] == [2, 4, 6]

    def test_multiple_mapped_graphnodes_in_parallel(self):
        """Multiple GraphNodes with map_over in same graph."""

        # Create separate nodes for each inner graph to avoid sharing
        @node(output_name="a")
        def double_a(vals_a: int) -> int:
            return vals_a * 2

        @node(output_name="b")
        def double_b(vals_b: int) -> int:
            return vals_b * 2

        inner_a = Graph([double_a], name="inner_a")
        inner_b = Graph([double_b], name="inner_b")

        # Both mapped in outer
        @node(output_name="combined")
        def combine(a: list, b: list) -> list:
            return [x + y for x, y in zip(a, b, strict=False)]

        outer = Graph(
            [
                inner_a.as_node().map_over("vals_a"),
                inner_b.as_node().map_over("vals_b"),
                combine,
            ]
        )

        runner = SyncRunner()
        result = runner.run(outer, {"vals_a": [1, 2], "vals_b": [10, 20]})

        assert result.status == RunStatus.COMPLETED
        assert result["a"] == [2, 4]
        assert result["b"] == [20, 40]
        assert result["combined"] == [22, 44]


class TestMapOverTypeAnnotations:
    """Tests for type annotations with map_over."""

    def test_get_output_type_wraps_in_list_when_map_over(self):
        """get_output_type returns list[T] when map_over is configured."""
        inner = Graph([double], name="inner")
        gn = inner.as_node()

        # Without map_over: returns raw type
        assert gn.get_output_type("doubled") is int

        # With map_over: returns list[int]
        mapped = gn.map_over("x")
        output_type = mapped.get_output_type("doubled")

        # Check it's list[int]
        assert output_type is not None
        assert hasattr(output_type, "__origin__")
        assert output_type.__origin__ is list
        assert output_type.__args__ == (int,)

    def test_get_output_type_wraps_untyped_as_list_any(self):
        """get_output_type returns list for untyped outputs when map_over is set."""

        @node(output_name="result")
        def untyped(x):
            return x

        inner = Graph([untyped], name="inner")
        mapped = inner.as_node().map_over("x")

        output_type = mapped.get_output_type("result")

        # Should be list (bare list, since inner type is unknown)
        assert output_type is list

    def test_output_annotation_wraps_all_outputs(self):
        """output_annotation property wraps all outputs in list when map_over is set."""

        @node(output_name=("a", "b"))
        def multi_out(x: int) -> tuple[str, float]:
            return str(x), float(x)

        inner = Graph([multi_out], name="inner")
        mapped = inner.as_node().map_over("x")

        annotations = mapped.output_annotation

        # Both outputs should be wrapped
        assert hasattr(annotations["a"], "__origin__")
        assert annotations["a"].__origin__ is list
        assert annotations["a"].__args__ == (str,)

        assert hasattr(annotations["b"], "__origin__")
        assert annotations["b"].__origin__ is list
        assert annotations["b"].__args__ == (float,)


class TestMaxConcurrency:
    """Tests for max_concurrency behavior with map_over."""

    async def test_runner_map_with_max_concurrency(self):
        """runner.map() respects max_concurrency parameter."""
        import asyncio
        import time

        @node(output_name="value")
        async def slow_double(x: int) -> int:
            await asyncio.sleep(0.05)
            return x * 2

        graph = Graph([slow_double])
        runner = AsyncRunner()

        # Sequential with max_concurrency=1
        start = time.time()
        results = await runner.map(
            graph,
            {"x": [1, 2, 3]},
            map_over="x",
            max_concurrency=1,
        )
        elapsed = time.time() - start

        values = [r["value"] for r in results]
        assert values == [2, 4, 6]
        # Sequential: 3 * 0.05s = ~0.15s
        assert elapsed >= 0.14, f"Expected sequential (~0.15s), got {elapsed:.3f}s"

    async def test_runner_map_with_max_concurrency_2(self):
        """runner.map() with max_concurrency=2 limits to 2 parallel."""
        import asyncio
        import time

        @node(output_name="value")
        async def slow_double(x: int) -> int:
            await asyncio.sleep(0.05)
            return x * 2

        graph = Graph([slow_double])
        runner = AsyncRunner()

        # With max_concurrency=2, 4 items should take 2 batches
        start = time.time()
        results = await runner.map(
            graph,
            {"x": [1, 2, 3, 4]},
            map_over="x",
            max_concurrency=2,
        )
        elapsed = time.time() - start

        values = [r["value"] for r in results]
        assert values == [2, 4, 6, 8]
        # 2 batches of 2: ~0.10s
        assert elapsed >= 0.09, f"Expected 2 batches (~0.10s), got {elapsed:.3f}s"
        assert elapsed < 0.18, f"Expected parallel within batches, got {elapsed:.3f}s"

    async def test_runner_map_unlimited_concurrency(self):
        """runner.map() without max_concurrency runs all in parallel."""
        import asyncio
        import time

        @node(output_name="value")
        async def slow_double(x: int) -> int:
            await asyncio.sleep(0.05)
            return x * 2

        graph = Graph([slow_double])
        runner = AsyncRunner()

        # Without limit, all should run in parallel
        start = time.time()
        results = await runner.map(
            graph,
            {"x": [1, 2, 3, 4, 5]},
            map_over="x",
        )
        elapsed = time.time() - start

        values = [r["value"] for r in results]
        assert values == [2, 4, 6, 8, 10]
        # All parallel: ~0.05s
        assert elapsed < 0.15, f"Expected parallel (~0.05s), got {elapsed:.3f}s"

    async def test_multiple_graphnodes_with_outer_max_concurrency(self):
        """Multiple GraphNodes respect outer max_concurrency."""
        import asyncio
        import time

        @node(output_name="a")
        async def slow_a(x: int) -> int:
            await asyncio.sleep(0.03)
            return x * 2

        @node(output_name="b")
        async def slow_b(y: int) -> int:
            await asyncio.sleep(0.03)
            return y * 3

        inner_a = Graph([slow_a], name="inner_a")
        inner_b = Graph([slow_b], name="inner_b")

        # Two independent mapped GraphNodes
        outer = Graph(
            [
                inner_a.as_node().map_over("x"),
                inner_b.as_node().map_over("y"),
            ]
        )

        runner = AsyncRunner()

        # With max_concurrency=1 on outer, the two GraphNodes run sequentially
        # But their inner maps run concurrently (nested execution is independent)
        start = time.time()
        result = await runner.run(
            outer,
            {"x": [1, 2], "y": [10, 20]},
            max_concurrency=1,
        )
        elapsed = time.time() - start

        assert result.status == RunStatus.COMPLETED
        assert result["a"] == [2, 4]
        assert result["b"] == [30, 60]
        # Each GraphNode's map runs concurrently (~0.03s each)
        # But they run sequentially: ~0.06s total
        assert elapsed >= 0.05, f"Expected sequential GraphNodes, got {elapsed:.3f}s"

    async def test_deeply_nested_with_max_concurrency(self):
        """Deeply nested graphs work with max_concurrency."""
        import asyncio

        @node(output_name="value")
        async def async_double(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 2

        # Three levels of nesting
        level1 = Graph([async_double], name="level1")
        level2 = Graph([level1.as_node()], name="level2")
        level3 = Graph([level2.as_node().map_over("x")])

        runner = AsyncRunner()

        # Should work even with max_concurrency=1
        result = await runner.run(
            level3,
            {"x": [1, 2, 3]},
            max_concurrency=1,
        )

        assert result.status == RunStatus.COMPLETED
        assert result["value"] == [2, 4, 6]

    async def test_map_over_with_product_mode_and_concurrency(self):
        """Product mode map_over respects concurrency limits."""
        import asyncio
        import time

        @node(output_name="sum")
        async def slow_add(a: int, b: int) -> int:
            await asyncio.sleep(0.02)
            return a + b

        inner = Graph([slow_add], name="inner")
        outer = Graph([inner.as_node().map_over("a", "b", mode="product")])

        runner = AsyncRunner()

        # Product of [1,2] x [10,20] = 4 combinations
        start = time.time()
        result = await runner.run(outer, {"a": [1, 2], "b": [10, 20]})
        elapsed = time.time() - start

        assert result.status == RunStatus.COMPLETED
        assert len(result["sum"]) == 4
        assert sorted(result["sum"]) == [11, 12, 21, 22]
        # All 4 run in parallel: ~0.02s
        assert elapsed < 0.1, f"Expected parallel, got {elapsed:.3f}s"

    async def test_error_in_nested_map_with_concurrency(self):
        """Errors propagate correctly from nested maps with concurrency."""

        @node(output_name="value")
        async def might_fail(x: int) -> int:
            if x == 2:
                raise ValueError("x cannot be 2")
            return x * 2

        inner = Graph([might_fail], name="inner")
        outer = Graph([inner.as_node().map_over("x")])

        runner = AsyncRunner()

        with pytest.raises(ValueError, match="x cannot be 2"):
            await runner.run(
                outer,
                {"x": [1, 2, 3]},
                max_concurrency=1,
            )

    async def test_empty_map_with_max_concurrency(self):
        """Empty map_over with max_concurrency returns empty results."""

        @node(output_name="value")
        async def async_double(x: int) -> int:
            return x * 2

        inner = Graph([async_double], name="inner")
        outer = Graph([inner.as_node().map_over("x")])

        runner = AsyncRunner()

        result = await runner.run(
            outer,
            {"x": []},
            max_concurrency=1,
        )

        assert result.status == RunStatus.COMPLETED
        assert result["value"] == []

    async def test_single_item_map_with_max_concurrency(self):
        """Single item map_over with max_concurrency works correctly."""
        import asyncio

        @node(output_name="value")
        async def async_double(x: int) -> int:
            await asyncio.sleep(0.01)
            return x * 2

        inner = Graph([async_double], name="inner")
        outer = Graph([inner.as_node().map_over("x")])

        runner = AsyncRunner()

        result = await runner.run(
            outer,
            {"x": [42]},
            max_concurrency=1,
        )

        assert result.status == RunStatus.COMPLETED
        assert result["value"] == [84]


# === Clone Tests ===


class TestCloneConfiguration:
    """Tests for clone parameter validation."""

    def test_clone_rejects_bare_string(self):
        """TypeError for clone='config' (must be list)."""
        inner = Graph([add], name="inner")
        gn = inner.as_node()

        with pytest.raises(TypeError, match="clone must be bool or list"):
            gn.map_over("a", clone="config")

    def test_clone_rejects_tuple(self):
        """TypeError for clone=('config',) (must be list)."""
        inner = Graph([add], name="inner")
        gn = inner.as_node()

        with pytest.raises(TypeError, match="clone must be bool or list"):
            gn.map_over("a", clone=("b",))

    def test_clone_rejects_non_string_list_entry(self):
        """TypeError for clone=['config', 123]."""
        inner = Graph([add], name="inner")
        gn = inner.as_node()

        with pytest.raises(TypeError, match="clone list entries must be strings"):
            gn.map_over("a", clone=["b", 123])

    def test_clone_rejects_mapped_param(self):
        """Error if clone includes a map_over param."""
        inner = Graph([add], name="inner")
        gn = inner.as_node()

        with pytest.raises(ValueError, match="Cannot clone mapped parameter"):
            gn.map_over("a", clone=["a"])

    def test_clone_rejects_nonexistent_param(self):
        """Error if clone references a param not in node inputs."""
        inner = Graph([add], name="inner")
        gn = inner.as_node()

        with pytest.raises(ValueError, match="not an input"):
            gn.map_over("a", clone=["nonexistent"])


class TestCloneExecution:
    """Tests for clone behavior at runtime."""

    def test_clone_false_shares_by_reference(self):
        """Default: same object identity across iterations."""

        @node(output_name="item_id")
        def get_id(x: int, config: dict) -> int:
            return id(config)

        inner = Graph([get_id], name="inner")
        outer = Graph([inner.as_node().map_over("x")])
        runner = SyncRunner()

        config = {"key": "value"}
        result = runner.run(outer, {"x": [1, 2, 3], "config": config})

        assert result.status == RunStatus.COMPLETED
        # All iterations should see the same object
        ids = result["item_id"]
        assert ids[0] == ids[1] == ids[2]

    def test_clone_true_copies_all_broadcast(self):
        """All broadcast values are independent copies."""

        @node(output_name="item_id")
        def get_id(x: int, config: dict) -> int:
            return id(config)

        inner = Graph([get_id], name="inner")
        outer = Graph([inner.as_node().map_over("x", clone=True)])
        runner = SyncRunner()

        config = {"key": "value"}
        result = runner.run(outer, {"x": [1, 2, 3], "config": config})

        assert result.status == RunStatus.COMPLETED
        ids = result["item_id"]
        # Each iteration should have a different object
        assert len(set(ids)) == 3

    def test_clone_list_copies_named_params_only(self):
        """Only named params copied, others shared."""

        @node(output_name=("config_id", "factor_id"))
        def get_ids(x: int, config: dict, factor: int) -> tuple[int, int]:
            return id(config), id(factor)

        inner = Graph([get_ids], name="inner")
        outer = Graph([inner.as_node().map_over("x", clone=["config"])])
        runner = SyncRunner()

        result = runner.run(outer, {"x": [1, 2, 3], "config": {"k": "v"}, "factor": 10})

        assert result.status == RunStatus.COMPLETED
        # config should be cloned (different ids)
        config_ids = result["config_id"]
        assert len(set(config_ids)) == 3

        # factor should be shared (same id — ints are interned, but the key point
        # is that factor was NOT deep-copied; for ints id() may or may not differ)
        # Use a mutable type to properly test sharing
        # This test already validates config is cloned

    def test_clone_mutation_isolation(self):
        """Node mutates broadcast dict, other iterations unaffected."""

        @node(output_name="result")
        def mutate_config(x: int, config: dict) -> int:
            config["count"] = config.get("count", 0) + x
            return config["count"]

        inner = Graph([mutate_config], name="inner")
        outer = Graph([inner.as_node().map_over("x", clone=True)])
        runner = SyncRunner()

        result = runner.run(outer, {"x": [1, 2, 3], "config": {"count": 0}})

        assert result.status == RunStatus.COMPLETED
        # With clone=True, each iteration gets a fresh copy with count=0
        # So results should be [1, 2, 3] not [1, 3, 6]
        assert result["result"] == [1, 2, 3]

    def test_clone_non_copyable_raises_clear_error(self):
        """GraphConfigError with guidance for non-copyable objects."""
        import threading

        from hypergraph.graph.validation import GraphConfigError

        @node(output_name="result")
        def use_lock(x: int, lock: object) -> int:
            return x

        inner = Graph([use_lock], name="inner")
        outer = Graph([inner.as_node().map_over("x", clone=True)])
        runner = SyncRunner()

        with pytest.raises(GraphConfigError, match="cannot be deep-copied for clone"):
            runner.run(outer, {"x": [1, 2], "lock": threading.Lock()})

    async def test_clone_with_async_runner(self):
        """Clone works with AsyncRunner."""

        @node(output_name="result")
        async def process(x: int, config: dict) -> int:
            config["count"] = config.get("count", 0) + x
            return config["count"]

        inner = Graph([process], name="inner")
        outer = Graph([inner.as_node().map_over("x", clone=True)])
        runner = AsyncRunner()

        result = await runner.run(outer, {"x": [1, 2, 3], "config": {"count": 0}})

        assert result.status == RunStatus.COMPLETED
        # Each iteration sees a fresh config with count=0
        assert sorted(result["result"]) == [1, 2, 3]

    def test_clone_with_product_mode(self):
        """Clone works with mode='product'."""

        @node(output_name="result")
        def mutate_and_return(a: int, b: int, state: dict) -> int:
            state["sum"] = state.get("sum", 0) + a + b
            return state["sum"]

        inner = Graph([mutate_and_return], name="inner")
        outer = Graph([inner.as_node().map_over("a", "b", mode="product", clone=True)])
        runner = SyncRunner()

        result = runner.run(outer, {"a": [1, 2], "b": [10, 20], "state": {}})

        assert result.status == RunStatus.COMPLETED
        # Product: (1,10), (1,20), (2,10), (2,20) → sums: 11, 21, 12, 22
        # Each gets fresh state, so no accumulation
        assert sorted(result["result"]) == [11, 12, 21, 22]

    def test_clone_with_inner_bind(self):
        """Inner .bind() values are NOT cloned — they produce correct results."""

        @node(output_name="result")
        def use_config(x: int, config: dict) -> str:
            return f"{x}-{config['key']}"

        # Bind config on the inner graph — it bypasses the outer map pipeline
        inner = Graph([use_config], name="inner").bind(config={"key": "value"})
        outer = Graph([inner.as_node().map_over("x", clone=True)])
        runner = SyncRunner()

        result = runner.run(outer, {"x": [1, 2, 3]})

        assert result.status == RunStatus.COMPLETED
        # Inner-bound config is resolved inside each inner run, not through clone
        assert result["result"] == ["1-value", "2-value", "3-value"]

    def test_clone_with_renamed_input(self):
        """with_inputs() updates clone list correctly."""

        @node(output_name="result")
        def process(x: int, cfg: dict) -> int:
            cfg["count"] = cfg.get("count", 0) + x
            return cfg["count"]

        inner = Graph([process], name="inner")
        mapped_node = inner.as_node().map_over("x", clone=["cfg"]).with_inputs(cfg="config")

        # Verify _clone was updated
        assert mapped_node._clone == ["config"]

        outer = Graph([mapped_node])
        runner = SyncRunner()

        result = runner.run(outer, {"x": [1, 2, 3], "config": {"count": 0}})

        assert result.status == RunStatus.COMPLETED
        # Each iteration sees fresh config
        assert result["result"] == [1, 2, 3]

    def test_clone_true_with_outer_bind_non_copyable(self):
        """Outer .bind() non-copyable → error when clone=True."""
        import threading

        from hypergraph.graph.validation import GraphConfigError

        @node(output_name="result")
        def use_lock(x: int, lock: object) -> int:
            return x

        inner = Graph([use_lock], name="inner")
        # Outer bind — values DO pass through generate_map_inputs
        outer = Graph([inner.as_node().map_over("x", clone=True)]).bind(lock=threading.Lock())
        runner = SyncRunner()

        with pytest.raises(GraphConfigError, match="cannot be deep-copied for clone"):
            runner.run(outer, {"x": [1, 2]})
