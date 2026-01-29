"""Tests for red-team findings fixes.

Each test validates a specific issue from the consolidated red-team report.
Tests are written to fail before the fix and pass after.
"""

import pytest

from hypergraph import Graph, node, route, END, SyncRunner
from hypergraph.nodes._rename import RenameError


# === Fix #1: Mutable default arguments shared across runs ===


class TestMutableDefaults:
    def test_list_default_not_shared_across_runs(self):
        """A mutable list default must not leak between runs."""

        @node(output_name="result")
        def append_to_list(item: str, container: list = []) -> list:  # noqa: B006
            container.append(item)
            return container

        graph = Graph(nodes=[append_to_list])
        runner = SyncRunner()

        res1 = runner.run(graph, {"item": "A"})
        res2 = runner.run(graph, {"item": "B"})

        assert res1["result"] == ["A"]
        assert res2["result"] == ["B"], "List default leaked from run 1 to run 2"

    def test_dict_default_not_shared_across_runs(self):
        """A mutable dict default must not leak between runs."""

        @node(output_name="result")
        def update_dict(key: str, val: int, d: dict = {}) -> dict:  # noqa: B006
            d[key] = val
            return d

        graph = Graph(nodes=[update_dict])
        runner = SyncRunner()

        res1 = runner.run(graph, {"key": "a", "val": 1})
        res2 = runner.run(graph, {"key": "b", "val": 2})

        assert res1["result"] == {"a": 1}
        assert res2["result"] == {"b": 2}, "Dict default leaked"

    def test_non_copyable_default_warns_but_continues(self):
        """Non-copyable defaults should warn but not crash."""
        import threading
        import warnings

        lock = threading.Lock()

        @node(output_name="result")
        def use_lock(x: int, sync: threading.Lock = lock) -> int:
            # Just verifies the lock is usable
            with sync:
                return x * 2

        graph = Graph(nodes=[use_lock])
        runner = SyncRunner()

        # Should emit warning but still run successfully
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = runner.run(graph, {"x": 5})
            assert result["result"] == 10
            # Should have warned about non-copyable default
            assert any("Cannot deep-copy" in str(warning.message) for warning in w)


# === Fix #2: Cycle termination off-by-one ===


class TestCycleTermination:
    def test_no_extra_iteration_after_end(self):
        """Gate returning END must stop the loop immediately."""

        @node(output_name="count")
        def increment(count: int) -> int:
            return count + 1

        @route(targets=["increment", END])
        def check(count: int) -> str:
            return END if count >= 3 else "increment"

        graph = Graph(nodes=[increment, check])
        runner = SyncRunner()
        result = runner.run(graph, {"count": 0})

        assert result["count"] == 3, (
            f"Expected count=3, got count={result['count']}. "
            "Extra iteration after END."
        )

    def test_single_iteration_loop(self):
        """Loop that should execute exactly once."""

        @node(output_name="count")
        def increment(count: int) -> int:
            return count + 1

        @route(targets=["increment", END])
        def check(count: int) -> str:
            return END if count >= 1 else "increment"

        graph = Graph(nodes=[increment, check])
        runner = SyncRunner()
        result = runner.run(graph, {"count": 0})

        assert result["count"] == 1


# === Fix #4: Intermediate value injection ===


class TestIntermediateInjection:
    def test_skip_upstream_with_intermediate_value(self):
        """Providing an intermediate output should skip upstream nodes."""

        @node(output_name="mid")
        def step1(start: str) -> str:
            return start + "_mid"

        @node(output_name="end")
        def step2(mid: str) -> str:
            return mid + "_end"

        graph = Graph(nodes=[step1, step2])
        runner = SyncRunner()

        # Provide "mid" directly — step1 should be skipped
        result = runner.run(graph, {"mid": "SKIP"})
        assert result["end"] == "SKIP_end"

    def test_normal_execution_still_works(self):
        """Normal execution (no intermediate injection) still works."""

        @node(output_name="mid")
        def step1(start: str) -> str:
            return start + "_mid"

        @node(output_name="end")
        def step2(mid: str) -> str:
            return mid + "_end"

        graph = Graph(nodes=[step1, step2])
        runner = SyncRunner()

        result = runner.run(graph, {"start": "hello"})
        assert result["end"] == "hello_mid_end"

    def test_multi_output_partial_injection_still_requires_upstream_inputs(self):
        """Partial injection of multi-output node must still require its inputs.

        If a node produces (left, right) and user only provides "left" but NOT "x",
        validation should STILL require "x" because "right" is needed downstream.
        The bug: current code marks split as bypassed if ANY output is provided.
        """
        from hypergraph.exceptions import MissingInputError

        @node(output_name=("left", "right"))
        def split(x: int) -> tuple[int, int]:
            return x, x + 1

        @node(output_name="double_left")
        def use_left(left: int) -> int:
            return left * 2

        @node(output_name="double_right")
        def use_right(right: int) -> int:
            return right * 2

        graph = Graph(nodes=[split, use_left, use_right])
        runner = SyncRunner()

        # Provide only "left" - but "right" is still needed from split!
        # Should raise MissingInputError for "x"
        with pytest.raises(MissingInputError, match="x"):
            runner.run(graph, {"left": 100})

    def test_multi_output_full_injection_skips_node(self):
        """Full injection of ALL outputs from a node properly bypasses it."""

        @node(output_name=("left", "right"))
        def split(x: int) -> tuple[int, int]:
            return x, x + 1

        @node(output_name="double_left")
        def use_left(left: int) -> int:
            return left * 2

        @node(output_name="double_right")
        def use_right(right: int) -> int:
            return right * 2

        graph = Graph(nodes=[split, use_left, use_right])
        runner = SyncRunner()

        # Provide BOTH outputs — split is fully bypassed, no need for "x"
        result = runner.run(graph, {"left": 100, "right": 200})
        assert result["double_left"] == 200
        assert result["double_right"] == 400

    def test_multi_output_unused_output_can_be_skipped(self):
        """If a multi-output node has an unused output, partial injection works."""

        @node(output_name=("left", "right"))
        def split(x: int) -> tuple[int, int]:
            return x, x + 1

        @node(output_name="double_left")
        def use_left(left: int) -> int:
            return left * 2

        # NOTE: "right" is not consumed by anyone

        graph = Graph(nodes=[split, use_left])
        runner = SyncRunner()

        # Provide only "left" - and "right" is unused, so split can be bypassed
        result = runner.run(graph, {"left": 100})
        assert result["double_left"] == 200


