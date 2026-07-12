"""Tests for the Daft-backed runner."""

from __future__ import annotations

import pytest

pytest.importorskip("daft")

from hypergraph import DaftRunner, Graph, RunStatus, SyncRunner, node


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


@node(output_name="combined")
def combine(a: int, b: str) -> str:
    return f"{a}:{b}"


def test_daft_runner_map_matches_sync_runner_for_basic_batches():
    graph = Graph([double], name="basic_batch")

    daft_results = DaftRunner().map(graph, {"x": [1, 2, 3]}, map_over="x")
    sync_results = SyncRunner().map(graph, {"x": [1, 2, 3]}, map_over="x")

    assert daft_results["doubled"] == sync_results["doubled"] == [2, 4, 6]
    assert daft_results.status == RunStatus.COMPLETED


def test_daft_runner_run_rejects_map_only_option_kwarg():
    graph = Graph([double], name="run_reserved")

    with pytest.raises(ValueError, match="runner\\.run\\(\\) does not accept map_over=.*runner\\.map"):
        DaftRunner().run(graph, x=1, map_over="x")


def test_daft_runner_run_dotted_kwarg_input_raises():
    inner = Graph([double], name="inner")
    outer = Graph([inner.as_node(namespaced=True)], name="outer")

    with pytest.raises(ValueError, match="Dotted input address 'inner\\.x'.*values=\\{'inner\\.x':"):
        DaftRunner().run(outer, **{"inner.x": 1})


def test_daft_runner_map_rejects_run_only_option_kwarg():
    graph = Graph([double], name="map_reserved")

    with pytest.raises(ValueError, match="runner\\.map\\(\\) does not accept max_iterations=.*runner\\.run"):
        DaftRunner().map(graph, {"x": [1]}, map_over="x", max_iterations=10)


def test_daft_runner_supports_product_mode():
    graph = Graph([combine], name="product_batch")

    results = DaftRunner().map(
        graph,
        {"a": [1, 2], "b": ["x", "y"]},
        map_over=["a", "b"],
        map_mode="product",
    )

    assert results["combined"] == ["1:x", "1:y", "2:x", "2:y"]


def test_daft_runner_run_nested_graphnode_with_map_over():
    inner = Graph([double], name="inner")
    outer = Graph([inner.as_node(name="mapper").rename_inputs(x="items").map_over("items")], name="outer")

    runner = DaftRunner()
    # `items` is owned by the `mapper` GraphNode at outer scope.
    result = runner.run(outer, {"items": [2, 4, 6]})

    assert result["doubled"] == [4, 8, 12]


def test_daft_runner_handles_bound_inputs_inside_nested_map_over():
    @node(output_name="scored")
    def score(text: str, multiplier: int) -> int:
        return len(text) * multiplier

    scorer = Graph([score], name="scorer").bind(multiplier=3)
    batch_graph = Graph([scorer.as_node(name="score_many").rename_inputs(text="texts").map_over("texts")], name="batch_graph")

    # `texts` is owned by the `score_many` GraphNode at batch_graph scope.
    result = DaftRunner().run(batch_graph, {"texts": ["a", "tool", "hypergraph"]})

    assert result["scored"] == [3, 12, 30]


def test_daft_runner_continue_mode_preserves_failures():
    @node(output_name="safe_double")
    def maybe_fail(x: int) -> int:
        if x == 2:
            raise ValueError("boom")
        return x * 2

    graph = Graph([maybe_fail], name="failure_batch")
    results = DaftRunner().map(
        graph,
        {"x": [1, 2, 3]},
        map_over="x",
        error_handling="continue",
    )

    assert results.status == RunStatus.PARTIAL
    assert results["safe_double"] == [2, None, 6]
    assert len(results.failures) == 1
    assert isinstance(results.failures[0].error, ValueError)


def test_daft_runner_raise_mode_re_raises_first_failure():
    @node(output_name="safe_double")
    def maybe_fail(x: int) -> int:
        if x == 2:
            raise ValueError("boom")
        return x * 2

    graph = Graph([maybe_fail], name="raise_batch")

    with pytest.raises(ValueError, match="boom"):
        DaftRunner().map(graph, {"x": [1, 2, 3]}, map_over="x")


def test_daft_runner_rejects_nested_graph_with_async_nodes():
    """DaftRunner must raise at plan time for GraphNodes with async inner graphs."""
    from hypergraph.runners._shared.validation import IncompatibleRunnerError

    @node(output_name="result")
    async def async_step(x: int) -> int:
        return x + 1

    inner = Graph([async_step], name="async_inner")
    outer = Graph([inner.as_node(name="sub").rename_inputs(x="val")], name="outer")

    with pytest.raises(IncompatibleRunnerError, match="async nodes"):
        DaftRunner().run(outer, {"val": 1})


