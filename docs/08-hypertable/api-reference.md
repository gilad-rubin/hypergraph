# HyperTable API reference

## Construction

### `Graph.as_table(...)`

```python
table = graph.as_table(
    identity="upload_id",
    store=LanceDBStore("./data"),
    runner=AsyncRunner(),
    page_max_concurrency=16,
    on_error="store",
    name="uploads",
)
```

Parameters:

- `identity: str` — public entity-key column.
- `store: TableStore` — opened lazily on first use.
- `runner` — execution policy. Defaults to `SyncRunner()`. A runner with no
  `checkpointer` records nothing when you drive the table yourself, but
  inherits the Run Home when the table is derived inside a durable Run, so
  the recipe's per-node cost lands under the Host Run that drove it (see
  [`node_timings`](../06-api-reference/host.md#what-did-the-work-cost)). Pass
  `checkpointer=` to decide that yourself; the table's own runner always wins.
- `page_max_concurrency: int` — maximum independent child-page graphs in
  flight for one parent mutation. Defaults to `16`; synchronous runners
  preserve the same results but execute child pages serially.
- `on_error: Literal["raise", "store"]` — raise immediately or persist a
  typed row error. Defaults to `"raise"`.
- `name: str | None` — physical root table name. The default derives from the
  identity.

Graphs containing interrupts require `AsyncRunner`. Using the default runner
fails on first derivation with `IncompatibleRunnerError` and names the fix.

The returned object is a `HyperTable`. Its `graph` property exposes the graph
artifact, `table_name` exposes the physical root name, and `store` exposes the
analyzed `TableStore` boundary for application-owned catalog or maintenance
operations. Accessing `store` opens the table schema first; callers do not need
to invoke private analysis methods.

## Materialization Branches

### `HyperTable.attach(name, *, graph, outputs) -> MaterializationBranch`

Persist another complete derivation recipe over this table's current root
rows. `name` is the stable attachment id. `outputs` maps query roles to logical
terminal columns.

Before, a caller had to build one superset graph, invent output names, and feed
the source collection through that table again:

```python
extended.sync(source_rows)
extended.rederive("vector_v2", missing_only=True)
extended.create_index("candidate", vector="vector_v2")
```

After, the recipe remains complete and the branch reads the existing root:

```python
branch = documents.attach(
    "search-large",
    graph=configured_recipe,
    outputs={"text": "chunk_text", "vector": "vector"},
)
receipt = branch.sync()
query_spec = branch.create_index("candidate")
```

`attach()` writes the branch registration and lineage plan before any
derivation. Reopening the same name with the same recipe returns the persisted
branch; reusing the name for a different recipe raises `GraphConfigError`.

Lineage matching requires both the producing recipe and all upstream lineage.
Matching columns and child grains share physical storage. A changed same-grain
step gets a new column; a changed fan-out step gets a new child table. New
physical names use the stable attachment id, not a display label.

### `MaterializationBranch.sync() -> TableReceipt`

Derive only missing or stale branch artifacts from all current root rows. Do
not pass source rows. With an `AsyncRunner`, await the returned coroutine.

### `MaterializationBranch.status() -> TableStatus`

Project readiness and stale columns without running the graph.

### `MaterializationBranch.output(name) -> MaterializedArtifact`

Resolve a declared output alias to `table`, `column`, and opaque `lineage`.
`references` and `shared` derive from every persisted branch reference plus the
base table; no artifact stores an owner.

### `MaterializationBranch.artifacts() -> tuple[MaterializedArtifact, ...]`

Project every reachable column and child-table artifact for plan inspection.
A child-table artifact has `column=None`; a column artifact names both its
physical `table` and `column`. This read-only reachability view runs no graph
work.

### `MaterializationBranch.create_index(name, *, rows=None) -> dict`

Delegate to the existing named query-spec policy using the branch's declared
`text` and `vector` outputs. Both outputs must resolve to the same grain.

Root `delete()` and collection `sync()` cascade through every registered child
grain, including branches that have not been reopened in the current process.

## Write receipts

```python
class RowStatus(Enum):
    COMPLETE = "complete"
    WAITING = "waiting"
    ERROR = "error"
    PARTIAL = "partial"


class WriteOutcome(Enum):
    INSERTED = "inserted"
    UPDATED = "updated"
    SKIPPED = "skipped"
    HEALED = "healed"
```

`HEALED` is reported by `sync()` or `insert()` when an unchanged parent row
had damaged child rows rebuilt — rows physically missing, stored in error
under `on_error="store"`, or extra rows left behind by an interrupted write —
and everything the repair derived landed healthy. A repair whose retry failed
again healed nothing, so it reports `UPDATED` and the child stays in error for
the next attempt to find. One exception: when the fan-out boundary also
produces a stored parent column, retiring an extra row derives no new row, so
that pass reports `SKIPPED`, as described next, unless re-running the
parent's nodes stored a different value.

`SKIPPED` is a claim about derivation, not about bytes: it means no row was
derived. A pass over an
unchanged parent still re-stamps its child rows at a newer generation and
retires the rows they replace; that is bookkeeping, and such a pass reports
`SKIPPED`. Nor does it mean nothing executed: when the fan-out boundary also
produces a stored parent column, the pass runs the graph to establish that the
stored row still stands, derives no new row from it, and reports `SKIPPED`.
Mapped items that share a child identity never settle as `SKIPPED` or
`HEALED`: the write is refused before any child row is written (see
[Child tables](#child-tables)), and under `on_error="store"` the parent is
stored as an `ERROR` row naming the collision. The one exception is a row an
earlier build already stored over colliding items with a recorded count that
matches its present child rows: nothing re-runs its boundary, so it stays
`SKIPPED` until something does.

`PARTIAL` is reported under `on_error="store"` when one node failed and the
other derived columns were produced anyway: those columns are stored, the
failed ones are null, and the row records why (see `partial()`, which also
names the two derivation paths that still write the whole-row shape). A partial
row names every node of its graph that failed. When the failed node is a
fan-out boundary, the row still keeps the columns that succeeded; its change
entry names the `map_over` input rather than a stored column, the child rows
derived earlier stay as they are, and the next `sync()` re-runs only the
boundary — unless the boundary also produces a stored parent column, in which
case the whole graph runs again for the row, as it does for such a boundary
under `SKIPPED` above.
Three failures stay `ERROR`, so a row never claims column granularity the run
cannot support: one the runner cannot blame on a node (a missing input, a
plan-level error), one that leaves no derived column standing, and one on a
top-level node of this table's graph that owns no stored column and is not a
fan-out boundary (a node with no output), which no change entry could name and
no column-scoped heal would run again. A node with no output that fails inside
a mounted graph leaves a partial row, because the heal re-runs that mounted
graph whole.

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

`pause` is present only for `WAITING`; `error` is present for `ERROR` and for
`PARTIAL`, where it carries the first failure the run could attribute.
The three boolean properties deliberately match `RunResult` serving code.

### `MaterializationReceipt`

`HyperTable.as_node()` emits the frozen Pydantic counterpart of `RowReceipt`.
Its enum fields and normalized pause question survive the default JSON
checkpointer and restore as typed values.

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
    def partial(self) -> tuple[RowReceipt, ...]: ...

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

### `as_node(*, name=None, output_name="receipt") -> HyperNode`

Return a graph node that inserts and derives one row. Its named inputs are the
table identity followed by the recipe's source columns in table-spec order;
the adapter never generates identity. Ordinary graph wiring supplies upstream
values, and constants use the enclosing graph's existing binding surface:

```python
workflow = Graph([
    resolve_version,
    protocols.as_node(
        name="materialize_protocol",
        output_name="materialization",
    ),
    publish_protocol,
]).bind(active=False)
```

The single output is a checkpoint-safe `MaterializationReceipt`. Recipe and
child-grain events propagate through the enclosing Run's event processors.

The mounted node **visualizes as a container**, not as one opaque function
box: it reports `node_type == "GRAPH"` and exposes the table's recipe as its
`nested_graph`, so `visualize(depth=2)` (or expanding the node in the widget)
draws the derivation pipeline inside it. This is structure for the diagram
only — execution still runs the table's own insert-and-derive path, and no
durable record changes, because step and boundary records store the node's
class name rather than this property.

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
- metadata-only changes persist without derivation and report `UPDATED`
  (the row is rewritten, so the receipt does not claim a skip).

If a source upstream of an old answer changes, the answer's provenance no
longer matches. The row becomes `WAITING` with a fresh question and fresh
provenance.

### `sync(items: list[dict]) -> TableReceipt`

Converge a complete collection: insert new identities, update changed ones,
skip fresh ones, and delete missing ones.

Unchanged parents are also self-repairing: a successful parent must not make
child damage permanent, so `sync()` rebuilds child rows that are physically
missing or stored as an error row (`on_error="store"`) and reports the row as
`HEALED`. To detect damage, `sync()` inspects each child table once per
unchanged parent row — it compares the number of distinct child identities
the fan-out produced, recorded on the parent row, against the deduplicated
child rows physically present, and reads their `_status` so a stored failure
never counts as a healthy child (one child-table read per parent; no writes).
When the recorded count matches the child rows present and every one is
complete, the row is a zero-execution, zero-write `SKIPPED`; when a child is
damaged, only that child runs the child graph — present children and parent
derived columns are not re-derived, though present child rows are rewritten at
the repair's generation. A physically missing row leaves nothing to rebuild the
item list from, so the fan-out boundary re-runs once to regenerate it; a stored
error row still carries its own item, so the stored list is reused and the
boundary does not re-run. A stale recorded count, or a boundary that now
produces a different number of child identities, leaves a count that disagrees
with the healthy children present; that is damage too. The boundary re-runs,
the parent records the count it produced, and the repair reports `HEALED`
because the item list was rebuilt. Once the recorded count matches the child
rows written, the next `sync()` is a zero-execution skip again. A row an
earlier version stored over repeated child identities recorded the raw item
count, so this is where it surfaces: the re-run item list still collides, the
write is refused (see [Child tables](#child-tables)), and under
`on_error="store"` the row becomes an `ERROR` row.
When the fan-out boundary also produces a stored parent column, every repair
runs the parent's nodes once to regenerate the item list, while the child graph
still runs only for the damaged child. The parent row is rewritten only when
one of its recorded stamps moved or is missing — the boundary's recorded count,
a derived column's provenance, or the recipe stamp — and this is the same cost
`insert()` pays to repair that shape. A rewrite that changes the recorded count
or a stored value (a node whose output varies between runs can answer
differently) is a rebuild and reports the repair; one that only moves stamps
over identical values is bookkeeping and reports `SKIPPED`. Values are compared
at the column's declared type, and a `list[float]` column is declared float32,
so on either store a difference below float32 precision is not reported. On
`LanceDBStore`, which stores that column as float32, a vector read back rounded
is not a change and NaN counts as the same value as NaN. `SqliteTableStore`
keeps the float64 values and stores a NaN as NULL (see
[Shipped stores](implementing-a-store.md#shipped-stores)), so there a rewrite
over a stored NaN counts as a changed value and reports the repair. A retry
that fails again leaves the child in error and reports `UPDATED` rather than
`HEALED`, and the next `sync()` tries it again.

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

### `partial() -> tuple[PartialRow, ...]`

```python
@dataclass(frozen=True)
class PartialRow:
    id: str
    changes: tuple[ColumnChange, ...]
    row: dict[str, Any]


@dataclass(frozen=True)
class ColumnChange:
    column: str
    reason: ChangeReason
    node: str
    error: str | None = None


class ChangeReason(Enum):
    NODE_ERROR = "node_error"
    UPSTREAM_ERROR = "upstream_error"
    NOT_RUN = "not_run"
```

Rows stored with `on_error="store"` that kept some derived columns and nulled
the rest. One `ColumnChange` per nulled column, plus one per fan-out whose
boundary raised, and the reason is the true one:

- `NODE_ERROR` — this column's own producer raised; `error` carries its text.
- `UPSTREAM_ERROR` — a failed node reaches this producer through the graph, so
  its inputs never arrived.
- `NOT_RUN` — nothing on this column's own path failed; the run ended before
  its producer was scheduled. Retrying may well derive it.

A column whose producer ran and returned `None` gets no entry at all: that is a
value, not a failure, and it is stored with its provenance like any other.

A fan-out entry names the `map_over` input (`"words"` for
`map_over("words", ...)`), not a stored column, so it is not a key of `row`.
There is one per input, however many child tables map over it. Its reason is
always `NODE_ERROR`, and `node` names the boundary that raised (`stage/split`
when the boundary sits inside a mounted graph). The next `sync()` re-runs only
that boundary and rebuilds the child rows from it; a boundary that also
produces a stored parent column makes the whole graph run again for the row.

A partial row stays queryable and counts as stale in `status()`, never fresh.
The next `sync()` re-derives exactly the null columns: the surviving columns
keep their provenance stamps, so the column-scoped reconcile path reuses them
and the expensive earlier stages are not paid twice. A retry that fails again
keeps the columns the first attempt saved.

A partial row does not touch its child rows: the fan-out is not run, and child
rows written by an earlier generation are left exactly as they are — neither
re-derived nor deleted, so a child table can read fresh under a parent that is
not. The heal that completes the parent converges them.

Two failures under `on_error="store"` still write the whole-row `ERROR` shape
rather than a partial row: one raised by a routed (gate-selected) branch, and
one raised while deriving the columns that follow an answered interrupt.

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

A child identity must be unique within one parent row. The logical child key
is `(parent identity, child identity)`, so two mapped items that produce the
same identity under the same parent would occupy one child row and one item's
derived values would be lost. Such a write is refused:

```python
@node(output_name="words")
def split_words(text: str) -> list[dict]:
    return [{"word_id": word, "text": word} for word in text.split()]  # the word is the identity

# tag_word is any child graph over one word's text
table = Graph([split_words, tag_word.as_node().map_over("words", identity="word_id")]).as_table(
    identity="doc_id", store=SqliteTableStore()
)
table.insert(doc_id="d1", text="alpha beta alpha")
# DuplicateChildIdentityError: Child table 'word' got two items with word_id='alpha' under parent 'd1'.
```

`DuplicateChildIdentityError` is a `ValueError` exported from `hypergraph`; it
carries `table`, `identity`, `value` and `parent`. It is raised before any child
graph runs and before any child row of that parent is written, restamped or
retired, and every child table of the row is checked together, so a collision
in one leaves the others untouched. Under `on_error="raise"` the write raises
and nothing for that row is written. Under `on_error="store"` the row is stored
as an `ERROR` row whose `error` names the collision, `errors()` lists it, and
the other rows of the same call proceed. Every write that produces the item
list refuses it — `insert()`, `update()`, `sync()`, `rederive()`, answering a
waiting row, and a Materialization Branch's `sync()` (which raises). An item
without the identity field counts as the empty identity, so two such items
collide too. Identities are compared the way the child table keys them, as
text, so `1` and `"1"`, or `None` and `"None"`, collide as well. Derive the
identity from something unique per item — the loop index, a primary key — not
from content that can repeat.

A row stored by an earlier version over colliding items keeps its one child row
until a write re-runs its fan-out boundary; that write meets the refusal, so
under `on_error="store"` the row becomes an `ERROR` row rather than a `SKIPPED`
or `HEALED` one. Two exceptions apply to such a row. If its recorded count
already matches its present child rows, nothing re-runs the boundary and it
stays `SKIPPED`. If it also predates the recipe stamp, `sync()` refreshes that
stamp before it probes the children, which re-stamps the stored child rows and
retires an older duplicate copy of one; only then does the refusal store the
`ERROR` row, so on that path child rows are restamped and retired after all.

Each child table is named after its child identity (`word_id` → `word`), so two
fan-outs whose identities resolve to one table name are refused when the table
is first analyzed, with a `GraphConfigError` naming both fan-outs, and so is a
fan-out whose child table would take the root table's own name (`doc_id` →
`doc`, or the `name=` you passed) — its child rows would land among the root
rows. The name `recipe_journal` is reserved for the store's recipe journal, so
a root or child table that resolves to it (`recipe_journal_id`, or
`name="recipe_journal"`) is refused the same way. Give each child graph its own
identity.

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

```python
from hypergraph.materialization import SqliteTableStore, Table

store = SqliteTableStore("./data/host.db")  # stdlib sqlite3, no lancedb
quota = Table(identity="user_id", store=store)
quota.append(user_id="u1", used=0)

row = quota.get("u1")
won = quota.compare_and_set("u1", expected={"used": row["used"]}, used=row["used"] + 1)
store.close()
```

`compare_and_set(id, expected=..., **changes)` applies `changes` only when the
stored row still holds every `expected` value, and returns whether it did. The
store makes the comparison and the write one step, across threads and
processes.
