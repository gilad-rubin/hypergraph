"""Tests for empty collection edge cases across topologies (GAP-01)."""

from hypergraph import Graph, node
from hypergraph.nodes.gate import END, route
from hypergraph.runners import RunStatus, SyncRunner

# === Test Fixtures ===


@node(output_name="processed")
def identity_list(items: list) -> list:
    return items


@node(output_name="length")
def list_length(items: list) -> int:
    return len(items)


@node(output_name="processed")
def dict_keys(data: dict) -> list:
    return list(data.keys())


@node(output_name="count")
def counter_with_list(count: int, items: list) -> int:
    return count + len(items)


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="items")
def gen_items(n: int):
    """Generator that yields n items."""
    yield from range(n)


@node(output_name="result_a")
def process_a(items: list) -> int:
    return len(items)


@node(output_name="result_b")
def process_b(items: list) -> str:
    return str(items)


# === Tests ===


class TestEmptyListRuntimeInput:
    """Test empty list as direct runtime input."""

    def test_empty_list_flows_through_graph(self):
        """Empty list passed as input flows correctly through graph."""
        graph = Graph([identity_list])
        runner = SyncRunner()

        result = runner.run(graph, {"items": []})

        assert result.status == RunStatus.COMPLETED
        assert result["processed"] == []

    def test_empty_list_length_is_zero(self):
        """Node correctly processes empty list."""
        graph = Graph([list_length])
        runner = SyncRunner()

        result = runner.run(graph, {"items": []})

        assert result.status == RunStatus.COMPLETED
        assert result["length"] == 0

    def test_empty_list_in_linear_chain(self):
        """Empty list flows through linear chain correctly."""

        @node(output_name="filtered")
        def filter_list(items: list) -> list:
            return [x for x in items if x > 0]

        @node(output_name="sum")
        def sum_list(filtered: list) -> int:
            return sum(filtered)

        graph = Graph([filter_list, sum_list])
        runner = SyncRunner()

        result = runner.run(graph, {"items": []})

        assert result.status == RunStatus.COMPLETED
        assert result["filtered"] == []
        assert result["sum"] == 0


class TestEmptyDictRuntimeInput:
    """Test empty dict as direct runtime input."""

    def test_empty_dict_flows_through_graph(self):
        """Empty dict passed as input flows correctly through graph."""
        graph = Graph([dict_keys])
        runner = SyncRunner()

        result = runner.run(graph, {"data": {}})

        assert result.status == RunStatus.COMPLETED
        assert result["processed"] == []

    def test_empty_dict_in_chain(self):
        """Empty dict flows through chain correctly."""

        @node(output_name="values")
        def dict_values(data: dict) -> list:
            return list(data.values())

        @node(output_name="count")
        def count_values(values: list) -> int:
            return len(values)

        graph = Graph([dict_values, count_values])
        runner = SyncRunner()

        result = runner.run(graph, {"data": {}})

        assert result.status == RunStatus.COMPLETED
        assert result["values"] == []
        assert result["count"] == 0


class TestEmptyListInCycleSeed:
    """Test empty list as cycle seed value."""

    def test_empty_list_accumulator(self):
        """Cycle with empty list seed and gate accumulates correctly."""

        @node(output_name="acc")
        def accumulator(acc: list, value: int, limit: int = 3) -> list:
            if len(acc) >= limit:
                return acc
            return acc + [value]

        @route(targets=["accumulator", END])
        def accumulator_gate(acc: list, limit: int = 3) -> str:
            return END if len(acc) >= limit else "accumulator"

        graph = Graph([accumulator, accumulator_gate])
        runner = SyncRunner()

        result = runner.run(graph, {"acc": [], "value": 42, "limit": 3})

        assert result.status == RunStatus.COMPLETED
        assert result["acc"] == [42, 42, 42]

    def test_empty_list_immediate_termination(self):
        """Cycle with empty list terminates immediately when condition met."""

        @node(output_name="items")
        def collector(items: list, max_items: int = 0) -> list:
            if len(items) >= max_items:
                return items
            return items + [1]

        graph = Graph([collector])
        runner = SyncRunner()

        # max_items=0 means we should stop immediately
        result = runner.run(graph, {"items": [], "max_items": 0})

        assert result.status == RunStatus.COMPLETED
        assert result["items"] == []


