"""Test that validate_node_types handles dual-import scenarios gracefully.

Reproduces a real-world bug where the same class loaded from two different
sys.path entries creates distinct type objects. The identity check in
validate_node_types fails because type(node) is not the same object as
the type in supported_types, even though they have the same __name__.

This happens in Jupyter notebooks when the kernel uses system Python but
packages are injected via sys.path from a venv.
"""

import pytest

from hypergraph import Graph, node
from hypergraph.nodes.function import FunctionNode
from hypergraph.runners._shared.validation import validate_node_types


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


def _make_shadow_class(original: type) -> type:
    """Create a new class with the same name/qualname but different identity.

    Simulates what happens when Python imports the same module from two
    different sys.path entries — you get two class objects with identical
    names that fail identity checks.
    """
    shadow = type(original.__name__, original.__bases__, dict(original.__dict__))
    shadow.__qualname__ = original.__qualname__
    shadow.__module__ = original.__module__
    return shadow


def test_validate_node_types_fails_on_dual_import():
    """validate_node_types rejects a node whose type was loaded from a different path.

    This is the confusing error:
        TypeError: Runner does not support node type 'FunctionNode'.
                   Supported types: ['FunctionNode']
    """
    graph = Graph(nodes=[double])

    # Simulate a runner whose supported_types contains a *different*
    # FunctionNode class object — same name, different identity.
    shadow_function_node = _make_shadow_class(FunctionNode)
    supported = {shadow_function_node}

    with pytest.raises(TypeError, match="FunctionNode.*FunctionNode"):
        validate_node_types(graph, supported)
