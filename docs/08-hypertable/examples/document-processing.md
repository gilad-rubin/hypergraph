# Document Indexing Pipeline

Incremental document processing with HyperTable. Extract text from documents, split into pages, clean and embed each page, store everything. When you re-run, only changed documents re-process. Failed pages don't block their siblings.

## When to Use

- Building a knowledge base or search index from a document collection
- Processing PDFs, articles, or any content that splits into sub-parts
- Pipelines you re-run frequently and want to skip unchanged work
- Incremental ingestion where new documents insert and stale ones are removed

## The Pipeline

```text
document --> extract_text --> split_pages --> [per page: clean --> embed]
```

One graph handles the document level (extract, split). A child graph handles each page (clean, embed). HyperTable connects them, stores every column, and tracks what changed.

## Complete Implementation

```python
from typing import TypedDict

from hypergraph import Graph, node
from hypergraph.materialization import HyperTable
from hypergraph.materialization.stores import LanceDBStore
from hypergraph.runners import SyncRunner


# ═══════════════════════════════════════════════════════════════
# COMPONENTS
# ═══════════════════════════════════════════════════════════════

class Embedder:
    """Wraps an embedding model. _config() lets HyperTable detect swaps."""

    def __init__(self, model_name: str = "text-embedding-3-small", dim: int = 256):
        self.model_name = model_name
        self.dim = dim

    def _config(self):
        return {"model": self.model_name, "dim": self.dim}

    def embed(self, text: str) -> list[float]:
        from openai import OpenAI
        client = OpenAI()
        response = client.embeddings.create(
            model=self.model_name,
            input=text,
            dimensions=self.dim,
        )
        return response.data[0].embedding


# ═══════════════════════════════════════════════════════════════
# DOCUMENT-LEVEL NODES
# ═══════════════════════════════════════════════════════════════

@node(output_name="raw_text")
def extract_text(content: str) -> str:
    """
    Extract plain text from document content.
    In production, use a PDF parser (pymupdf, unstructured, etc.).
    """
    # Pseudocode — swap in your real extractor
    return content.strip()


class Page(TypedDict):
    page_id: str
    text: str
    page_number: int


@node(output_name="pages")
def split_pages(raw_text: str) -> list[Page]:
    """
    Split document text into pages/chunks.
    Each page gets a stable ID for incremental tracking.
    """
    paragraphs = [p.strip() for p in raw_text.split("\n\n") if p.strip()]
    return [
        Page(page_id=f"p{i}", text=para, page_number=i)
        for i, para in enumerate(paragraphs)
    ]


# ═══════════════════════════════════════════════════════════════
# PAGE-LEVEL NODES (child graph)
# ═══════════════════════════════════════════════════════════════

@node(output_name="clean_text")
def clean(text: str) -> str:
    """Normalize whitespace, strip control characters."""
    return " ".join(text.split()).strip()


@node(output_name="vector")
def embed_page(clean_text: str, embedder: Embedder) -> list[float]:
    """Embed the cleaned page text."""
    return embedder.embed(clean_text)


# The child graph processes one page: clean it, then embed it.
process_page = Graph([clean, embed_page], name="process_page")


# ═══════════════════════════════════════════════════════════════
# DECLARE THE TABLE
# ═══════════════════════════════════════════════════════════════

embedder = Embedder()
store = LanceDBStore("./index_store")

documents = (
    HyperTable(
        [
            extract_text,
            split_pages,
            process_page.as_node().map_over("pages", identity="page_id"),
        ],
        identity="doc_id",
        store=store,
        on_error="store",
    )
    .bind(embedder=embedder)
    .with_runner(SyncRunner())
)
```

The declaration reads top to bottom like the pipeline diagram. `extract_text` and `split_pages` produce columns on the document table. `process_page.as_node().map_over("pages", identity="page_id")` creates a child table with one row per page, where each row has the child graph's derived columns (`clean_text`, `vector`).

`on_error="store"` means a page that fails (bad content, embedding timeout) gets an error row instead of crashing the whole batch. The `embedder` is bound as a component — its config hash is included in the row fingerprint, so swapping it triggers re-derivation.

## Processing a Batch

```python
# First run: process a collection of documents
batch = [
    {"doc_id": "rfc-2616", "content": "HTTP/1.1 defines...\n\nRequest methods include..."},
    {"doc_id": "rfc-7231", "content": "Semantics and content...\n\nStatus codes indicate..."},
    {"doc_id": "rfc-9110", "content": "HTTP semantics...\n\nFields convey metadata...\n\nRepresentations describe..."},
]

result = documents.sync(batch)
print(result)
# SyncResult(inserted=3, updated=0, deleted=0, skipped=0, errored=0, errors=())
```

