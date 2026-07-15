# Example: documents and page rows

A document is the root entity. Splitting produces page items, and a mapped
child graph derives page-level columns.

```python
from typing import TypedDict

from hypergraph import Graph, SyncRunner, node
from hypergraph.materialization import LanceDBStore


class Page(TypedDict):
    page_id: str
    text: str


@node(output_name="pages")
def split_pages(text: str) -> list[Page]:
    return [
        Page(page_id=f"p-{index}", text=page)
        for index, page in enumerate(text.split("\f"), start=1)
    ]


@node(output_name="clean_text")
def clean_page(text: str) -> str:
    return " ".join(text.lower().split())


page_graph = Graph([clean_page], name="process_page")
documents_graph = Graph(
    [split_pages, page_graph.as_node().map_over("pages", identity="page_id")],
    name="documents",
)

documents = documents_graph.as_table(
    identity="document_id",
    store=LanceDBStore("./data/documents"),
    runner=SyncRunner(),
)
```

Insert the document once. The receipt describes the root write; page rows are
available through their named handle.

```python
receipt = documents.insert(
    document_id="manual-7",
    text="Front page\fSecond page",
    collection="manuals",
)
assert receipt.completed

pages = documents.child("page")
assert pages.rows(parent="manual-7") == [
    {
        "page_id": "p-1",
        "text": "Front page",
        "clean_text": "front page",
        "document_id": "manual-7",
    },
    {
        "page_id": "p-2",
        "text": "Second page",
        "clean_text": "second page",
        "document_id": "manual-7",
    },
]
```

Parent metadata can scope child reads. HyperTable resolves the parent matches
and joins their identities to the physical child rows:

```python
manual_pages = pages.rows(where={"collection": "manuals"})
assert {page["document_id"] for page in manual_pages} == {"manual-7"}
```

Annotations remain metadata and do not re-run page derivation:

```python
pages.set({"document_id": "manual-7"}, reviewed=True)
assert all(page["reviewed"] for page in pages.rows(parent="manual-7"))
```

On a later `sync()`, unchanged documents and pages are provenance cache hits.
Changed document text rebuilds only the affected page set. Identities absent
from the incoming complete collection are deleted with their page rows.