class TestEmptyListToMapOver:
    """Test map_over with empty list (should yield empty results)."""

    def test_map_over_empty_list_returns_empty(self):
        """map_over with empty list returns empty output list."""
        inner = Graph([double], name="inner")
        outer = Graph([inner.as_node().map_over("x")])
        runner = SyncRunner()

        result = runner.run(outer, {"x": []})

        assert result.status == RunStatus.COMPLETED
        assert result["doubled"] == []

    def test_map_over_empty_list_no_execution(self):
        """map_over with empty list doesn't execute inner graph."""
        call_count = 0

        @node(output_name="result")
        def counting_node(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x

        inner = Graph([counting_node], name="inner")
        outer = Graph([inner.as_node().map_over("x")])
        runner = SyncRunner()

        result = runner.run(outer, {"x": []})

        assert result.status == RunStatus.COMPLETED
        assert call_count == 0
        assert result["result"] == []


class TestNestedGraphWithEmptyInput:
    """Test empty collection passed to nested GraphNode."""

    def test_nested_graph_processes_empty_list(self):
        """Nested graph correctly processes empty list input."""
        inner = Graph([identity_list], name="inner")
        outer = Graph([inner.as_node()])
        runner = SyncRunner()

        result = runner.run(outer, {"items": []})

        assert result.status == RunStatus.COMPLETED
        assert result["processed"] == []

    def test_nested_graph_empty_list_to_length(self):
        """Nested graph computes length of empty list."""
        inner = Graph([list_length], name="inner")
        outer = Graph([inner.as_node()])
        runner = SyncRunner()

        result = runner.run(outer, {"items": []})

        assert result.status == RunStatus.COMPLETED
        assert result["length"] == 0


class TestEmptyListFanOut:
    """Test empty list flowing to multiple downstream nodes."""

    def test_empty_list_fans_out_correctly(self):
        """Empty list flows to multiple downstream nodes."""
        graph = Graph([identity_list, process_a, process_b])

        # Rewire: both process_a and process_b consume the same output
        @node(output_name="source")
        def source_node(items: list) -> list:
            return items

        @node(output_name="result_a")
        def consumer_a(source: list) -> int:
            return len(source)

        @node(output_name="result_b")
        def consumer_b(source: list) -> str:
            return str(source)

        graph = Graph([source_node, consumer_a, consumer_b])
        runner = SyncRunner()

        result = runner.run(graph, {"items": []})

        assert result.status == RunStatus.COMPLETED
        assert result["source"] == []
        assert result["result_a"] == 0
        assert result["result_b"] == "[]"


class TestEmptyGeneratorOutput:
    """Test generator that yields nothing."""

    def test_empty_generator_returns_empty_list(self):
        """Generator yielding 0 items returns empty list."""
        graph = Graph([gen_items])
        runner = SyncRunner()

        result = runner.run(graph, {"n": 0})

        assert result.status == RunStatus.COMPLETED
        assert result["items"] == []

    def test_empty_generator_in_chain(self):
        """Empty generator output flows through chain."""

        @node(output_name="items")
        def empty_gen():
            """Generator that yields nothing."""
            return
            yield  # Make it a generator

        @node(output_name="length")
        def items_length(items: list) -> int:
            return len(items)

        graph = Graph([empty_gen, items_length])
        runner = SyncRunner()

        result = runner.run(graph, {})

        assert result.status == RunStatus.COMPLETED
        assert result["items"] == []
        assert result["length"] == 0


class TestNoneInListInput:
    """Test list containing None values."""

    def test_list_with_none_values_flows_correctly(self):
        """List containing None values flows through graph."""

        @node(output_name="processed")
        def process_list(items: list) -> list:
            return items

        graph = Graph([process_list])
        runner = SyncRunner()

        result = runner.run(graph, {"items": [None, 1, None, 2]})

        assert result.status == RunStatus.COMPLETED
        assert result["processed"] == [None, 1, None, 2]

    def test_list_with_only_none_values(self):
        """List containing only None values."""

        @node(output_name="filtered")
        def filter_none(items: list) -> list:
            return [x for x in items if x is not None]

        graph = Graph([filter_none])
        runner = SyncRunner()

        result = runner.run(graph, {"items": [None, None, None]})

        assert result.status == RunStatus.COMPLETED
        assert result["filtered"] == []

    def test_none_values_counted_correctly(self):
        """None values in list are counted correctly."""

        @node(output_name="count")
        def count_none(items: list) -> int:
            return sum(1 for x in items if x is None)

        graph = Graph([count_none])
        runner = SyncRunner()

        result = runner.run(graph, {"items": [None, 1, None, 2, None]})

        assert result.status == RunStatus.COMPLETED
        assert result["count"] == 3


class TestEmptyCollectionWithDefaults:
    """Test empty collections with default values."""

    def test_empty_list_default(self):
        """Node with empty list as default."""

        @node(output_name="length")
        def with_default_list(items: list = None) -> int:
            if items is None:
                items = []
            return len(items)

        graph = Graph([with_default_list])
        runner = SyncRunner()

        # Provide empty list explicitly
        result = runner.run(graph, {"items": []})
        assert result["length"] == 0

        # Use default (None)
        result = runner.run(graph, {})
        assert result["length"] == 0

    def test_empty_dict_default(self):
        """Node with empty dict as default."""

        @node(output_name="count")
        def with_default_dict(data: dict = None) -> int:
            if data is None:
                data = {}
            return len(data)

        graph = Graph([with_default_dict])
        runner = SyncRunner()

        # Provide empty dict explicitly
        result = runner.run(graph, {"data": {}})
        assert result["count"] == 0

        # Use default (None)
        result = runner.run(graph, {})
        assert result["count"] == 0
