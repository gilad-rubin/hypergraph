"""Tests for explicit edges mode in Graph."""

import pytest

from hypergraph import Graph, GraphConfigError, SyncRunner, node, route, END


# ── Fixtures ─────────────────────────────────────────────────────────────────


@node(output_name="y")
def double(x: int) -> int:
    return x * 2


@node(output_name="z")
def add_one(y: int) -> int:
    return y + 1


@node(output_name="result")
def square(z: int) -> int:
    return z**2


@node(output_name="query")
def ask_user() -> str:
    return "hello"


@node(output_name="messages")
def add_query(messages: list, query: str) -> list:
    return [*messages, {"role": "user", "content": query}]


@node(output_name="response")
def generate(messages: list) -> str:
    return f"reply to {len(messages)} messages"


@node(output_name="messages")
def add_response(messages: list, response: str) -> list:
    return [*messages, {"role": "assistant", "content": response}]


# ── Construction tests ───────────────────────────────────────────────────────


class TestExplicitEdgesConstruction:
    """Graph construction with explicit edges."""

    def test_basic_linear(self):
        """Simple A→B→C with explicit edges."""
        g = Graph(
            [double, add_one, square],
            edges=[(double, add_one), (add_one, square)],
        )
        assert g.inputs.required == ("x",)
        assert set(g.outputs) == {"y", "z", "result"}

    def test_node_refs(self):
        """Edge tuples using node objects resolve to names."""
        g = Graph(
            [double, add_one],
            edges=[(double, add_one)],
        )
        edges = list(g.nx_graph.edges(data=True))
        assert len(edges) == 1
        assert edges[0][0] == "double"
        assert edges[0][1] == "add_one"

    def test_string_names(self):
        """Edge tuples using string names."""
        g = Graph(
            [double, add_one],
            edges=[("double", "add_one")],
        )
        edges = list(g.nx_graph.edges(data=True))
        assert len(edges) == 1
        assert edges[0][:2] == ("double", "add_one")

    def test_explicit_value_names(self):
        """3-tuple with explicit values."""
        g = Graph(
            [double, add_one],
            edges=[("double", "add_one", "y")],
        )
        edge_data = g.nx_graph.edges["double", "add_one"]
        assert edge_data["edge_type"] == "data"
        assert edge_data["value_names"] == ["y"]

    def test_explicit_value_names_list(self):
        """3-tuple with list of explicit values."""
        g = Graph(
            [double, add_one],
            edges=[("double", "add_one", ["y"])],
        )
        edge_data = g.nx_graph.edges["double", "add_one"]
        assert edge_data["value_names"] == ["y"]

    def test_inferred_value_names(self):
        """2-tuple correctly infers values from output/input overlap."""
        g = Graph(
            [double, add_one],
            edges=[(double, add_one)],
        )
        edge_data = g.nx_graph.edges["double", "add_one"]
        assert edge_data["edge_type"] == "data"
        assert edge_data["value_names"] == ["y"]

    def test_ordering_only_edge(self):
        """2-tuple with no overlap creates ordering-only edge."""
        g = Graph(
            [ask_user, generate],
            edges=[(generate, ask_user)],
        )
        edge_data = g.nx_graph.edges["generate", "ask_user"]
        assert edge_data["edge_type"] == "ordering"
        assert edge_data["value_names"] == []

    def test_same_output_name_allowed(self):
        """Two producers of 'messages' with explicit ordering — no error."""
        g = Graph(
            [ask_user, add_query, generate, add_response],
            edges=[
                (ask_user, add_query),
                (add_query, generate),
                (generate, add_response),
                (add_response, ask_user),
                (add_response, add_query),
            ],
        )
        assert g.has_cycles
        # Both add_query and add_response produce "messages"
        producers = [
            n for n in g.nodes.values() if "messages" in n.outputs
        ]
        assert len(producers) == 2

    def test_no_synthetic_edges_in_explicit(self):
        """Undeclared name-match edges don't appear in explicit mode."""
        # double produces "y", add_one consumes "y" — but we don't declare
        # that edge. Only declare double→square (no overlap = ordering)
        @node(output_name="unrelated")
        def unrelated(z: int) -> int:
            return z

        g = Graph(
            [double, add_one, unrelated],
            edges=[(double, unrelated)],
        )
        # Even though double.outputs={y} and add_one.inputs={y}, no edge exists
        assert not g.nx_graph.has_edge("double", "add_one")

    def test_cycle_detected(self):
        """Explicit edges correctly create cycles."""
        g = Graph(
            [ask_user, add_query, generate, add_response],
            edges=[
                (ask_user, add_query),
                (add_query, generate),
                (generate, add_response),
                (add_response, add_query),
            ],
        )
        assert g.has_cycles


# ── Input spec tests ─────────────────────────────────────────────────────────


