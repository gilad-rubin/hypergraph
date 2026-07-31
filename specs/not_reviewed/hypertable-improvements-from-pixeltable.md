# HyperTable Improvements — Lessons from Pixeltable

Status: implemented — child fingerprints and on_error policy shipped in PRD 0005 (June 2026)

## Context

A deep comparison of HyperTable and Pixeltable surfaced areas where Pixeltable's model is more production-ready. This spec proposes targeted improvements that fit HyperTable's existing design (graph-native, store-agnostic, write-generation model).

**What we're NOT doing:** Pixeltable is a Postgres-backed system with its own query engine, UDF registry, and type system. HyperTable is a thin persistence layer over Hypergraph graphs + pluggable stores. We want the *behaviors* that matter for production pipelines, not the machinery.

**Key constraints (from Codex review):**
- `runner.run()` is atomic — all nodes succeed or it throws. Per-column error granularity is impossible without runner changes.
- LanceDB appends rows, doesn't overwrite — two-phase writes with same `_write_gen` create unresolvable ties in `_dedup_rows()`.
- Improvements must work at the `TableStore` protocol level, not just LanceDB.

**Validated against Panda** (the primary production consumer):
- Panda stores source documents separately — source data is never at risk from HyperTable failures.
- Panda is migrating from an external state machine (`WorkItem` status tracking) to a **Hypergraph lifecycle graph** with interrupt/resume. `WorkItem` will become a view over runner state.
- Panda processes one document at a time through the lifecycle graph, not batch sync.
- New HyperTable methods (`filter_children`, `set_children`, `delete_children`) already landed on `feat/hypertable`.

---

## Two Levels of Error Handling

Panda's migration reveals that errors surface at two distinct levels:

### Level 1: Lifecycle graph (document-level)

The ingestion lifecycle graph wraps `derive_staged_pages` (which calls `protocols.insert()`). If the whole insert fails, the lifecycle graph handles it — the runner reports the error, the work item shows "failed", and the operator can retry via `/work-items/{id}/retry`.

**This level is already handled** by Hypergraph's runner error reporting + checkpoint/resume. No HyperTable changes needed.

### Level 2: HyperTable insert (page-level, within map_over)

Within a single `protocols.insert()`, the parent graph runs, then `map_over` processes each page through a subgraph (vision LLM, enrichment LLM, embedding). If page 5 of 20 fails (e.g., vision LLM rate-limited), today the entire insert fails — all 20 pages are lost, including the 19 that succeeded.

**This is where `on_error` matters.** The question: should HyperTable store the 19 successful pages and record an error for page 5? Or should the whole insert fail atomically?

### The Answer Depends on the Use Case

| Scenario | Right behavior |
|----------|---------------|
| **Small documents (Panda today)** | Atomic is fine — retry the whole document, it's cheap |
| **Large documents (100+ pages)** | Partial is better — re-processing 99 succeeded pages wastes LLM calls |
| **Batch sync of many documents** | Partial is essential — one bad document shouldn't block 999 good ones |

