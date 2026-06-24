# PRD 0005 — HyperTable child fingerprints and error policy

status: ready-for-agent

## Problem Statement

HyperTable inserts are fragile in two ways that block production use:

**1. Children are never skipped on re-insert.** The row fingerprint mechanism works
for parent rows — if the source content and graph definition haven't changed, the
row is skipped. But child rows (created at grain boundaries via `map_over`) have
their `_row_fingerprint` hardcoded to `""`. Every re-insert re-runs every child's
subgraph, even when nothing changed. For a 100-page document, retrying after a
crash on page 95 re-processes all 100 pages instead of skipping the 94 that
already succeeded.

**2. One child failure loses all children.** When `map_over` processes N children
and child K fails (rate limit, transient LLM error, malformed input), the
exception propagates and the entire parent insert fails. The N-1 successfully
derived children are discarded. The user must retry the whole document, re-running
all children including the ones that already succeeded.

These are orthogonal problems — fingerprints are about *skipping unchanged work*,
error policy is about *surviving failures* — but they compose: with both fixed,
a retry after a partial failure skips already-complete children and only re-attempts
the failed ones.

## Solution

Two layers, delivered together because they share the same insert code paths:

**Layer 1 — Child fingerprints.** Compute real fingerprints for child rows using
the child's source inputs, child graph node hashes, and component config hashes.
Before running a child's subgraph, check if a child row with matching
`(_parent_id, child_identity, _row_fingerprint)` already exists. If it does and
its `_status` is `"complete"`, skip it. This makes insert naturally resumable —
crash at child 50 of 100, re-insert, and only children 51-100 run.

**Layer 2 — `on_error` policy.** Add an `on_error` parameter to HyperTable
(`"raise"` or `"store"`). When `"store"`, a failed child (or parent) writes an
error row with `_status="error"` and `_error="{ExceptionType}: {message}"`. Source
columns and identity are preserved; derived columns are None. Successful siblings
are unaffected. On the next insert/sync, error rows with matching fingerprints are
retried (not skipped), while complete rows with matching fingerprints are skipped.

Default is `"raise"` — identical to today's behavior. No existing code changes
unless `on_error="store"` is explicitly set.

## User Stories

1. As a developer, I want child rows to have real fingerprints, so that re-inserting
   a parent doesn't re-process children whose inputs and graph haven't changed.

2. As a developer, I want a crashed insert to resume where it left off, so that
   retrying a 100-page document only processes the pages that weren't stored yet.

3. As a developer, I want to set `on_error="store"` on my HyperTable, so that one
   failing child doesn't discard the work done for all the other children.

4. As a developer, I want error rows to record the exception type and message, so
   that I can diagnose which children failed and why.

5. As a developer, I want error rows to be retried on the next insert (not skipped),
   so that transient failures are automatically recovered without manual intervention.

6. As a developer, I want successful children to be skipped on retry even when
   `on_error="store"` is set, so that only the failed children are re-processed.

7. As a developer, I want `on_error="raise"` to be the default, so that existing
   code behaves exactly as before.

8. As a developer, I want to query error rows via `children(parent_id,
   include_status=True)`, so that I can see which children failed after a partial
   insert.

9. As a developer, I want `SyncResult` to include `ErrorRow` details, so that
   `sync()` callers can programmatically inspect which items errored.

10. As a developer, I want parent-level errors stored the same way (source +
    identity + `_status="error"`), so that the error model is consistent across
    parent and child tables.

11. As a developer, I want the `on_error` policy to work with both `SyncRunner` and
    `AsyncRunner`, so that async pipelines get the same resilience.

12. As a developer, I want column names starting with `_` to be rejected at
    construction time, so that user columns never collide with internal columns
    (`_status`, `_error`, `_write_gen`, etc.).

13. As a developer, I want `evolve_schema()` to be idempotent (no-op for existing
    same-type columns), so that reopening a table with new internal columns doesn't
    fail or corrupt data.

14. As a developer, I want the `on_error` policy to propagate through `bind()` and
    `with_runner()`, so that table copies retain the error policy.

15. As a developer, I want `get(identity, include_status=True)` to expose `_status`
    and `_error` on the returned row, so that I can check whether a specific row
    succeeded or failed.

16. As a developer, I want `filter(where=[("_status", "eq", "error")],
    include_status=True)` to find all error rows, so that I can build monitoring
    or retry logic.

