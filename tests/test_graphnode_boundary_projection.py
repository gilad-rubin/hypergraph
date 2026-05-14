"""GraphNode boundary projection semantics."""

from __future__ import annotations

import pytest

from hypergraph import Graph, interrupt, node
from hypergraph.exceptions import MissingInputError
from hypergraph.graph.validation import GraphConfigError
from hypergraph.nodes import RenameError
from hypergraph.runners import AsyncRunner, SyncRunner


def test_namespaced_graphnode_projects_parent_facing_addresses_and_runs():
    @node(output_name="y")
    def double(x: int) -> int:
        return x * 2

    inner = Graph([double])

    graph_node = inner.as_node(name="researcher", namespaced=True)
    assert graph_node.inputs == ("researcher.x",)
    assert graph_node.outputs == ("researcher.y",)

    outer = Graph([graph_node], name="outer")

    assert outer.inputs.required == ("researcher.x",)

    result = SyncRunner().run(outer, {"researcher.x": 2})

    assert result["researcher.y"] == 4


@pytest.mark.asyncio
async def test_deep_namespaced_interrupt_resume_key_is_validated_as_parent_address():
    @interrupt(output_name="decision")
    def approve() -> str | None:
        return None

    inner = Graph([approve], name="inner")
    middle = Graph([inner.as_node(namespaced=True)], name="middle")
    outer = Graph([middle.as_node(namespaced=True)], name="outer")

    runner = AsyncRunner()

    paused = await runner.run(outer, {})
    assert paused.pause is not None
    assert paused.pause.response_key == "middle.inner.decision"

    resumed = await runner.run(outer, {paused.pause.response_key: "yes"})

    assert resumed["middle.inner.decision"] == "yes"


@pytest.mark.asyncio
async def test_stale_partial_interrupt_resume_key_is_not_accepted_for_deep_namespaced_graphnode():
    @interrupt(output_name="decision")
    def approve() -> str | None:
        return None

    inner = Graph([approve], name="inner")
    middle = Graph([inner.as_node(namespaced=True)], name="middle")
    outer = Graph([middle.as_node(namespaced=True)], name="outer")

    with pytest.warns(UserWarning, match="Not recognized"):
        paused = await AsyncRunner().run(outer, {"inner.decision": "yes"})

    assert paused.pause is not None
    assert paused.pause.response_key == "middle.inner.decision"


def test_describe_uses_resolved_graphnode_port_addresses():
    @node(output_name="docs")
    def retrieve(query: str) -> str:
        return f"docs:{query}"

    outer = Graph([Graph([retrieve], name="retrieval").as_node(namespaced=True)], name="outer")

    summary = outer.describe(show_types=False)

    assert "required: retrieval.query" in summary
    assert "Outputs: retrieval.docs" in summary


def test_exposed_namespaced_inputs_share_one_flat_parent_input():
    @node(output_name="retrieved")
    def retrieve(query: str) -> str:
        return f"docs:{query}"

    @node(output_name="answer")
    def generate(query: str) -> str:
        return f"answer:{query}"

    retrieval = Graph([retrieve], name="retrieval")
    generation = Graph([generate], name="generation")

    outer = Graph(
        [
            retrieval.as_node(namespaced=True).expose("query"),
            generation.as_node(namespaced=True).expose("query"),
        ],
        name="rag",
    )

    assert outer.inputs.required == ("query",)
    assert "retrieval.query" not in outer.inputs.all
    assert "generation.query" not in outer.inputs.all

    result = SyncRunner().run(outer, {"query": "what is hypergraph?"})

    assert result["retrieval.retrieved"] == "docs:what is hypergraph?"
    assert result["generation.answer"] == "answer:what is hypergraph?"


def test_exposed_output_alias_is_flat_and_autowires_downstream():
    @node(output_name=("response", "trace"))
    def agent(prompt: str) -> tuple[str, str]:
        return (f"response:{prompt}", "trace")

    @node(output_name="judgement")
    def judge(researcher_response: str) -> str:
        return f"judged:{researcher_response}"

    agent_graph = Graph([agent], name="agent")
    researcher = agent_graph.as_node(name="researcher", namespaced=True).expose(response="researcher_response")
    outer = Graph([researcher, judge], name="team")

    assert researcher.outputs == ("researcher_response", "researcher.trace")
    assert outer.inputs.required == ("researcher.prompt",)

    result = SyncRunner().run(outer, {"researcher.prompt": "hello"})

    assert result["researcher_response"] == "response:hello"
    assert result["researcher.trace"] == "trace"
    assert result["judgement"] == "judged:response:hello"


