"""Tests for partial input semantics: active subgraph, entrypoints, and select-aware InputSpec."""

import pytest

from hypergraph import Graph, SyncRunner, node
from hypergraph.graph.validation import GraphConfigError

# === Shared test nodes ===


@node(output_name="r1")
def root1(x):
    return x


@node(output_name="r2")
def root2(y):
    return y


@node(output_name="r3")
def root3(z=10):
    return z


@node(output_name="a")
def merge_node(r1, r2, r3):
    return f"{r1}-{r2}-{r3}"


@node(output_name="b")
def process_node(a):
    return a.upper()


# === Select-aware InputSpec ===


class TestSelectReducesRequired:
    """select() should narrow InputSpec to only what's needed."""

    def test_select_single_output_reduces_required(self):
        """B(a, y) depends on A(x)->a. select("a") only needs x."""

        @node(output_name="a_val")
        def node_a(x):
            return x

        @node(output_name="b_val")
        def node_b(a_val, y):
            return f"{a_val}-{y}"

        graph = Graph([node_a, node_b])
        assert set(graph.inputs.required) == {"x", "y"}

        selected = graph.select("a_val")
        assert set(selected.inputs.required) == {"x"}

    def test_select_all_same_as_no_select(self):
        """Selecting all outputs == no select."""
        graph = Graph([root1, root2, root3, merge_node, process_node])
        all_selected = graph.select(*graph.outputs)

        assert set(graph.inputs.required) == set(all_selected.inputs.required)
        assert set(graph.inputs.optional) == set(all_selected.inputs.optional)

    def test_select_leaf_output_includes_full_chain(self):
        """Selecting leaf 'b' needs the full chain."""
        graph = Graph([root1, root2, root3, merge_node, process_node])
        selected = graph.select("b")

        # Same as full graph: x, y required, z optional
        assert set(selected.inputs.required) == {"x", "y"}

    def test_select_intermediate_hides_downstream(self):
        """Selecting 'a' excludes process_node from active set."""
        graph = Graph([root1, root2, root3, merge_node, process_node])
        selected = graph.select("a")

        # a is produced by merge_node; still needs all roots
        assert set(selected.inputs.required) == {"x", "y"}

    def test_select_root_output_excludes_everything_downstream(self):
        """Selecting a root output needs only that root's inputs."""

        @node(output_name="out1")
        def first(x):
            return x

        @node(output_name="out2")
        def second(out1, y):
            return f"{out1}-{y}"

        graph = Graph([first, second])
        assert set(graph.inputs.required) == {"x", "y"}

        selected = graph.select("out1")
        assert set(selected.inputs.required) == {"x"}
        assert "y" not in set(selected.inputs.required) | set(selected.inputs.optional)


# === Select with gates ===


class TestSelectWithGate:
    """Pessimistic gate expansion when gates are in the needed set."""

    def test_select_with_gate_pessimistic(self):
        """Gate in needed set -> all branches' inputs required."""
        from hypergraph import ifelse

        @node(output_name="val")
        def producer(raw):
            return raw

        @ifelse(when_true="branch_a_node", when_false="branch_b_node")
        def my_gate(val):
            return val > 0

        @node(output_name="result_a")
        def branch_a_node(val):
            return val * 2

        @node(output_name="result_b")
        def branch_b_node(val, extra):
            return val * 3 + extra

        @node(output_name="final")
        def consumer(result_a):
            return result_a

        graph = Graph([producer, my_gate, branch_a_node, branch_b_node, consumer])
        # select("final") -> consumer -> branch_a -> gate -> pessimistic -> branch_b
        selected = graph.select("final")
        assert "raw" in selected.inputs.required
        # branch_b_node's "extra" is required due to pessimistic gate expansion
        assert "extra" in selected.inputs.required


# === Entrypoint narrows active set ===


class TestEntrypointNarrowsActiveSet:
    """with_entrypoint() should narrow InputSpec."""

    def test_entrypoint_skips_upstream(self):
        """with_entrypoint at merge skips root nodes."""
        graph = Graph([root1, root2, root3, merge_node, process_node])

        g2 = graph.with_entrypoint("merge_node")
        # merge_node's inputs become user-provided (roots are skipped)
        assert "r1" in g2.inputs.required
        assert "r2" in g2.inputs.required
        assert "r3" in g2.inputs.required
        # Root inputs should NOT appear
        assert "x" not in set(g2.inputs.required) | set(g2.inputs.optional)
        assert "y" not in set(g2.inputs.required) | set(g2.inputs.optional)

    def test_entrypoint_at_leaf_requires_only_leaf_inputs(self):
        """with_entrypoint at leaf node requires only its inputs."""
        graph = Graph([root1, root2, root3, merge_node, process_node])

        g2 = graph.with_entrypoint("process_node")
        assert set(g2.inputs.required) == {"a"}

    def test_entrypoint_dag_skips_upstream(self):
        """with_entrypoint in DAG -> required = entry node's inputs."""

        @node(output_name="mid")
        def dag_first(x):
            return x

        @node(output_name="out")
        def dag_second(mid):
            return mid * 2

        graph = Graph([dag_first, dag_second])
        assert set(graph.inputs.required) == {"x"}

        g2 = graph.with_entrypoint("dag_second")
        assert set(g2.inputs.required) == {"mid"}
        assert "x" not in set(g2.inputs.required) | set(g2.inputs.optional)