# === Fix #6: Rename collision silently allowed ===


class TestRenameCollision:
    def test_duplicate_output_rename_raises(self):
        """Renaming two outputs to the same name must raise."""

        @node(output_name=("left", "right"))
        def split(x: int) -> tuple[int, int]:
            return x, x + 1

        with pytest.raises(RenameError, match="duplicate"):
            split.with_outputs(left="dup", right="dup")

    def test_duplicate_input_rename_raises(self):
        """Renaming two inputs to the same name must raise."""

        @node(output_name="result")
        def add(a: int, b: int) -> int:
            return a + b

        with pytest.raises(RenameError, match="duplicate"):
            add.with_inputs(a="same", b="same")

    def test_non_colliding_rename_works(self):
        """Non-colliding renames still work fine."""

        @node(output_name=("left", "right"))
        def split(x: int) -> tuple[int, int]:
            return x, x + 1

        renamed = split.with_outputs(left="a", right="b")
        assert renamed.outputs == ("a", "b")

    def test_mutex_graphnode_in_outer_graph(self):
        """GraphNode with mutex branches sharing an output composes into outer graph.

        Reproduces: GraphConfigError: Multiple nodes produce 'index_results'
          -> process_all_documents creates 'index_results'
          -> process_all_documents creates 'index_results'
        """

        @node(output_name="index_results")
        def skip_document(reason: str) -> dict:
            return {"status": "skipped", "reason": reason}

        @node(output_name="index_results")
        def process_document(text: str, config: dict) -> dict:
            return {"status": "indexed", "chunks": len(text) // 100}

        @route(targets=["skip_document", "process_document"])
        def check_document(text: str) -> str:
            return "skip_document" if len(text) < 10 else "process_document"

        process_all_documents = Graph(
            nodes=[check_document, skip_document, process_document],
            name="process_all_documents",
        )

        @node(output_name="summary")
        def summarize(index_results: dict) -> str:
            return f"Done: {index_results['status']}"

        pipeline = Graph(nodes=[process_all_documents.as_node(), summarize])
        assert "summary" in pipeline.outputs

    def test_mutex_graphnode_rename(self):
        """GraphNode with mutex outputs can be renamed via with_outputs."""

        @node(output_name="index_results")
        def skip_document(reason: str) -> dict:
            return {"status": "skipped"}

        @node(output_name="index_results")
        def process_document(text: str) -> dict:
            return {"status": "indexed"}

        @route(targets=["skip_document", "process_document"])
        def check_document(text: str) -> str:
            return "skip_document" if len(text) < 10 else "process_document"

        inner = Graph(
            nodes=[check_document, skip_document, process_document],
            name="process_all_documents",
        )
        graph_node = inner.as_node()

        renamed = graph_node.with_outputs(index_results="all_results")
        assert "all_results" in renamed.outputs


# === Fix #9: Control-only cycles don't require seed inputs ===


class TestControlOnlyCycles:
    def test_control_only_cycle_no_false_seeds(self):
        """A control-only cycle should not mark data inputs as seeds."""

        @node(output_name="result")
        def process(data: str) -> str:
            return data.upper()

        @route(targets=["process", END])
        def check(result: str) -> str:
            return END if len(result) > 5 else "process"

        graph = Graph(nodes=[process, check])

        # "data" feeds process but is NOT part of any data cycle
        assert "data" in graph.inputs.required, (
            f"'data' should be required, got: {graph.inputs.required}"
        )
        assert "data" not in graph.inputs.seeds, (
            f"'data' should NOT be a seed: {graph.inputs.seeds}"
        )


# === Fix #11: Output rename propagation in GraphNode ===


class TestOutputRenamePropagation:
    def test_graphnode_with_outputs_propagates(self):
        """with_outputs() on a GraphNode must translate output names."""

        @node(output_name="inner_out")
        def inner_step(x: int) -> int:
            return x * 2

        inner_graph = Graph(nodes=[inner_step], name="inner")
        graph_node = inner_graph.as_node().with_outputs(inner_out="renamed_out")

        @node(output_name="final")
        def outer_step(renamed_out: int) -> int:
            return renamed_out + 1

        outer_graph = Graph(nodes=[graph_node, outer_step])
        runner = SyncRunner()
        result = runner.run(outer_graph, {"x": 5})

        assert result["final"] == 11, (
            f"Expected 11 (5*2+1), got {result['final']}"
        )