def test_daft_runner_rejects_with_runner_override():
    """DaftRunner must reject GraphNodes with with_runner() set."""
    from hypergraph.runners._shared.validation import IncompatibleRunnerError

    inner = Graph([double], name="inner")
    gn = inner.as_node(name="sub").with_runner(SyncRunner())
    outer = Graph([gn], name="outer")

    with pytest.raises(IncompatibleRunnerError, match="runner overrides"):
        DaftRunner().run(outer, {"x": 1})


def test_daft_runner_select_uses_active_scope():
    @node(output_name="boom")
    def explode(doubled: int) -> int:
        raise RuntimeError("should not run")

    graph = Graph([double, explode], name="selected_scope").select("doubled")

    result = DaftRunner().run(graph, {"x": 2})

    assert result.values == {"doubled": 4}


def test_daft_runner_with_entrypoint_uses_active_scope():
    @node(output_name="result")
    def add_one(doubled: int) -> int:
        return doubled + 1

    graph = Graph([double, add_one], name="entry_scope").with_entrypoint("add_one")

    result = DaftRunner().run(graph, {"doubled": 4})

    assert result.values == {"doubled": 4, "result": 5}


def test_daft_runner_map_dataframe_returns_dataframe():
    """map_dataframe should return a Daft DataFrame, not MapResult."""
    import daft

    graph = Graph([double], name="df_test")
    df = daft.from_pydict({"x": [1, 2, 3]})

    result_df = DaftRunner().map_dataframe(graph, df)

    assert isinstance(result_df, daft.DataFrame)
    collected = result_df.collect().to_pydict()
    assert collected["doubled"] == [2, 4, 6]
    assert collected["x"] == [1, 2, 3]


def test_daft_runner_map_dataframe_rejects_map_option_kwarg():
    import daft

    graph = Graph([double], name="df_reserved")
    df = daft.from_pydict({"x": [1]})

    with pytest.raises(ValueError, match="runner\\.map_dataframe\\(\\) does not accept map_over=.*runner\\.map"):
        DaftRunner().map_dataframe(graph, df, map_over="x")


def test_daft_runner_map_dataframe_rejects_run_option_kwarg():
    import daft

    graph = Graph([double], name="df_run_reserved")
    df = daft.from_pydict({"x": [1]})

    with pytest.raises(ValueError, match="runner\\.map_dataframe\\(\\) does not accept max_iterations=.*runner\\.run"):
        DaftRunner().map_dataframe(graph, df, max_iterations=10)


def test_daft_runner_map_dataframe_with_broadcast_values():
    """Broadcast values should be captured in UDF closures."""
    import daft

    @node(output_name="greeting")
    def greet(name: str, prefix: str) -> str:
        return f"{prefix}, {name}!"

    graph = Graph([greet], name="broadcast_test")
    df = daft.from_pydict({"name": ["Alice", "Bob"]})

    result_df = DaftRunner().map_dataframe(graph, df, prefix="Hi")
    collected = result_df.collect().to_pydict()
    assert collected["greeting"] == ["Hi, Alice!", "Hi, Bob!"]


def test_daft_runner_map_dataframe_renamed_graphnode_multi_outputs():
    """Renamed GraphNode outputs should unpack under parent-facing names."""
    import daft

    @node(output_name=("lo", "hi"))
    def bounds(x: int) -> tuple[int, int]:
        return x - 1, x + 1

    inner = Graph([bounds], name="inner_bounds")
    outer = Graph(
        [
            inner.as_node(name="nested_bounds").with_outputs(
                lo="public_lo",
                hi="public_hi",
            )
        ],
        name="outer_bounds",
    )
    df = daft.from_pydict({"x": [5, 10]})

    result_df = DaftRunner().map_dataframe(outer, df)

    collected = result_df.collect().to_pydict()
    assert collected["public_lo"] == [4, 9]
    assert collected["public_hi"] == [6, 11]
    assert "lo" not in collected
    assert "hi" not in collected


