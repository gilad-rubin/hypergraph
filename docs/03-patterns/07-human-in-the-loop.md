# Human-in-the-Loop

Use an interrupt when a workflow has enough context to ask a human one
specific question, then must wait for that answer before continuing.

{% hint style="warning" %}
Interrupts require `AsyncRunner`. Running a graph that contains an
`InterruptNode` on `SyncRunner` raises `IncompatibleRunnerError`.
{% endhint %}

## Interrupts: ask a question, name the answer

An `@interrupt` node declares two things and nothing else:

- **What the human sees** — the function's return value, a typed question
  (`Choice`, `Confirm`, `FreeText`, `Form[Model]`). It becomes
  `result.pause.value`. It never enters the graph's dataflow.
- **Where the answer lands** — `answer_name`. This is the node's output:
  downstream nodes consume it by parameter name like any other value, and it
  is the `response_key` your app passes back when resuming.

```python
@interrupt(answer_name="decision")
def review(draft: str) -> Confirm:
    return Confirm(prompt=f"Publish this draft?", evidence=(draft,))

@node(output_name="result")
def publish(draft: str, decision: bool) -> str: ...

result = await runner.run(graph, {"draft": d})
result.pause.value          # the Confirm — show it to the user
result.pause.response_key   # "decision" — where to return their answer

await runner.run(graph, {"draft": d, "decision": True})   # resumes
```

Reaching an interrupt means asking: the node always pauses — unless the answer
is already in the values dict, in which case it never pauses at all (the same
line serves headless/CSV/batch callers). To ask only sometimes, route into it
(`@ifelse`/`@route`): conditionality lives in topology, where `describe()` and
the visualizer can show it.

`answer_name` is `output_name`'s sibling, renamed because on an interrupt the
return is the question, not the output. Everything you know about outputs still
applies to it: name matching, `describe()`, renaming via `rename_outputs`, and
(in a HyperTable) one column per answer.

## The structural question seam

The question vocabulary ships in a companion package. Hypergraph does not
import its concrete `Choice`, `Confirm`, or `Form` classes; it checks this
small structural contract:

- The handler's **return annotation** exposes class-level `answer_type`.
  Hypergraph uses that exact object as the answer port's type. It can be a
  normal class such as `bool` or a typing alias such as `tuple[str, ...]`.
- The returned **question instance** has `prompt: str`,
  `options: tuple[str, ...] | None`, and `evidence: tuple`.

Until the companion vocabulary is installed, this minimal class is enough for
a runnable application or test:

```python
from dataclasses import dataclass
from typing import ClassVar

@dataclass(frozen=True)
class Confirm:
    answer_type: ClassVar[object] = bool
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple[object, ...] = ()
```

`answer_type` describes the human's answer, not the question object. Under
`strict_types=True`, a `Confirm` therefore connects to a `bool` parameter:

```python
@interrupt(answer_name="decision")
def review(draft: str) -> Confirm:
    return Confirm(prompt="Publish?", evidence=(draft,))

@node(output_name="result")
def publish(draft: str, decision: bool) -> str:
    return draft if decision else "not published"

graph = Graph([review, publish], strict_types=True)

# The decorated handler remains ordinary, directly testable Python.
assert review.func("Release notes").prompt == "Publish?"
```

Graph construction fails if the return annotation is missing or does not
expose `answer_type`. Returning `None` also fails loudly at runtime: an
interrupt that asks nothing is a bug.

## A complete pause and resume

```python
from dataclasses import dataclass
from typing import ClassVar

from hypergraph import AsyncRunner, Graph, interrupt, node

@dataclass(frozen=True)
class Confirm:
    answer_type: ClassVar[object] = bool
    prompt: str
    options: tuple[str, ...] | None = None
    evidence: tuple[object, ...] = ()

@interrupt(answer_name="decision")
def review(draft: str) -> Confirm:
    return Confirm(prompt="Publish this draft?", evidence=(draft,))

@node(output_name="result")
def publish(draft: str, decision: bool) -> str:
    return f"published: {draft}" if decision else "not published"

graph = Graph([review, publish])
runner = AsyncRunner()
draft = "Hypergraph v4"

paused = await runner.run(graph, {"draft": draft})
assert paused.paused
assert isinstance(paused.pause.value, Confirm)
assert paused.pause.value.prompt == "Publish this draft?"
assert paused.pause.response_key == "decision"

# Without a checkpointer, re-drive with the full original values plus the answer.
completed = await runner.run(
    graph,
    {"draft": draft, paused.pause.response_key: True},
)
assert completed["result"] == "published: Hypergraph v4"
```

