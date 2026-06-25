# Sync and async DerivedTable are separate classes, not one class parameterized by the runner

**Status:** Superseded — the `DerivedTable` / `AsyncDerivedTable` design was abandoned in favor of `HyperTable` (graph-native, `TableStore` protocol), and the `DerivedTable` stack was removed before merge. Retained for the sync/async-by-color reasoning, which still bears on how `HyperTable` should structure its runner handling (see PRD 0004).

`DerivedTable` (sync) and `AsyncDerivedTable` (async) are two classes split by execution *color*. The Hypergraph runner is injected to pick the engine within that color — `SyncRunner` (or `DaftRunner`) for the sync class, `AsyncRunner` for the async class.

We chose two classes over a single class with twin `insert`/`ainsert` methods because Python methods are color-typed. With the runner fixed at construction, half of a single class's twin methods would be dead on any instance, and the reconcile method `sync()` has no clean async twin — `async def sync` collides with the word "synchronous." Two classes keep method names identical and every method valid, share one sync storage/content-key/cascade core (LanceDB writes are synchronous either way), and mirror Hypergraph's own "pick your runner" model.

## Considered options

- **One class, twin methods (`insert`/`ainsert`)** — rejected: half the methods are dead on any instance, and `sync()` has no clean async name.
- **One class, sometimes-awaitable return** (`insert()` returns a coroutine under an async runner) — rejected: a return type that depends on construction is a footgun.
- **Two classes (chosen).**

## Consequences

- A materialization chain is uniformly sync or async. The runner is set on the **root** table only and inherited by chained tables, so the uniform-color invariant is *structural*, not validated after the fact — and it matches the root-scoped write lock (the operation is root-scoped, so the engine is too).
- The existing sync test suite stays on `DerivedTable` unchanged; concurrency arrives via `AsyncDerivedTable`.
