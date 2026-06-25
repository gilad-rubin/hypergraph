# HyperTable API Reference

A **HyperTable** turns a Hypergraph graph into an incremental, persisted table. Each graph input becomes a source column, each node output becomes a derived column, and a content-addressed fingerprint decides whether a row needs re-derivation on the next insert.

- **Incremental by default** -- re-inserting a row with unchanged source columns and unchanged node code is a no-op
- **Grain boundaries** -- a `map_over` node fans one parent row into many child rows, each derived by a nested graph
- **Error isolation** -- `on_error="store"` writes error rows instead of crashing, so one bad item does not block the rest
- **Component injection** -- `bind()` injects shared resources (embedders, LLM clients) without polluting node signatures

```python
from hypergraph import node, Graph
from hypergraph.materialization import HyperTable
from hypergraph.materialization.stores import LanceDBStore
from hypergraph.runners import SyncRunner

@node(output_name="clean_text")
def clean(text: str) -> str:
    return text.strip().lower()

@node(output_name="word_count")
def count_words(clean_text: str) -> int:
    return len(clean_text.split())

store = LanceDBStore("/tmp/docs_table")
table = HyperTable(
    [clean, count_words],
    identity="doc_id",
    store=store,
).with_runner(SyncRunner())

table.insert(doc_id="d1", text="Hello World")
print(table.get("d1"))
# {'doc_id': 'd1', 'text': 'hello world', 'clean_text': 'hello world', 'word_count': 2}
```

The graph's required inputs (`text`) become source columns. The node outputs (`clean_text`, `word_count`) become derived columns. The identity column (`doc_id`) is the primary key.

## Constructor

### `HyperTable(nodes, *, identity, store, on_error="raise")`

Create a table backed by a graph pipeline and a persistent store.

```python
from hypergraph.materialization import HyperTable
from hypergraph.materialization.stores import LanceDBStore

table = HyperTable(
    [clean, count_words],
    identity="doc_id",
    store=LanceDBStore("/tmp/my_store"),
    on_error="store",
)
```

