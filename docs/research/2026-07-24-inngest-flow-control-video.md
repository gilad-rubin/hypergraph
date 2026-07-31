# Inngest flow control video: evidence and Hypergraph implications

Researched 2026-07-24 against official Inngest video/docs, local browser
history, and current Hypergraph canon/code.

**Status:** research evidence, not current-behavior canon.

## Identification

| Field | Value |
|---|---|
| Title | **Throttle, Concurrency, Debounce & Idempotency: The Flow Control Playbook** |
| Video ID | `H-FhIm238VY` |
| URL | [youtube.com/watch?v=H-FhIm238VY](https://www.youtube.com/watch?v=H-FhIm238VY) |
| Channel | [Inngest](https://www.youtube.com/@inngest), channel ID `UCRJT3yvhhhuio-2N0B12bVQ` |
| Published | 2026-06-01 |
| Duration | 7:37 |

This, not the earlier durable-agent video, is the source relevant to the
LLM-capacity question.

## Local visit proof

Dia's current Chromium history has no exact video-ID/title row. OpenAI Atlas
history does:

`~/Library/Application Support/com.openai.atlas/browser-data/host/user-77SsRLa9MGLc1F5CatAPRhoj__9c297f00-7f1a-449f-a2c0-bc99d088e447/History`

A read-only `visits`/`urls` join, converted to local time, returned:

| Local time | URL |
|---|---|
| 2026-06-02 12:49:59 | `watch?v=H-FhIm238VY&pp=...` |
| 2026-06-02 12:50:01 | `watch?v=H-FhIm238VY` |
| 2026-06-06 08:18:07 | `watch?v=H-FhIm238VY&t=285s` |
| 2026-06-06 09:54:33 | `watch?v=H-FhIm238VY` |

## What the video establishes

The video applies:

- a lossless, carrier-keyed throttle of 80 starts/minute
  ([2:04](https://www.youtube.com/watch?v=H-FhIm238VY&t=124s));
- warehouse-keyed concurrency of 10, where the eleventh job waits
  ([2:48](https://www.youtube.com/watch?v=H-FhIm238VY&t=168s)); and
- the core distinction: throttle is work per unit of time; concurrency is work
  active at the same moment
  ([3:45](https://www.youtube.com/watch?v=H-FhIm238VY&t=225s)).

Its final warning that a global cap can let one tenant delay others
([6:43](https://www.youtube.com/watch?v=H-FhIm238VY&t=403s)) is about fairness.
It is not a reason to omit a real global provider cap.

Current official docs make the exact semantics clearer:

| Control | Bounds | Excess behavior | LLM-map fit |
|---|---|---|---|
| [`concurrency`](https://www.inngest.com/docs/guides/concurrency) | Active **steps**, not whole runs; sleeping/waiting releases capacity | Queue | **Yes** for “10 at once” |
| [`throttle`](https://www.inngest.com/docs/guides/throttling) | Function-run starts over time; optional key/burst | FIFO delay | **Yes** for RPM-style limits |
| [`rateLimit`](https://www.inngest.com/docs/guides/rate-limiting) | Function-run starts over time | **Skip** excess events | **No** for required Map items |
| Debounce | Event bursts | Keep the last event | No; distinct items would be lost |
| Idempotency | Duplicate triggers | Suppress a duplicate key | Orthogonal to capacity |

Two naming cautions follow:

1. The video sometimes says concurrency caps “runs”; current docs say active
   **steps**. Hypergraph should acquire at the scarce LLM call, not around the
   whole subgraph or durable Run.
2. The video uses “rate limiting” informally for pacing. Inngest's actual
   `rateLimit` feature is lossy. A provider-capacity wait must never silently
   drop a Hypergraph item.

## The scope lesson

Inngest can combine a global provider constraint with per-tenant fairness:

```ts
concurrency: [
  { scope: "account", key: `"openai"`, limit: 10 },
  { scope: "fn", key: "event.data.tenant_id", limit: 2 },
]
```

For Hypergraph, the quota key must match the provider's real quota unit:
usually a credential/project and model group, not a graph, Map, Batch, tenant,
or workflow ID. A tenant key alone is unsafe: ten tenants with ten permits each
can send 100 calls into a provider that permits ten globally.

Do not copy Inngest's surface blindly:

- Its throttle is per function; identical keys on two functions do not share a
  time budget.
- Each throttled request has weight 1, so it does not model token/minute.
- FIFO is promised within one function, not across functions.
- A named Hypergraph/provider pool should have one authority; conflicting
  declarations should fail. For a quota owned by an underlying service, that
  authority is often the shared provider component. The composition root
  constructs and shares the component, but the component owns admission.

## Hypergraph implications

Current `AsyncRunner(max_concurrency=...)` is already a good **per-root node
budget**. It acquires at leaf `FunctionNode` calls, propagates through nested
graphs, and `runner.map(..., max_concurrency=10)` shares it across that map's
children
([executor](../../src/hypergraph/runners/async_/executors/function_node.py),
[runner](../../src/hypergraph/runners/async_/runner.py)).

It is not a provider-resource limit:

- it counts every `FunctionNode`, not just LLM calls;
- two concurrent roots with a limit of 10 can expose 20 LLM calls;
- `max_active_runs` controls Host work admission, not an external resource; and
- a cap inside one nested Map does not cover another Map, root, or Host Batch.

Hypergraph can expose admission at three useful scopes:

| Scope | What it controls | Best fit |
|---|---|---|
| Graph | Work admitted by one graph/root | Bound the total work of that graph |
| Node | Calls through one node | Protect or tune one hot call site |
| Component | All calls through one shared dependency | Enforce the real provider/resource quota across graphs and nodes |

These scopes compose; they do not replace one another. For an LLM quota, prefer
the **component scope** when several graphs or nodes share the same client or
provider. The component owns the limiter and acquires it at the exact scarce
call. Graphs and nodes may still add narrower budgets for their own work.

### Before

```python
await asyncio.gather(
    runner.map(graph, {"prompt": a}, map_over="prompt", max_concurrency=10),
    runner.map(graph, {"prompt": b}, map_over="prompt", max_concurrency=10),
)
# Two root budgets: the provider can see 20 calls.
```

### After

```python
llm = LimitedLLM(
    client=provider_client,
    concurrency=10,
)  # one shared component; conceptual API

@node(output_name="answer")
async def ask_llm(prompt: str) -> str:
    return await llm.complete(prompt)   # component admits the scarce call

graph_a = build_graph(llm=llm)
graph_b = build_other_graph(llm=llm)
```

Every nested Map, `runner.map`, `runner.run`, and Host Definition that consumes
the same provider quota must receive the same component (or a component backed
by the same admission authority). Keep the runner's broader `max_concurrency`;
it solves a different problem. A graph- or node-level limiter remains useful
when the intended quota truly belongs to that graph or node rather than the
underlying provider.

If the safe concurrent count is unknown, sibling project
[`hyperlimit`](../../../hyperlimit/README.md) already provides an asyncio AIMD
gate that grows on clean completions and shrinks on 429s/connection pressure.
It does not provide requests/minute or weighted tokens/minute; add those as a
separate lossless throttle when the provider contract requires them.

## Verdict for Durable Host

This evidence **validates A3 rather than changing it**. Accepted
[ADR 0005](../adr/0005-durable-hosts-accept-commands-runners-retain-execution.md)
already keeps Run Home work admission separate from injected provider-resource
admission; permit waits are not failures and do not spend retry policy.

Do not redirect Durable Host implementation toward Inngest-style config on
`serve()`/`submit()`, and do not use `max_active_runs` as an LLM quota. Carry
one proof into a later provider-integration ticket:

1. Run concurrent root maps, nested maps, and Host child Runs through one
   instrumented provider wrapper.
2. Prove peak in-flight calls `<= 10` while all ten slots stay busy whenever
   eligible work exists.
3. Prove waits do not fail or spend retries, and cancellation/errors release
   permits.
4. Prove retry backoff and `Retry-After` waits hold no in-flight permit.

One SQLite Host worker can share one in-process limiter. A later multi-worker
Host needs shared provider admission if all workers consume the same quota.
That fleet concern must stay separate from Durable Host v1 work admission.

## Primary sources

- [Official video](https://www.youtube.com/watch?v=H-FhIm238VY)
- [Concurrency](https://www.inngest.com/docs/guides/concurrency)
- [Throttling](https://www.inngest.com/docs/guides/throttling)
- [Rate limiting](https://www.inngest.com/docs/guides/rate-limiting)
- [Flash sales and bursty workflows](https://www.inngest.com/docs/patterns/flow/flash-sales-and-bursty-workflows)
- [Handling idempotency](https://www.inngest.com/docs/guides/handling-idempotency)
