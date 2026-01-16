#!/usr/bin/env python3
"""Test Graph examples from the new getting-started.md section."""

from hypergraph import node, Graph

print("=== Basic Graph Construction ===")

@node(output_name="result")
def add(a: int, b: int) -> int:
    return a + b

@node(output_name="final")
def double(result: int) -> int:
    return result * 2

g = Graph([add, double])

print(f"g.nodes.keys() = {list(g.nodes.keys())}")  # ['add', 'double']
print(f"g.outputs = {g.outputs}")                   # ('final',)
print(f"g.inputs = {g.inputs}")

print("\n=== Graph Properties ===")
print(f"g.inputs.required = {g.inputs.required}")  # ('a', 'b')
print(f"g.inputs.optional = {g.inputs.optional}")  # ()
print(f"g.outputs = {g.outputs}")                  # ('final',)
print(f"g.has_cycles = {g.has_cycles}")            # False
print(f"g.has_async_nodes = {g.has_async_nodes}")  # False

print("\n=== Binding Values ===")
bound = g.bind(a=10)
print(f"bound.inputs.required = {bound.inputs.required}")  # ('b',)
print(f"bound.inputs.bound = {bound.inputs.bound}")        # ('a',)

print("\n=== strict_types Compatible ===")

@node(output_name="value")
def producer() -> int:
    return 42

@node(output_name="result")
def consumer(value: int) -> int:
    return value * 2

g = Graph([producer, consumer], strict_types=True)
print(f"g.strict_types = {g.strict_types}")  # True

print("\n=== Union Type ===")

@node(output_name="value")
def producer2() -> int:
    return 42

@node(output_name="result")
def consumer2(value: int | str) -> str:
    return str(value)

g = Graph([producer2, consumer2], strict_types=True)
print(f"Union type works: {g.strict_types}")  # True

print("\n=== Type Mismatch Error ===")

@node(output_name="value")
def bad_producer() -> int:
    return 42

@node(output_name="result")
def bad_consumer(value: str) -> str:
    return value.upper()

try:
    Graph([bad_producer, bad_consumer], strict_types=True)
    print("ERROR: Should have raised!")
except Exception as e:
    print(f"Correctly raised: {type(e).__name__}")
    print(f"Error contains 'Type mismatch': {'Type mismatch' in str(e)}")

print("\n=== Missing Annotation Error ===")

@node(output_name="value")
def no_annotation_producer():  # No return type
    return 42

@node(output_name="result")
def annotated_consumer(value: int) -> int:
    return value * 2

try:
    Graph([no_annotation_producer, annotated_consumer], strict_types=True)
    print("ERROR: Should have raised!")
except Exception as e:
    print(f"Correctly raised: {type(e).__name__}")
    print(f"Error contains 'Missing type annotation': {'Missing type annotation' in str(e)}")

print("\n=== All Graph examples passed! ===")