def test_expose_targets_local_port_names_not_projected_addresses():
    @node(output_name="answer")
    def agent(query: str) -> str:
        return query

    graph_node = Graph([agent], name="retrieval").as_node(namespaced=True)

    with pytest.raises(ValueError, match="Local port names"):
        graph_node.expose("retrieval.query")


def test_expose_replaces_namespaced_address_for_input():
    @node(output_name="answer")
    def agent(query: str) -> str:
        return query

    graph_node = Graph([agent], name="retrieval").as_node(namespaced=True).expose("query")

    assert graph_node.inputs == ("query",)
    assert "retrieval.query" not in graph_node.inputs


def test_exposed_alias_stays_flat_when_graphnode_is_renamed():
    @node(output_name="answer")
    def agent(query: str) -> str:
        return query

    graph_node = Graph([agent], name="retrieval").as_node(namespaced=True).expose(answer="final_answer")
    renamed = graph_node.with_name("generation")

    assert renamed.inputs == ("generation.query",)
    assert renamed.outputs == ("final_answer",)


def test_stale_namespaced_input_address_after_rename_suggests_current_address():
    @node(output_name="answer")
    def agent(query: str) -> str:
        return query

    graph_node = Graph([agent], name="retrieval").as_node(namespaced=True).with_inputs(query="user_query")
    outer = Graph([graph_node])

    with pytest.raises(ValueError, match="Use 'retrieval.user_query'"):
        SyncRunner().run(outer, {"retrieval.query": "hello"})


def test_stale_namespaced_input_address_after_graphnode_rename_suggests_current_address():
    @node(output_name="answer")
    def agent(query: str) -> str:
        return query

    graph_node = Graph([agent], name="retrieval").as_node(namespaced=True).with_name("generation")
    outer = Graph([graph_node])

    with pytest.raises(ValueError, match="Use 'generation.query'"):
        SyncRunner().run(outer, {"retrieval.query": "hello"})


def test_stale_namespaced_input_address_for_flat_graphnode_suggests_flat_address():
    @node(output_name="answer")
    def agent(query: str) -> str:
        return query

    outer = Graph([Graph([agent], name="retrieval").as_node()])

    with pytest.raises(ValueError, match="Use 'query'"):
        SyncRunner().run(outer, {"retrieval.query": "hello"})


def test_renaming_exposed_local_port_is_rejected():
    @node(output_name="answer")
    def agent(query: str) -> str:
        return query

    graph_node = Graph([agent], name="retrieval").as_node(namespaced=True).expose("query", "answer")

    with pytest.raises(RenameError, match="Cannot rename exposed local input"):
        graph_node.with_inputs(query="user_query")

    with pytest.raises(RenameError, match="Cannot rename exposed local output"):
        graph_node.with_outputs(answer="final_answer")


def test_nested_dict_addressing_is_valid_for_namespaced_inputs():
    @node(output_name="answer")
    def agent(query: str) -> str:
        return f"answer:{query}"

    outer = Graph([Graph([agent], name="retrieval").as_node(namespaced=True)])

    result = SyncRunner().run(outer, {"retrieval": {"query": "hello"}})

    assert result["retrieval.answer"] == "answer:hello"


def test_nested_dict_addressing_is_not_created_for_flat_graphnodes():
    @node(output_name="answer")
    def agent(query: str) -> str:
        return f"answer:{query}"

    outer = Graph([Graph([agent], name="retrieval").as_node()])

    with pytest.warns(UserWarning, match="Not recognized"), pytest.raises(MissingInputError) as exc:
        SyncRunner().run(outer, {"retrieval": {"query": "hello"}})

    assert exc.value.missing == ["query"]
    assert "retrieval" in exc.value.provided


def test_stale_exposed_runtime_address_errors_with_suggestion():
    @node(output_name="answer")
    def agent(query: str) -> str:
        return f"answer:{query}"

    graph_node = Graph([agent], name="retrieval").as_node(namespaced=True).expose("query")
    outer = Graph([graph_node])

    with pytest.raises(ValueError, match="Use 'query'"):
        SyncRunner().run(outer, {"retrieval.query": "hello"})

    with pytest.raises(ValueError, match="Use 'query'"):
        SyncRunner().run(outer, {"query": "hello", "retrieval.query": "hello"})

    with pytest.raises(ValueError, match="Use 'query'"):
        SyncRunner().run(outer, {"retrieval": {"query": "hello"}})


