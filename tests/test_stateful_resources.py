from __future__ import annotations

import pickle

import pytest

from hypergraph import AsyncRunner, Graph, GraphConfigError, SyncRunner, node, stateful


@stateful(resource=True)
class PickleResource:
    def __init__(self, value: str) -> None:
        self.value = value

    def close(self) -> None:
        pass


def test_sync_resource_scope_materializes_bound_stateful_resource() -> None:
    events: list[tuple[str, str]] = []

    @stateful(resource=True)
    class Prefixer:
        def __init__(self, prefix: str) -> None:
            events.append(("init", prefix))
            self.prefix = prefix

        def format(self, text: str) -> str:
            return f"{self.prefix}:{text}"

        def close(self) -> None:
            events.append(("close", self.prefix))

    @node(output_name="formatted")
    def format_text(text: str, prefixer: Prefixer) -> str:
        return prefixer.format(text)

    prefixer = Prefixer("prod")
    graph = Graph([format_text]).bind(prefixer=prefixer)

    assert events == []

    with graph.resources() as ready_graph:
        assert events == [("init", "prod")]
        result = SyncRunner().run(ready_graph, {"text": "hello"})
        assert result.values["formatted"] == "prod:hello"

    assert events == [("init", "prod"), ("close", "prod")]


def test_sync_resource_scope_rejects_async_only_resource_before_opening() -> None:
    events: list[str] = []

    @stateful(resource=True)
    class AsyncOnly:
        def __init__(self) -> None:
            events.append("init")

        async def aclose(self) -> None:
            events.append("aclose")

    @node(output_name="out")
    def use_resource(resource: AsyncOnly) -> str:
        return "ok"

    graph = Graph([use_resource]).bind(resource=AsyncOnly())

    with pytest.raises(TypeError, match="async cleanup"), graph.resources():
        pass

    assert events == []


@pytest.mark.asyncio
async def test_async_resource_scope_handles_sync_async_and_dual_cleanup() -> None:
    events: list[str] = []

    @stateful(resource=True)
    class CloseOnly:
        def __init__(self) -> None:
            events.append("close:init")

        def close(self) -> None:
            events.append("close:close")

    @stateful(resource=True)
    class ACloseOnly:
        def __init__(self) -> None:
            events.append("aclose:init")

        async def aclose(self) -> None:
            events.append("aclose:aclose")

    @stateful(resource=True)
    class Both:
        def __init__(self) -> None:
            events.append("both:init")

        def close(self) -> None:
            events.append("both:close")

        async def aclose(self) -> None:
            events.append("both:aclose")

    @node(output_name="out")
    async def use_resources(close_only: CloseOnly, aclose_only: ACloseOnly, both: Both) -> str:
        return "ok"

    graph = Graph([use_resources]).bind(
        close_only=CloseOnly(),
        aclose_only=ACloseOnly(),
        both=Both(),
    )

    async with graph.resources() as ready_graph:
        result = await AsyncRunner().run(ready_graph)
        assert result.values["out"] == "ok"

    assert events == [
        "close:init",
        "aclose:init",
        "both:init",
        "both:aclose",
        "aclose:aclose",
        "close:close",
    ]


def test_resource_true_requires_cleanup_unless_resource_false() -> None:
    with pytest.raises(TypeError, match="resource=True"):

        @stateful(resource=True)
        class MissingCleanup:
            pass

    @stateful(resource=False)
    class PlainState:
        def __init__(self, value: str) -> None:
            self.value = value

    @node(output_name="out")
    def use_state(state: PlainState) -> str:
        return state.value

    graph = Graph([use_state]).bind(state=PlainState("ok"))

    with graph.resources() as ready_graph:
        result = SyncRunner().run(ready_graph)

    assert result.values["out"] == "ok"


def test_resource_cleanup_method_names_can_be_explicit() -> None:
    events: list[str] = []

    @stateful(resource=True, close="shutdown")
    class CustomShutdown:
        def __init__(self) -> None:
            events.append("init")

        def shutdown(self) -> None:
            events.append("shutdown")

    @node(output_name="out")
    def use_resource(resource: CustomShutdown) -> str:
        return "ok"

    graph = Graph([use_resource]).bind(resource=CustomShutdown())

    with graph.resources() as ready_graph:
        result = SyncRunner().run(ready_graph)
        assert result.values["out"] == "ok"

    assert events == ["init", "shutdown"]


