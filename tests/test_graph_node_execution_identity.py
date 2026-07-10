"""Public execution-identity surface for configured ``GraphNode`` values."""

from dataclasses import FrozenInstanceError, dataclass

import pytest

from hypergraph import Graph, GraphNodeMapExecutionConfig, node


@dataclass
class Page:
    document_id: str
    text: str


@node(output_name="prepared")
def prepare_page(page: Page, context: dict[str, str]) -> str:
    return f"{context['prefix']}:{page.text}"


def test_map_execution_config_exposes_all_effective_runner_values() -> None:
    graph_node = Graph([prepare_page], name="prepare_page").as_node()

    assert graph_node.map_execution_config is None

    mapped = graph_node.map_over(
        "page",
        mode="product",
        error_handling="continue",
        clone=["context"],
        identity="document_id",
        schema=Page,
    )

    assert mapped.map_execution_config == GraphNodeMapExecutionConfig(
        params=("page",),
        mode="product",
        error_handling="continue",
        clone=("context",),
        identity="document_id",
        schema=Page,
    )
    assert mapped.map_config == (["page"], "product", "continue")


@pytest.mark.parametrize("clone", [False, True])
def test_map_execution_config_distinguishes_boolean_clone(clone: bool) -> None:
    mapped = Graph([prepare_page], name="prepare_page").as_node().map_over("page", clone=clone)

    assert mapped.map_execution_config == GraphNodeMapExecutionConfig(
        params=("page",),
        mode="zip",
        error_handling="raise",
        clone=clone,
        identity=None,
        schema=None,
    )


def test_map_execution_config_is_frozen_and_preserved_by_renames() -> None:
    mapped = Graph([prepare_page], name="prepare_page").as_node().map_over("page", clone=["context"], identity="document_id", schema=Page)
    renamed = mapped.rename_inputs(page="pages", context="settings").with_name("renamed")

    assert renamed.map_execution_config == GraphNodeMapExecutionConfig(
        params=("pages",),
        mode="zip",
        error_handling="raise",
        clone=("settings",),
        identity="document_id",
        schema=Page,
    )
    assert renamed.map_config == (["pages"], "zip", "raise")

    with pytest.raises(FrozenInstanceError):
        renamed.map_execution_config.mode = "product"


def test_complete_on_stop_exposes_effective_value_and_survives_copy() -> None:
    graph = Graph([prepare_page], name="prepare_page")

    assert graph.as_node().complete_on_stop is False

    finishing = graph.as_node(complete_on_stop=True)
    assert finishing.complete_on_stop is True
    assert finishing.with_name("renamed").complete_on_stop is True
