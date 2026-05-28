# Stateful Resource Lifecycle

## Problem Statement

Hypergraph users often bind expensive, stateful objects such as LLM clients, vector stores, embedders, database pools, and HTTP clients into graphs. Today `.bind()` can inject those objects, but it does not provide a first-class lifecycle: users must decide where construction happens, remember to close resources, and avoid accidentally opening production connections while inspecting or visualizing a graph locally.

This becomes more visible with nested graphs. A subgraph should be able to declare the resources it needs in place, stay runnable on its own, and still compose cleanly into a parent graph. The parent graph should be able to open and close the resources used by the whole nested graph tree without requiring Hypergraph to know about Hypster or any other configuration framework.

The current Daft `@stateful` support also points in this direction. Daft already has a lazy class model where initialization happens on workers. Hypergraph should align with that concept while keeping the core graph API decoupled from Daft and Hypster.

## Solution

Introduce lazy `@stateful` handles and a graph resource scope.

Users will mark classes that should be constructed lazily:

```python
@stateful(resource=True)
class LLM:
    async def aclose(self) -> None:
        ...
```

Calling a stateful class will return a lazy handle instead of opening the underlying resource immediately. Binding that handle to a graph will make the graph input optional, just like any existing `.bind()` value, but the resource will not be constructed until a resource scope opens:

```python
graph = Graph([generate]).bind(llm=LLM(model="gpt-5"))

async with graph.resources() as ready_graph:
    result = await AsyncRunner().run(ready_graph, {"query": "hello"})
```

The resource scope will materialize each distinct handle once, replace the bound handles with live instances for the duration of the scope, and close owned resources when the scope exits. The same handle bound in multiple places will be shared and closed once. Different handles, even if constructed with the same arguments, will remain different resources.

Cleanup semantics will be deterministic:

| Resource shape | Sync scope | Async scope |
| --- | --- | --- |
| `close()` only | construct and call `close()` on exit | construct and call `close()` on exit |
| `aclose()` only | error before opening | construct and await `aclose()` on exit |
| both `close()` and `aclose()` | construct and call `close()` | construct and await `aclose()` |
| neither | invalid for `resource=True` | invalid for `resource=True` |

`resource=False` remains lazy state without lifecycle ownership.

## User Stories

1. As a graph author, I want to bind a stateful LLM lazily, so that graph construction does not open network connections.
2. As a graph author, I want to visualize or inspect a graph with stateful resources, so that local development does not require production credentials or services.
3. As an async application developer, I want to use `async with graph.resources()`, so that async resources are opened and closed deterministically.
4. As a sync application developer, I want to use `with graph.resources()`, so that sync resources are opened and closed deterministically.
5. As a sync application developer, I want a clear error when a graph contains an async-only resource, so that I do not accidentally leak a resource that requires `await`.
6. As a resource class author, I want `resource=True` to validate that cleanup exists, so that lifecycle bugs are caught early.
7. As a resource class author, I want to support classes with `close()`, so that common sync clients work naturally.
8. As a resource class author, I want to support classes with `aclose()`, so that async clients work naturally.
9. As a resource class author, I want classes with both `close()` and `aclose()` to use the cleanup method that matches the scope, so that double-close bugs are avoided.
10. As a graph author, I want resources with no cleanup to be invalid when `resource=True`, so that the lifecycle contract remains meaningful.
11. As a graph author, I want `resource=False` to allow lazy state with no cleanup, so that stateful non-resource objects are still supported.
12. As a nested graph author, I want subgraphs to declare their own resources, so that each subgraph remains standalone.
13. As a parent graph author, I want a parent resource scope to open resources across nested subgraphs, so that the whole composed workflow has one lifecycle boundary.
14. As a parent graph author, I want the same lazy handle used in multiple bindings to become one live instance, so that explicit sharing is possible.
15. As a parent graph author, I want different lazy handles to remain different resources, so that Hypergraph does not infer unsafe sharing from constructor arguments.
16. As an advanced user, I want to implement pooling inside my own resource class if needed, so that Hypergraph does not need provider-specific singleton logic.
17. As a FastAPI user, I want to open graph resources in the app lifespan, so that long-lived clients are created once and closed at shutdown.
18. As a FastAPI user, I want request handlers to receive a ready graph, so that request execution does not repeatedly open expensive resources.
19. As a Daft user, I want `@stateful` constructor arguments to be captured lazily, so that Daft workers construct resources where execution happens.
20. As a Daft user, I want existing class-level Daft options to continue working, so that `max_concurrency`, CPU/GPU, and retry controls remain available.
21. As a maintainer, I want this feature to live in Hypergraph core, so that Hypster remains optional and decoupled.
22. As a Hypster user, I want an advanced composition pattern using `hp.nest(...)`, so that lazy handles can be produced by configs without adding Hypergraph-Hypster coupling.
23. As a test author, I want behavior tests through public graph and runner APIs, so that the implementation can be refactored without brittle tests.