## Implementation Decisions

### Child fingerprint computation

Child fingerprints use the same algorithm as parent fingerprints: `sha256(source
inputs + node definition hashes + component config hashes)`. The inputs are the
child's source column values (from the child item dict), the nodes are from the
child subgraph, and the components are the same bound components (filtered to
those the child graph accepts).

### Child fingerprint check flow

Before running a child's subgraph:
1. Read the existing child row by `(_parent_id, child_identity)`.
2. Compute the new fingerprint from the child's source inputs + child graph.
3. If existing row has matching fingerprint AND `_status="complete"` → skip.
4. If existing row has matching fingerprint AND `_status="error"` → re-run
   (same inputs, previous attempt failed).
5. If no existing row or different fingerprint → run the child subgraph.

### `_status` and `_error` internal columns

Two new internal columns on every table (parent and child):

| Column | Type | Values |
|--------|------|--------|
| `_status` | `str` | `"complete"` or `"error"` |
| `_error` | `str` or `None` | `None` (success) or `"{ExceptionType}: {message}"` |

These are added alongside existing internal columns (`_write_gen`,
`_row_fingerprint`, `_provenance_*`) in the table spec generation. For existing
tables, they are reconciled on open via idempotent `evolve_schema()`.

### `on_error` parameter

Constructor parameter on `HyperTable`. Literal type: `"raise" | "store"`.
Default: `"raise"`. Stored as `self._on_error`. Propagated through `bind()` and
`with_runner()`.

### Error row structure

When `on_error="store"` and derivation fails:
- Identity column: preserved
- Source columns: preserved (from the input item)
- Derived columns: `None`
- `_row_fingerprint`: computed normally (from source inputs + graph definition)
- `_status`: `"error"`
- `_error`: `"{ExceptionType}: {message}"`
- `_write_gen`: current write generation
- Provenance columns: `None`

### Reserved name validation

At graph analysis time (lazy, on first use), reject any column whose name starts
with `_`. This applies to identity columns, source columns (graph inputs), and
derived columns (node outputs). The check runs before the store is opened,
producing a clear `SchemaError`.

### Idempotent `evolve_schema()`

The `TableStore.evolve_schema()` contract gains an idempotency requirement:
- Column exists with same type → no-op
- Column exists with different type → raise `SchemaError`
- Column is new → add with `None` default

This is a contract clarification, not a new method. The existing
`LanceDBStore.evolve_schema()` implementation needs to check for existing columns
before dropping and recreating the table.

The open path calls `evolve_schema()` for any missing internal columns after
`store.open()` returns the current column list. This runs on every open but is a
no-op once columns exist.

### `SyncResult` enhancement

`SyncResult` gains an `errors` field:

```python
@dataclass(frozen=True)
class SyncResult:
    inserted: int
    updated: int
    deleted: int
    skipped: int
    errored: int
    errors: tuple[ErrorRow, ...] = ()
```

### `_public_row` enhancement

`_public_row()` gains an `include_status` parameter. When `True`, the `_status`
and `_error` fields are included in the returned dict. Available on `get()`,
`children()`, `filter()`, `filter_children()`.

### Async parity

Both `_insert_one` / `_insert_one_async` and `_insert_children` /
`_insert_children_async` get the same fingerprint and on_error logic. The
patterns are identical — the only difference is `await` on runner calls.

## Testing Decisions

### What makes a good test

Tests verify behavior through the public API: `insert()`, `get()`, `children()`,
`filter()`, `sync()`, `count()`. No tests reach into `_insert_children` or
`_compute_row_fingerprint` directly. The observable outcomes are: which rows exist
in the store, what their `_status`/`_error` values are, and whether the subgraph
ran (observable via side effects like call counts on components).

### Prior art

Existing HyperTable test files provide the patterns:
- `test_hypertable_mutations.py` — insert/update/sync through public API
- `test_hypertable_child_ordering.py` — crash-safety with a `MemoryStore`
- `test_hypertable_async.py` — async variants of mutation tests
- `test_hypertable_construction.py` — schema validation at construction time

### Test areas

**Child fingerprints:**
- Insert parent with children → re-insert same parent → children skipped (count unchanged, no re-derivation)
- Insert parent with children → change one child's source input → only that child re-derives
- Insert parent with children → change component config → all children re-derive (fingerprint includes component hashes)
- Insert parent with children → crash mid-insert (simulated) → re-insert → already-stored children skipped, remaining children processed

