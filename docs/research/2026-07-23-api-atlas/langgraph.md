# API Atlas — LangGraph vs hypergraph

Sources: docs.langchain.com OSS Python LangGraph pages + LangSmith Agent Server pages,
fetched 2026-07-23 as raw markdown (`<page>.md`). Library version at time of research:
**langgraph 1.2.9** (PyPI, released 2026-07-10; 1.0.0 GA 2025-10-17, 1.1.0 2026-03-10,
1.2.0 2026-05-12). Several surfaces below are gated `langgraph>=1.1` (stream v2 format)
or `>=1.2` (event streaming v3, timeouts, error handlers, drain, DeltaChannel).
Existing workspace research referenced, not repeated: interrupt() re-execution/typing/audit
weaknesses are in `superposition/docs/research/2026-07-16-ingestion-lifecycle-primitives/hitl.md` §3a;
durable-execution positioning in `hypergraph/docs/research/2026-07-21-durable-execution-landscape.md`.

---

## 1. Identity

Low-level agent **orchestration runtime**: Pregel-style message passing in discrete
super-steps over a shared typed state; **state-based recovery** (checkpoint at every
super-step boundary, resume from last checkpoint — not event-sourced replay), with a
replay-flavored wrinkle: a resumed node restarts from its top and completed `@task`
results are restored from the checkpoint, so task/interrupt *order* must be deterministic.
Python + JS, MIT, v1.0 GA Oct 2025, current 1.2.9; paid control plane (LangSmith
Deployment / Agent Server) carries all the queue/cron/background verbs.
(https://docs.langchain.com/oss/python/langgraph/overview)

## 2. Authoring

Graph API: `StateGraph(State)` + `add_node` / `add_edge` / `add_conditional_edges` /
`compile(...)`. State is a TypedDict/dataclass/Pydantic model whose per-key **reducers**
(`Annotated[list, operator.add]`) define merge semantics. Nodes are plain functions;
extra powers are **injected by signature** — declare `config: RunnableConfig`,
`runtime: Runtime[Context]`, `writer: StreamWriter`, `error: NodeError` and the runtime
supplies them. (https://docs.langchain.com/oss/python/langgraph/graph-api)

```python
class State(TypedDict):
    topic: str
    joke: str

def refine_topic(state: State):
    return {"topic": state["topic"] + " and cats"}

graph = (
    StateGraph(State)
    .add_node(refine_topic)
    .add_node(generate_joke)
    .add_edge(START, "refine_topic")
    .add_edge("refine_topic", "generate_joke")
    .add_edge("generate_joke", END)
    .compile(checkpointer=InMemorySaver())   # also: store=, cache=, transformers=
)
```

`add_node` per-node kwargs: `retry_policy=`, `timeout=`, `error_handler=`,
`cache_policy=`, `defer=True` (wait for all pending fan-out tasks before running —
the reduce step of map-reduce). `set_node_defaults(...)` sets these graph-wide (>=1.2).
`StateGraph(Overall, input_schema=..., output_schema=..., context_schema=...)` splits
public input/output from internal channels; nodes may write private channels.

Functional API (same runtime, no graph): —
(https://docs.langchain.com/oss/python/langgraph/functional-api)

```python
@task
def write_essay(topic: str) -> str: ...

@entrypoint(checkpointer=InMemorySaver())
def workflow(topic: str) -> dict:
    essay = write_essay("cat").result()      # task -> future
    is_approved = interrupt({"essay": essay, "action": "Please approve/reject the essay"})
    return {"essay": essay, "is_approved": is_approved}
```

Entrypoints must take a single positional arg; injectables `previous` (prior return on
the thread), `store`, `writer`, `config`. `entrypoint.final(value=..., save=...)`
decouples the caller-visible return from what the checkpoint saves. Tasks and
entrypoint inputs/outputs must be JSON-serializable. `@task`s are also callable
**inside StateGraph nodes** to get checkpointed sub-steps within one node.

Subgraphs: a compiled graph is just a node (`builder.add_node("sub", subgraph)`), or
call it as a function inside a node. Subgraph inherits the parent checkpointer by
default (whole subgraph = one parent super-step); `compile(checkpointer=True)` gives
it its own checkpoint history (needed for time travel *inside* it);
`Command(goto=..., graph=Command.PARENT)` navigates back into the parent.
(https://docs.langchain.com/oss/python/langgraph/use-subgraphs)

## 3. Run verbs — the complete surface

### In-process (OSS)

| Their verb | What it does | hypergraph equivalent |
|---|---|---|
| `graph.invoke(input, config)` / `ainvoke` | Run to completion; returns final state dict (v1) or, with `version="v2"`, a `GraphOutput` with `.value` / `.interrupts` | `runner.run` → `RunResult` (ours typed from day one) |
| `graph.invoke(..., context={...})` | Runtime dependency injection (typed `context_schema`), separate from state and from `config` | hypster config nesting (different philosophy; per-call context override is MISSING in hypergraph) |
| `graph.invoke(..., durability="exit"\|"async"\|"sync")` | Per-call checkpoint durability: only-on-exit / write-behind / write-through | MISSING — hypergraph has no per-call durability dial |
| `graph.invoke(..., config={"recursion_limit": 5})` | Cap super-steps; `GraphRecursionError`; default 1000 | MISSING (no step budget concept) |
| `graph.stream(input, stream_mode="values"\|"updates"\|"messages"\|"custom"\|"checkpoints"\|"tasks"\|"debug")` / `astream` | Iterator of raw runtime events; `stream_mode` may be a **list**; `subgraphs=True` adds namespace; `version="v2"` unifies every chunk to `{"type", "ns", "data"}` | `runner.iter` (one event vocabulary; no mode selection) |
| `graph.stream_events(input, version="v3")` / `astream_events` | **Run-stream object** with typed projections: `stream.messages`, `.values`, `.subgraphs`, `.output`, `.interrupts`, `.interrupted`, `.extensions`, `.interleave(...)`; multiple concurrent consumers | `runner.iter` + `result.paused` (partial); projection object is MISSING |
| `graph.batch` / `abatch` (inherited from Runnable) | Parallel invokes; not a documented LangGraph surface (no per-item keys, no partial-failure story) | `runner.map` / `map_iter` — hypergraph is far ahead (map_over, MapResult, backpressure) |
| `Send("node", {..}, timeout=...)` returned from a conditional edge | In-graph map-reduce fan-out with per-dispatch state and per-dispatch timeout override; `defer=True` node = reduce | in-graph analog of `runner.map`; hypergraph has no per-item policy override → MISSING our side |
| `graph.invoke(Command(resume=value), config)` | Resume a paused interrupt; value becomes `interrupt()`'s return; `Command(resume={interrupt_id: value})` settles several at once | re-run with answers / designed `host.answer(pause_id=...)` |
| `graph.invoke(None, config)` | Resume after error/drain from last checkpoint; with a `checkpoint_id` in config = **replay** from that point (later nodes re-execute) | checkpointer resume; `fork_from`/`retry_from` |
| `graph.get_state(config, subgraphs=True)` | Latest (or by `checkpoint_id`) `StateSnapshot`: `values, next, config, metadata, created_at, parent_config, tasks` | MISSING as a first-class verb (RunResult only at end) |
| `graph.get_state_history(config)` | All checkpoints, newest first — the time-travel index | lineage ids exist; queryable history MISSING at Tier 0 |
| `graph.update_state(config, values, as_node=...)` | **Fork**: new checkpoint branching from any prior one, values run through reducers, `as_node` controls resume point | MISSING (hypergraph edits state only by re-running with new inputs) |
| `control = RunControl(); graph.invoke(..., control=control)`; `control.request_drain("sigterm")` | Cooperative stop at next super-step boundary; raises `GraphDrained(reason)` with a resumable checkpoint; nodes can read `runtime.drain_requested` | `handle.stop()` / designed `host.stop` (LangGraph's in-node drain visibility is the extra) |
| `runtime.heartbeat()` | Refresh a node's idle-timeout clock | MISSING |
| `interrupt(payload)` | Dynamic pause anywhere in node/tool code (see §5) | interrupt gates |
| **Background / detached in-process** | **NONE** — no OSS `start_run`; background = deploy to Agent Server | `runner.start_run` / `start_map` — hypergraph Tier 0 advantage |

### Agent Server (LangSmith Deployment; `langgraph_sdk` client)

(https://docs.langchain.com/langsmith/background-run, /langsmith/runs, /langsmith/streaming,
/langsmith/cancel-run, /langsmith/double-texting, /langsmith/cron-jobs, /langsmith/stateless-runs)

| Their verb | What it does | hypergraph equivalent |
|---|---|---|
| `client.threads.create()` | Create the durable state container first (thread must exist before a stateful run) | `workflow_id` (implicit; no create step) |
| `client.runs.create(thread_id, assistant_id, input=..., multitask_strategy=, webhook=, interrupt_before=, interrupt_after=)` | **Background run**; returns immediately with `run_id`, `status="pending"` | designed `host.submit` → receipt |
| `client.runs.stream(thread_id, assistant_id, input, stream_mode=...)` | Create run + stream its output (SSE); closes when run completes | `host.submit` + `watch` fused |
| `client.runs.wait(thread_id_or_None, assistant_id, input)` | Create run + block for final output | the deliberately omitted `host.run()` — LangGraph ships it |
| `client.runs.join(thread_id, run_id)` | Block until an existing run finishes | await receipt/result |
| `client.runs.join_stream(thread_id, run_id)` | Attach to a running run's stream — **"output is not buffered, so any output produced before joining will not be received"** | designed `host.watch` = durable replay **+** live tail — hypergraph strictly better |
| `client.threads.join_stream(thread_id)` | Long-lived stream of **every** run on a thread (chat UIs, HITL resumptions) | MISSING (watch is per-workflow) |
| `client.runs.list(thread_id)` / `runs.get(...)` | Enumerate/inspect runs + status (`pending`/`success`/`interrupted`/...) | Run Home queries (designed) |
| `client.runs.cancel(thread_id, run_id, action="interrupt")` | Cancel; run left `"interrupted"`, thread state intact | `host.stop` |
| `client.runs.cancel(..., action="rollback")` | Cancel **and delete** the run and its checkpoints | MISSING (conflicts with our evidence-honesty stance — deliberate) |
| `multitask_strategy="reject"\|"enqueue"(default)\|"interrupt"\|"rollback"` | "Double texting": what to do when a second run hits a busy thread | submit dedup via start-fingerprint — theirs handles *different-input* collisions, ours handles *same-work* dedup; each is MISSING the other half |
| `client.crons.create(assistant_id, schedule="27 15 * * *", input=...)` / `crons.create_for_thread(...)` / `crons.delete(...)` | Cron runs (UTC); stateless crons make a fresh thread per firing | MISSING (only "scheduled answers" designed) |
| Stateless runs: `runs.stream(None, ...)` / `runs.wait(None, ...)`; REST `POST /runs`, "Create Run Batch" | Fire-and-forget runs with no thread; batch endpoint submits many at once | closest to `runner.map` at server tier, but no batch identity/manifest — a bag of independent runs |
| `webhook=` on run create | POST on completion | MISSING (pull-only `watch`) |
| `interrupt_before=` / `interrupt_after=` (run kwargs) | Static breakpoints at named nodes, chosen at submit time by the operator, no code change | MISSING — hypergraph gates are graph-declared only |
| `client.threads.get_state / update_state / get_history` | Server mirrors of the state/time-travel verbs | Run Home (designed) |
| Store REST: put/get/delete/search items, list namespaces | Server-hosted cross-thread KV + semantic search | MISSING (panda uses its own stores; likely fine) |
| Worker entrypoint | **NONE user-facing** — queue/workers are inside the managed server | designed `host.work_forever` — hypergraph exposes the loop; LangGraph sells it |

## 4. Observation

- **Raw stream modes** cover the whole progress spectrum: `updates` (per-node deltas),
  `values` (full state per step), `messages` (LLM token 2-tuples with `langgraph_node` /
  `tags` metadata for filtering), `custom` (anything a node pushes via
  `get_stream_writer()`), `checkpoints` (each write, same shape as `get_state`),
  `tasks` (task start/finish with results/errors), `debug` (all of it). Modes combine as
  a list. (https://docs.langchain.com/oss/python/langgraph/streaming)
- **Event streaming (v3, >=1.2)** is the recommended layer: one run-stream object,
  typed projections consumable concurrently (`stream.messages` while `stream.values`
  while `stream.output`), `stream.interleave("values", "messages")` for strict arrival
  order in sync code, raw `ProtocolEvent` envelopes (`seq`, `method`, `namespace`,
  `timestamp`, `data`) underneath, and user-defined **StreamTransformers** that publish
  derived projections (`stream.extensions.tool_activity`) registered per-call or baked
  in at `compile(transformers=[...])`. Channels include `tools` and `lifecycle`
  (`started/running/completed/failed/interrupted`, with `cause` linking a child scope to
  the dispatching tool call / fan-out send). Wire format is the public **Agent Protocol**
  (`langchain-protocol` on PyPI). (https://docs.langchain.com/oss/python/langgraph/event-streaming)
- **State queries**: `get_state`, `get_state_history` (filter idioms: by `next`, by
  `metadata["step"]`, by `source == "update"` to find forks, by `tasks[*].interrupts`
  to find pauses). `runtime.execution_info` inside nodes exposes `node_attempt`,
  `thread_id`, `run_id`, `checkpoint_id`, `task_id`.
- **Watching a batch**: nothing batch-shaped. A `Send` fan-out is observed as many
  `updates`/`tasks` events in one super-step; a server "run batch" is N independent runs
  you poll individually. No aggregate progress, no per-item outcome table.
- Dashboards: LangSmith tracing + Studio (commercial); `graph.get_graph().draw_mermaid_png()` for topology.

## 5. HITL / pause

Core mechanics, re-execution semantics, free-form resume typing, and the missing audit
story are already analyzed in **hitl.md §3a** — unchanged in 1.2. What the current docs
add on top (https://docs.langchain.com/oss/python/langgraph/interrupts):

- Interrupts now surface ergonomically: `stream.interrupted: bool` and
  `stream.interrupts: tuple[Interrupt, ...]` on the v3 run stream; on v2 `invoke`,
  `GraphOutput.interrupts` (the magic `result["__interrupt__"]` key is deprecated).
- Each `Interrupt` carries an `id`; parallel pauses resolve in one call:
  `Command(resume={i.id: answer for i in stream.interrupts})`.
- Validation is still a **pattern, not an API**: docs prescribe one `interrupt()` per
  node invocation + a conditional edge looping back with the re-prompt question stored
  in state — explicitly warning that `while True: interrupt()` inside a node causes
  *exponential* re-execution on each resume.
- Rules of interrupts (documented footguns): never wrap `interrupt` in `try/except`
  (it pauses by raising), never reorder interrupt calls (index-based matching), keep
  pre-interrupt side effects idempotent, don't return complex objects through it.
- `interrupt()` bypasses retry policies and error handlers (`GraphBubbleUp`).
- Static breakpoints (`interrupt_before/after` node lists) exist as run-submit kwargs
  on the server — an operator can force a pause without touching graph code.
- Contrast: hypergraph's designed `host.answer` validates against a **graph-declared
  answer schema before settling** and records `source_ref` provenance; LangGraph has
  neither (schema or audit) — this remains our clearest HITL edge.

## 6. Failure & retry surface

(https://docs.langchain.com/oss/python/langgraph/fault-tolerance — most of it `>=1.2`)

User-configurable, per node (or graph-wide via `set_node_defaults`):

- `RetryPolicy(max_attempts=3, initial_interval=0.5, backoff_factor=2.0,
  max_interval=128.0, jitter=True, retry_on=default_retry_on)` — default predicate
  retries anything except a curated "programmer error" list, and for `requests`/`httpx`
  retries **only 5xx**. `retry_on` takes exception types or a callable.
- `timeout=` (seconds / `timedelta` / `TimeoutPolicy(run_timeout=, idle_timeout=,
  refresh_on="auto"|"heartbeat")`): `run_timeout` is a hard wall-clock cap;
  `idle_timeout` fires only when the node stops making *observable progress* (state
  writes, stream chunks, LLM tokens, child scheduling all refresh it; `"heartbeat"`
  narrows refresh to explicit `runtime.heartbeat()`). Raises typed `NodeTimeoutError`
  (`node, elapsed, kind, idle_timeout, run_timeout`); retryable by default; failed
  attempt's writes are cleared. Async-only — sync nodes with timeout rejected at compile.
- `error_handler=` — runs after retries exhaust, receives `(state, error: NodeError)`
  (frozen dataclass `node` + `error`), may return `Command(update=..., goto=...)` for
  saga/compensation routing. Failure provenance is checkpointed (handler sees the same
  NodeError after a crash-resume). One handler per node; handler exceptions bubble.
- Super-steps are **transactional** (any branch failure discards the step's state), but
  **pending writes** from the successful siblings are persisted so resume doesn't re-run them.
- `durability="exit"/"async"/"sync"` trades checkpoint safety vs speed per call.
- Node/`@task` caching: `cache_policy=CachePolicy(key_func=..., ttl=...)` + `compile(cache=...)`;
  cached hits marked `'__metadata__': {'cached': True}` in the update stream.

Operator verbs: resume-after-error = `invoke(None, config)`; **replay** = invoke with a
prior `checkpoint_id` (later nodes re-execute — interrupts re-fire); **fork** =
`update_state(...)` then `invoke(None, fork_config)`; server: `cancel(action="interrupt"|"rollback")`,
delete run, and the four double-texting strategies. **No bulk operator verbs, no
tolerance/failure-budget concept, no redrive-failed-items** — a partially failed
`Send` fan-out has no item-level retry surface at all; hypergraph's designed
`BatchTolerance` + `redrive(item_keys=...)` has no LangGraph counterpart.

## 7. Concurrency & flow control

| Control | Scope | Notes |
|---|---|---|
| Super-step parallelism | graph structure | all nodes triggered in a step run in parallel; transactional step |
| `Send(...)` fan-out | conditional edge | unbounded by default |
| `config={"configurable": {"max_concurrency": 10}}` | per-invocation | the only cap; inherited RunnableConfig key (https://docs.langchain.com/oss/python/langgraph/use-graph-api "Set max concurrency") |
| `defer=True` on a node | node | run only after all pending tasks finish (fan-in barrier) |
| `recursion_limit` | per-invocation | super-step budget; `RemainingSteps` managed value for graceful degradation in-graph |
| `multitask_strategy` | server, per thread×run | reject/enqueue/interrupt/rollback |
| Cross-run worker pools, rate limits, fairness keys, priorities, debounce | — | **NONE user-facing anywhere** (managed server internals) |

No rate limiting, no priority, no adaptive control (cf. panda's hyperlimit AIMD gate —
nothing like it here). Concurrency is a single number at invocation scope.

## 8. Scheduling & timers

- OSS: **nothing**. No durable sleep, no wake-at, no reminders. An `interrupt()` "waits
  indefinitely" but nothing wakes it. No timer primitive in nodes (contrast Temporal).
- Server: `client.crons.create(...)` / `create_for_thread(...)` with cron expressions,
  **UTC only**, same static input every firing; docs warn it is "**very** important to
  delete `Cron` jobs that are no longer useful. Otherwise you could rack up unwanted API
  charges to the LLM!" (https://docs.langchain.com/langsmith/cron-jobs)
- hypergraph's designed "scheduled answers" (auto-resolve a pause at a deadline) has no
  LangGraph equivalent; LangGraph's cron has no hypergraph equivalent. Both sides MISSING one.

## 9. The 3 steals (4)

**1. `TimeoutPolicy(idle_timeout=..., refresh_on="heartbeat")` + `runtime.heartbeat()` —
liveness-based timeouts instead of wall-clock guesses.** A node is killed only when it
stops *making progress*, and "progress" defaults to signals the runtime already sees
(state writes, stream chunks, LLM tokens) with an explicit-heartbeat mode for strict
control. This is exactly the fix for panda-style "timeout 240 killed a healthy slow
ingest" failures, and it composes with retry (writes cleared, clock reset per attempt).

```python
builder.add_node("call_model", call_model,
    timeout=TimeoutPolicy(idle_timeout=30, refresh_on="heartbeat"),
    retry_policy=RetryPolicy(max_attempts=3))

async def long_running_node(state, runtime: Runtime):
    for batch in fetch_batches():
        process(batch)
        runtime.heartbeat()
```

**2. The v3 run-stream object with typed projections.** One call returns an object where
`stream.messages`, `stream.values`, `stream.subgraphs`, `stream.interrupts`, and
`stream.output` are independently consumable views over one event flow — no
`if chunk["type"] == ...` dispatch in user code, and `stream.output` doubles as
"drive to completion and give me the result." `runner.iter` should grow this shape;
it also subsumes `show_progress` (a progress bar is just a built-in transformer).

```python
stream = graph.stream_events(input, version="v3")
for message in stream.messages:
    for token in message.text:
        print(token, end="", flush=True)
final_state = stream.output
if stream.interrupted:
    print(stream.interrupts)
```

**3. `set_node_defaults(...)` — one seam for graph-wide failure policy.** Retry, timeout,
error handler, cache defaults declared once at the builder, per-node values override,
resolved at compile; with a documented applicability matrix (error-handler nodes never
get a default `error_handler` — "Handlers must never catch themselves"). Hypergraph's
typed nodes need the same one-place policy seam rather than per-node kwargs everywhere.

```python
StateGraph(State).set_node_defaults(
    retry_policy=RetryPolicy(max_attempts=3),
    error_handler=default_error_handler,
    timeout=TimeoutPolicy(run_timeout=30),
)
```

**4. Per-dispatch policy override on fan-out: `Send(node, state, timeout=...)`.** The
map item, not just the node, carries the policy — "set a default timeout on the node and
tighten it for individual calls." `runner.map` / the durable Batch should accept
per-item overrides (timeout, retry) the same way.

```python
def fan_out(state):
    return [Send("process_item", {"item": item}, timeout=TimeoutPolicy(idle_timeout=15))
            for item in state["items"]]
```

Honorable mentions: `entrypoint.final(value=..., save=...)` (decouple return from
checkpoint), `runtime.execution_info.node_attempt` (switch to fallback provider on
retry N inside the node), `update_state(as_node=...)` fork ergonomics, and
`get_state_history` filter idioms.

## 10. The warnings (their own docs admit these)

1. **Streaming API churn — three coexisting formats.** v1 `stream()` output
  *shape-shifts* with options (raw dict → `(mode, data)` tuples → `(namespace, mode, data)`
  triples); v1.1 added `version="v2"` `StreamPart` dicts to fix it; v1.2 added a third
  API, `stream_events(version="v3")`, now "recommended for new applications." Migration
  tables and deprecation notes (`result["__interrupt__"]` dict access "deprecated and
  will be removed") litter the docs. Lesson: version-parameter escape hatches multiply;
  hypergraph should keep ONE event surface and evolve it additively.
2. **`Command(update=...)` as input silently strands a finished thread.** Docs carry an
  explicit WRONG/CORRECT block: any `Command` input resumes from the latest checkpoint,
  so using it to continue a conversation makes the graph "appear stuck." An overloaded
  input type (dict = fresh start, `None` = resume, `Command` = resume-with-value) is a
  user-visible trap.
3. **Interrupt rules are all convention, enforced by nothing** (hitl.md §3a): node-restart
  re-execution, exponential replay of in-node interrupt loops, try/except swallowing
  pauses, index-based resume matching, idempotency demands on pre-interrupt code. The
  1.2 docs now dedicate a "Rules of interrupts" section — i.e., the API can't stop you.
4. **Config-key confusion by design**: `recursion_limit` goes on `config` top-level while
  everything else user-defined goes under `configurable` ("Importantly... should not be
  passed inside the configurable key"); `max_concurrency` conversely lives *inside*
  `configurable`. Plus `config` vs `context` overlap since the 1.0 `context_schema` change.
5. **Private state leaks in streams**: `stream_mode="values"` emits ALL channels
  including "private" ones — input/output schemas constrain `invoke` but not `stream`
  (docs Warning; mitigation `output_keys=`).
6. **Checkpoint growth**: full channel values written every super-step; append-heavy
  channels (messages) balloon storage — acknowledged by the beta `DeltaChannel`
  (`>=1.2`, "API may change"), which brings its own hazards (pruning can silently
  corrupt reconstruction; a broken by-id lookup makes delta channels "reconstruct as
  empty — silently, with no error").
7. **Python <3.11 async holes**: `get_stream_writer()` doesn't work, configs must be
  hand-threaded into `ainvoke` — a whole recurring warning band across pages.
8. **Server-tier cliff**: background runs, queueing, double-texting, crons, webhooks,
  `join_stream` are all Agent Server (paid platform) features — "not available in the
  LangGraph open source framework." And `join_stream` has no replay: pre-join output is
  lost, exactly the gap hypergraph's `watch` (durable replay + live tail) closes.
9. **Migration limits**: renamed state keys lose saved state; interrupted threads can't
  survive node rename/removal ("if this is a blocker please reach out").

## 11. Verdict vs hypergraph

What they have that our design lacks at the API level: a **durability dial per call**
(`"exit"/"async"/"sync"` — notebook speed vs safety without changing stores), a **rich
per-node failure policy surface** (typed `RetryPolicy` with a smart default predicate,
idle-vs-run `TimeoutPolicy` with heartbeats, `error_handler` saga routing, and
`set_node_defaults` to declare it once), **first-class state surgery and time travel**
(`get_state_history` → `update_state(as_node=)` fork → `invoke(None, ...)`), **static
operator breakpoints at submit time**, per-dispatch `Send` overrides, cron, thread-scoped
streaming, and the genuinely lovely v3 typed-projection run stream. What we do better:
the entire **batch identity layer** (their map-reduce is anonymous fan-out with one
`max_concurrency` int — no item keys, no manifest, no tolerance, no redrive, no batch
watch), **background execution in-process** (`start_run` exists at Tier 0; theirs
requires the paid server), **watch with durable replay** (their `join_stream` drops
pre-join output), **submit dedup by fingerprint** (they only arbitrate concurrent runs
via double-texting), **typed/validated pause answers with provenance** (their resume is
schemaless, audit-free), and **honest result objects from day one** (they are still
migrating off magic dict keys). Net: don't envy their runtime — envy three ergonomic
moves and take them: liveness timeouts with heartbeats, the typed projection stream
object, and the one-seam node-defaults policy block; and treat their streaming-format
churn (v1/v2/v3 in one library) as the cautionary tale for why hypergraph's single
event surface should never grow a `version=` parameter.