def test_inner_bind_values_project_through_namespaced_and_exposed_boundary():
    @node(output_name="answer")
    def agent(query: str, model: str) -> str:
        return f"{model}:{query}"

    inner = Graph([agent], name="agent").bind(model="gpt")

    namespaced = Graph([inner.as_node(name="researcher", namespaced=True)])
    exposed = Graph([inner.as_node(name="researcher", namespaced=True).expose("model")])

    assert namespaced.inputs.bound == {"researcher.model": "gpt"}
    assert exposed.inputs.bound == {"model": "gpt"}


def test_with_inputs_renames_local_name_before_projection():
    @node(output_name="answer")
    def agent(query: str) -> str:
        return f"answer:{query}"

    graph_node = Graph([agent], name="retrieval").as_node(namespaced=True).with_inputs(query="user_query")
    outer = Graph([graph_node])

    assert graph_node.local_inputs == ("user_query",)
    assert graph_node.inputs == ("retrieval.user_query",)

    result = SyncRunner().run(outer, {"retrieval.user_query": "hello"})

    assert result["retrieval.answer"] == "answer:hello"


def test_map_over_and_clone_use_local_input_names():
    @node(output_name="doubled")
    def double(x: int, config: dict[str, int]) -> int:
        return x * config["multiplier"]

    inner = Graph([double], name="worker")

    graph_node = inner.as_node(namespaced=True).map_over("x", clone=["config"])
    outer = Graph([graph_node])

    assert graph_node.map_config is not None
    assert graph_node.map_config[0] == ["x"]

    result = SyncRunner().run(outer, {"worker.x": [1, 2], "worker.config": {"multiplier": 3}})

    assert result["worker.doubled"] == [3, 6]

    with pytest.raises(ValueError, match="not an input"):
        inner.as_node(namespaced=True).map_over("worker.x")

    with pytest.raises(ValueError, match="not an input"):
        inner.as_node(namespaced=True).map_over("x", clone=["worker.config"])


def test_same_local_name_can_be_projected_as_input_and_output_for_cycles():
    @node(output_name="messages")
    def append_message(messages: list[str]) -> list[str]:
        return [*messages, "assistant"]

    graph_node = Graph([append_message], name="chat", entrypoint="append_message").as_node(namespaced=True)
    outer = Graph([graph_node], entrypoint="chat")

    assert "chat.messages" in graph_node.inputs
    assert "chat.messages" in graph_node.outputs
    assert outer.inputs.required == ("chat.messages",)


def test_duplicate_exposed_outputs_inside_one_graphnode_error():
    @node(output_name=("a", "b"))
    def produce() -> tuple[int, int]:
        return (1, 2)

    graph_node = Graph([produce], name="producer").as_node(namespaced=True)

    with pytest.raises(ValueError, match="already used"):
        graph_node.expose(a="x", b="x")


def test_cross_direction_exposed_alias_collision_inside_one_graphnode_errors():
    @node(output_name="response")
    def produce(query: str) -> str:
        return query

    graph_node = Graph([produce], name="producer").as_node(namespaced=True)

    with pytest.raises(ValueError, match="already used"):
        graph_node.expose(query="shared", response="shared")


def test_duplicate_exposed_input_aliases_inside_one_graphnode_error():
    @node(output_name="combined")
    def combine(query: str, prompt: str) -> str:
        return f"{query}:{prompt}"

    graph_node = Graph([combine], name="producer").as_node(namespaced=True)

    with pytest.raises(ValueError, match="already used"):
        graph_node.expose(query="shared", prompt="shared")


def test_select_defines_exposable_output_surface():
    @node(output_name=("response", "trace"))
    def agent(prompt: str) -> tuple[str, str]:
        return (f"response:{prompt}", "trace")

    selected = Graph([agent], name="agent").select("response")

    graph_node = selected.as_node(name="researcher", namespaced=True).expose("response")

    assert graph_node.outputs == ("response",)
    with pytest.raises(ValueError, match="not on GraphNode surface"):
        selected.as_node(name="researcher", namespaced=True).expose("trace")


def test_exposed_inputs_follow_default_consistency_rules():
    @node(output_name="retrieved")
    def retrieve(query: str = "docs") -> str:
        return query

    @node(output_name="answer")
    def generate(query: str = "answer") -> str:
        return query

    retrieval = Graph([retrieve], name="retrieval").as_node(namespaced=True).expose("query")
    generation = Graph([generate], name="generation").as_node(namespaced=True).expose("query")

    with pytest.raises(GraphConfigError, match="Inconsistent defaults for 'query'"):
        Graph([retrieval, generation])


