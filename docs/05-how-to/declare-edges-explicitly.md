# How to Declare Edges Explicitly

Control graph topology directly instead of relying on name matching. Hypergraph has three wiring modes — automatic, explicit, and additive — and picking the right one makes the difference between a graph that rewires itself silently and a topology you can review in a diff.

## The Three Wiring Modes

| Mode | Constructor | Data edges come from |
|------|-------------|----------------------|
| Automatic (default) | `Graph([a, b, c])` | Name matching: output `x` feeds input `x` |
| Explicit | `Graph([...], edges=[...])` | Your declared edges only — auto-inference is disabled |
| Additive | `Graph([...], edges=[...], shared=[...])` | Auto-inference for non-shared params, plus your declared edges on top |

In **all three modes**, two kinds of edges are always auto-wired:

- **Control edges** from gate nodes (`@route`, `@ifelse`) to their targets
- **Ordering edges** from `emit`/`wait_for` declarations

You never declare those by hand. Don't declare edges from a gate to its targets — the explicit edge would override the auto-wired control edge type.

## Automatic Mode (Default)

The default you already know: matching output/input names create edges.

```python
from hypergraph import Graph, node

@node(output_name="embedding")
def embed(text: str) -> list[float]: ...

@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]: ...

graph = Graph([embed, retrieve])  # embed -> retrieve, inferred from "embedding"
```

This is the right mode for small pipelines: zero wiring code, and renaming an output deliberately rewires every consumer.

## Explicit Mode: `edges=` Without `shared=`

Pass `edges` to take over data wiring completely. Auto-inference is disabled; the edges you declare are the **only** data edges in the graph.

```python
@node(output_name="messages")
def add_query(messages: list, query: str) -> list:
    return [*messages, {"role": "user", "content": query}]

@node(output_name="response")
def generate(messages: list) -> str:
    return llm.chat(messages)

@node(output_name="messages")
def add_response(messages: list, response: str) -> list:
    return [*messages, {"role": "assistant", "content": response}]

chat = Graph(
    [add_query, generate, add_response],
    edges=[
        (add_query, generate),         # carries: messages
        (generate, add_response),      # carries: response
        (add_response, add_query),     # carries: messages (cycle)
    ],
)
```

Each edge is a 2-tuple or 3-tuple:

| Form | Behavior |
|------|----------|
| `(a, b)` | Value names auto-detected from the intersection of `a`'s outputs and `b`'s inputs |
| `(a, b, "x")` | Pins exactly which value flows; build fails if `x` is not an output of `a` and an input of `b` |
| `(a, b, ["x", "y"])` | Pins multiple values |
| `(a, b)` with no name overlap | Ordering-only edge — a structural dependency with no data flow |

Source and target can be node objects or string names. Inputs not fed by any declared edge become graph inputs, provided at `run()` time or via `bind()`.

## Additive Mode: `edges=` With `shared=`

When `shared=` is present, the semantics flip: auto-inference **still runs** for all non-shared params, and your explicit edges are added on top. This is the chat-loop/cycle pattern — shared state flows through run state, and you only declare the ordering that name matching can't determine:

```python
from hypergraph import Graph, node, route, END

@node(output_name="messages")
def add_user_message(messages: list, user_input: str) -> list: ...

@node(output_name="response")
def generate(messages: list) -> str: ...

@node(output_name="messages")
def add_response(messages: list, response: str) -> list: ...

@route(targets=["add_user_message", END])
def should_continue(messages: list) -> str: ...

graph = Graph(
    [add_user_message, generate, add_response, should_continue],
    shared=["messages"],
    entrypoint="add_user_message",
    edges=[
        (add_user_message, generate),   # ordering only: "messages" is shared
        (add_response, should_continue),
    ],
)
```

Here `response` is still auto-wired from `generate` to `add_response` by name. The two declared edges only impose ordering: since `messages` is shared, no data flows on them — nodes read the latest `messages` from run state. Edges whose only overlapping values are shared params become ordering-only edges automatically.

See [Shared State](../06-api-reference/graph.md#shared-state) for shared-param rules and [Agentic Loops](../03-patterns/03-agentic-loops.md) for the full pattern.

## When to Prefer Explicit Edges

Automatic wiring is the right default, but reach for explicit `edges=` when:

- **The graph has more than ~5 nodes.** At that size, the topology is no longer obvious from reading node signatures, and an explicit edge list doubles as documentation.
- **The graph is reviewed in PRs.** With explicit edges, topology changes show up in the diff. With auto-inference, renaming an output can silently rewire consumers across the file — in explicit mode a rename can never attach a consumer to a new producer, and pinned 3-tuple values fail loudly at build time.
- **Multiple nodes produce the same output.** Auto-inference rejects this as ambiguous; explicit edges (or `shared=`) make the intent unambiguous.
- **You need ordering without data flow** and `emit`/`wait_for` would be more ceremony than a single ordering-only edge.

Two restrictions to know about:

- `add_nodes()` raises on a graph with explicit edges — create a new `Graph` with the complete node and edge lists instead.
- Explicit and automatic data wiring don't mix without `shared=`. If you want "mostly automatic plus a few extra edges", that's exactly what additive mode is for.

## What's Next?

- [Graph API Reference](../06-api-reference/graph.md#explicit-edges) — full edge format and validation rules
- [Agentic Loops](../03-patterns/03-agentic-loops.md) — cycles, shared outputs, and gates
- [Rename and Adapt](rename-and-adapt.md) — reusing nodes under different names