class TestExplicitEdgesInputSpec:
    """InputSpec computed from explicit edges."""

    def test_input_spec_linear(self):
        """Unconnected inputs become graph-level required inputs."""
        g = Graph(
            [double, add_one, square],
            edges=[(double, add_one), (add_one, square)],
        )
        assert g.inputs.required == ("x",)

    def test_input_spec_with_entrypoints(self):
        """Cycle entrypoint seeds are detected correctly."""
        g = Graph(
            [ask_user, add_query, generate, add_response],
            edges=[
                (ask_user, add_query),
                (add_query, generate),
                (generate, add_response),
                (add_response, add_query),
            ],
        )
        # messages is a cycle seed (produced and consumed within cycle)
        assert "messages" in {
            p for params in g.inputs.entrypoints.values() for p in params
        }


# ── Execution tests ──────────────────────────────────────────────────────────


class TestExplicitEdgesExecution:
    """End-to-end execution with explicit edges."""

    def test_linear_execution(self):
        """Simple linear graph executes correctly."""
        g = Graph(
            [double, add_one, square],
            edges=[(double, add_one), (add_one, square)],
        )
        runner = SyncRunner()
        result = runner.run(g, {"x": 3})
        assert result["y"] == 6
        assert result["z"] == 7
        assert result["result"] == 49

    def test_same_output_name_graph_topology(self):
        """Chat pattern graph topology is correct with shared 'messages'."""
        g = Graph(
            [ask_user, add_query, generate, add_response],
            edges=[
                (ask_user, add_query),
                (add_query, generate),
                (generate, add_response),
                (add_response, add_query),
                (add_response, ask_user),
            ],
        )
        # Both add_query and add_response produce "messages" — graph constructs
        assert g.has_cycles
        # Verify edges
        assert g.nx_graph.has_edge("ask_user", "add_query")
        assert g.nx_graph.has_edge("add_query", "generate")
        assert g.nx_graph.has_edge("generate", "add_response")
        assert g.nx_graph.has_edge("add_response", "add_query")
        # add_response → ask_user is ordering-only (no overlap)
        edge_data = g.nx_graph.edges["add_response", "ask_user"]
        assert edge_data["edge_type"] == "ordering"


# ── Emit/wait_for interaction ────────────────────────────────────────────────


class TestExplicitEdgesWithEmitWaitFor:
    """emit/wait_for ordering edges still work in explicit mode."""

    def test_emit_wait_for_works(self):
        """Ordering edges from emit/wait_for are still created."""
        @node(output_name="x", emit=("ready",))
        def producer(val: int) -> int:
            return val

        @node(output_name="y", wait_for=("ready",))
        def consumer(x: int) -> int:
            return x + 1

        g = Graph(
            [producer, consumer],
            edges=[(producer, consumer)],
        )
        # Data edge from explicit declaration
        data = g.nx_graph.edges["producer", "consumer"]
        assert data["edge_type"] == "data"
        assert "x" in data["value_names"]

    def test_control_edges_work(self):
        """Gate nodes still auto-wire control edges."""
        @node(output_name="x")
        def start(val: int) -> int:
            return val

        @route(targets=["do_thing", END])
        def gate(x: int) -> str | type[END]:
            return "do_thing"

        @node(output_name="result")
        def do_thing(x: int) -> int:
            return x * 2

        g = Graph(
            [start, gate, do_thing],
            edges=[(start, gate)],
        )
        # gate → do_thing should be a control edge (auto-wired from gate targets)
        assert g.nx_graph.has_edge("gate", "do_thing")


# ── Bind / select / add_nodes ────────────────────────────────────────────────


class TestExplicitEdgesModifiers:
    """bind(), select(), and add_nodes() behavior."""

    def test_bind_preserves_edges(self):
        """bind() carries explicit edges through shallow copy."""
        g = Graph(
            [double, add_one],
            edges=[(double, add_one)],
        )
        bound = g.bind(x=5)
        assert bound.inputs.bound == {"x": 5}
        # Edges still present
        assert bound.nx_graph.has_edge("double", "add_one")

    def test_select_preserves_edges(self):
        """select() carries explicit edges through."""
        g = Graph(
            [double, add_one],
            edges=[(double, add_one)],
        )
        selected = g.select("z")
        assert selected.selected == ("z",)
        assert selected.nx_graph.has_edge("double", "add_one")

    def test_add_nodes_raises(self):
        """add_nodes() on explicit-edge graph raises error."""
        g = Graph(
            [double, add_one],
            edges=[(double, add_one)],
        )
        with pytest.raises(GraphConfigError, match="Cannot use add_nodes"):
            g.add_nodes(square)


# ── Subgraph pattern ─────────────────────────────────────────────────────────


