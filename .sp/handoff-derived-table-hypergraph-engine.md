# Handoff: Wire Hypergraph as DerivedTable's internal engine

## What's built and working

DerivedTable is a declarative incremental materialization system. Fully tested (62 tests, 3-round adversarial review CLEAN). A real end-to-end demo runs ElevenLabs STT + OpenAI embeddings on Subtext Hebrew broadcast audio.

### Files (all in `/Users/giladrubin/python_workspace/hypergraph/`)

```
src/hypergraph/materialization/
  __init__.py           20 lines   package exports
  _markers.py            9 lines   Identity, ContentKey sentinel objects
  _keys.py             114 lines   content key computation, marker extraction
  _store.py            158 lines   LanceDB store backend, dependent registration
  _table.py            709 lines   DerivedTable — the main implementation
  _types.py             44 lines   ErrorRow, SyncResult, DerivationError, ChainedTableError

tests/
  test_derived_table.py        796 lines   41 tests
  test_materialization_types.py  240 lines   21 tests

examples/
  subtext_materialization.py   329 lines   working e2e demo (real API calls)
```

### Working demo

```bash
cd /Users/giladrubin/python_workspace/hypergraph
uv run python examples/subtext_materialization.py
```

Trims 90s of a Channel 14 broadcast → ElevenLabs STT (3 diarized Hebrew turns) → OpenAI embeddings (1536-dim, cascaded automatically) → content-key skip on re-insert → cosine similarity search → versioning snapshot. All local LanceDB.

### What works in the current implementation

- Frozen dataclass types with `Annotated[str, Identity]` / `Annotated[str, ContentKey]`
- `DerivedTable(source, output, derive, components, store)` with `_config()` validation
- insert / update / delete / sync / recompute — full mutation API
- Content-key skip (hash of content fields + component configs + definition hash + schema fingerprint)
- One-to-many explosion (derive returns `list[OutputType]`)
- Push cascading with stored registration (always on, survives process restarts)
- Chained tables (source is another DerivedTable, populated only via cascade)
- Write-new-then-delete-old crash safety
- Per-row error handling (`on_error="raise"` / `"ignore"`)
- Versioning with `at(version)` snapshots and `revert()`
- Thread-safe write exclusion via `threading.RLock`
- `get()`, `filter()`, `count()`, `errors()` query API

## The gap: no Hypergraph involvement

The design spec (`/Users/giladrubin/python_workspace/superposition/.sp/design/derived-table-target-tests.py`) says on line 15:

> Hypergraph AsyncRunner handles concurrency and progress under the hood.

**This is not implemented.** `_derive_and_store` (line 331-390 of `_table.py`) is a plain sequential `for` loop:

```python
# CURRENT — plain for loop, no runner
for item in source_items:
    ...
    result = self._derive_item(item)
    ...
```

## What we tried and why it was wrong

### Attempt 1: Graph wrapper on top of DerivedTable

Created a Hypergraph `Graph` with nodes that called `turns_table.insert()` internally. This was fundamentally wrong — two engines (graph + DerivedTable) both claiming to orchestrate the same work. The graph nodes were thin wrappers that poked DerivedTable to do the real work. Deleted.

### Attempt 2: Codex suggestion — `asyncio.run(AsyncRunner().map(...))` bridge

Codex proposed keeping DerivedTable sync and using `asyncio.run()` internally to bridge to AsyncRunner. This is wrong:
1. **SyncRunner.map already exists** — same API, no async bridge needed
2. **DerivedTable doesn't have to be sync** — the design spec says AsyncRunner, so making the API async is the clean path

## The correct direction

DerivedTable should use Hypergraph's runner internally. Two runner options, not mutually exclusive:

### Option 1: SyncRunner.map (works today, sequential)

- Drop-in replacement for the for loop
- No async boundary issues
- But: sequential — 100 items processed one at a time
- Good for: getting the Hypergraph integration working and tested first

### Option 2: AsyncRunner.map (the design spec target)

