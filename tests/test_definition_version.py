"""A declared ``version`` stands in for a node's code in every hash that keys on it."""

from __future__ import annotations

import functools
import importlib.util
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from hypergraph import FunctionNode, Graph, node
from hypergraph._utils import hash_definition
from hypergraph.materialization._fingerprint import compute_table_recipe_fingerprint
from hypergraph.nodes.gate import ifelse, route
from hypergraph.nodes.interrupt import interrupt


def _load(tmp_path: Path, source: str):
    """Import `source` as a fresh module from a real file, so inspect.getsource reads it."""
    name = f"_defver_{uuid.uuid4().hex}"
    path = tmp_path / f"{name}.py"
    path.write_text(textwrap.dedent(source))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


BEFORE = """
from hypergraph import node

@node(output_name="title", version=VERSION)
def page_title(filename: str) -> str:
    return filename.rsplit(".", 1)[0]
"""

# The same output, by other code: a docstring, a comment, a helper, a rename of a local.
REFACTORED = """
from hypergraph import node

def _stem(name: str) -> str:
    head, _, _ = name.rpartition(".")
    return head or name

@node(output_name="title", version=VERSION)
def page_title(filename: str) -> str:
    \"\"\"The document's display title: its filename without the extension.\"\"\"
    # rpartition keeps a name with no dot whole, as rsplit did
    return _stem(filename)
"""


def _step(tmp_path: Path, source: str, version):
    return _load(tmp_path, source.replace("VERSION", repr(version))).page_title


def test_a_code_edit_keeps_a_versioned_hash_and_moves_its_source_hash(tmp_path):
    before = _step(tmp_path, BEFORE, "1")
    after = _step(tmp_path, REFACTORED, "1")

    assert before.definition_hash == after.definition_hash
    assert before.source_hash != after.source_hash
    assert before.version == after.version == "1"


def test_a_new_version_moves_the_hash(tmp_path):
    one = _step(tmp_path, BEFORE, "1")
    two = _step(tmp_path, BEFORE, "2")

    assert one.definition_hash != two.definition_hash


def test_an_unversioned_node_keeps_todays_hash(tmp_path):
    module = _load(tmp_path, BEFORE.replace(", version=VERSION", ""))
    step = module.page_title

    assert step.version is None
    assert step.definition_hash == hash_definition(step.func)
    assert step.definition_hash == step.source_hash


def test_a_move_to_another_module_keeps_a_versioned_hash(tmp_path):
    here = _step(tmp_path, BEFORE, "3")
    there = _step(tmp_path, BEFORE, "3")

    assert here.func.__module__ != there.func.__module__
    assert here.definition_hash == there.definition_hash


def test_two_functions_at_one_version_stay_apart():
    @node(output_name="a", version="1")
    def first(x: int) -> int:
        return x

    @node(output_name="a", version="1")
    def second(x: int) -> int:
        return x

    assert first.definition_hash != second.definition_hash


def test_an_int_version_is_its_text_and_bad_versions_are_refused():
    def f(x: int) -> int:
        return x

    assert FunctionNode(f, output_name="y", version=2).definition_hash == FunctionNode(f, output_name="y", version="2").definition_hash
    for bad in (True, 1.5, ["1"]):
        with pytest.raises(TypeError, match="version"):
            FunctionNode(f, output_name="y", version=bad)
    with pytest.raises(ValueError, match="version"):
        FunctionNode(f, output_name="y", version="  ")


def test_instance_state_and_bound_arguments_still_enter_a_versioned_hash():
    class Summarizer:
        def __init__(self, model: str) -> None:
            self.model = model

        def summarize(self, text: str) -> str:
            return text

    small = FunctionNode(Summarizer("small").summarize, output_name="s", version="1")
    large = FunctionNode(Summarizer("large").summarize, output_name="s", version="1")
    assert small.definition_hash != large.definition_hash

    def scale(x: int, factor: int) -> int:
        return x * factor

    double = FunctionNode(functools.partial(scale, factor=2), name="double", output_name="y", version="1")
    triple = FunctionNode(functools.partial(scale, factor=3), name="triple", output_name="y", version="1")
    assert double.definition_hash != triple.definition_hash


def test_gates_and_interrupts_take_a_version():
    @route(targets=["a", "b"], version="1")
    def pick(x: int) -> str:
        return "a"

    @ifelse(when_true="a", when_false="b", version="1")
    def check(x: int) -> bool:
        return True

    @interrupt(answer_name="decision", version="1")
    def ask(x: int) -> str:
        return "?"

    assert (pick.version, check.version, ask.version) == ("1", "1", "1")
    for gate in (pick, check, ask):
        assert gate.definition_hash == hash_definition(gate.func, "1")
        assert gate.definition_hash != gate.source_hash


def test_a_graph_and_a_table_recipe_follow_the_versions_not_the_code(tmp_path):
    def recipe(module):
        graph = Graph([module.page_title], name="pages")
        return graph.definition_hash, compute_table_recipe_fingerprint(graph, {})

    before = recipe(_load(tmp_path, BEFORE.replace("VERSION", "'1'")))
    refactored = recipe(_load(tmp_path, REFACTORED.replace("VERSION", "'1'")))
    bumped = recipe(_load(tmp_path, REFACTORED.replace("VERSION", "'2'")))

    assert before == refactored
    assert bumped[0] != before[0] and bumped[1] != before[1]
