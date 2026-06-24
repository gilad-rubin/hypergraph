# How to Build Incremental Tables

Use `HyperTable` to persist graph outputs as table columns and only re-derive what changed.

## The Pattern: Think Singular, Store Persistently

```python
from hypergraph import node, Graph
from hypergraph.materialization import HyperTable
from hypergraph.materialization.stores import LanceDBStore
from hypergraph.runners import SyncRunner

# 1. Write logic for ONE item
@node(output_name="clean_text")
def clean(text: str) -> str:
    return text.strip().lower()

@node(output_name="word_count")
def count_words(clean_text: str) -> int:
    return len(clean_text.split())

# 2. Wrap the graph in a HyperTable
docs = HyperTable(
    [clean, count_words],
    identity="doc_id",
    store=LanceDBStore("./data"),
).with_runner(SyncRunner())

# 3. Insert items — each node output becomes a stored column
docs.insert(doc_id="d1", text="  Hello World  ")
docs.insert(doc_id="d2", text="Testing 123")

# 4. Re-insert unchanged item — skipped, no re-derivation
docs.insert(doc_id="d1", text="  Hello World  ")
```

This is the same "think singular, scale with map" pattern from batch processing, but with persistence. The graph defines stateless compute. HyperTable adds identity, storage, and incrementality: insert an item, store the results, and on the next change only recompute what's affected.

That same shape works for:

- document ingestion pipelines (clean, chunk, embed)
- feature extraction (audio transcription, embedding generation)
- ETL where source data changes over time
- any workflow where re-processing everything on each run is too expensive


## Basic Usage

```python
from hypergraph import node
from hypergraph.materialization import HyperTable
from hypergraph.materialization.stores import LanceDBStore
from hypergraph.runners import SyncRunner

@node(output_name="clean_text")
def clean(text: str) -> str:
    return text.strip().lower()

@node(output_name="word_count")
def count_words(clean_text: str) -> int:
    return len(clean_text.split())

# identity: the stable key for each row
# store: where rows are persisted (LanceDB)
docs = HyperTable(
    [clean, count_words],
    identity="doc_id",
    store=LanceDBStore("./data"),
).with_runner(SyncRunner())

# Insert a row — runs the graph, stores all columns
docs.insert(doc_id="d1", text="  Hello World  ")

# Read it back
row = docs.get("d1")
# {'doc_id': 'd1', 'text': '  Hello World  ', 'clean_text': 'hello world', 'word_count': 2}

# Count rows
docs.count()  # 1
```

The table schema is inferred from the graph:

- **Identity column** — from `identity="doc_id"`
- **Source columns** — from graph inputs (`text`)
- **Derived columns** — from node outputs (`clean_text`, `word_count`)

Extra kwargs at insert time (not matching any graph input) are stored as metadata columns. They don't trigger re-derivation when changed.

```python
# 'station' is metadata — stored, but changing it won't re-run the graph
docs.insert(doc_id="d2", text="Cardiology notes", station="NICU")
```

### Binding Components

Nodes that take injected dependencies (models, clients, config) use `.bind()`, the same way as a regular `Graph`:

```python
@node(output_name="vector")
def embed(clean_text: str, embedder: Embedder) -> list[float]:
    return embedder.embed(clean_text)

docs = HyperTable(
    [clean, embed],
    identity="doc_id",
    store=LanceDBStore("./data"),
).bind(embedder=Embedder()).with_runner(SyncRunner())
```

Bound values are components, not stored columns. They participate in fingerprinting (swapping `Embedder("v1")` for `Embedder("v2")` triggers re-derivation) but don't appear as row data.


## How Incrementality Works

HyperTable tracks a **row fingerprint** for each row: a hash of the source column values, node definition hashes, and component config hashes. On insert, it compares the new fingerprint to the stored one. If they match, the row is skipped.

Three things can trigger re-derivation:

### 1. Source data changed

```python
docs.insert(doc_id="d1", text="  Hello World  ")   # first insert — runs graph
docs.insert(doc_id="d1", text="  Hello World  ")   # same text — skipped
docs.insert(doc_id="d1", text="  Changed Text  ")  # different text — re-derives
```

### 2. Node logic changed

If you edit a node function's body, the definition hash changes. On the next insert or sync, rows whose fingerprint was computed with the old hash will mismatch and re-derive.

### 3. Component config changed

```python
# Original
docs = HyperTable([clean, embed], identity="doc_id", store=store) \
    .bind(embedder=Embedder(model="v1")).with_runner(SyncRunner())
docs.insert(doc_id="d1", text="hello")

# Swap embedder — fingerprint changes, rows re-derive on next insert/sync
docs_v2 = HyperTable([clean, embed], identity="doc_id", store=store) \
    .bind(embedder=Embedder(model="v2")).with_runner(SyncRunner())
docs_v2.insert(doc_id="d1", text="hello")  # re-derives with new embedder
```