The `Confirm` object is preserved by identity in `PauseInfo.value`; Hypergraph
does not serialize, flatten, or send it through downstream nodes. Only the
later `bool` answer enters the `decision` port.

## The two roles of `answer_name`

`answer_name` connects the application boundary and graph topology without
making either side understand the other.

### Outside the graph: the return address

A FastAPI endpoint, decisions inbox, or queue worker can persist the whole
pause envelope. `response_key` tells it where to put the answer:

```python
if result.paused:
    pending.store(
        workflow_id=result.workflow_id,
        question=result.pause.value,
        answer_name=result.pause.response_key,
    )
```

The host does not need to inspect graph nodes or reconstruct a parameter name.

### Inside the graph: the output port

The same name auto-wires matching consumers:

```python
@interrupt(answer_name="dup_decision")
def review_duplicate(upload_path: str) -> Choice:
    return Choice(
        prompt=f"What should happen to {upload_path}?",
        options=("replace-existing", "keep-both"),
    )

@node(output_name="receipt")
def apply(dup_decision: str) -> str:
    return f"applied:{dup_decision}"

graph = Graph([review_duplicate, apply])
assert "dup_decision" not in graph.inputs.required
```

`dup_decision` is produced by the interrupt, so it is not a phantom graph
input. The consumer runs only after that answer is supplied.

## Resume channels

### Stateless re-drive

Without a checkpointer, send the original graph inputs again and add every
answer collected so far:

```python
values = {"draft": "Release notes"}
paused = await runner.run(graph, values)

values[paused.pause.response_key] = True
completed = await runner.run(graph, values)
```

The second call skips the interrupt handler because its answer port is already
supplied.

### Checkpointed answer-only resume

With a checkpointer and `workflow_id`, completed upstream state is restored.
Resume with only the answer:

```python
from hypergraph import AsyncRunner
from hypergraph.checkpointers import SqliteCheckpointer

checkpointer = SqliteCheckpointer("runs.db")
runner = AsyncRunner(checkpointer=checkpointer)

paused = await runner.run(
    graph,
    {"draft": "Release notes"},
    workflow_id="release-42",
)

completed = await runner.run(
    graph,
    {paused.pause.response_key: True},
    workflow_id="release-42",
)
```

Close the SQLite checkpointer when the application shuts down:

```python
await checkpointer.close()
```

### Supply an answer up front

Headless and batch callers use the same port. With no checkpointer, supplying
the answer on the first run skips the question handler entirely:

```python
result = await runner.run(
    graph,
    {"draft": "Release notes", "decision": True},
)
assert result.completed
```

There is no separate automation hook and no auto-resolving handler return.

## Interrupts in a HyperTable

A checkpointer resumes an execution. A HyperTable converges a domain row under
the current graph and stored facts. The pause-reading contract is deliberately
the same:

```python
from hypergraph import AsyncRunner
from hypergraph.materialization import LanceDBStore

reviews = graph.as_table(
    identity="document_id",
    store=LanceDBStore("./data/reviews"),
    runner=AsyncRunner(),
)

receipt = await reviews.insert(document_id="d-1", draft="Release notes")
if receipt.paused:
    show(receipt.pause.value)
    answer_key = receipt.pause.response_key
    receipt = await reviews.update("d-1", **{answer_key: True})

assert receipt.completed
```

A paused derivation is a waiting row carrying the question envelope. It is
not a complete row with missing outputs:

```python
waiting = reviews.waiting()[0]
assert waiting.id == "d-1"
assert waiting.pause.value.prompt == "Publish this draft?"
assert waiting.pause.response_key == "decision"
assert waiting.provenance
```

The answer named by `response_key` is a derived column supplied by the human.
`update()` re-drives derivation with stored sources and the new answer;
provenance-clean upstream columns do not run again. Supplying that answer on
the initial `insert()` is the headless path and bypasses the interrupt
handler.

Answer provenance records the question's direct inputs, code, and
configuration. If an upstream fact changes after completion, the old answer
is stale: the row returns to waiting with a fresh question and provenance.
This is row convergence, not mid-run resume. Cycles and shared execution state
still belong to a checkpointer-backed runner.