# === Entrypoint + select composition ===


class TestEntrypointAndSelectCompose:
    """Both with_entrypoint and select narrow together."""

    def test_entrypoint_and_select_compose(self):
        """with_entrypoint + select narrows from both ends."""

        @node(output_name="mid")
        def compose_a(x):
            return x

        @node(output_name="out")
        def compose_b(mid):
            return mid

        @node(output_name="other")
        def compose_c(mid):
            return mid

        graph = Graph([compose_a, compose_b, compose_c])
        # with_entrypoint("compose_a") -> active = {compose_a, compose_b, compose_c}
        # .select("out") -> only compose_b needed -> active = {compose_a, compose_b}
        g2 = graph.with_entrypoint("compose_a").select("out")
        assert set(g2.inputs.required) == {"x"}

    def test_entrypoint_bind_compose(self):
        """with_entrypoint + bind reduces required."""
        graph = Graph([root1, root2, root3, merge_node, process_node])

        g2 = graph.with_entrypoint("merge_node").bind(r2=5)
        assert "r1" in g2.inputs.required
        assert "r2" not in g2.inputs.required  # bound
        assert "r3" in g2.inputs.required

    def test_all_four_dimensions(self):
        """entrypoint + select + bind + default all compose."""

        @node(output_name="m")
        def merge_4d(a, b, c, d=99):
            return a + b + c + d

        @node(output_name="out")
        def final_4d(m):
            return m

        @node(output_name="side")
        def side_4d(m):
            return m

        @node(output_name="a")
        def root_a(x):
            return x

        graph = Graph([root_a, merge_4d, final_4d, side_4d])
        # entrypoint at merge_4d -> skip root_a
        # select("out") -> skip side_4d
        # bind(b=10) -> b is optional
        # d has default -> optional
        g = graph.with_entrypoint("merge_4d").select("out").bind(b=10)
        assert set(g.inputs.required) == {"a", "c"}


# === Multi-entrypoint ===


class TestMultiEntrypoint:
    """Multiple entry points."""

    def test_multi_entrypoint_independent_branches(self):
        """with_entrypoint("root1", "root2") activates both plus downstream."""
        graph = Graph([root1, root2, root3, merge_node, process_node])

        g2 = graph.with_entrypoint("root1", "root2")
        assert "x" in g2.inputs.required
        assert "y" in g2.inputs.required
        # root3 is skipped, so r3 is not produced -> required by merge_node
        assert "r3" in g2.inputs.required
        # z should not appear (root3 not active)
        assert "z" not in set(g2.inputs.required) | set(g2.inputs.optional)

    def test_multi_entrypoint_chained(self):
        """Chaining with_entrypoint == passing all at once."""
        graph = Graph([root1, root2, root3, merge_node, process_node])

        g_once = graph.with_entrypoint("root1", "root2")
        g_chained = graph.with_entrypoint("root1").with_entrypoint("root2")

        assert set(g_once.inputs.required) == set(g_chained.inputs.required)
        assert set(g_once.inputs.optional) == set(g_chained.inputs.optional)

    def test_redundant_entrypoint_accepted(self):
        """Redundant entry points (one reachable from another) are accepted."""
        graph = Graph([root1, root2, root3, merge_node, process_node])

        # merge_node is downstream of root1 — both accepted silently
        g2 = graph.with_entrypoint("root1", "merge_node")
        assert "x" in g2.inputs.required


# === with_entrypoint validation ===


class TestEntrypointValidation:
    """Validation of with_entrypoint arguments."""

    def test_unknown_node_raises(self):
        graph = Graph([root1])
        with pytest.raises(GraphConfigError, match="Unknown entry point"):
            graph.with_entrypoint("nonexistent")

    def test_gate_node_raises(self):
        from hypergraph import ifelse

        @ifelse(when_true="root1", when_false="root2")
        def gate_val(x):
            return x > 0

        graph = Graph([gate_val, root1, root2])
        with pytest.raises(GraphConfigError, match="gate"):
            graph.with_entrypoint("gate_val")


# === Immutability ===


