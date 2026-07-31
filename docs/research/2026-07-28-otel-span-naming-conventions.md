# OTel span naming/typing research for issue #351

Source-backed findings gathered 2026-07-28 for
[#351 — Improve OpenTelemetry graph/node span naming, typing, and success status](https://github.com/gilad-rubin/hypergraph/issues/351).

## Question

Is there an existing, authoritative convention that says "this span is a graph run"
versus "this span is a node execution"? If yes, adopt it. If no, what is the
smallest defensible Hypergraph-namespaced addition?

## Findings

### OpenTelemetry core — `SpanKind`

`SpanKind` is `INTERNAL | SERVER | CLIENT | PRODUCER | CONSUMER`. It describes the
span's relationship to its caller, not the domain concept being executed. There is
no `GRAPH` or `NODE`.

**Verdict:** Hypergraph spans stay `INTERNAL`. Nothing to adopt.

### OpenTelemetry core — span status

The trace API spec is explicit and normative:

> "Generally, Instrumentation Libraries SHOULD NOT set the status code to `Ok`,
> unless explicitly configured to do so."

> "Instrumentation Libraries SHOULD leave the status code as `Unset` unless there
> is an error, as described above."

> "Application developers and Operators may set the status code to `Ok`."

`OpenTelemetryProcessor` is an instrumentation library by this definition — it
projects framework events into spans on the user's behalf.

**Verdict:** #351's acceptance criterion "Normal graph and node completion sets
`StatusCode.OK`" contradicts the spec **as an unconditional default**. The spec's
own escape hatch is "unless explicitly configured to do so", so the compliant
shape is an opt-in constructor flag, default `Unset`.

Source: <https://opentelemetry.io/docs/specs/otel/trace/api/>

### OpenTelemetry GenAI semantic conventions

Defines `gen_ai.operation.name` with values including `invoke_agent`,
`invoke_workflow`, `create_agent`, `execute_tool`. Prescribed span name is
`"invoke_agent {gen_ai.agent.name}"`. Prescribed span kind for in-process agent
invocation is `INTERNAL`. Status: **Development** (not stable).

Two reasons not to adopt:

1. It is GenAI-scoped. Hypergraph is a general workflow orchestrator; a `score`
   graph over pure Python functions is not a "GenAI workflow".
2. Its span naming convention *prefixes the operation onto the name*
   (`invoke_agent score`) — structurally the same thing #351 wants to remove
   (`graph score`). Adopting it would not achieve the issue's stated goal.

Source: <https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md>

### OpenInference

Defines three real graph attributes:

| Attribute | Type | Example | Description |
|---|---|---|---|
| `graph.node.id` | String | `search_api_0` | The id of the node in the execution graph. This along with `graph.node.parent_id` are used to visualize the execution graph. |
| `graph.node.name` | String | `Search API` | Human readable name for the node. Optional. |
| `graph.node.parent_id` | String | `router_0` | References the id of the parent node. Unset or empty implies the current span is the root node. |

`openinference.span.kind` permitted values are `LLM`, `EMBEDDING`, `CHAIN`,
`RETRIEVER`, `RERANKER`, `TOOL`, `AGENT`, `GUARDRAIL`, `EVALUATOR`, `PROMPT`.

**`GRAPH` and `NODE` are not permitted values.** #351's warning was correct.

Source: <https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md>

### Phoenix / Arize

Phoenix builds an agent-graph visualization from `graph.node.id` +
`graph.node.parent_id`, described as a logical flow map over span metadata.

Critical identity semantics, confirmed against Arize's own guidance:

- `graph.node.id` is a **stable logical node name** (`input_parser`,
  `research_agent`), not a per-execution unique id.
- `graph.node.parent_id` holds the **parent node's `graph.node.id`** — it is
  *not* an OTel span id.
- Root nodes simply omit `graph.node.parent_id`.

`graph.node.parent_id` is single-valued, so it can express **containment**
(which subgraph a node lives in) but cannot express a DAG's many-to-many data
edges. Hypergraph should map containment and say so explicitly.

Source: <https://arize.com/docs/ax/observe/agents/implementing-agent-metadata-for-arize>

### LangSmith

`langsmith.span.kind` values are `llm`, `chain`, `tool`, `retriever`,
`embedding`, `prompt`, `parser`. Identity is carried by `langsmith.trace.id`,
`langsmith.span.id`, `langsmith.span.parent_id`, `langsmith.span.dotted_order`.

There is **no** graph-versus-node span type. LangGraph structure is conveyed by
ordinary parent/child nesting plus LangChain-native metadata
(`metadata.langgraph_node`, `metadata.langgraph_step`) — a vendor namespace, not
a portable convention.

Source: <https://docs.langchain.com/langsmith/trace-with-opentelemetry>

### Langfuse

`langfuse.observation.type` with values including `span`, `generation`, `agent`,
`tool`. Vendor namespace under `langfuse.*`, no graph/node concept.

Source: <https://langfuse.com/docs/observability/sdk/instrumentation>

## Conclusion

No existing convention defines a portable graph-run-versus-node-execution span
type. Option 2 in #351 is the correct choice: **OpenInference `graph.node.*` for
the parts it genuinely defines, plus one small Hypergraph-namespaced attribute
for execution role.**

## Recommended schema

### The governing rule

> **Use the bare name. One span per level of the user's mental model.**

A first draft proposed fully bare names everywhere (`score`, `compare`). Peer
review (Codex `gpt-5.6-sol`, 2026-07-28) rejected that on the grounds that a map
root and a mapped item are different operation classes and must not share a name.
That objection holds. Codex also proposed path-qualifying every node
(`score/compare`) — rejected: a node name is unambiguous in context and
`graph_name` is already on the span.

Investigating the remaining collisions showed most of them were not naming
problems at all. Measured on the current implementation, a nested graph emits two
1:1 spans covering the same work:

| span | duration | unique information |
|---|---|---|
| `node score` | 8.940 ms | node identity in the parent graph |
| `graph score` | 8.773 ms | the nested run's own `run_id` and outcome |

The mapped case is the same shape — `node score` has exactly one child, `map
score`. In both cases the GraphNode span is a pure wrapper that doubles trace
depth at every level of nesting.

**So collapse it.** A GraphNode span and its single run child become one span.
That removes the `score`-inside-`score` collision at its source instead of
renaming around it, and leaves `.item` as the only generated suffix in the whole
scheme.

| | Graph run | Map root | Map item | Nested graph (collapsed) | Node |
|---|---|---|---|---|---|
| span name | `score` | `score` | `score.item` | node name | `compare` |
| `SpanKind` | `INTERNAL` | `INTERNAL` | `INTERNAL` | `INTERNAL` | `INTERNAL` |
| `hypergraph.span.role` | `graph` | `map` | `graph` | `graph` or `map` | `node` |
| `hypergraph.item_index` | — | — | `0,1,2…` | — | inherited |
| status on success | `Unset` default, `Ok` only when opted in | same | same | same | same |
| status on terminal failure | `Error` + `exception` event | same | same | same | same |

A top-level map root keeps the bare graph name: it has no run-span parent to
collide with, and its children are `score.item`.

`hypergraph.span.role` rather than `.kind`: OTel already has `SpanKind` and
OpenInference has `openinference.span.kind`. A third "kind" in one trace is
needless collision.

### Attributes on a collapsed span

The two merged spans disagree on shared keys — `hypergraph.graph_name` is the
*parent* graph on the node span but the *nested* graph on the run span, and their
`run_id`s differ. Every existing key keeps its current meaning; the inner run's
values move to a `hypergraph.nested.*` namespace so no existing filter changes
behavior:

| attribute | value |
|---|---|
| `hypergraph.node_name` | node name in the parent graph |
| `hypergraph.graph_name` | **parent** graph name (as on any node span) |
| `hypergraph.run_id` | **outer** run id (as on any node span) |
| `hypergraph.nested.graph_name` | the nested graph's own name |
| `hypergraph.nested.run_id` | the nested run's own id |
| `hypergraph.nested.outcome` | the nested run's outcome |

The span **name** is the node name, not the nested graph's name. These are usually
identical, but `as_node(name=...)` lets them diverge — and the name a reader
expects to see beside its sibling nodes is the one written in the parent graph.

`hypergraph.span.role` on a collapsed span is `graph` (or `map` when the GraphNode
maps): the span covers a graph run. Presence of `hypergraph.node_name` is what
tells a consumer it is also a node in a parent graph.

### Accepted trade-off: repeated node names

Two different nodes both named `compare`, in different graphs, produce spans that
share the name `compare` and therefore aggregate together in Jaeger-style
per-operation metrics. This is accepted: node names are the user's own vocabulary,
`hypergraph.graph_name` and `hypergraph.node_name` disambiguate on the span, and
trace position makes it obvious in any waterfall. The self-nesting cases above are
different — there the *same* span name appears as its own direct ancestor, which
reads as duplicate instrumentation rather than as two similarly-named steps.

### The `.` separator is safe

`GraphNode._RESERVED_CHARS` is exactly `{'.', '/'}` and rejects both in node and
port names, and `validation.py` requires node and output names to be valid Python
identifiers. So `.` can never appear in a user-authored name — `score.item` is
unambiguously framework-generated and can never collide with a graph the user
actually called `score.item`.

### Why `.item` survives the collapse

Collapsing removes the wrapper spans, but a map root and its per-item runs are
still two genuinely different operations that would otherwise share one name:

- In Phoenix the span name is the dominant waterfall label; `score` directly
  inside `score` reads as duplicate instrumentation, not as two layers.
- Jaeger's service performance monitoring aggregates rate/error/duration **by
  operation name**. Merging a fan-out coordinator with a single mapped item blends
  two unrelated latency populations into a meaningless average.
- Datadog's operation-name mapping varies by ingestion path, but the trace view
  displays names prominently either way.

`score` versus `score.item` is exactly the kind of bounded, useful cardinality
split those tools reward — one extra operation name, not one per item.

### Item spans must not carry the index

Every item span is named `score.item`, whether there are two or two million.
Instance identity lives only in `hypergraph.item_index`. Names like `score[1847]`
create unbounded operation cardinality — the thing OTel's low-cardinality naming
guidance exists to prevent. Repeated sibling names are normal and fine (thousands
of `SELECT users` spans); giving *different* operation classes the *same* name is
the actual problem.

### Worked examples

All three shapes, before and after. "Before" is verified current behavior.

**Top-level map** — no wrapper to collapse; only `.item` changes:

```text
map score                          score                 role=map
└── graph score      item=0        └── score.item        role=graph  item_index=0
    ├── node compare                   ├── compare       role=node
    └── node tally                     └── tally         role=node
```

**Plain nested graph** — two spans become one:

```text
graph review_batch                 review_batch          role=graph
└── node score                     └── score             role=graph
    └── graph score                    └── compare       role=node
        └── node compare
```

**Map over a nested graph**, the hardest case — three layers become two:

```text
graph review_batch                 review_batch          role=graph
└── node score                     └── score             role=map
    └── map score                      ├── score.item    role=graph  item_index=0
        ├── graph score  item=0        │   └── compare   role=node
        │   └── node compare           └── score.item    role=graph  item_index=1
        └── graph score  item=1            └── compare   role=node
            └── node compare
```

## OpenInference enrichment: opt-in, not default

The first draft proposed emitting `openinference.span.kind = CHAIN` and
`graph.node.*` on every span. Two corrections:

1. OpenInference defines `CHAIN` as LLM-application orchestration glue. Hypergraph
   is a general Python workflow framework; blanket-labelling a pure-Python `score`
   graph as `CHAIN` is semantically false. Make it an explicit Phoenix/OpenInference
   integration profile the user turns on.
2. `graph.node.id` as "a stable logical node name" is a **Hypergraph
   interpretation**, not an OpenInference guarantee. The spec calls it only "the id
   of the node in the execution graph"; Arize's guidance calls it a unique name.
   Neither settles what repeated mapped executions should do. Document it as our
   chosen mapping, and do not append `item_index` unless real Phoenix testing
   proves execution-unique ids are required.

`graph.node.parent_id` is single-valued and therefore cannot represent
Hypergraph's DAG edges at all. It can project logical containment only. Do not
advertise it as "the Hypergraph DAG in Phoenix".

## Success status: keep `Unset`

Peer review confirmed the reading. `Ok` is an application/operator assertion;
OTel's error-recording convention says successful instrumented operations leave
status unset, and setting `Ok` can suppress errors an analysis tool would
otherwise infer. Today `otel.py` already ends successful spans without setting
status — that is correct and should stay the default.

If a Phoenix deployment wants explicit green, provide an opt-in flag, and set
`Ok` only for genuinely completed outcomes — never for paused, stopped, or
abandoned runs that merely ended without an exception.

**Issue #351's unconditional "Normal graph and node completion sets
`StatusCode.OK`" acceptance criterion should be amended before implementation.**

Source: <https://opentelemetry.io/docs/specs/semconv/general/recording-errors/>

## Migration surface

Two user-visible changes, both breaking:

1. **Span names.** Filters matching `graph *` / `map *` / `node *` migrate to
   `hypergraph.span.role = graph|map|node`.
2. **Span count.** Nested graphs emit one span per level instead of two. Anything
   counting spans, or expecting a `node X` span to have a `graph X` child, breaks.
   This is wider than #351 as written and must be called out in the issue.

Collapsing changes ambient-context bookkeeping too: today the GraphNode span and
the inner run span each attach, and `otel.py`'s attach/detach invariants are
load-bearing (see the `_detach_ambient_if_current` comments). One span means one
attach — simpler, but the interrupt and shutdown paths need re-verification, not
just the happy path.

In-repo, the prefixed names are asserted in `tests/test_run_log/` —
`test_otel_processor.py`, `test_otel_instrumented.py`,
`test_otel_ambient_context.py` — and documented in
`docs/05-how-to/observe-execution.md` (the `extra_attributes` note describes run
roots as "`graph …`/`map …`"). There is currently **no** OTel test covering a
nested graph at all, which is why the double-span behavior went unnoticed; the
implementation should add one.
