# Manage Stateful Resources

Use `@stateful` and `graph.resources()` when a graph depends on objects that
should be constructed lazily and closed deterministically.

Good examples are LLM clients, embedders, vector stores, HTTP clients, database
pools, and SDK objects that open sockets or hold locks.

## One Graph

```python
from hypergraph import AsyncRunner, Graph, node, stateful


@stateful(resource=True)
class LLM:
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self.client = open_client(api_key)

    async def generate(self, prompt: str) -> str:
        return await self.client.generate(model=self.model, prompt=prompt)

    async def aclose(self) -> None:
        await self.client.close()


@node(output_name="answer")
async def answer_question(question: str, llm: LLM) -> str:
    return await llm.generate(question)


graph = Graph([answer_question]).bind(
    llm=LLM(api_key="...", model="gpt-5"),
)

async with graph.resources() as ready_graph:
    result = await AsyncRunner().run(
        ready_graph,
        {"question": "What changed?"},
    )
```

`LLM(...)` creates a lazy handle. The real `LLM` object is constructed when the
resource scope opens, not when the graph is defined.

## Cleanup Rules

`graph.resources()` follows the cleanup method that matches the scope:

| Resource class | `with graph.resources()` | `async with graph.resources()` |
| --- | --- | --- |
| `close()` only | calls `close()` | calls `close()` |
| `aclose()` only | raises before opening | awaits `aclose()` |
| both | calls `close()` | awaits `aclose()` |
| neither | invalid with `resource=True` | invalid with `resource=True` |

When a class uses a different method name, declare it:

```python
@stateful(resource=True, close="shutdown")
class SearchClient:
    def shutdown(self) -> None:
        ...
```

Use `resource=False` for lazy state that does not need cleanup:

```python
@stateful(resource=False)
class PromptFormatter:
    ...
```

## Sharing

Sharing is explicit.

```python
shared_llm = LLM(api_key="...", model="gpt-5")

graph = Graph([draft, revise]).bind(
    draft_llm=shared_llm,
    revise_llm=shared_llm,
)
```

The same lazy handle materializes once and closes once.

Different handles stay different resources, even when their constructor
arguments are equal:

```python
graph = Graph([draft, revise]).bind(
    draft_llm=LLM(api_key="...", model="gpt-5"),
    revise_llm=LLM(api_key="...", model="gpt-5"),
)
```

Hypergraph does not deduplicate resources by constructor arguments. If a class
can safely pool lower-level transports, implement that inside the class.

## Parent Graph With Subgraphs

Subgraphs can bind their own resources and remain runnable on their own:

```python
@node(output_name="validation")
async def validate(query: str, validation_llm: LLM) -> bool:
    ...


def validation_graph() -> Graph:
    return Graph([validate], name="validation").bind(
        validation_llm=LLM(api_key="...", model="gpt-5-mini"),
    )


@node(output_name="answer")
async def generate(query: str, generation_llm: LLM) -> str:
    ...


def generation_graph() -> Graph:
    return Graph([generate], name="generation").bind(
        generation_llm=LLM(api_key="...", model="gpt-5"),
    )
```

The parent opens one scope over the whole graph tree:

```python
parent = Graph(
    [
        validation_graph().as_node(name="validation", namespaced=True),
        generation_graph().as_node(name="generation", namespaced=True),
    ],
    name="rag",
)

async with parent.resources() as ready_parent:
    result = await AsyncRunner().run(
        ready_parent,
        {"validation.query": query, "generation.query": query},
    )
```

Those two LLM handles are different resources. That is the safe default.

If two subgraphs should truly share the same LLM configuration, pass the same
handle into both graphs before composing:

```python
shared = LLM(api_key="...", model="gpt-5")

validation = Graph([validate], name="validation").bind(validation_llm=shared)
generation = Graph([generate], name="generation").bind(generation_llm=shared)
```

## FastAPI Lifespan

For a web app, open resources once during startup and close them at shutdown:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request

from hypergraph import AsyncRunner


graph = Graph([answer_question]).bind(
    llm=LLM(api_key="...", model="gpt-5"),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with graph.resources() as ready_graph:
        app.state.graph = ready_graph
        app.state.runner = AsyncRunner()
        yield


app = FastAPI(lifespan=lifespan)


@app.post("/answer")
async def answer(request: Request, body: dict):
    result = await request.app.state.runner.run(
        request.app.state.graph,
        {"question": body["question"]},
    )
    return {"answer": result.values["answer"]}
```

## Optional Hypster Pattern

Hypergraph does not depend on Hypster. Advanced users can still create lazy
handles from Hypster configs because they are ordinary Python values:

```python
def llm_config(hp):
    model = hp.text("gpt-5", name="model")
    effort = hp.select(["low", "medium", "high"], name="reasoning_effort")
    return LLM(api_key="...", model=model, reasoning_effort=effort)


def generation_graph_config(hp):
    llm = hp.nest(
        llm_config,
        name="llm",
        values={"reasoning_effort": "high"},
    )
    return Graph([generate], name="generation").bind(llm=llm)
```

This keeps configuration hierarchical without making Hypergraph import or know
about Hypster.