class TestImmutability:
    """with_entrypoint returns new graph, doesn't mutate original."""

    def test_with_entrypoint_is_immutable(self):
        graph = Graph([root1, root2, root3, merge_node, process_node])
        original_required = set(graph.inputs.required)

        g2 = graph.with_entrypoint("merge_node")
        # Original unchanged
        assert set(graph.inputs.required) == original_required
        # New graph is different
        assert set(g2.inputs.required) != original_required

    def test_entrypoints_config_property(self):
        graph = Graph([root1, process_node])
        assert graph.entrypoints_config is None

        g2 = graph.with_entrypoint("process_node")
        assert g2.entrypoints_config == ("process_node",)

    def test_add_nodes_resets_entrypoints(self):
        """add_nodes creates fresh graph without entrypoints."""
        graph = Graph([root1, process_node])
        g2 = graph.with_entrypoint("process_node")
        assert g2.entrypoints_config is not None

        @node(output_name="extra")
        def extra_node(a):
            return a

        g3 = g2.add_nodes(extra_node)
        assert g3.entrypoints_config is None


# === Runtime select narrows validation ===


class TestRuntimeSelectNarrowsValidation:
    """runner.run(select=...) should only require inputs for selected outputs."""

    def test_runtime_select_narrows_validation(self):
        """runner.run(select="a_val") only validates inputs for a_val."""

        @node(output_name="a_val")
        def node_a(x):
            return x

        @node(output_name="b_val")
        def node_b(a_val, y):
            return f"{a_val}-{y}"

        graph = Graph([node_a, node_b])
        runner = SyncRunner()

        # Without select, both x and y are required → missing y raises
        with pytest.raises(Exception, match="y"):
            runner.run(graph, {"x": 1})

        # With select="a_val", only x is required → succeeds
        result = runner.run(graph, {"x": 1}, select="a_val")
        assert result["a_val"] == 1

    def test_runtime_select_overrides_graph_select(self):
        """Runtime select takes precedence over graph.select()."""

        @node(output_name="a_val")
        def node_a(x):
            return x

        @node(output_name="b_val")
        def node_b(a_val, y):
            return f"{a_val}-{y}"

        # Graph-level select requires both x and y
        graph = Graph([node_a, node_b]).select("b_val")
        runner = SyncRunner()

        # Runtime select="a_val" should narrow validation to just x
        result = runner.run(graph, {"x": 1}, select="a_val")
        assert result["a_val"] == 1


# === Entrypoint execution tests ===


class TestEntrypointExecution:
    """with_entrypoint should affect actual execution, not just validation."""

    def test_entrypoint_skips_upstream_execution(self):
        """with_entrypoint skips upstream nodes — they never execute."""
        call_log = []

        @node(output_name="mid")
        def upstream(x):
            call_log.append("upstream")
            return x * 2

        @node(output_name="out")
        def downstream(mid):
            call_log.append("downstream")
            return mid + 1

        graph = Graph([upstream, downstream]).with_entrypoint("downstream")
        runner = SyncRunner()
        result = runner.run(graph, {"mid": 10})
        assert result["out"] == 11
        assert call_log == ["downstream"]  # upstream never called

    def test_entrypoint_downstream_executes(self):
        """with_entrypoint at non-leaf → downstream still runs."""
        call_log = []

        @node(output_name="mid")
        def first(x):
            call_log.append("first")
            return x

        @node(output_name="out")
        def second(mid):
            call_log.append("second")
            return mid * 2

        graph = Graph([first, second]).with_entrypoint("first")
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5})
        assert result["out"] == 10
        assert call_log == ["first", "second"]

    def test_entrypoint_upstream_never_fires_even_with_provided_inputs(self):
        """Upstream node should NOT fire under with_entrypoint even if its inputs are provided."""
        call_log = []

        @node(output_name="mid")
        def upstream_prov(x):
            call_log.append("upstream")
            return x * 2

        @node(output_name="out")
        def downstream_prov(mid):
            call_log.append("downstream")
            return mid + 1

        graph = Graph([upstream_prov, downstream_prov]).with_entrypoint("downstream_prov")
        runner = SyncRunner()
        # Provide both mid (needed) AND x (upstream's input) — x should be ignored
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            result = runner.run(graph, {"mid": 10, "x": 5})
        assert result["out"] == 11
        # upstream should NOT fire even though x is provided
        assert call_log == ["downstream"]

    def test_no_entrypoint_all_nodes_execute(self):
        """Without with_entrypoint, all nodes execute (backward compat)."""
        call_log = []

        @node(output_name="mid")
        def first_node(x):
            call_log.append("first")
            return x

        @node(output_name="out")
        def second_node(mid):
            call_log.append("second")
            return mid * 2

        graph = Graph([first_node, second_node])
        runner = SyncRunner()
        result = runner.run(graph, {"x": 5})
        assert result["out"] == 10
        assert set(call_log) == {"first", "second"}