**`on_error="store"` — child level:**
- Child subgraph fails → error row stored with `_status="error"` and `_error` message
- Sibling children succeed → their rows stored with `_status="complete"`
- Re-insert same parent → error child retried, complete children skipped
- Error child succeeds on retry → row updated to `_status="complete"`

**`on_error="store"` — parent level:**
- Parent graph fails → error row stored with source columns, derived columns None
- Re-insert same parent → error row retried (same fingerprint, error status)

**`on_error="raise"` (default):**
- Child subgraph fails → exception propagates, no rows stored (backward compat)
- Parent graph fails → exception propagates (backward compat)

**Reserved name validation:**
- Column named `_status` → `SchemaError` at construction
- Column named `_anything` → `SchemaError` at construction
- Applies to identity, source, and derived columns

**Idempotent `evolve_schema()`:**
- Call with existing same-type column → no-op, returns current columns
- Call with new column → column added
- Call with existing different-type column → `SchemaError`

**`SyncResult` errors:**
- `sync()` with `on_error="store"` → `SyncResult.errored` counts, `SyncResult.errors` populated
- `sync()` with `on_error="raise"` → exception on first failure

**`include_status` on reads:**
- `get(id, include_status=True)` → `_status` and `_error` in returned dict
- `children(parent_id, include_status=True)` → status on child rows
- `filter(where=..., include_status=True)` → status on filtered rows
- Without `include_status` → `_status` and `_error` stripped (backward compat)

**Async parity:**
- Same test scenarios as sync, but with `AsyncRunner`

## Out of Scope

- **Per-column error tracking.** `runner.run()` is atomic — we know "row succeeded
  or failed", not "which column failed". Per-column errors require runner-level
  partial-result reporting (Runner V2).

- **Automatic retry with backoff.** `on_error="store"` records the failure but does
  not retry automatically. Retry is triggered by the next `insert()` or `sync()`
  call. Application-level retry (lifecycle graph, external scheduler) handles
  backoff policy.

- **`invalidate_columns()`.** Column-scoped re-derivation requires per-column
  dependency graphs and cascade logic. Deferred to a future PRD.

- **Two-phase write (source-first, then final).** LanceDB's append model creates
  unresolvable dedup ties with same `_write_gen`. Single-write with error status
  is simpler and correct.

- **New `TableStore` implementations.** The protocol contract changes
  (`evolve_schema` idempotency) apply to all stores, but only `LanceDBStore` is
  updated in this PRD. External store implementations must update independently.

- **UI/API changes.** `_status`/`_error` are internal columns exposed via
  `include_status=True` on read methods. No new API endpoints or UI components.

## Further Notes

### Interaction with `sync()`

`sync()` iterates items and calls `_insert_one` per item. With `on_error="store"`,
a failed item writes an error row and `sync()` continues to the next item. The
returned `SyncResult` includes `errored` count and `errors` tuple. With
`on_error="raise"`, the first failure propagates and `sync()` stops (today's
behavior).

### Interaction with `recompute()` and `backfill()`

`recompute()` re-derives a single column for all rows. Error rows (where the
original derivation failed) are skipped by `recompute()` — re-derivation requires
all upstream columns, which error rows don't have. To retry error rows, use
`insert()` or `sync()`.

`backfill()` populates a new column for rows where it's `None`. Error rows have
all derived columns as `None`, but `backfill()` should skip them (the missing
values are due to failure, not a schema addition). The skip condition is:
`_status == "error"` → skip.

### Composition with lifecycle graph (Panda)

Panda's ingestion lifecycle graph wraps `protocols.insert()` in a
`derive_staged_pages` node. With `on_error="store"`, partial page-level failures
are handled inside HyperTable. The lifecycle graph's `build_publish_blockers` node
can query `filter_children(where=[("_status", "eq", "error")],
include_status=True)` to detect page-level failures and block publication.

### Migration path for existing tables

Existing tables lack `_status` and `_error` columns. On first open with the new
code, `evolve_schema()` adds them with `None` defaults. Existing rows are treated
as `_status="complete"` (the `_public_row` helper defaults missing `_status` to
`"complete"`). No data migration needed.