## Implementation Decisions

- `@stateful` becomes the public lazy-constructor decorator for classes.
- `@stateful(resource=True)` marks the lazy handle as an owned graph resource that must support deterministic cleanup.
- `@stateful(resource=False)` marks lazy state without lifecycle ownership.
- Calling a decorated class returns a lazy handle that captures the original class, positional arguments, keyword arguments, Daft options, and resource metadata.
- The original class is not instantiated when the lazy handle is created.
- A graph resource scope materializes handles and returns a graph with live instances bound in place of the handles.
- The same lazy handle identity materializes once per scope and is reused anywhere that handle appears.
- Different lazy handle identities materialize as distinct live resources, even if their constructor arguments are equal.
- Hypergraph will not provide constructor-field deduping such as `share_by=("endpoint", "api_key")`.
- Hypergraph will not provide a graph-level singleton/multiton pool for different handles in this implementation.
- Sync scopes reject async-only resources before opening anything.
- Async scopes can manage both sync and async cleanup.
- When a resource has both `close()` and `aclose()`, sync scope uses `close()` and async scope uses `aclose()`.
- Cleanup runs once per materialized handle in reverse materialization order.
- Cleanup errors should propagate from scope exit using Python context manager semantics.
- `Graph.resources()` is the intended lifecycle boundary for sync and async runners.
- `Graph.bind(...)` remains the injection mechanism; no separate `bind_resource(...)` API is introduced.
- Runtime inputs should not be used as a resource ownership mechanism. Resources should be bound before opening a resource scope.
- Daft lowering should use the lazy handle metadata to construct worker-side resources with captured constructor arguments.
- Hypster examples are documentation-only. Hypergraph will not import or depend on Hypster.

## Testing Decisions

- Tests should exercise behavior through public interfaces: `@stateful`, `Graph.bind(...)`, `Graph.resources()`, `SyncRunner`, and `AsyncRunner`.
- Tests should avoid checking private registries or implementation-specific helper structures.
- The tracer-bullet test will prove that a bound stateful resource is not constructed at bind time, is constructed inside the resource scope, is usable by a runner, and is closed on scope exit.
- Additional tests will cover sync vs async cleanup behavior, invalid resource declarations, same-handle sharing, separate-handle separation, and nested graph resource scope behavior.
- Daft tests should cover that lazy handles lower to Daft stateful operations without requiring zero-argument constructors.
- Existing bind and nested graph tests provide prior art for public Graph behavior.
- Existing Daft stateful tests provide prior art for stateful lowering behavior, but should be updated away from eager instance construction.

## Out of Scope

- Automatic resource deduplication based on constructor arguments.
- `share_by=...` or other constructor-field identity APIs.
- Built-in singleton/multiton transport pooling for LLM clients.
- Hypster API changes.
- FastAPI integration package code.
- Automatic per-run resource scope opening in runners.
- Lifecycle management for values provided directly at `runner.run(...)` time.
- Support for async constructors.
- Public resource override APIs beyond ordinary graph composition and binding.

## Further Notes

The design intentionally follows the spirit of FastAPI lifespan/dependency cleanup, Dishka scopes, Dependency Injector resource providers, and Python `ExitStack`/`AsyncExitStack`: lifecycle belongs to an explicit owner/scope, not to arbitrary object equality.

This keeps the Hypergraph API small:

```python
graph = Graph([generate]).bind(llm=LLM(model="gpt-5"))

async with graph.resources() as ready_graph:
    await AsyncRunner().run(ready_graph, {"query": "hello"})
```

For nested graphs, subgraphs can bind their own handles and parent graphs can open one resource scope over the composed graph. Advanced Hypster users can produce those handles with `hp.nest(...)`, but Hypergraph only sees ordinary Python objects and graph bindings.