`sync()` is the main entry point for batch processing. It compares the incoming list against what's already stored and does the minimum work: insert new documents, update changed ones, delete removed ones, skip unchanged ones.

## Reading Results

```python
# Get a single document with all its derived columns
doc = documents.get("rfc-9110")
print(doc["raw_text"][:60])    # "HTTP semantics Fields convey..."
print(doc["doc_id"])            # "rfc-9110"

# Get all pages for a document
pages = documents.children("rfc-9110")
print(len(pages))               # 3 (one per paragraph)
print(pages[0]["clean_text"])   # cleaned first paragraph
print(len(pages[0]["vector"]))  # 256 (embedding dimension)

# Find pages across all documents
all_pages = documents.filter_children(
    where=[("page_number", "eq", 0)],
)
print(len(all_pages))  # 3 — first page of each document
```

`get()` returns the document row. `children()` returns its page rows. `filter_children()` queries across all pages regardless of parent. The `_parent_id` field on each child links back to the document.

## What Happens on Re-Run

```python
# Second run: one document changed, one is new, one was removed
updated_batch = [
    {"doc_id": "rfc-2616", "content": "HTTP/1.1 defines...\n\nRequest methods include..."},  # unchanged
    {"doc_id": "rfc-7231", "content": "UPDATED content...\n\nNew status codes..."},           # changed
    {"doc_id": "rfc-9114", "content": "HTTP/3 uses QUIC...\n\nStreams multiplex..."},          # new
    # rfc-9110 is missing — will be deleted
]

result = documents.sync(updated_batch)
print(result)
# SyncResult(inserted=1, updated=1, deleted=1, skipped=1, errored=0, errors=())
```

What happened:

- **rfc-2616** — content unchanged, row fingerprint matches. Skipped entirely, no graph execution.
- **rfc-7231** — content changed, fingerprint mismatch. The document re-runs through `extract_text` and `split_pages`. Each page is checked individually: if a page's text didn't change, its child row is skipped too.
- **rfc-9114** — new document. Inserted and fully processed.
- **rfc-9110** — not in the incoming batch. Deleted along with all its child page rows.

The fingerprint includes the source column values (`content`), all node definition hashes, and all component config hashes. Change any of these and the row re-derives.

## Handling Failures

With `on_error="store"`, a failing page writes an error row instead of crashing. Its siblings still process normally.

```python
# A document with one malformed page
bad_batch = [
    {"doc_id": "corrupted", "content": "Good paragraph here.\n\n\x00\x01\x02"},
]
result = documents.sync(bad_batch)
print(result.errored)  # 1 (if the embedder chokes on control characters)
print(result.errors[0].error_msg)  # "ValueError: invalid text for embedding"

# The good page succeeded, the bad one has an error row
pages = documents.children("corrupted", include_status=True)
for page in pages:
    print(f"{page['page_id']}: {page['_status']}")
# p0: complete
# p1: error

# Query all error pages across the entire table
errors = documents.filter_children(
    where=[("_status", "eq", "error")],
    include_status=True,
)
for err in errors:
    print(f"doc={err['_parent_id']} page={err['page_id']}: {err['_error']}")
```

Error rows are retried automatically. On the next `sync()` (or `insert()`), error rows with matching identity are re-processed even if their fingerprint hasn't changed — the `_status="error"` flag prevents skipping.

```python
# Fix the underlying issue (e.g., add input sanitization to clean()),
# then re-sync the same batch. The error page retries and succeeds.
result = documents.sync(bad_batch)
print(result.skipped)  # 1 — the good page was skipped
# The error page re-ran through the child graph and is now complete
```

## Production Considerations

**Component swaps trigger re-derivation.** If you upgrade the embedder, the component config hash changes and every row's fingerprint is invalidated. On the next `sync()`, all documents re-process. For targeted re-derivation of just the embedding column, use `recompute()`:

```python
new_embedder = Embedder(model_name="text-embedding-3-large", dim=1024)
documents_v2 = documents.bind(embedder=new_embedder)
documents_v2.recompute("vector")
```

**Monitor errors with `filter_children`.** In a recurring pipeline, check for accumulated errors before and after each run:

```python
error_count = len(documents.filter_children(
    where=[("_status", "eq", "error")],
    include_status=True,
))
print(f"{error_count} pages in error state")
```

**Use `AsyncRunner` for throughput.** Embedding calls are I/O-bound. Swap the runner for concurrent execution:

```python
from hypergraph.runners import AsyncRunner

documents_async = documents.with_runner(AsyncRunner())
result = await documents_async.sync(batch)
```

**Metadata columns are free.** Extra fields in the input dict that don't feed any node are stored as metadata without triggering re-derivation:

```python
documents.sync([
    {"doc_id": "rfc-2616", "content": "...", "source_url": "https://...", "author": "Fielding"},
])
# source_url and author are stored but don't affect the pipeline
```