**Args:**
- `nodes` (list): Nodes and map-over nodes that define the table's derivation pipeline. Plain nodes form the root graph; nodes created via `.map_over()` define child tables.
- `identity` (str): The primary key column name. Must be provided explicitly in every `insert()` call. Cannot be a [reserved name](#reserved-column-names).
- `store` (TableStore): Storage backend. Must be a `TableStore` subclass (e.g., `LanceDBStore`).
- `on_error` (str): Error handling policy. `"raise"` (default) propagates exceptions immediately. `"store"` writes an error row and continues processing remaining items.

**Raises:**
- `ValueError` -- If `on_error` is not `"raise"` or `"store"`
- `ValueError` -- If `identity` is a reserved column name
- `TypeError` -- If `store` is not a `TableStore` subclass

## Configuration Methods

Configuration methods return new `HyperTable` instances. The original is unchanged.

### `bind(**components) -> HyperTable`

Inject shared components (embedders, LLM clients, database connections) into the graph pipeline. Bound components are passed to any node that declares a matching parameter name.

```python
class Embedder:
    def __init__(self, model: str = "text-embedding-3-small", dim: int = 256):
        self.model = model
        self.dim = dim

    def _config(self):
        return {"model": self.model, "dim": self.dim}

    def embed(self, text: str) -> list[float]:
        return call_embedding_api(text, model=self.model, dim=self.dim)

@node(output_name="vector")
def embed_text(clean_text: str, embedder: Embedder) -> list[float]:
    return embedder.embed(clean_text)

embedder = Embedder(model="text-embedding-3-large", dim=1024)
table = HyperTable(
    [clean, embed_text],
    identity="doc_id",
    store=store,
).bind(embedder=embedder)
```

Components with a `_config()` method contribute to the row fingerprint. When you swap in a different embedder config and call `sync()`, rows are re-derived because the fingerprint changes.

```python
# Swap embedder -- sync detects the config change and re-derives
table_v2 = table.bind(embedder=Embedder(model="text-embedding-3-small", dim=256))
result = table_v2.sync(items)
print(result.updated)  # all rows re-derived
```

**Args:**
- `**components`: Keyword arguments mapping parameter names to component instances.

**Returns:** New `HyperTable` with merged component bindings.

### `with_runner(runner) -> HyperTable`

Set the execution runner for write operations. Required before calling `insert()`, `update()`, `delete()`, `sync()`, `recompute()`, or `backfill()`.

```python
from hypergraph.runners import SyncRunner, AsyncRunner

# Sync usage
table = HyperTable([clean, count_words], identity="doc_id", store=store)
table = table.with_runner(SyncRunner())
table.insert(doc_id="d1", text="hello")

# Async usage
table = table.with_runner(AsyncRunner())
await table.insert(doc_id="d1", text="hello")
```

When an `AsyncRunner` is set, write methods return coroutines. Read methods (`get`, `filter`, `children`, `count`) never need a runner.

**Args:**
- `runner`: A `SyncRunner` or `AsyncRunner` instance.

**Returns:** New `HyperTable` with the runner set.

## Write Operations

Write operations require a runner. Call `.with_runner()` first.

### `insert(**kwargs) -> None`

Insert a single row or upsert if the identity already exists. Runs the graph pipeline to derive output columns.

```python
table.insert(doc_id="d1", text="Hello World")

row = table.get("d1")
print(row["clean_text"])  # "hello world"
print(row["word_count"])  # 2
```

If the identity already exists and the source columns are unchanged (same fingerprint), the row is skipped without re-running the graph:

```python
table.insert(doc_id="d1", text="Hello World")  # skipped -- same fingerprint
table.insert(doc_id="d1", text="Goodbye World")  # re-derived -- text changed
```

You can also insert a batch by passing a list of dicts:

```python
table.insert([
    {"doc_id": "d1", "text": "first document"},
    {"doc_id": "d2", "text": "second document"},
    {"doc_id": "d3", "text": "third document"},
])
```

Extra keyword arguments beyond identity and source columns are stored as metadata:

```python
table.insert(doc_id="d1", text="Hello World", title="Greeting", source_url="https://example.com")
row = table.get("d1")
print(row["title"])  # "Greeting"
```

**Args:**
- `**kwargs`: Column values. Must include the identity column and all source columns (graph required inputs).

**Returns:** `None` (sync) or coroutine (async runner).

**Raises:**
- `RuntimeError` -- If no runner is set

### `update(identity_value, **changes) -> None`

Update a row's columns. If a source column changes, the graph re-derives downstream columns. If only metadata changes, no re-derivation occurs.

```python
# Source column changed -- re-derives clean_text and word_count
table.update("d1", text="one two three four")
row = table.get("d1")
print(row["word_count"])  # 4

# Metadata only -- no re-derivation
table.update("d1", title="Updated Title")
row = table.get("d1")
print(row["title"])  # "Updated Title"
print(row["word_count"])  # 4 (unchanged)
```

When a parent row's **source** columns change, its children are cascade-rederived from the new parent outputs. Metadata-only updates don't trigger re-derivation of the row or its children.

**Args:**
- `identity_value` (str): The identity value of the row to update.
- `**changes`: Column values to change.

**Returns:** `None` (sync) or coroutine (async runner).

**Raises:**
- `KeyError` -- If no row exists with the given identity value
- `RuntimeError` -- If no runner is set

### `delete(identity_value) -> None`

Delete a row and cascade-delete all of its child rows.

```python
table.insert(doc_id="d1", text="hello")
table.insert(doc_id="d2", text="world")
print(table.count())  # 2

table.delete("d1")
print(table.count())  # 1
print(table.get("d1"))  # None
```

Deleting a nonexistent row is a no-op:

```python
table.delete("nonexistent")  # no error, nothing happens
```

**Args:**
- `identity_value` (str): The identity value of the row to delete.

**Returns:** `None` (sync) or coroutine (async runner).

### `sync(items) -> SyncResult`

Reconcile the table to match a list of items. Inserts new items, updates changed items, deletes items no longer in the list, and skips unchanged items.

```python
# Start with three documents
table.insert(doc_id="d1", text="unchanged")
table.insert(doc_id="d2", text="will change")
table.insert(doc_id="d3", text="will be removed")

# Sync to the desired state
result = table.sync([
    {"doc_id": "d1", "text": "unchanged"},       # skipped
    {"doc_id": "d2", "text": "new content"},      # updated
    {"doc_id": "d4", "text": "brand new"},        # inserted
    # d3 is absent -- deleted
])

print(result.inserted)  # 1
print(result.updated)   # 1
print(result.deleted)   # 1
print(result.skipped)   # 1
print(result.errored)   # 0
```

With `on_error="store"`, failed items are recorded in `result.errors` instead of raising:

```python
table = HyperTable(
    [clean, count_words],
    identity="doc_id",
    store=store,
    on_error="store",
).with_runner(SyncRunner())

result = table.sync([
    {"doc_id": "d1", "text": "good"},
    {"doc_id": "d2", "text": "bad input that causes failure"},
    {"doc_id": "d3", "text": "also good"},
])

print(result.errored)  # 1
print(result.errors[0].identity)    # {"doc_id": "d2"}
print(result.errors[0].error_type)  # "ValueError"
print(result.errors[0].error_msg)   # "ValueError: ..."
```

**Args:**
- `items` (list[dict[str, Any]]): The desired state of the table. Each dict must include the identity column and all source columns.

**Returns:** [`SyncResult`](#syncresult) with counts for each outcome.

**Raises:**
- `RuntimeError` -- If no runner is set
- Propagates graph errors when `on_error="raise"` (default)

## Read Operations

Read operations do not need a runner. They query the store directly.

### `get(identity_value, *, include_status=False) -> dict[str, Any] | None`

Get a single row by its identity value. Returns `None` if not found.

```python
row = table.get("d1")
print(row)
# {'doc_id': 'd1', 'text': 'hello world', 'clean_text': 'hello world', 'word_count': 2}

missing = table.get("nonexistent")
print(missing)  # None
```

Internal columns (`_row_fingerprint`, `_write_gen`, `_provenance_*`) are always stripped. Status columns (`_status`, `_error`) are stripped by default. Pass `include_status=True` to see them:

```python
row = table.get("d1", include_status=True)
print(row["_status"])  # "complete"
print(row["_error"])   # None
```

**Args:**
- `identity_value` (str): The identity value to look up.
- `include_status` (bool): If `True`, include `_status` and `_error` in the returned dict. Default: `False`.

**Returns:** A dict of column values, or `None` if not found.

### `filter(where=None, *, limit=None, include_status=False) -> list[dict[str, Any]]`

Query rows matching a predicate.

```python
# All rows
all_rows = table.filter()

# Filter by column value
errors = table.filter(
    where=[("_status", "eq", "error")],
    include_status=True,
)

# Multiple conditions (AND)
recent_short = table.filter(
    where=[("word_count", "lt", 10)],
    limit=5,
)
```

The `where` parameter accepts a list of `(column, operator, value)` tuples. Supported operators: `"eq"`, `"ne"`, `"lt"`, `"lte"`, `"gt"`, `"gte"`, `"in"`. You can also pass a dict for simple equality filters:

```python
# Dict shorthand -- equivalent to [("doc_id", "eq", "d1")]
row = table.filter(where={"doc_id": "d1"})
```

**Args:**
- `where` (list[tuple] | dict | None): Filter predicate. `None` returns all rows.
- `limit` (int | None): Maximum number of rows to return. `None` returns all matches.
- `include_status` (bool): If `True`, include `_status` and `_error`. Default: `False`.

**Returns:** List of row dicts matching the predicate.

### `children(parent_id, *, include_status=False) -> list[dict[str, Any]]`

Get all child rows belonging to a parent. Only applies to tables with a `map_over` grain boundary.

```python
from typing import TypedDict

class Utterance(TypedDict):
    utterance_id: str
    text: str
    speaker: str

@node(output_name="utterances")
def split_utterances(transcript: str) -> list[Utterance]:
    words = transcript.split()
    return [
        Utterance(utterance_id=f"u{i}", text=w, speaker="Alice")
        for i, w in enumerate(words)
    ]

process_utterance = Graph([clean, embed_text], name="process_utterance")

table = HyperTable(
    [transcribe, split_utterances,
     process_utterance.as_node().map_over("utterances", identity="utterance_id")],
    identity="video_id",
    store=store,
).bind(embedder=embedder).with_runner(SyncRunner())

table.insert(video_id="v1", path="/data/meeting.mp4")

children = table.children("v1")
for c in children:
    print(c["utterance_id"], c["clean_text"], c["vector"][:3])
# u0 transcript [0.95, 0.91, 0.80]
# u1 of [0.90, 0.84, 0.0]
# ...
```

Returns an empty list if the table has no child tables or the parent has no children:

```python
table.children("nonexistent")  # []
```

**Args:**
- `parent_id` (str): The identity value of the parent row.
- `include_status` (bool): If `True`, include `_status` and `_error` on child rows. Default: `False`.

**Returns:** List of child row dicts.

### `filter_children(where=None, *, limit=None, include_status=False) -> list[dict[str, Any]]`

Query child rows across all parents, filtered by a predicate.

```python
# All children of a specific parent
v1_children = table.filter_children(where=[("_parent_id", "eq", "v1")])

# Find error children across all parents
error_children = table.filter_children(
    where=[("_status", "eq", "error")],
    include_status=True,
)
print(len(error_children))  # number of failed child derivations

# Combine parent and column filters
hello_from_v1 = table.filter_children(
    where=[("_parent_id", "eq", "v1"), ("clean_text", "eq", "hello")],
    limit=1,
)
```

Returns an empty list if the table has no child tables.

**Args:**
- `where` (list[tuple] | dict | None): Filter predicate. `None` returns all child rows.
- `limit` (int | None): Maximum number of rows to return. `None` returns all matches.
- `include_status` (bool): If `True`, include `_status` and `_error`. Default: `False`.

**Returns:** List of child row dicts matching the predicate.

### `count(child_table=None) -> int`

Count rows in the root table or a child table.

```python
print(table.count())              # number of parent rows
print(table.count("utterance"))   # number of child rows across all parents
```

**Args:**
- `child_table` (str | None): Name of a child table to count. `None` counts root table rows. The child table name is derived from the child identity minus `_id` (e.g., identity `utterance_id` produces table name `utterance`).

**Returns:** Integer row count.

## Child Mutation Operations

These methods mutate child rows directly, without re-running the parent graph.

### `set_children(where=None, **fields) -> int`

Bulk update metadata fields on child rows matching a predicate. Does not trigger re-derivation. Useful for annotation workflows where human reviewers tag child rows after derivation.

```python
# Tag all children of a parent as reviewed
count = table.set_children(
    where=[("_parent_id", "eq", "v1")],
    reviewed=True,
    reviewer="alice",
)
print(count)  # 2 (number of rows updated)

# Verify
children = table.filter_children(where=[("_parent_id", "eq", "v1")])
print(children[0]["reviewed"])  # True
print(children[0]["reviewer"])  # "alice"
```

New fields are added to the schema automatically. Existing derived columns are preserved.

**Args:**
- `where` (list[tuple] | dict | None): Filter predicate to select which child rows to update.
- `**fields`: Column values to set on matching rows.

**Returns:** Number of rows updated.

### `delete_children(where=None) -> int`

Delete child rows matching a predicate.

```python
# Delete one specific child
count = table.delete_children(
    where=[("_parent_id", "eq", "v1"), ("utterance_id", "eq", "u0")],
)
print(count)  # 1

# Delete all children of a parent
count = table.delete_children(where=[("_parent_id", "eq", "v1")])
print(count)  # remaining children of v1
```

Returns 0 if the table has no child tables or no rows match.

**Args:**
- `where` (list[tuple] | dict | None): Filter predicate. `None` deletes all child rows.

**Returns:** Number of rows deleted.

## Maintenance Operations

### `recompute(column) -> None`

Re-derive a single column for all rows using the current graph and bound components. Useful after swapping a component (e.g., upgrading an embedder model) when you want to refresh one specific derived column.

```python
# Original table with small embedder
table = HyperTable(
    [clean, embed_text],
    identity="doc_id",
    store=store,
).bind(embedder=Embedder(model="v1", dim=256)).with_runner(SyncRunner())

table.insert(doc_id="d1", text="hello world")
print(len(table.get("d1")["vector"]))  # 256

# Swap embedder and recompute just the vector column
table_v2 = table.bind(embedder=Embedder(model="v2", dim=256))
table_v2.recompute("vector")

# All rows now have vectors from the new embedder
print(len(table_v2.get("d1")["vector"]))  # 256 (re-derived with new model)
```

**Args:**
- `column` (str): Name of the derived column to re-derive.

**Returns:** `None`

**Raises:**
- `RuntimeError` -- If no runner is set

### `backfill(column) -> None`

Populate a new derived column for existing rows that have `NULL` for that column. Useful after adding a new node to the graph -- existing rows lack the new column, and `backfill` fills them in without touching rows that already have a value.

```python
# v1: table with clean only
table_v1 = HyperTable(
    [clean],
    identity="doc_id",
    store=store,
).with_runner(SyncRunner())

table_v1.insert(doc_id="d1", text="hello world")
table_v1.insert(doc_id="d2", text="one two three")

# v2: add word_count node
table_v2 = HyperTable(
    [clean, count_words],
    identity="doc_id",
    store=store,
).with_runner(SyncRunner())

table_v2.backfill("word_count")

print(table_v2.get("d1")["word_count"])  # 2
print(table_v2.get("d2")["word_count"])  # 3
```

`backfill` skips rows where the column already has a non-null value:

```python
# Only rows with NULL word_count are processed
table_v2.insert(doc_id="d3", text="four words right here")  # word_count derived on insert
table_v2.backfill("word_count")  # d3 skipped, only d1 and d2 filled
```

**Args:**
- `column` (str): Name of the derived column to backfill.

**Returns:** `None`

**Raises:**
- `RuntimeError` -- If no runner is set

## Types

### `SyncResult`

Dataclass returned by `sync()`. Reports how many rows fell into each category.

```python
from hypergraph.materialization import SyncResult

result = table.sync(items)
print(result.inserted)  # int -- new rows added
print(result.updated)   # int -- rows with changed source columns
print(result.deleted)   # int -- rows removed (not in items list)
print(result.skipped)   # int -- rows with unchanged fingerprint
print(result.errored)   # int -- rows that failed derivation (on_error="store")
print(result.errors)    # tuple[ErrorRow, ...] -- details for each failure
```

**Fields:**
- `inserted` (int): Number of new rows inserted.
- `updated` (int): Number of existing rows re-derived due to changed source columns.
- `deleted` (int): Number of rows deleted because they were absent from the input list.
- `skipped` (int): Number of rows skipped because the fingerprint matched.
- `errored` (int): Number of rows that failed derivation (only with `on_error="store"`).
- `errors` (tuple[ErrorRow, ...]): Error details for each failed row. Empty when `on_error="raise"`.

### `ErrorRow`

Dataclass describing a single row that failed derivation. Returned as part of `SyncResult.errors`.

```python
from hypergraph.materialization import ErrorRow

result = table.sync(items)
for err in result.errors:
    print(err.identity)    # {"doc_id": "d2"} -- identity column as a dict
    print(err.error_type)  # "ValueError" -- exception class name
    print(err.error_msg)   # "ValueError: invalid input" -- full error string
```

**Fields:**
- `identity` (dict): A dict mapping the identity column name to its value (e.g., `{"doc_id": "d2"}`).
- `error_type` (str): The exception class name.
- `error_msg` (str): The full error message string.

## Key Concepts

### Error Handling: `on_error`

The `on_error` parameter controls what happens when graph execution fails for a row.

**`on_error="raise"` (default)** -- The exception propagates immediately. No error row is written. Use this during development or when partial results are not acceptable.

```python
table = HyperTable([clean], identity="doc_id", store=store).with_runner(SyncRunner())
table.insert(doc_id="d1", text="bad input")  # raises if clean() fails
```

**`on_error="store"`** -- The error is captured and written as an error row. Processing continues with the next item. Source columns are preserved; derived columns are set to `None`.

```python
table = HyperTable(
    [clean], identity="doc_id", store=store, on_error="store",
).with_runner(SyncRunner())

table.insert(doc_id="d1", text="bad input")  # error row written, no exception

row = table.get("d1", include_status=True)
print(row["_status"])      # "error"
print(row["_error"])       # "ValueError: ..."
print(row["text"])         # "bad input" (source preserved)
print(row["clean_text"])   # None (derived not computed)
```

Error rows are automatically retried on the next `insert()` or `sync()` call. If the underlying issue is fixed, the row transitions from error to complete:

```python
# Fix the issue, then re-insert -- error row is replaced
table.insert(doc_id="d1", text="fixed input")
row = table.get("d1", include_status=True)
print(row["_status"])  # "complete"
```

### Row Status and `include_status`

Every row has internal `_status` and `_error` fields. By default, these are hidden from read results. Pass `include_status=True` to any read method to see them.

A `_status` of `None` is treated as `"complete"` for backward compatibility (rows written before the status column was added). When `include_status=True`, `None` status is normalized to `"complete"` in the returned dict.

```python
# Without include_status (default) -- clean output
row = table.get("d1")
print("_status" in row)  # False

# With include_status -- see derivation state
row = table.get("d1", include_status=True)
print(row["_status"])  # "complete" or "error"
print(row["_error"])   # None or "ValueError: ..."
```

All read methods support `include_status`: `get()`, `filter()`, `children()`, `filter_children()`.

### Where Predicates

Methods that accept a `where` parameter use a list of `(column, operator, value)` tuples. Multiple tuples are combined with AND.

```python
# Single condition
table.filter(where=[("word_count", "gt", 5)])

# Multiple conditions (AND)
table.filter(where=[
    ("word_count", "gt", 5),
    ("_status", "eq", "complete"),
], include_status=True)

# Dict shorthand for equality
table.filter(where={"doc_id": "d1"})
# equivalent to: where=[("doc_id", "eq", "d1")]
```

Supported operators: `"eq"`, `"ne"`, `"lt"`, `"lte"`, `"gt"`, `"gte"`, `"in"`.

### Identity Column

The identity column is always explicitly named -- there is no convention-based inference. It serves as the primary key for the root table and must be included in every `insert()` call.

```python
# Identity is "doc_id"
table = HyperTable([clean], identity="doc_id", store=store)
table.insert(doc_id="d1", text="hello")

# Identity is "video_id"
table = HyperTable([transcribe], identity="video_id", store=store)
table.insert(video_id="v1", path="/data/a.mp4")
```

### Child Tables via `map_over`

A `map_over` node creates a grain boundary: one parent row fans out into many child rows, each derived by a nested graph.

```python
from typing import TypedDict

class Chunk(TypedDict):
    chunk_id: str
    text: str

@node(output_name="chunks")
def split_into_chunks(document: str) -> list[Chunk]:
    paragraphs = document.split("\n\n")
    return [
        Chunk(chunk_id=f"c{i}", text=p)
        for i, p in enumerate(paragraphs)
    ]

# Nested graph processes each chunk independently
chunk_pipeline = Graph([clean, embed_text], name="process_chunk")

table = HyperTable(
    [
        split_into_chunks,
        chunk_pipeline.as_node().map_over("chunks", identity="chunk_id"),
    ],
    identity="doc_id",
    store=store,
).bind(embedder=embedder).with_runner(SyncRunner())

table.insert(doc_id="d1", document="First paragraph.\n\nSecond paragraph.")
print(table.count())           # 1 parent row
print(table.count("chunk"))    # 2 child rows
print(table.children("d1"))    # [{"chunk_id": "c0", ...}, {"chunk_id": "c1", ...}]
```

The child table name is derived from the child identity: `chunk_id` becomes table name `chunk`. Child rows include a `_parent_id` column linking them to the parent.

The items returned by the splitting node can be dicts, `TypedDict` instances, Pydantic models, or dataclasses. All are normalized to dicts before processing:

```python
from pydantic import BaseModel

class Chunk(BaseModel):
    chunk_id: str
    text: str

@node(output_name="chunks")
def split_into_chunks(document: str) -> list[Chunk]:
    # Pydantic models work in map_over
    return [Chunk(chunk_id="c0", text="hello")]
```

Child rows have their own fingerprints and incrementality. If a child item is unchanged on re-insert, its derivation is skipped.

### Reserved Column Names

The following column names are reserved for internal use and cannot be used as identity, source, or derived (node output) column names:

- `_status` -- Row derivation status (`"complete"` or `"error"`)
- `_error` -- Error message string when `_status` is `"error"`
- `_row_fingerprint` -- Content-addressed hash for incrementality
- `_write_gen` -- Monotonic generation counter for crash recovery
- `_parent_id` -- Link from child row to parent row
- `_provenance_*` -- Per-column provenance hashes (any name starting with `_provenance_`)

```python
# These raise ValueError at analysis time
HyperTable([clean], identity="_status", store=store)       # reserved
HyperTable([clean], identity="_write_gen", store=store)     # reserved
HyperTable([clean], identity="_provenance_x", store=store)  # reserved prefix

# Underscore names that are not reserved are fine
HyperTable([clean], identity="_doc_ref", store=store)  # allowed
```

## Complete Example

A document processing pipeline with vector search, child tables, error handling, and schema evolution.

```python
from typing import TypedDict

from hypergraph import node, Graph
from hypergraph.materialization import HyperTable
from hypergraph.materialization.stores import LanceDBStore
from hypergraph.runners import SyncRunner

# --- Components ---

class Embedder:
    def __init__(self, model: str = "text-embedding-3-small", dim: int = 8):
        self.model = model
        self.dim = dim

    def _config(self):
        return {"model": self.model, "dim": self.dim}

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for i, c in enumerate(text[:self.dim]):
            vec[i] = float(ord(c)) / 122.0
        return vec

# --- Nodes ---

@node(output_name="clean_text")
def clean(text: str) -> str:
    return text.strip().lower()

@node(output_name="vector")
def embed_text(clean_text: str, embedder: Embedder) -> list[float]:
    return embedder.embed(clean_text)

@node(output_name="word_count")
def count_words(clean_text: str) -> int:
    return len(clean_text.split())

class Chunk(TypedDict):
    chunk_id: str
    text: str

@node(output_name="chunks")
def split_chunks(clean_text: str) -> list[Chunk]:
    words = clean_text.split()
    mid = len(words) // 2
    return [
        Chunk(chunk_id="c0", text=" ".join(words[:mid])),
        Chunk(chunk_id="c1", text=" ".join(words[mid:])),
    ]

chunk_pipeline = Graph([clean, embed_text], name="process_chunk")

# --- Build table ---

store = LanceDBStore("/tmp/docs_pipeline")
embedder = Embedder()

table = (
    HyperTable(
        [clean, embed_text, count_words, split_chunks,
         chunk_pipeline.as_node().map_over("chunks", identity="chunk_id")],
        identity="doc_id",
        store=store,
        on_error="store",
    )
    .bind(embedder=embedder)
    .with_runner(SyncRunner())
)

# --- Insert documents ---

table.insert(doc_id="d1", text="Hello World from Hypergraph")
table.insert(doc_id="d2", text="Incremental processing is efficient")

# --- Read ---

print(table.count())           # 2
print(table.count("chunk"))    # 4 (2 chunks per doc)

row = table.get("d1")
print(row["word_count"])       # 4
print(len(row["vector"]))     # 8

children = table.children("d1")
print(len(children))           # 2

# --- Sync to new state ---

result = table.sync([
    {"doc_id": "d1", "text": "Hello World from Hypergraph"},  # unchanged -- skip
    {"doc_id": "d3", "text": "A brand new document"},         # new -- insert
    # d2 absent -- deleted
])
print(result)
# SyncResult(inserted=1, updated=0, deleted=1, skipped=1, errored=0, errors=())

# --- Upgrade: add a new column via backfill ---

@node(output_name="summary")
def summarize(clean_text: str) -> str:
    return clean_text[:20] + "..."

table_v2 = (
    HyperTable(
        [clean, embed_text, count_words, summarize, split_chunks,
         chunk_pipeline.as_node().map_over("chunks", identity="chunk_id")],
        identity="doc_id",
        store=store,
        on_error="store",
    )
    .bind(embedder=embedder)
    .with_runner(SyncRunner())
)

table_v2.backfill("summary")
print(table_v2.get("d1")["summary"])  # "hello world from hyp..."

# --- Swap embedder and recompute vectors ---

big_embedder = Embedder(model="text-embedding-3-large", dim=8)
table_v3 = table_v2.bind(embedder=big_embedder)
table_v3.recompute("vector")
```
