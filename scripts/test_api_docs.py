#!/usr/bin/env python3
"""Verify API documentation examples work correctly."""

from hypergraph import node, Graph, FunctionNode

print("=== Graph API Examples ===")

# Basic graph
@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

@node(output_name="result")
def add_one(doubled: int) -> int:
    return doubled + 1

g = Graph([double, add_one])
print(f"nodes: {list(g.nodes.keys())}")
print(f"outputs: {g.outputs}")
print(f"inputs.required: {g.inputs.required}")

# Named graph
g2 = Graph([double], name="my_graph")
print(f"name: {g2.name}")

# strict_types
@node(output_name="value")
def producer() -> int:
    return 42

@node(output_name="final")
def consumer(value: int) -> int:
    return value * 2

g3 = Graph([producer, consumer], strict_types=True)
print(f"strict_types: {g3.strict_types}")

print("\n=== GraphNode Examples ===")

inner = Graph([double], name="doubler")
gn = inner.as_node()
print(f"gn.name: {gn.name}")
print(f"gn.inputs: {gn.inputs}")
print(f"gn.outputs: {gn.outputs}")
print(f"gn.graph.name: {gn.graph.name}")
print(f"gn.is_async: {gn.is_async}")

# Override name
gn2 = inner.as_node(name="my_doubler")
print(f"override name: {gn2.name}")

# Type forwarding
print(f"get_output_type('doubled'): {gn.get_output_type('doubled')}")

# Nested composition
@node(output_name="tripled")
def triple(doubled: int) -> int:
    return doubled + doubled // 2

inner2 = Graph([double, triple], name="multiply")
@node(output_name="final")
def finalize(tripled: int) -> str:
    return f"Result: {tripled}"

outer = Graph([inner2.as_node(), finalize])
print(f"outer.inputs.required: {outer.inputs.required}")
print(f"outer.outputs: {outer.outputs}")

print("\n=== InputSpec Examples ===")

@node(output_name="embedding")
def embed(text: str) -> list[float]:
    return [0.1, 0.2, 0.3]

@node(output_name="docs")
def retrieve(embedding: list[float], top_k: int = 5) -> list[str]:
    return ["doc1", "doc2"]

g4 = Graph([embed, retrieve])
print(f"required: {g4.inputs.required}")
print(f"optional: {g4.inputs.optional}")
print(f"seeds: {g4.inputs.seeds}")
print(f"bound: {g4.inputs.bound}")
print(f"all: {g4.inputs.all}")

# Binding
bound = g4.bind(top_k=10)
print(f"after bind - bound: {bound.inputs.bound}")
print(f"after bind - optional: {bound.inputs.optional}")

print("\n=== All API doc examples passed! ===")
