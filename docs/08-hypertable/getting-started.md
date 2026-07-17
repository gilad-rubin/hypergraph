# Getting started with HyperTable

Use HyperTable when a graph derives durable columns for identified entities.
Install the development or materialization dependencies, then import the
shipped store from the public package:

```python
from hypergraph.materialization import LanceDBStore
```

## Create a graph-backed table

```python
from hypergraph import Graph, node
from hypergraph.materialization import LanceDBStore, WriteOutcome


@node(output_name="clean_text")
def clean(text: str) -> str:
    return " ".join(text.lower().split())


@node(output_name="word_count")
def count_words(clean_text: str) -> int:
    return len(clean_text.split())


graph = Graph([clean, count_words], name="documents")
documents = graph.as_table(
    identity="document_id",
    store=LanceDBStore("./data/documents"),
)

first = documents.insert(document_id="d-1", text="  Hello table  ")
assert first.outcome is WriteOutcome.INSERTED
assert first.completed

same = documents.insert(document_id="d-1", text="  Hello table  ")
assert same.outcome is WriteOutcome.SKIPPED

changed = documents.update("d-1", text="Hello durable table")
assert changed.outcome is WriteOutcome.UPDATED
assert documents.get("d-1") == {
    "document_id": "d-1",
    "text": "Hello durable table",
    "clean_text": "hello durable table",
    "word_count": 3,
}
```

`SyncRunner()` is the default. Pass `runner=AsyncRunner()` when the graph is
asynchronous or contains interrupts; its write methods then return coroutines.

## Pause, inspect, and answer

The answer is an ordinary column. The question is the interrupt function's
return value.

```python
from dataclasses import dataclass
from typing import ClassVar

from hypergraph import AsyncRunner, Graph, interrupt, node
from hypergraph.materialization import LanceDBStore


@dataclass(frozen=True)
class Choice:
    answer_type: ClassVar[object] = str
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple[object, ...] = ()


@node(output_name="prepared")
def prepare(text: str) -> str:
    return text.strip().lower()


@interrupt(answer_name="decision")
def review(prepared: str) -> Choice:
    return Choice(
        prompt=f"File {prepared}?",
        options=("publish", "archive"),
        evidence=({"preview": prepared},),
    )


@node(output_name="filed")
def apply(prepared: str, decision: str) -> str:
    return f"{decision}:{prepared}"


intake = Graph([prepare, review, apply], name="intake")
uploads = intake.as_table(
    identity="upload_id",
    store=LanceDBStore("./data/uploads"),
    runner=AsyncRunner(),
)

receipt = await uploads.insert(upload_id="u-41", text=" Draft ")
assert receipt.paused
assert receipt.pause.value.prompt == "File draft?"
assert receipt.pause.response_key == "decision"

waiting = uploads.waiting()[0]
assert waiting.id == "u-41"
assert waiting.provenance
assert waiting.pause.value.answer_type == "builtins.str"

receipt = await uploads.update(
    waiting.id,
    **{waiting.pause.response_key: "publish"},
)
assert receipt.completed
assert uploads.get("u-41")["filed"] == "publish:draft"
```

The in-process receipt carries the original question object. A later
`waiting()` read rebuilds a frozen structural view with `prompt`, `options`,
`evidence`, and a display-safe string for `answer_type`.

The complete executable version is
[`examples/cold-boot.py`](examples/cold-boot.py).

## Supply answers headlessly

CSV and batch callers may already know an answer. Supplying the answer on
insert skips the interrupt handler and drives downstream derivation:

```python
receipt = await uploads.insert(
    upload_id="u-42",
    text="Ready",
    decision="archive",
)
assert receipt.completed
assert uploads.get("u-42")["filed"] == "archive:ready"
```

## Batch convergence

```python
receipt = documents.sync(
    [
        {"document_id": "d-1", "text": "updated"},
        {"document_id": "d-2", "text": "new"},
    ]
)

print(receipt.inserted, receipt.updated, receipt.skipped, receipt.deleted)
for row_receipt in receipt.waiting:
    print(row_receipt.id, row_receipt.pause.value.prompt)
for row_receipt in receipt.errors:
    print(row_receipt.id, row_receipt.error)
```

`sync()` inserts new identities, converges changed ones, skips fresh ones,
and deletes identities absent from the incoming collection. An unchanged
parent whose child rows were physically lost is self-repairing: `sync()`
compares each child table's recorded fan-out count against the child rows
physically present and rebuilds only the missing children, reporting the row
as `healed` instead of `skipped`.

## Stored errors

The default `on_error="raise"` is best for development. Opt into durable row
errors when sibling rows should continue:

```python
documents = graph.as_table(
    identity="document_id",
    store=LanceDBStore("./data/documents"),
    on_error="store",
)

receipt = documents.insert(document_id="bad", text="...")
if receipt.failed:
    print(receipt.error)

for failed in documents.errors():
    print(failed.id, failed.error, failed.row)
```

## Child grains

A mapped child graph gets one named handle:

```python
pages = documents.child("page")
pages.rows(parent="d-1")
pages.rows(where={"reviewed": False})
pages.set({"reviewed": False}, reviewed=True)
pages.delete({"page_id": "p-9"})
pages.count()
```

Each public child row exposes the parent's named identity column. Predicates
may reference either child columns or parent columns.

## Re-derive one column

```python
documents.rederive("clean_text")
documents.rederive("word_count", missing_only=True)
```

The first form derives the selected column for every row. The second only
fills rows where that column is missing.

## Related guides

- [Batch processing](../05-how-to/batch-processing.md)
- [Visualize graphs](../05-how-to/visualize-graphs.md)
- [Test without the framework](../05-how-to/test-without-framework.md)
