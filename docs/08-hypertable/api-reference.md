# HyperTable API reference

## Construction

### `Graph.as_table(...)`

```python
table = graph.as_table(
    identity="upload_id",
    store=LanceDBStore("./data"),
    runner=AsyncRunner(),
    on_error="store",
    name="uploads",
)
```

Parameters:

- `identity: str` — public entity-key column.
- `store: TableStore` — opened lazily on first use.
- `runner` — execution policy. Defaults to `SyncRunner()`.
- `on_error: Literal["raise", "store"]` — raise immediately or persist a
  typed row error. Defaults to `"raise"`.
- `name: str | None` — physical root table name. The default derives from the
  identity.

Graphs containing interrupts require `AsyncRunner`. Using the default runner
fails on first derivation with `IncompatibleRunnerError` and names the fix.

The returned object is a `HyperTable`. Its `graph` property exposes the graph
artifact and `table_name` exposes the physical root name.

## Write receipts

```python
class RowStatus(Enum):
    COMPLETE = "complete"
    WAITING = "waiting"
    ERROR = "error"


class WriteOutcome(Enum):
    INSERTED = "inserted"
    UPDATED = "updated"
    SKIPPED = "skipped"
    HEALED = "healed"
```

`HEALED` is reported by `sync()` when an unchanged parent row had physically
missing child rows rebuilt. A receipt is never `SKIPPED` on a path that wrote
rows.

### `RowReceipt`

```python
@dataclass(frozen=True)
class RowReceipt:
    id: str
    outcome: WriteOutcome
    status: RowStatus
    pause: PauseInfo | None = None
    error: str | None = None

    @property
    def paused(self) -> bool: ...

    @property
    def completed(self) -> bool: ...

    @property
    def failed(self) -> bool: ...
```

`pause` is present only for `WAITING`; `error` is present only for `ERROR`.
The three boolean properties deliberately match `RunResult` serving code.

### `TableReceipt`

```python
@dataclass(frozen=True)
class TableReceipt:
    receipts: tuple[RowReceipt, ...]
    deleted: int = 0

    @property
    def inserted(self) -> int: ...

    @property
    def updated(self) -> int: ...

    @property
    def skipped(self) -> int: ...

    @property
    def healed(self) -> int: ...

    @property
    def waiting(self) -> tuple[RowReceipt, ...]: ...

    @property
    def errors(self) -> tuple[RowReceipt, ...]: ...

    @property
    def paused(self) -> bool: ...

    @property
    def completed(self) -> bool: ...

    @property
    def failed(self) -> bool: ...
```

## Writes

When the table uses `AsyncRunner`, each write returns the documented value
through a coroutine.

### `insert(**row) -> RowReceipt`

Derive and persist one row. An existing identity converges under current
source values, graph code, configuration, and answers.

```python
receipt = await uploads.insert(upload_id="u-1", path="/in/a.pdf")
```

### `insert(rows: list[dict]) -> TableReceipt`

Derive a list without deleting identities not present in that list.

### `update(id, **changes) -> RowReceipt`

- source changes re-derive affected downstream columns;
- answer changes answer an interrupt and continue downstream;
- metadata-only changes persist without derivation and report `SKIPPED`.

If a source upstream of an old answer changes, the answer's provenance no
longer matches. The row becomes `WAITING` with a fresh question and fresh
provenance.

### `sync(items: list[dict]) -> TableReceipt`

Converge a complete collection: insert new identities, update changed ones,
skip fresh ones, and delete missing ones.

Unchanged parents are also self-repairing: a successful parent must not make
child loss permanent, so `sync()` rebuilds physically missing child rows and
reports the row as `HEALED`. To detect loss, `sync()` inspects each child
table once per unchanged parent row — it compares the fan-out count recorded
on the parent row against the deduplicated child rows physically present
(one child-table read per parent; no writes). When every child row is
present, the row is a zero-execution, zero-write `SKIPPED`; when rows are
missing, the fan-out boundary re-runs once to regenerate the item list and
only the missing children run the child graph — present children and parent
derived columns are not re-derived.

