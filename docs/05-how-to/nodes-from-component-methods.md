# How to Build Nodes from Component Methods

Most real pipelines delegate the heavy lifting to configured components — an LLM client, a retriever, a reranker. The usual pattern wraps each call in a thin function node:

```python
@node(output_name="answer")
def generate(llm: LLM, question: str) -> str:
    return llm.generate(question)
```

That works, and it makes the component a graph input (late binding). But when the component is already constructed at graph-build time, the wrapper adds nothing — you can pass the **bound method** to `FunctionNode` directly:

```python
from hypergraph.nodes.function import FunctionNode

llm = LLM(model="gpt-5.5-mini")

generate = FunctionNode(llm.generate, name="generate", output_name="answer")
```

`inspect.signature` strips `self`, so the node's inputs are exactly the method's remaining parameters (`question`). The instance travels with the node — no wrapper function, no component input port.

## Two Instances, Two Nodes

Because the instance is bound, the same class can appear in one graph under different configurations:

```python
class Summarizer:
    def __init__(self, model: str):
        self.model = model

    def summarize(self, text: str) -> str:
        return call_llm(self.model, f"Summarize: {text}")

fast = Summarizer(model="gpt-5.5-mini")
deep = Summarizer(model="gpt-5.5")

draft = FunctionNode(fast.summarize, name="draft", output_name="draft")
final = FunctionNode(
    deep.summarize, name="final", output_name="final"
).rename_inputs(text="draft")

graph = Graph([draft, final])
```

Each node carries its own instance state. Combined with `rename_inputs` / `rename_outputs` at the boundaries, this removes most "wrapper" nodes whose only job was to call a method on a component.

## When to Prefer Which Binding

| | Bound method (early binding) | Component as input (late binding) |
|---|---|---|
| Component is chosen | at graph construction | at `run()` time |
| Component appears in `graph.inputs` | no | yes |
| Same graph, different component per run | rebuild the graph | pass a different input |
| Wrapper function needed | no | yes |

If a config factory builds both the components and the graph in one place, early binding is natural — the configuration already decided which instance to use. Reach for late binding when the *same compiled graph* must serve differently-configured components at run time.

## Caching Caveat

`definition_hash` for a bound method includes an **instance fingerprint**: a `cache_key()` method on the instance if it defines one, otherwise a deterministic serialization of `vars(instance)`. Two differently-configured instances therefore hash differently, and `cache=True` on bound-method nodes is safe.

The fingerprint is captured when the node is constructed. If you mutate instance state after constructing the node, the hash does not track it — construct nodes from fully-configured instances.

```python
node_fast = FunctionNode(fast.summarize, name="s", output_name="out")
node_deep = FunctionNode(deep.summarize, name="s", output_name="out")
assert node_fast.definition_hash != node_deep.definition_hash  # different model attrs
```

## What's Next?

- [Rename and Adapt](rename-and-adapt.md) — wiring reused nodes under different names
- [Declare Edges Explicitly](declare-edges-explicitly.md) — making topology reviewable
- [Caching](../03-patterns/08-caching.md) — cache keys, batch runs, and invalidation