class TestExplicitEdgesSubgraph:
    """Subgraph (GraphNode) interaction with explicit edges."""

    def test_subgraph_pattern(self):
        """Inner auto-inference + outer explicit edges works."""
        @node(output_name="query")
        def _ask() -> str:
            return "hello"

        @node(output_name="messages")
        def _add_query(messages: list, query: str) -> list:
            return [*messages, {"role": "user", "content": query}]

        @node(output_name="response")
        def _generate(messages: list) -> str:
            return "world"

        @node(output_name="messages")
        def _add_response(messages: list, response: str) -> list:
            return [*messages, {"role": "assistant", "content": response}]

        # Inner graphs use auto-inference
        ask_phase = Graph([_ask, _add_query], name="ask")
        respond_phase = Graph([_generate, _add_response], name="respond")

        # Outer graph uses explicit edges
        chat = Graph(
            [ask_phase.as_node(), respond_phase.as_node()],
            edges=[
                ("ask", "respond"),
                ("respond", "ask"),
            ],
        )
        assert chat.has_cycles
        # ask outputs messages, respond consumes messages
        edge_data = chat.nx_graph.edges["ask", "respond"]
        assert edge_data["edge_type"] == "data"
        assert "messages" in edge_data["value_names"]


# ── Error cases ──────────────────────────────────────────────────────────────


class TestExplicitEdgesErrors:
    """Error handling for invalid edge specs."""

    def test_unknown_source_raises(self):
        """Invalid source node → GraphConfigError."""
        with pytest.raises(GraphConfigError, match="unknown source"):
            Graph(
                [double, add_one],
                edges=[("nonexistent", "add_one")],
            )

    def test_unknown_target_raises(self):
        """Invalid target node → GraphConfigError."""
        with pytest.raises(GraphConfigError, match="unknown target"):
            Graph(
                [double, add_one],
                edges=[("double", "nonexistent")],
            )

    def test_invalid_value_not_in_source_raises(self):
        """3-tuple with value not in source outputs → error."""
        with pytest.raises(GraphConfigError, match="not an output"):
            Graph(
                [double, add_one],
                edges=[("double", "add_one", "nonexistent")],
            )

    def test_invalid_value_not_in_target_raises(self):
        """3-tuple with value not in target inputs → error."""
        @node(output_name=("a", "b"))
        def multi_out(x: int) -> tuple[int, int]:
            return x, x + 1

        @node(output_name="c")
        def consumer(a: int) -> int:
            return a

        with pytest.raises(GraphConfigError, match="not an input"):
            Graph(
                [multi_out, consumer],
                edges=[(multi_out, consumer, "b")],
            )

    def test_bad_tuple_arity_raises(self):
        """1-tuple or 4-tuple → error."""
        with pytest.raises(GraphConfigError, match="2-tuple or 3-tuple"):
            Graph(
                [double, add_one],
                edges=[("double",)],
            )

        with pytest.raises(GraphConfigError, match="2-tuple or 3-tuple"):
            Graph(
                [double, add_one],
                edges=[("double", "add_one", "y", "extra")],
            )

    def test_unordered_same_output_raises(self):
        """Two same-name producers with no path between them → raises."""
        @node(output_name="x")
        def producer_a() -> int:
            return 1

        @node(output_name="x")
        def producer_b() -> int:
            return 2

        @node(output_name="result")
        def consumer(x: int) -> int:
            return x

        with pytest.raises(GraphConfigError, match="Multiple nodes produce 'x'"):
            Graph(
                [producer_a, producer_b, consumer],
                edges=[
                    (producer_a, consumer),
                    (producer_b, consumer),
                ],
            )

    def test_non_tuple_edge_raises(self):
        """Edge that is not a tuple → error."""
        with pytest.raises(GraphConfigError, match="2-tuple or 3-tuple"):
            Graph(
                [double, add_one],
                edges=["double->add_one"],
            )


# ── Visualization ────────────────────────────────────────────────────────────


class TestExplicitEdgesVisualization:
    """Visualization renders correctly with explicit edges."""

    def test_visualize_works(self):
        """Visualization doesn't crash with explicit edges."""
        g = Graph(
            [double, add_one, square],
            edges=[(double, add_one), (add_one, square)],
        )
        # to_flat_graph is the canonical viz input — should not error
        flat = g.to_flat_graph()
        assert len(flat.nodes) == 3
        assert len(flat.edges) == 2

    def test_flat_graph_preserves_edge_types(self):
        """Flattened graph preserves data/ordering edge types."""
        g = Graph(
            [ask_user, generate],
            edges=[(ask_user, generate), (generate, ask_user)],
        )
        flat = g.to_flat_graph()
        for u, v, data in flat.edges(data=True):
            if u == "ask_user":
                # ask_user → generate: ask_user has no matching outputs for generate's inputs
                # actually ask_user has output "query" and generate has input "messages" — no overlap
                assert data["edge_type"] == "ordering"
            elif u == "generate":
                # generate → ask_user: generate has output "response", ask_user has no inputs
                assert data["edge_type"] == "ordering"
