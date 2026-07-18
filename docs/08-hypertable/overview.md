# HyperTable

HyperTable is a Hypergraph graph with a durable table behind it. Graph inputs
become source columns, node outputs become derived columns, and each domain
entity has a stable identity.

```python
from hypergraph import Graph, node
from hypergraph.materialization import LanceDBStore


@node(output_name="normalized")
def normalize(text: str) -> str:
    return text.strip().lower()


pipeline = Graph([normalize], name="documents")
documents = pipeline.as_table(
    identity="document_id",
    store=LanceDBStore("./data/documents"),
)

receipt = documents.insert(document_id="d-1", text="  Hello  ")
assert receipt.completed
assert documents.get("d-1")["normalized"] == "hello"
```

The graph remains the compute artifact. `as_table()` adds identity, storage,
row convergence, and typed write receipts.

## Rows converge; runs resume

A checkpointer owns an execution. Resuming it re-enters that execution with
its saved state. HyperTable owns a row. Updating it starts derivation against
the row's stored facts and the current graph.

That distinction produces these rules:

- unchanged sources, code, and configuration are skipped;
- changed facts re-derive only affected columns;
- an answer remains valid while the provenance of its question is unchanged;
- changed upstream facts invalidate a stale answer and ask again;
- cycles and shared execution state belong to checkpointer-backed runs.

## Pauses live in the table

An interrupt is a node executed by a human. Its `answer_name` is a derived
answer column whose value arrives through `update()`.

```python
receipt = await uploads.insert(upload_id="u-41", path="/in/a.pdf")

if receipt.paused:
    show(receipt.pause.value)
    answer_key = receipt.pause.response_key
    receipt = await uploads.update("u-41", **{answer_key: "keep-both"})

assert receipt.completed
```

A paused derivation is persisted as a waiting row carrying the question
envelope. `waiting()` rebuilds the same `PauseInfo` shape used by runner
results, so serving code reads `paused`, `pause.value`, and
`pause.response_key` identically in both modes.

Public row reads contain data columns only. Use receipts, `waiting()`, and
`errors()` for derivation state.

## Choose the right durable noun

| Need | Use |
|---|---|
| A graph-derived entity table | `graph.as_table(...)` |
| Another complete recipe over the same source rows | `table.attach(...)` |
| A durable table with no derivation | `Table(...)` and `append(...)` |
| Resume a particular execution | a runner with a checkpointer |
| Inspect work awaiting a human | `table.waiting()` |

## What is stored

HyperTable stores source columns, derived columns, answer columns, metadata,
and internal provenance. Internal generations, recipe stamps, status, and the
question envelope never appear as stringly fields in `get()` or `rows()`.

The shipped `LanceDBStore` is available from
`hypergraph.materialization`. Custom stores implement `TableStore` and can be
checked with `check_store_conformance`.

## Next

- [Getting started](getting-started.md)
- [API reference](api-reference.md)
- [Implementing a store](implementing-a-store.md)
- [Human-in-the-loop](../03-patterns/07-human-in-the-loop.md)
