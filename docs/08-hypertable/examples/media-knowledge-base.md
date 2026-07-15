# Example: duplicate review in a media intake table

This intake graph normalizes a title, asks a human how to handle a duplicate,
and persists the decision plus its downstream result.

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


@node(output_name="normalized_title")
def normalize(title: str) -> str:
    return " ".join(title.lower().split())


@interrupt(answer_name="duplicate_decision")
def review_duplicate(normalized_title: str) -> Choice:
    return Choice(
        prompt=f"How should '{normalized_title}' be filed?",
        options=("replace", "keep-both", "archive-old"),
        evidence=({"candidate": normalized_title},),
    )


@node(output_name="filed_as")
def file_media(normalized_title: str, duplicate_decision: str) -> str:
    return f"{duplicate_decision}:{normalized_title}"


intake = Graph([normalize, review_duplicate, file_media], name="media_intake")
media = intake.as_table(
    identity="media_id",
    store=LanceDBStore("./data/media"),
    runner=AsyncRunner(),
    on_error="store",
)
```

## Cold boot

```python
receipt = await media.insert(media_id="m-17", title="  Field Notes  ")

assert receipt.paused
assert receipt.pause.value.prompt == "How should 'field notes' be filed?"
assert receipt.pause.response_key == "duplicate_decision"

inbox_item = media.waiting()[0]
assert inbox_item.id == "m-17"
assert inbox_item.provenance
```

The row already contains the source and normalization derived before the
interrupt. It is not reported as complete and no downstream value is invented.

## Answer through an update

```python
receipt = await media.update(
    inbox_item.id,
    **{inbox_item.pause.response_key: "keep-both"},
)

assert receipt.completed
assert media.waiting() == ()
assert media.get("m-17") == {
    "media_id": "m-17",
    "title": "  Field Notes  ",
    "normalized_title": "field notes",
    "duplicate_decision": "keep-both",
    "filed_as": "keep-both:field notes",
}
```

Normalization remains a provenance cache hit. Only work downstream of the
new answer executes.

## Re-ask after upstream change

```python
receipt = await media.update("m-17", title="Revised Field Notes")
assert receipt.paused
assert media.waiting()[0].pause.value.prompt == (
    "How should 'revised field notes' be filed?"
)
```

The old answer was valid for the old normalized title. The new question has
different provenance, so row convergence asks again.

## Headless intake

```python
receipt = await media.insert(
    media_id="m-18",
    title="Known item",
    duplicate_decision="archive-old",
)
assert receipt.completed
assert media.get("m-18")["filed_as"] == "archive-old:known item"
```

Supplying the answer column up front bypasses the interrupt handler and drives
the same downstream graph.