def test_daft_runner_map_dataframe_columns_filter():
    """columns= selects graph inputs while preserving passthrough columns."""
    import daft

    graph = Graph([double], name="col_filter")
    df = daft.from_pydict({"x": [1, 2], "extra": ["a", "b"]})

    result_df = DaftRunner().map_dataframe(graph, df, columns=["x"])
    collected = result_df.collect().to_pydict()
    assert collected["doubled"] == [2, 4]
    assert collected["x"] == [1, 2]
    assert collected["extra"] == ["a", "b"]


def test_daft_runner_map_dataframe_missing_column_raises():
    """columns= with a name not in the DataFrame should raise."""
    import daft

    from hypergraph.graph.validation import GraphConfigError

    graph = Graph([double], name="bad_col")
    df = daft.from_pydict({"x": [1]})

    with pytest.raises(GraphConfigError, match="missing requested column"):
        DaftRunner().map_dataframe(graph, df, columns=["x", "missing"])


def test_daft_runner_map_dataframe_overlap_raises():
    """Broadcast values overlapping with DataFrame columns should raise."""
    import daft

    from hypergraph.graph.validation import GraphConfigError

    @node(output_name="out")
    def add(x: int, y: int) -> int:
        return x + y

    graph = Graph([add], name="overlap")
    df = daft.from_pydict({"x": [1], "y": [2]})

    with pytest.raises(GraphConfigError, match="both the Daft DataFrame and broadcast"):
        DaftRunner().map_dataframe(graph, df, y=10)


def test_daft_runner_map_dataframe_respects_graph_select():
    """graph.select() must prune unselected output columns from map_dataframe (D15 / #143)."""
    import daft

    @node(output_name="final")
    def add_one(doubled: int) -> int:
        return doubled + 1

    graph = Graph([double, add_one], name="df_select").select("final")
    df = daft.from_pydict({"x": [1, 2]})

    result_df = DaftRunner().map_dataframe(graph, df)

    assert result_df.column_names == ["x", "final"]
    assert result_df.collect().to_pydict()["final"] == [3, 5]


def test_daft_runner_map_dataframe_select_matches_run_output_keys():
    """Parity: run() values keys == map_dataframe output columns minus passthrough inputs."""
    import daft

    @node(output_name="final")
    def add_one(doubled: int) -> int:
        return doubled + 1

    graph = Graph([double, add_one], name="df_select_parity").select("final")

    run_keys = set(DaftRunner().run(graph, {"x": 2}).values)

    df = daft.from_pydict({"x": [2]})
    result_df = DaftRunner().map_dataframe(graph, df)
    output_columns = set(result_df.column_names) - set(df.column_names)

    assert output_columns == run_keys


def test_daft_runner_map_dataframe_without_select_keeps_all_output_columns():
    """No selection set -> every output column (including intermediates) is kept."""
    import daft

    @node(output_name="final")
    def add_one(doubled: int) -> int:
        return doubled + 1

    graph = Graph([double, add_one], name="df_no_select")
    df = daft.from_pydict({"x": [1]})

    result_df = DaftRunner().map_dataframe(graph, df)

    assert result_df.column_names == ["x", "doubled", "final"]


def test_daft_runner_map_dataframe_select_composes_with_columns_filter():
    """columns= governs input passthrough; select governs which output columns appear."""
    import daft

    @node(output_name="final")
    def add_one(doubled: int) -> int:
        return doubled + 1

    graph = Graph([double, add_one], name="df_select_columns").select("final")
    df = daft.from_pydict({"x": [1, 2], "extra": ["a", "b"]})

    result_df = DaftRunner().map_dataframe(graph, df, columns=["x"])
    collected = result_df.collect().to_pydict()

    # All DataFrame columns pass through (columns= only picks graph inputs);
    # select() prunes the output columns down to "final".
    assert result_df.column_names == ["x", "extra", "final"]
    assert collected["extra"] == ["a", "b"]
    assert collected["final"] == [3, 5]


def test_daft_runner_map_dataframe_select_prunes_renamed_multi_output_graphnode():
    """select() keeps only the chosen parent-facing output of a multi-output GraphNode."""
    import daft

    @node(output_name=("lo", "hi"))
    def bounds(x: int) -> tuple[int, int]:
        return x - 1, x + 1

    inner = Graph([bounds], name="inner_bounds_select")
    outer = Graph(
        [inner.as_node(name="nested_bounds").with_outputs(lo="public_lo", hi="public_hi")],
        name="outer_bounds_select",
    ).select("public_lo")
    df = daft.from_pydict({"x": [5, 10]})

    result_df = DaftRunner().map_dataframe(outer, df)

    assert result_df.column_names == ["x", "public_lo"]
    assert result_df.collect().to_pydict()["public_lo"] == [4, 9]