def test_same_stateful_handle_is_materialized_once_per_scope() -> None:
    events: list[str] = []

    @stateful(resource=True)
    class Resource:
        def __init__(self, name: str) -> None:
            events.append(f"init:{name}")
            self.name = name

        def close(self) -> None:
            events.append(f"close:{self.name}")

    @node(output_name="shared")
    def compare_resources(left: Resource, right: Resource) -> bool:
        return left is right

    handle = Resource("shared")
    graph = Graph([compare_resources]).bind(left=handle, right=handle)

    with graph.resources() as ready_graph:
        result = SyncRunner().run(ready_graph)
        assert result.values["shared"] is True

    assert events == ["init:shared", "close:shared"]


def test_parent_resource_scope_materializes_nested_graph_resources() -> None:
    events: list[str] = []

    @stateful(resource=True)
    class Resource:
        def __init__(self, name: str) -> None:
            events.append(f"init:{name}")
            self.name = name

        def close(self) -> None:
            events.append(f"close:{self.name}")

    @node(output_name="left_value")
    def use_left(resource: Resource) -> str:
        return resource.name

    @node(output_name="right_value")
    def use_right(resource: Resource) -> str:
        return resource.name

    left = Graph([use_left], name="left").bind(resource=Resource("left"))
    right = Graph([use_right], name="right").bind(resource=Resource("right"))
    parent = Graph(
        [
            left.as_node(name="left", namespaced=True),
            right.as_node(name="right", namespaced=True),
        ],
        name="parent",
    )

    with parent.resources() as ready_graph:
        result = SyncRunner().run(ready_graph)
        assert result.values["left.left_value"] == "left"
        assert result.values["right.right_value"] == "right"

    assert events == [
        "init:left",
        "init:right",
        "close:right",
        "close:left",
    ]


def test_different_stateful_handles_are_not_deduplicated_by_constructor_args() -> None:
    events: list[str] = []

    @stateful(resource=True)
    class Resource:
        def __init__(self, name: str) -> None:
            events.append(f"init:{name}")
            self.name = name

        def close(self) -> None:
            events.append(f"close:{self.name}")

    @node(output_name="same")
    def compare_resources(left: Resource, right: Resource) -> bool:
        return left is right

    graph = Graph([compare_resources]).bind(left=Resource("same"), right=Resource("same"))

    with graph.resources() as ready_graph:
        result = SyncRunner().run(ready_graph)
        assert result.values["same"] is False

    assert events == [
        "init:same",
        "init:same",
        "close:same",
        "close:same",
    ]


def test_materialized_resource_is_instance_of_decorated_class() -> None:
    @stateful(resource=True)
    class Resource:
        def close(self) -> None:
            pass

    @node(output_name="is_resource")
    def check_resource(resource: Resource) -> bool:
        return isinstance(resource, Resource)

    graph = Graph([check_resource]).bind(resource=Resource())

    with graph.resources() as ready_graph:
        result = SyncRunner().run(ready_graph)

    assert result.values["is_resource"] is True


def test_resource_scope_can_be_reentered_with_fresh_instances() -> None:
    events: list[str] = []

    @stateful(resource=True)
    class Resource:
        def __init__(self) -> None:
            events.append("init")

        def close(self) -> None:
            events.append("close")

    @node(output_name="ok")
    def use_resource(resource: Resource) -> bool:
        return isinstance(resource, Resource)

    graph = Graph([use_resource]).bind(resource=Resource())
    scope = graph.resources()

    with scope as ready_graph:
        assert SyncRunner().run(ready_graph).values["ok"] is True

    with scope as ready_graph:
        assert SyncRunner().run(ready_graph).values["ok"] is True

    assert events == ["init", "close", "init", "close"]


def test_runner_explains_stateful_handle_needs_resource_scope() -> None:
    @stateful(resource=True)
    class Resource:
        def close(self) -> None:
            pass

        def value(self) -> str:
            return "ok"

    @node(output_name="value")
    def use_resource(resource: Resource) -> str:
        return resource.value()

    graph = Graph([use_resource]).bind(resource=Resource())

    with pytest.raises(GraphConfigError, match=r"graph\.resources\(\)"):
        SyncRunner().run(graph)


def test_stateful_handle_round_trips_with_standard_pickle() -> None:
    handle = PickleResource("ok")
    restored = pickle.loads(pickle.dumps(handle))

    @node(output_name="value")
    def use_resource(resource: PickleResource) -> str:
        return resource.value

    graph = Graph([use_resource]).bind(resource=restored)

    with graph.resources() as ready_graph:
        result = SyncRunner().run(ready_graph)

    assert result.values["value"] == "ok"