- Concurrent processing with `max_concurrency` control
- Progress events via `event_processors`
- Requires the DerivedTable public API to be async (`async def insert(...)`)
- Sync derive functions need `asyncio.to_thread` wrapping for real concurrency
- The Subtext ElevenLabs component already has `async def transcribe(...)` — this is the natural shape

### The design spec supports both plain functions and Graphs as derive

**Plain function derive** (current common case):
- Wrap in `FunctionNode(derive, name=..., output_name=...)` → `Graph([node]).bind(**components)`
- Runner processes items via `map_over`

**Graph derive** (design spec Part 3, lines 906-957):
```python
embed_graph = Graph([clean_text, compute_embedding, build_result], name="embed_pipeline")

embeddings = DerivedTable(
    source=utterances,
    output=UtteranceEmbedding,
    derive=embed_graph,          # Graph, not a function
    components={"embedder": embedder},
    store=store,
)
```

Behind the scenes, DerivedTable calls:
```python
result = await AsyncRunner().map(
    embed_graph.bind(embedder=embedder),
    {"utt": items_to_process},
    map_over="utt",
    error_handling="continue",
)
```

## Implementation details from Codex (verified correct)

### on_error mapping

**Both** DerivedTable modes should use `error_handling="continue"` for the runner. The distinction is post-run:
- `on_error="raise"`: collect failed identities, write NO error rows, raise `DerivationError`
- `on_error="ignore"`: write error rows, return without raising

### Content-key pre-filter

Check all items BEFORE the runner call. Only send pending items (content changed or errored) to the runner:

```python
pending = []
for item in source_items:
    identity = self._get_identity_values(item)
    content_key = self._compute_key(item)
    existing = self._find_existing_row(identity)
    if existing and existing.get("_content_key") == content_key and not existing.get("_error"):
        continue
    pending.append((item, identity, content_key, source_id, existing))

# Only pending items go to runner.map
```

### Write from MapResult

`MapResult.results` is `tuple[RunResult, ...]` — aligned 1:1 with input items. Check `run_result.status == RunStatus.FAILED` for errors. Apply write-new-then-delete-old per item from the results.

### Graph detection

```python
from hypergraph.graph import Graph
isinstance(derive, Graph)  # True for graph derive
```

Graph class is at `src/hypergraph/graph/core.py`, exported via `graph/__init__.py`.

### definition_hash for graphs

When derive is a Graph, use the graph's own definition hash (captures node function hashes + topology), not `inspect.getsource(graph)`.

## Key Hypergraph API surfaces

```python
from hypergraph import Graph, node, FunctionNode
from hypergraph.runners import SyncRunner, AsyncRunner

# SyncRunner.map (template_sync.py:540):
def map(graph, values, *, map_over, error_handling="raise", ...) -> MapResult

# AsyncRunner.map (template_async.py:566):
async def map(graph, values, *, map_over, max_concurrency=None, error_handling="raise", ...) -> MapResult

# MapResult (types.py:312):
# - .results: tuple[RunResult, ...]
# - ["output_name"] → [value, value, None, ...]
# - len(), iter(), indexing

# graph.bind(**kwargs) → immutable copy with pre-filled inputs
# FunctionNode(func, name=..., output_name=...) → wraps plain function as a node
```

## Existing tests

All 62 tests must stay green. They test behavior (insert, skip, cascade, errors, versioning) not implementation — swapping the for loop for runner.map should not break them as long as the semantics are preserved.

## Design spec location

`/Users/giladrubin/python_workspace/superposition/.sp/design/derived-table-target-tests.py`

This is THE authoritative design artifact. Read it fully before implementing — especially:
- Lines 15-16: AsyncRunner under the hood
- Lines 50-56: both on_error modes use concurrent processing
- Lines 906-957: Graph as derive (Part 3)
- Lines 650-665: explosion chain with concurrent processing at each level

## Suggested implementation order

1. Start with SyncRunner.map replacing the for loop — get it working, tests green
2. Add Graph detection (`isinstance(derive, Graph)`) and the `graph.bind(**components)` path
3. Make the public API async, switch to AsyncRunner.map
4. Add `event_processors` passthrough for progress display
5. Update the Subtext demo to use an async derive with a Graph