Persisted questions retain `prompt`, `options`, `evidence`, and a stable
display representation of `answer_type`. Evidence values must be
JSON-serializable; an invalid item fails at pause-persist time rather than
silently dropping context.

## Conditional questions belong in topology

An interrupt cannot return an answer to avoid pausing. Route around the
interrupt when review is conditional:

```python
@route(
    targets=["review", "publish_without_review"],
    default_open=False,
)
def review_policy(risk: float) -> str:
    return "review" if risk >= 0.8 else "publish_without_review"
```

Now `describe()` and visualization show the condition. Every time `review`
is reached it asks; when the other route is selected, it does not run.

## Choice options and route targets

When a question has `options` and its answer feeds a `RouteNode` or
`IfElseNode`, every option must have a matching route target. Hypergraph checks
this immediately before surfacing the pause, when the runtime question exists:

```python
@interrupt(answer_name="dup_decision")
def review_duplicate(upload_path: str) -> Choice:
    return Choice(
        prompt=f"What should happen to {upload_path}?",
        options=("replace_existing", "keep_both"),
    )

@route(
    targets=["replace_existing", "keep_both"],
    default_open=False,
)
def choose_path(dup_decision: str) -> str:
    return dup_decision.replace("-", "_")
```

If the question also offered `"archive-old"` but the route mapped it to an
undeclared `archive_old` target, `run()` raises before a human sees a dead
option.

At pause time Hypergraph evaluates each consuming gate once per option using
the gate's current inputs. Keep routing functions pure and cheap, as they may
run again after the human answer arrives.
If another gate input is not yet settled, Hypergraph defers that gate's check
to its ordinary routing-time validation.

## Cycles and entrypoints

Cyclic graphs still require an explicit entrypoint, and the interrupt must be
in the entrypoint scope:

```python
@interrupt(answer_name="user_input")
def ask_user(messages: list[dict]) -> FreeText:
    return FreeText(prompt="What would you like to say?", evidence=(messages,))

graph = Graph(
    [ask_user, add_user_message, generate, remember, should_continue],
    shared=["messages"],
    entrypoint="ask_user",
)
```

On a resumed iteration, the supplied `user_input` passes through the interrupt
port. When the cycle reaches `ask_user` again after consuming that value, the
handler runs and produces the next question.

## Nested graphs and renaming

Nested pauses prefix `node_name` and project `response_key` to the outer graph's
port address:

```python
review_graph = Graph([review], name="review")
review_node = review_graph.as_node().rename_outputs(decision="verdict")
outer = Graph([review_node, publish_verdict])

paused = await runner.run(outer, {"draft": "Release notes"})
assert paused.pause.node_name == "review/review"
assert paused.pause.response_key == "verdict"
```

Namespaced GraphNodes similarly return addresses such as
`"editor.review.decision"`. Always resume with `pause.response_key`; do not
reconstruct nested addresses yourself.

## `PauseInfo`

The pause envelope has one answer slot:

```python
@dataclass
class PauseInfo:
    node_name: str    # Interrupt node, using "/" for nested execution
    value: Any        # The returned question payload
    response_key: str # Resolved graph-scope answer port
```

The old multi-output fields do not exist. A multi-field form still has one
`answer_name`; its answer is one structured model, for example `Form[Profile]`
with `answer_type = Profile`.

## InterruptNode API

```python
from hypergraph import InterruptNode

review = InterruptNode(my_question_handler, answer_name="decision")
review = InterruptNode(
    my_question_handler,
    name="review",
    answer_name="decision",
    emit="reviewed",
    wait_for="ready",
)
```

`answer_name` is required and keyword-only. It must be a `str`; tuple answer
names are rejected. The inherited node APIs continue to work:

| Surface | Meaning |
|---|---|
| `inputs` | Values needed to construct the question |
| `outputs` | The single answer port plus any emit-only ports |
| `data_outputs` | A one-item tuple containing the answer port |
| `answer_name` | Current local answer port, including output renames |
| `rename_inputs(...)` | Rename question inputs |
| `rename_outputs(...)` | Rename the answer port |
| `with_name(...)` | Rename the interrupt node |
| `is_interrupt` | Always `True` |

`@interrupt(output_name=...)` and `InterruptNode(..., output_name=...)` are not
accepted. Use `answer_name` to keep the question return distinct from the
answer output.