**Recommendation:** Make `on_error` a policy knob. Default to `"raise"` (today's behavior). Add `"store"` for production pipelines that need resilience.

---

## Improvement 1: Row-Level Error Data Model

### What

Two new internal columns on every HyperTable (parent and child):

| Column | Type | Values |
|--------|------|--------|
| `_status` | `pa.utf8()` | `"complete"` \| `"error"` |
| `_error` | `pa.utf8()` | `None` (success) or `"{ExceptionType}: {message}"` |

### Why Row-Level, Not Per-Column

- `runner.run()` is atomic — we track what we actually know: "this row's derivation succeeded or failed"
- Storing the same error string on N `_error_{col}` columns is dishonest (Codex round 1)
- Per-column errors require runner-level partial-result reporting (V2 concern)

### Reserved Name Validation

Reject any column (identity, source, or derived) starting with `_`:

```python
# In _analyze_graph(), for ALL column types:
for col in all_columns:
    if col.name.startswith("_"):
        raise SchemaError(
            f"Column name '{col.name}' is reserved (starts with '_'). "
            f"Rename the node output or input."
        )
```

**Why all columns, not just derived:** A source input named `_status` would collide with the internal status column and be hidden by `_public_row()`'s `not k.startswith("_")` filter (Codex round 2).

### Public Row Filtering

```python
def _public_row(self, row: dict, include_status: bool = False) -> dict:
    result = {k: v for k, v in row.items() if not k.startswith("_")}
    if include_status:
        result["_status"] = row.get("_status", "complete")
        result["_error"] = row.get("_error")
    return result
```

`include_status=True` available on `get()`, `filter()`, `children()`, `filter_children()`.

### Schema Addition

Add `_status` and `_error` to `TableSpec` generation alongside existing internal columns (`_write_gen`, `_row_fingerprint`, `_provenance_*`).

For existing tables: the store's `open()` path must reconcile missing internal columns. This requires `evolve_schema()` to be idempotent first (Improvement 3).

### How Panda Benefits

```python
# Today: external error tracking in WorkItem.needs
try:
    outcome = await self._processor(item, source)
except Exception as exc:
    item.status = "failed"
    item.needs = [f"Ingestion failed: {exc}"]

# After: HyperTable tracks it; lifecycle graph can query
errored_docs = protocols.filter(
    where=[("_status", "eq", "error")],
    include_status=True,
)
```

The lifecycle graph's `build_publish_blockers` node could check HyperTable status directly instead of relying on external state.

---

## Improvement 2: `on_error` Policy

### What

```python
# Default (backward compatible)
table = HyperTable([...], identity="doc_id", store=store, on_error="raise")

# Production pipelines
table = HyperTable([...], identity="doc_id", store=store, on_error="store")
```

| Policy | Parent insert failure | Child (map_over) failure | Row stored? |
|--------|----------------------|--------------------------|-------------|
| `"raise"` | Exception propagates, nothing stored | Exception propagates, nothing stored | No |
| `"store"` | Error row stored (source + `_status="error"`) | Successful children stored, failed children get error rows | Yes (partial) |

### Implementation: Single-Write Error Row (No Two-Phase)

Codex identified that two-phase writes (write source first, then overwrite with final) are broken with LanceDB's append model. Instead: **one write, one outcome.**

```python
def _insert_one(self, item, write_gen):
    graph_inputs = self._extract_graph_inputs(item)
    identity_value = item[self._identity]

    # Fingerprint check (unchanged)
    existing = self._store.read_one(...)
    if existing and existing.get("_row_fingerprint") == self._compute_row_fingerprint(...):
        if existing.get("_status") == "complete":
            return "skipped"
        # Error row with same fingerprint → retry derivation (inputs unchanged, but previous run failed)

    try:
        result = self._runner.run(self._graph, **graph_inputs)
        outputs = self._extract_outputs(result)
        row = self._build_row(item, graph_inputs, outputs, write_gen)
        row["_status"] = "complete"
        row["_error"] = None
    except Exception as e:
        if self._on_error == "raise":
            raise
        # on_error="store": write error row
        row = self._build_source_only_row(item, write_gen)
        row["_status"] = "error"
        row["_error"] = f"{type(e).__name__}: {e}"

    self._store.write_rows(self._spec.name, [row])
    if existing is not None:
        self._store.delete_rows(self._spec.name, [
            (self._identity, "eq", identity_value),
            ("_write_gen", "lt", write_gen),
        ])

    # Children: only if complete
    if row["_status"] == "complete":
        for child_spec in self._spec.children:
            self._insert_children(identity_value, outputs, child_spec, write_gen)

    return row["_status"]
```

**Key: `_build_source_only_row()`** — new method that builds a row with identity + source columns + metadata + internal columns, derived columns as None. No graph execution needed.

### Fingerprint Behavior for Error Rows

Error rows still get a fingerprint (computed from source inputs + node defs + component configs). On next `sync()` or manual re-insert:

- **Same fingerprint + `_status="complete"`** → skip (unchanged, already derived)
- **Same fingerprint + `_status="error"`** → retry derivation (same inputs, but previous attempt failed)
- **Different fingerprint** → re-derive (inputs or graph changed)

This solves the Codex round 2 finding that sync() would skip error rows.

### Child Error Handling (map_over)

When `on_error="store"`, child insertion wraps each child's subgraph execution:

```python
def _insert_children(self, parent_id, outputs, child_spec, write_gen):
    child_items = outputs.get(child_spec.map_input)
    if not child_items:
        return

    for child_item in child_items:
        try:
            child_result = self._runner.run(child_graph, **child_inputs)
            child_row = self._build_child_row(child_item, child_result, parent_id, write_gen)
            child_row["_status"] = "complete"
            child_row["_error"] = None
        except Exception as e:
            if self._on_error == "raise":
                raise
            child_row = self._build_child_source_only_row(child_item, parent_id, write_gen)
            child_row["_status"] = "error"
            child_row["_error"] = f"{type(e).__name__}: {e}"

        self._store.write_rows(child_spec.name, [child_row])
```

**This is the high-value case:** 19 of 20 pages indexed, 1 failed. The operator sees which page failed and can retry.

### `SyncResult` Enhancement

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

Uses the existing `ErrorRow` type from `_types.py`.

### Preserving `on_error` Across Copies

`bind()` and `with_runner()` create new `HyperTable` instances. They must propagate `_on_error`:

```python
def bind(self, **components):
    return HyperTable(..., on_error=self._on_error).bind(**merged_components)
```

---

## Improvement 3: Idempotent Schema Evolution

### Problem

`evolve_schema()` in LanceDBStore drops and recreates the table when adding columns. This must be idempotent (no-op for existing same-type columns) because:
1. `_status`/`_error` columns need to be added to existing tables on first open with new code
2. The open/resolve path must reconcile missing internal columns automatically

### Implementation (TableStore Protocol Level)

Add to the `TableStore` protocol contract:

```python
class TableStore(ABC):
    @abstractmethod
    def evolve_schema(self, table_name: str, new_columns: dict[str, pa.DataType]) -> list[str]:
        """Add columns to an existing table.

        - If column exists with same type: no-op (idempotent)
        - If column exists with different type: raise SchemaError
        - If column is new: add with None default
        """
```

### Open-Path Reconciliation

In `_resolve_store()`, after opening the store, check that internal columns exist:

```python
def _resolve_store(self):
    # ... existing open logic ...
    stored_columns = self._store.open(self._spec, ...)

    # Reconcile internal columns
    required_internal = {"_status": pa.utf8(), "_error": pa.utf8()}
    missing = {k: v for k, v in required_internal.items()
               if k not in stored_columns.get(self._spec.name, [])}
    if missing:
        self._store.evolve_schema(self._spec.name, missing)
```

This runs on every open but is a no-op once columns exist (idempotent).

---

## Improvement 4: Convenience CRUD Helpers

### `exists()` and `get_many()`

```python
def exists(self, identity_value: str) -> bool:
    return self._store.read_one(
        self._spec.name, self._identity, identity_value
    ) is not None

def get_many(
    self, identity_values: list[str], *, include_status: bool = False
) -> list[dict | None]:
    return [
        self.get(iv, include_status=include_status)
        for iv in identity_values
    ]
```

Trivial, no design risk. `get_many()` preserves input order, returns `None` for missing IDs.

---

## Priority & Sequencing

| Order | Improvement | Effort | Prerequisite |
|-------|------------|--------|-------------|
| 1 | **Idempotent schema evolution** (#3) | S | None — foundation for everything else |
| 2 | **Row-level error model** (#1) | S | #3 (need idempotent evolve to add internal columns) |
| 3 | **`on_error` policy** (#2) | M | #1 (need `_status`/`_error` columns to store into) |
| 4 | **CRUD helpers** (#4) | XS | None |

Total: ~1 week of focused work. No runner-level changes needed.

---

## Explicitly Deferred to V2

| Feature | Why Deferred | Prerequisite |
|---------|-------------|-------------|
| **Per-column error tracking** | `runner.run()` is atomic; need runner-level partial-result reporting | Runner V2 (ExecutionContext, per-node error capture) |
| **`invalidate_columns()`** | Needs column dependency graph + cascade invalidation + scoped re-derivation | Per-column errors + provenance-based dependency tracking |
| **Two-phase write (source-first)** | LanceDB append model creates dedup ties; single-write error row is simpler | Store-level upsert support or explicit dedup tie-breaking |
| **Async `on_error`** | Panda uses AsyncRunner; `on_error` sketch is sync-only | AsyncRunner parity for error handling path |

---

## What We're Deliberately NOT Adopting from Pixeltable

| Pixeltable Feature | Why Not |
|-------------------|---------|
| **Per-cell error columns** | Graph execution is atomic — row-level is honest |
| **Postgres backend** | HyperTable is store-agnostic (LanceDB, Azure AI Search, local JSON) |
| **Iterator-based views** | `map_over` already handles 1:N |
| **`add_computed_column()` at runtime** | Columns defined by graph structure — add a node |
| **Time-travel versioning** | Application-level versioning (Panda's manifest model) is sufficient |
| **UDF registry / `@pxt.udf`** | Nodes are plain functions |
| **`add_embedding_index()`** | LanceDB/Azure AI Search handle indexing |

---

## How Each Improvement Maps to Panda

### Improvement 1 (Error Model) → Lifecycle graph queries HyperTable status

```python
# In build_publish_blockers node:
@node
def build_publish_blockers(resolved_candidate, ingestion_result, workflow_ops):
    blockers = workflow_ops.build_publish_blockers(candidate, result)

    # NEW: check for page-level errors in HyperTable
    errored_pages = workflow_ops.get_errored_pages(candidate)
    if errored_pages:
        blockers.append(PublishBlocker(
            kind="page-errors",
            message=f"{len(errored_pages)} pages failed ingestion"
        ))
    return blockers
```

### Improvement 2 (`on_error`) → Partial page indexing

```python
# In protocols_table.py:
table = HyperTable(
    [...],
    identity="doc_version_id",
    store=store,
    on_error="store",  # ← page 5 fails, pages 1-4 and 6-20 still indexed
).bind(...).with_runner(AsyncRunner())
```

The lifecycle graph's `derive_staged_pages` node no longer needs try/except for the HyperTable call — partial success is handled internally. The `build_publish_blockers` node can check how many pages errored and decide whether to block publication.

### Improvement 3 (Schema Evolution) → Smooth upgrades

When Panda deploys new HyperTable code with `_status`/`_error` columns, existing LanceDB tables automatically gain these columns on next open. No migration script needed.

### Improvement 4 (CRUD) → Cleaner KnowledgeBase code

```python
# Today:
if protocols.get(parent_id) is not None:
    ...

# After:
if protocols.exists(parent_id):
    ...
```

---

## Appendix: Review History

### Codex Round 1 — Key Findings
1. Foundational inversion: error columns before schema evolution
2. Per-column errors dishonest when graph execution is atomic
3. `retry_errors()` duplicates sync()/recompute()
4. `on_error="skip"` → silent data loss
5. `invalidate_columns()` needs cascade logic
6. Query enhancements are scope creep
7. Reserved name collisions (only checked derived, not source/identity)
8. Crash safety gaps in two-phase write

### Codex Round 2 — Key Findings
1. Open/resolve path doesn't reconcile missing columns
2. `_status="error", _error=None` contradiction in crash scenario
3. Fingerprint match would skip error rows (sync wouldn't retry)
4. `recompute()` could mark row "complete" with other columns still NULL
5. `_build_source_row()` doesn't exist in codebase
6. Child table errors completely undesigned
7. Same `_write_gen` creates unresolvable dedup ties in LanceDB

### Panda Validation — Key Findings
1. Source data stored separately — partial row storage less critical than assumed
2. Lifecycle graph handles document-level errors — `on_error` matters at page level (child map_over)
3. No sync() usage — batch error handling less urgent
4. Provider-dispatched stores — fixes must be protocol-level, not LanceDB-specific
5. WorkItem becoming a view over runner state — HyperTable error model feeds into this
6. Operator-initiated retry — no automatic retry needed in HyperTable