def test_daft_runner_map_dataframe_select_emit_only_keeps_passthrough_columns():
    """Selecting only an emit signal should add no DataFrame output column."""
    import daft

    @node(output_name="value", emit="finished")
    def produce_value(x: int) -> int:
        return x + 1

    graph = Graph([produce_value], name="df_select_emit_only").select("finished")
    runner = DaftRunner()
    run_result = runner.run(graph, {"x": 2})
    df = daft.from_pydict({"x": [2], "passthrough": ["keep"]})

    result_df = runner.map_dataframe(graph, df, columns=["x"])

    assert result_df.column_names == ["x", "passthrough"]
    assert result_df.collect().to_pydict() == {"x": [2], "passthrough": ["keep"]}
    assert result_df.column_names[len(df.column_names) :] == list(run_result.values)


def test_daft_runner_map_dataframe_select_mixed_emit_preserves_data_order():
    """Emit-only selections are omitted without reordering selected data outputs."""
    import daft

    @node(output_name=("left", "right"), emit="finished")
    def split_value(x: int) -> tuple[int, int]:
        return x - 1, x + 1

    graph = Graph([split_value], name="df_select_mixed_emit").select(
        "right",
        "finished",
        "left",
    )
    runner = DaftRunner()
    run_result = runner.run(graph, {"x": 5})
    df = daft.from_pydict({"x": [5], "passthrough": ["keep"]})

    result_df = runner.map_dataframe(graph, df, columns=["x"])

    assert result_df.column_names == ["x", "passthrough", "right", "left"]
    assert result_df.collect().to_pydict() == {
        "x": [5],
        "passthrough": ["keep"],
        "right": [6],
        "left": [4],
    }
    assert result_df.column_names[len(df.column_names) :] == list(run_result.values)


def test_daft_runner_map_dataframe_select_keeps_data_name_also_emitted():
    """A name emitted by one node remains data when another node produces it."""
    import daft

    @node(output_name="seed", emit="shared")
    def emit_shared(x: int) -> int:
        return x + 1

    @node(output_name="shared", wait_for="shared")
    def produce_shared(seed: int) -> int:
        return seed * 2

    graph = Graph([emit_shared, produce_shared], name="df_select_emit_data_overlap").select("shared")
    runner = DaftRunner()
    run_result = runner.run(graph, {"x": 2})
    df = daft.from_pydict({"x": [2], "passthrough": ["keep"]})

    result_df = runner.map_dataframe(graph, df, columns=["x"])

    assert result_df.column_names == ["x", "passthrough", "shared"]
    assert result_df.collect().to_pydict()["shared"] == [6]
    assert result_df.column_names[len(df.column_names) :] == list(run_result.values)


def test_daft_runner_rejects_cycle_graph():
    """DaftRunner should reject graphs with cycles/gates."""
    from hypergraph import END, route
    from hypergraph.runners._shared.validation import IncompatibleRunnerError

    @node(output_name="x")
    def step(x: int) -> int:
        return x + 1

    @route(targets=["step", END])
    def gate(x: int) -> str:
        return "step" if x < 3 else END

    graph = Graph([step, gate], name="cycle_graph", entrypoint="step")

    with pytest.raises(IncompatibleRunnerError):
        DaftRunner().run(graph, {"x": 0})


def test_daft_runner_warns_and_ignores_carried_processors_on_run():
    """Carried processors are warned-and-ignored exactly like explicit ones."""
    from hypergraph.events import EventProcessor

    class Recorder(EventProcessor):
        def __init__(self):
            self.events = []

        def on_event(self, event):
            self.events.append(event)

    recorder = Recorder()
    graph = Graph([double], name="carried_daft_run").with_processors(recorder)

    with pytest.warns(UserWarning, match="carried default_event_processors will be ignored"):
        result = DaftRunner().run(graph, {"x": 2})

    assert result["doubled"] == 4
    assert recorder.events == []


def test_daft_runner_warns_and_ignores_carried_processors_on_map():
    from hypergraph.events import EventProcessor

    class Recorder(EventProcessor):
        def __init__(self):
            self.events = []

        def on_event(self, event):
            self.events.append(event)

    recorder = Recorder()
    graph = Graph([double], name="carried_daft_map").with_processors(recorder)

    with pytest.warns(UserWarning, match="carried default_event_processors will be ignored"):
        results = DaftRunner().map(graph, {"x": [1, 2]}, map_over="x")

    assert results["doubled"] == [2, 4]
    assert recorder.events == []