For the component config to participate in fingerprinting, the component must expose its config via a `_config()` method or a `__component_config__` attribute.


## Child Tables (map_over)

When each row contains a collection (a document has pages, a video has utterances), use `map_over` to create a child table. This is the same `map_over` from batch processing, but with persistent storage at each grain.

```python
from typing import TypedDict
from hypergraph import Graph, node
from hypergraph.materialization import HyperTable
from hypergraph.materialization.stores import LanceDBStore
from hypergraph.runners import SyncRunner

# --- Parent graph nodes ---

@node(output_name="audio_path")
def extract_audio(path: str) -> str:
    return transcribe_file(path)

@node(output_name="transcript")
def transcribe(audio_path: str) -> str:
    return whisper_transcribe(audio_path)

# The split function returns a list of child items
class Utterance(TypedDict):
    utterance_id: str
    text: str
    speaker: str

@node(output_name="utterances")
def split_utterances(transcript: str) -> list[Utterance]:
    # Parse transcript into utterances
    return [
        {"utterance_id": "u0", "text": "hello there", "speaker": "Alice"},
        {"utterance_id": "u1", "text": "how are you", "speaker": "Bob"},
    ]

# --- Child graph: processes ONE utterance ---

@node(output_name="clean_text")
def clean(text: str) -> str:
    return text.strip().lower()

@node(output_name="vector")
def embed_text(clean_text: str, embedder: Embedder) -> list[float]:
    return embedder.embed(clean_text)

process_utterance = Graph([clean, embed_text], name="process_utterance")

# --- Assemble the table ---

videos = HyperTable(
    [
        extract_audio,
        transcribe,
        split_utterances,
        process_utterance.as_node().map_over("utterances", identity="utterance_id"),
    ],
    identity="video_id",
    store=LanceDBStore("./data"),
).bind(embedder=Embedder()).with_runner(SyncRunner())

# Insert a video — parent row + child rows created
videos.insert(video_id="v1", path="/data/meeting.mp4")

# Read parent
videos.get("v1")
# {'video_id': 'v1', 'path': '/data/meeting.mp4', 'audio_path': '...', 'transcript': '...'}

# Read children
videos.children("v1")
# [
#   {'utterance_id': 'u0', '_parent_id': 'v1', 'text': 'hello there', 'speaker': 'Alice',
#    'clean_text': 'hello there', 'vector': [0.1, ...]},
#   {'utterance_id': 'u1', '_parent_id': 'v1', 'text': 'how are you', 'speaker': 'Bob',
#    'clean_text': 'how are you', 'vector': [0.2, ...]},
# ]
```

The key elements:

- **`split_utterances`** returns a `list[Utterance]` — this is the 1:N expansion
- **`process_utterance`** is a regular `Graph` that processes ONE utterance
- **`.map_over("utterances", identity="utterance_id")`** declares the grain boundary
- **`_parent_id`** is auto-stamped on every child row, linking back to the parent

Child rows have their own fingerprints, scoped to the child graph. Re-inserting the same parent with the same children skips both parent and child derivation.

### Counting child rows

```python
videos.count()                          # parent row count
videos.count(child_table="utterance")   # child row count
```


## Handling Failures with on_error

By default, a failed derivation raises an exception. For long-running ingestion, you often want partial success — process what you can, record failures, fix and retry later.

```python
videos = HyperTable(
    [extract_audio, transcribe, split_utterances,
     process_utterance.as_node().map_over("utterances", identity="utterance_id")],
    identity="video_id",
    store=LanceDBStore("./data"),
    on_error="store",       # write error rows instead of raising
).bind(embedder=Embedder()).with_runner(SyncRunner())
```

When `on_error="store"` and a child fails:

- The successful siblings are stored normally
- The failed child gets an **error row**: source columns preserved, derived columns `None`, `_status="error"`, and the exception recorded in `_error`
- The parent row is still written (it succeeded)

```python
# Insert a video — suppose utterance "u1" fails during embedding
videos.insert(video_id="v1", path="/data/meeting.mp4")

# Check children with status info
children = videos.children("v1", include_status=True)
for c in children:
    print(c["utterance_id"], c["_status"], c.get("_error"))
# u0  complete  None
# u1  error     ValueError: embedding failed for empty text
```

### Retrying errors

Error rows are automatically retried on the next insert. The fingerprint matches (same inputs), but `_status="error"` prevents skipping:

```python
# Fix the bug in embed_text, then re-insert the same parent
videos.insert(video_id="v1", path="/data/meeting.mp4")
# u0 → skipped (already complete)
# u1 → retried (was error, now succeeds)
```


## Syncing with a Data Source

`sync()` reconciles the table against a complete list of items: inserts new rows, re-derives changed rows, deletes rows no longer in the source, and skips unchanged rows.

