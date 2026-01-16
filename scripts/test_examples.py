#!/usr/bin/env python3
"""Test key examples from getting-started.md by writing to actual file."""

from hypergraph import node, FunctionNode, Graph

# Example 1: Basic node
@node(output_name="result")
def add(x: int, y: int) -> int:
    return x + y

print("=== Example 1: Basic node ===")
print(f"add.name = {add.name}")       # "add"
print(f"add.inputs = {add.inputs}")   # ("x", "y")
print(f"add.outputs = {add.outputs}") # ("result",)

# Example 2: Double node
@node(output_name="doubled")
def double(x: int) -> int:
    """Double the input."""
    return x * 2

print("\n=== Example 2: Double node ===")
result = double(5)
print(f"double(5) = {result}")         # 10
print(f"double.inputs = {double.inputs}")     # ("x",)
print(f"double.outputs = {double.outputs}")   # ("doubled",)
print(f"double.is_async = {double.is_async}") # False

# Example 3: Side-effect node
@node  # No output_name
def log(message: str) -> None:
    print(f"[LOG] {message}")

print("\n=== Example 3: Side-effect node ===")
print(f"log.outputs = {log.outputs}")  # ()

# Example 4: Multiple outputs
@node(output_name=("mean", "std"))
def statistics(data: list) -> tuple[float, float]:
    mean = sum(data) / len(data)
    std = (sum((x - mean) ** 2 for x in data) / len(data)) ** 0.5
    return mean, std

print("\n=== Example 4: Multiple outputs ===")
print(f"statistics.outputs = {statistics.outputs}")  # ("mean", "std")

# Example 5: Renaming
@node(output_name="result")
def process(text: str) -> str:
    return text.upper()

print("\n=== Example 5: Renaming ===")
adapted = process.with_inputs(text="raw_input").with_outputs(result="processed")
print(f"adapted.inputs = {adapted.inputs}")    # ("raw_input",)
print(f"adapted.outputs = {adapted.outputs}")  # ("processed",)
print(f"process.inputs = {process.inputs}")    # ("text",) - unchanged

# Example 6: with_name
preprocessor = process.with_name("string_preprocessor")
print(f"\npreprocessor.name = {preprocessor.name}")  # "string_preprocessor"

# Example 7: Generator
@node(output_name="chunks")
def chunk_text(text: str, size: int = 100) -> list[str]:
    return [text[i:i+size] for i in range(0, len(text), size)]

print("\n=== Example 7: Generator-like ===")
print(f"chunk_text.is_generator = {chunk_text.is_generator}")

# Example 8: Async
@node(output_name="data")
async def fetch(url: str) -> dict:
    return {}

print("\n=== Example 8: Async ===")
print(f"fetch.is_async = {fetch.is_async}")  # True

# Example 9: Cache
@node(output_name="result", cache=True)
def expensive(x: int) -> int:
    return x ** 100

print("\n=== Example 9: Cache ===")
print(f"expensive.cache = {expensive.cache}")  # True

# Example 10: Definition hash
print("\n=== Example 10: Definition hash ===")
print(f"len(process.definition_hash) = {len(process.definition_hash)}")  # 64

# Example 11: FunctionNode reconfiguration
print("\n=== Example 11: FunctionNode reconfiguration ===")
@node(output_name="original_output", cache=True)
def proc(x: int) -> int:
    return x * 2

reconfigured = FunctionNode(
    proc,
    name="new_name",
    output_name="new_output",
    cache=False,
)
print(f"reconfigured.name = {reconfigured.name}")        # "new_name"
print(f"reconfigured.outputs = {reconfigured.outputs}")  # ("new_output",)
print(f"reconfigured.cache = {reconfigured.cache}")      # False
print(f"proc.cache = {proc.cache}")                      # True (unchanged)
print(f"reconfigured.func is proc.func = {reconfigured.func is proc.func}")  # True

# Example 12: Graph with strict_types
print("\n=== Example 12: Graph with strict_types ===")
@node(output_name="result")
def producer() -> int:
    return 42

@node(output_name="final")
def consumer(result: int) -> int:
    return result * 2

g = Graph([producer, consumer], strict_types=True)
print(f"Graph created with strict_types={g.strict_types}")
print(f"Nodes: {list(g.nodes.keys())}")
print(f"Edge exists: {g.nx_graph.has_edge('producer', 'consumer')}")

print("\n=== All examples passed! ===")