def test_strict_types_validate_exposed_parent_address():
    @node(output_name="value")
    def produce() -> int:
        return 1

    @node(output_name="result")
    def consume(shared: int) -> int:
        return shared + 1

    producer = Graph([produce], name="producer").as_node(namespaced=True).expose(value="shared")
    graph = Graph([producer, consume], strict_types=True)

    assert SyncRunner().run(graph, {})["result"] == 2


def test_parent_wait_for_uses_resolved_namespaced_and_exposed_emit_addresses():
    @node(output_name="value", emit="done")
    def produce(query: str) -> str:
        return query

    @node(output_name="after", wait_for="retrieval.done")
    def after_namespaced() -> str:
        return "after"

    namespaced = Graph([Graph([produce], name="retrieval").as_node(namespaced=True), after_namespaced])
    assert SyncRunner().run(namespaced, {"retrieval.query": "hello"})["after"] == "after"

    @node(output_name="after", wait_for="done")
    def after_exposed() -> str:
        return "after"

    exposed_node = Graph([produce], name="retrieval").as_node(namespaced=True).expose("done")
    exposed = Graph([exposed_node, after_exposed])
    assert SyncRunner().run(exposed, {"retrieval.query": "hello"})["after"] == "after"


def test_graphnode_data_outputs_exclude_projected_emit_outputs():
    @node(output_name="value", emit="done")
    def produce() -> str:
        return "value"

    graph_node = Graph([produce], name="producer").as_node(namespaced=True)

    assert graph_node.outputs == ("producer.value", "producer.done")
    assert graph_node.data_outputs == ("producer.value",)


def test_boundary_projection_changes_definition_and_structural_hashes():
    @node(output_name="answer")
    def agent(query: str) -> str:
        return query

    inner = Graph([agent], name="agent")

    flat = inner.as_node()
    namespaced = inner.as_node(namespaced=True)
    exposed = inner.as_node(namespaced=True).expose("query")

    assert flat.definition_hash != namespaced.definition_hash
    assert namespaced.definition_hash != exposed.definition_hash
    assert Graph([flat]).structural_hash != Graph([namespaced]).structural_hash
    assert Graph([namespaced]).structural_hash != Graph([exposed]).structural_hash


def test_checkpoint_steps_use_parent_addresses_at_graphnode_boundary(tmp_path):
    from hypergraph.checkpointers import CheckpointPolicy, SqliteCheckpointer
    from hypergraph.checkpointers._migrate import ensure_schema

    @node(output_name="answer")
    def agent(query: str) -> str:
        return f"answer:{query}"

    checkpointer = SqliteCheckpointer(str(tmp_path / "checkpoint.db"))
    checkpointer.policy = CheckpointPolicy(durability="sync", retention="full")
    ensure_schema(checkpointer._sync_db())
    try:
        graph_node = Graph([agent], name="agent").as_node(name="researcher", namespaced=True).expose("query")
        graph = Graph([graph_node])

        SyncRunner(checkpointer=checkpointer).run(graph, {"query": "hello"}, workflow_id="wf")

        parent_step = next(step for step in checkpointer.steps("wf") if step.node_name == "researcher")
        child_step = next(step for step in checkpointer.steps("wf/researcher") if step.node_name == "agent")

        assert parent_step.input_versions == {"query": 1}
        assert parent_step.values == {"researcher.answer": "answer:hello"}
        assert child_step.input_versions == {"query": 1}
        assert child_step.values == {"answer": "answer:hello"}
    finally:
        checkpointer._sync_db().close()


@pytest.mark.asyncio
async def test_async_runner_uses_same_namespaced_expose_projection():
    @node(output_name="retrieved")
    async def retrieve(query: str) -> str:
        return f"docs:{query}"

    @node(output_name="answer")
    async def generate(query: str) -> str:
        return f"answer:{query}"

    retrieval = Graph([retrieve], name="retrieval")
    generation = Graph([generate], name="generation")
    outer = Graph(
        [
            retrieval.as_node(namespaced=True).expose("query"),
            generation.as_node(namespaced=True).expose("query"),
        ]
    )

    result = await AsyncRunner().run(outer, {"query": "hello"})

    assert result["retrieval.retrieved"] == "docs:hello"
    assert result["generation.answer"] == "answer:hello"