```python
from hypergraph.materialization import SyncResult

# Your data source
documents = [
    {"doc_id": "d1", "text": "hello world"},
    {"doc_id": "d2", "text": "new document"},       # new
    {"doc_id": "d3", "text": "updated content"},     # changed
    # d4 was in the table but is missing here → deleted
]

result: SyncResult = docs.sync(documents)

print(result)
# SyncResult(inserted=1, updated=1, deleted=1, skipped=1, errored=0, errors=())
```

`SyncResult` fields:

- `inserted` — new rows added
- `updated` — existing rows whose source data changed (re-derived)
- `deleted` — rows in the table but not in the input (removed, including children)
- `skipped` — rows with matching fingerprints (no work needed)
- `errored` — rows that failed derivation (only with `on_error="store"`)
- `errors` — tuple of `ErrorRow` for programmatic inspection

### Sync with error handling

```python
docs = HyperTable(
    [clean, count_words],
    identity="doc_id",
    store=LanceDBStore("./data"),
    on_error="store",
).with_runner(SyncRunner())

result = docs.sync(documents)

if result.errored:
    for err in result.errors:
        print(f"{err.identity}: {err.error_type} — {err.error_msg}")
    # {'doc_id': 'd2'}: ValueError — could not process empty text
```


## Updating and Deleting

### Update a row

`update()` changes source columns and re-derives downstream if the source feeds a node. Changes to metadata columns are stored directly without re-derivation.

```python
# Change source data — triggers re-derivation
docs.update("d1", text="completely new content")

row = docs.get("d1")
# clean_text and word_count are recomputed from the new text

# Change metadata only — no re-derivation
docs.update("d1", station="PICU")
```

### Bulk metadata updates with set()

For metadata-only changes across many rows, `set()` is faster than looping `update()`. It rejects content-key fields to prevent accidental skips of re-derivation:

```python
# Update metadata on all matching rows
count = docs.set([("station", "eq", "NICU")], active=True, reviewed=True)
print(f"Updated {count} rows")

# This raises ValueError — text is a content key
docs.set([("station", "eq", "NICU")], text="new text")
# ValueError: set() cannot update content-key fields: text
```

### Delete a row

`delete()` removes the row and cascade-deletes all children:

```python
docs.delete("d1")

# For parent-child tables, children are removed too
videos.delete("v1")
# Removes video v1 and all its utterance rows
```


## Inspecting Errors

When using `on_error="store"`, you can find and inspect failed rows.

### Filter child errors

```python
# Find all failed children across all parents
errors = videos.filter_children(
    where=[("_status", "eq", "error")],
    include_status=True,
)

for row in errors:
    print(f"Parent: {row['_parent_id']}, "
          f"Child: {row['utterance_id']}, "
          f"Error: {row['_error']}")
```

### Filter parent errors

```python
failed_docs = docs.filter(
    where=[("_status", "eq", "error")],
    include_status=True,
)

for row in failed_docs:
    print(f"{row['doc_id']}: {row['_error']}")
```

### SyncResult errors

After `sync()`, errors are available programmatically:

```python
result = docs.sync(documents)

for err in result.errors:
    print(f"Identity: {err.identity}")    # {'doc_id': 'd3'}
    print(f"Type: {err.error_type}")      # 'ValueError'
    print(f"Message: {err.error_msg}")    # 'ValueError: invalid input'
```

### include_status on read operations

By default, `_status` and `_error` are hidden from read results. Pass `include_status=True` to see them:

```python
# Without include_status — status fields hidden
docs.get("d1")
# {'doc_id': 'd1', 'text': '...', 'clean_text': '...', 'word_count': 2}

# With include_status — status fields visible
docs.get("d1", include_status=True)
# {'doc_id': 'd1', 'text': '...', 'clean_text': '...', 'word_count': 2,
#  '_status': 'complete', '_error': None}
```

The same parameter works on `children()`, `filter()`, and `filter_children()`.


## Async Support

The same API works with `AsyncRunner`. Write operations (`insert`, `update`, `delete`, `sync`) become coroutines. Read operations (`get`, `count`, `children`, `filter`) stay synchronous.

```python
from hypergraph.runners import AsyncRunner

docs = HyperTable(
    [clean, count_words],
    identity="doc_id",
    store=LanceDBStore("./data"),
).with_runner(AsyncRunner())

# Write operations are awaited
await docs.insert(doc_id="d1", text="hello world")
await docs.update("d1", text="new content")
await docs.delete("d1")

result = await docs.sync(documents)

# Read operations are synchronous — no await needed
row = docs.get("d1")
rows = docs.filter(where=[("station", "eq", "NICU")])
children = docs.children("v1")
```


## What's Next

- [Batch Processing](batch-processing.md) -- process many items through a graph with `runner.map()`
- [Visualize Graphs](visualize-graphs.md) -- render your table's graph structure
- [Testing Without Framework](test-without-framework.md) -- test node functions directly