### `delete(id) -> None`

Delete one root row and its child rows.

### `set(where, **fields) -> int`

Bulk-update metadata. Content-key fields are rejected because changing them
without derivation would make the row untruthful.

### `rederive(column, *, missing_only=False) -> TableReceipt`

Derive one column across every row, or only rows whose value is missing.

## Reads

### `get(id) -> dict | None`

Return one newest public row. Public reads never expose internal columns.

### `rows(where=None, *, limit=None) -> list[dict]`

Read public rows. `where` may be a mapping for equality predicates or a list
of `(column, operator, value)` tuples. Operators are `eq`, `ne`, `lt`, `lte`,
`gt`, `gte`, and `in`.

### `waiting() -> tuple[WaitingRow, ...]`

```python
@dataclass(frozen=True)
class WaitingRow:
    id: str
    pause: PauseInfo
    row: dict[str, Any]
    provenance: str
```

`pause.value` exposes `prompt`, `options`, `evidence`, and `answer_type`.
In-process receipts retain the original question object. Persisted questions
rebuild a frozen structural view; `answer_type` is a stable display string,
not an imported Python object. `provenance` is opaque and stable for the same
question inputs.

Question evidence must be JSON-serializable. Persistence fails loudly and
identifies the first invalid evidence item.

### `errors() -> tuple[ErroredRow, ...]`

```python
@dataclass(frozen=True)
class ErroredRow:
    id: str
    error: str
    row: dict[str, Any]
```

### `count() -> int`

Return the logical root-row count.

## Child tables

### `child(name) -> ChildTable`

Address a named mapped grain. The handle supports:

```python
child.get(parent_id, child_id)
child.rows(where=None, parent=None, limit=None)
child.waiting()
child.errors()
child.set(where, **fields)
child.delete(where)
child.count()
```

Child rows expose the parent's public identity name rather than the physical
link column. A `where` predicate may reference parent columns; the handle
joins matching parent identities before reading child rows.

## Diagnostics and retrieval

These existing typed surfaces are unchanged:

```python
table.status()                       # TableStatus
table.recipe_drift()                 # RecipeDrift
table.explain(identity_value)        # current column recipes
table.resolve_provenance(stamp)      # journal lookup
table.journal_rows()                 # raw recipe journal

table.create_index(...)
table.list_indexes()
table.drop_index(name)
table.search(...)
table.visualize()
```

## Serving a table pause

The same pause-reading lines work for a runner result and a table receipt:

```python
if result.paused:
    ask = result.pause.value
    answer_key = result.pause.response_key
```

A minimal FastAPI surface is three endpoints:

```python
from fastapi import FastAPI, HTTPException

app = FastAPI()


@app.post("/uploads")
async def upload(upload_id: str, path: str):
    receipt = await uploads.insert(upload_id=upload_id, path=path)
    if receipt.paused:
        return {
            "state": "waiting",
            "prompt": receipt.pause.value.prompt,
            "options": receipt.pause.value.options,
            "answer_key": receipt.pause.response_key,
        }
    if receipt.failed:
        raise HTTPException(422, receipt.error)
    return {"state": "complete", "row": uploads.get(upload_id)}


@app.get("/questions")
def questions():
    return [
        {
            "upload_id": waiting.id,
            "prompt": waiting.pause.value.prompt,
            "options": waiting.pause.value.options,
            "answer_key": waiting.pause.response_key,
            "provenance": waiting.provenance,
        }
        for waiting in uploads.waiting()
    ]


@app.post("/answers")
async def answer(upload_id: str, answer_key: str, value: str):
    receipt = await uploads.update(upload_id, **{answer_key: value})
    return {"state": receipt.status.value, "row": uploads.get(upload_id)}
```

## Plain `Table`

`Table(identity=..., store=...)` is the non-deriving companion.
Use `append()` to store rows, plus `update()`, `delete()`, `get()`, `rows()`,
and `count()`. It returns the same receipt vocabulary but never runs a graph.
