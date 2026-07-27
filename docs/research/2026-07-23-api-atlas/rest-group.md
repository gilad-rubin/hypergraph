# API Atlas — REST group: Temporal Python, Trigger.dev v3, pgflow, Resonate Python

Fetched 2026-07-23 from official docs (live pages as of this date). Lifecycle/HITL/ops
dimensions are already covered in workspace research and are referenced, not repeated:

- [engine comparison](https://github.com/gilad-rubin/superposition/blob/main/docs/research/2026-07-16-ingestion-lifecycle-primitives/engines.md) — full 7-dimension matrix on all four (fan-out, retry, HITL, state queries, idempotency, ops weight, store relationship)
- [HITL comparison](https://github.com/gilad-rubin/superposition/blob/main/docs/research/2026-07-16-ingestion-lifecycle-primitives/hitl.md) — Temporal signal/update/validator semantics in depth
- [durable execution landscape](../2026-07-21-durable-execution-landscape.md) — replay-vs-state philosophy and Temporal ID-conflict conventions already adopted into the Host design

This file covers what those do not: the user-facing **verb surface**.

---

# 1. Temporal Python

## Identity

Replay-based durable execution (deterministic workflows + journaled activities), the
reference implementation of that family. Python SDK GA, MIT, requires a dedicated
multi-service server cluster (see engines.md §Temporal). Docs: docs.temporal.io/develop/python.

## Authoring

Class-based workflow, decorated entrypoint, plain-function activities. Verbatim from
[samples-python/hello/hello_activity.py](https://github.com/temporalio/samples-python/blob/main/hello/hello_activity.py)
(Temporal's docs embed code from this repo):

```python
@activity.defn
def compose_greeting(input: ComposeGreetingInput) -> str:
    return f"{input.greeting}, {input.name}!"

@workflow.defn
class GreetingWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            compose_greeting,
            ComposeGreetingInput("Hello", name),
            start_to_close_timeout=timedelta(seconds=10),
        )

async with Worker(client, task_queue="hello-activity-task-queue",
                  workflows=[GreetingWorkflow], activities=[compose_greeting],
                  activity_executor=ThreadPoolExecutor(5)):
    result = await client.execute_workflow(GreetingWorkflow.run, "World",
        id="hello-activity-workflow-id", task_queue="hello-activity-task-queue")
```

Workflow code runs in a **sandbox** that blocks nondeterministic stdlib calls
(`datetime.date.today()`, `random`); escape hatches are `imports_passed_through()`,
`SandboxRestrictions`, `sandboxed=False`
([python-sdk-sandbox](https://docs.temporal.io/develop/python/python-sdk-sandbox)).
Multiple args discouraged: "use a single dataclass … fields added … backwards-compatible way."

## Run verbs

Sources: [temporal-client](https://docs.temporal.io/develop/python/temporal-client),
[message-passing](https://docs.temporal.io/develop/python/message-passing),
[child-workflows](https://docs.temporal.io/develop/python/child-workflows),
[continue-as-new](https://docs.temporal.io/develop/python/continue-as-new),
[schedules](https://docs.temporal.io/develop/python/schedules),
[observability](https://docs.temporal.io/develop/python/observability).

| Temporal verb | What it does | hypergraph equivalent |
|---|---|---|
| `Client.connect(addr)` | client to the server | implicit (in-process) / `RunHome.open` |
| `client.execute_workflow(Wf.run, arg, id=, task_queue=)` | start + block for result | `runner.run(graph, ...)` |
| `client.start_workflow(...)` → `WorkflowHandle` | fire, get handle | `runner.start_run` / `host.submit` → receipt |
| args on both: `id_reuse_policy`, `id_conflict_policy`, `search_attributes=TypedSearchAttributes([...])`, `start_delay=timedelta(...)`, `cron_schedule=`, `start_signal=` | dedup / metadata / delay / legacy cron / signal-with-start | submit dedup (adopted, landscape doc); scheduled answers; MISSING: start_delay, search attrs |
| `client.get_workflow_handle(workflow_id)` (`_for` typed variant) | re-attach to any run by id | `host.watch(wf)` (watch only; no general handle object) |
| `handle.result()` | await outcome | `handle.result()` |
| `handle.describe()` | status snapshot | Run Home read (host) |
| `handle.cancel()` | cooperative cancel | `host.stop(wf)` / `handle.stop()` |
| `handle.terminate()` | hard kill, no cleanup | **MISSING in hypergraph** (no force-kill verb) |
| `handle.signal(Wf.approve, arg)` | fire-and-forget durable write, no reply | closest: `host.answer` (pause-scoped only) |
| `handle.query(Wf.get_x, arg)` | sync read, `def` only, never in history | typed run-values inspection (Tier 0) / Run Home |
| `handle.execute_update(Wf.set_x, arg)` | tracked write, validated, returns result | `host.answer` (validated before settling — same shape) |
| `handle.start_update(..., wait_for_stage=WorkflowUpdateStage.ACCEPTED)` → `update_handle.result()` | async update with staged acknowledgment | MISSING (answer is one-shot) |
| `client.execute_update_with_start_workflow(Wf.add, arg, start_workflow_operation=WithStartWorkflowOperation(..., id_conflict_policy=USE_EXISTING))` | update-or-start atomically-ish (see hitl.md: NOT atomic) | submit dedup use-existing (adopted) |
| `workflow.execute_activity(fn, arg, start_to_close_timeout=, retry_policy=)` / `start_activity` | in-workflow step, blocking / handle | node execution (engine-internal, not a user verb) |
| `workflow.execute_child_workflow(Wf.run, arg, id=, parent_close_policy=)` / `start_child_workflow` | nested run; `ParentClosePolicy.TERMINATE` (default) / `ABANDON` / `REQUEST_CANCEL` | nested graphs (native); MISSING: parent-close policy knob |
| `workflow.continue_as_new(input)` + `workflow.info().is_continue_as_new_suggested()` | roll over to fresh history | not needed (state-based, no history growth) |
| `workflow.wait_condition(lambda: self.approved, timeout=)` | durable in-workflow pause on predicate | interrupt gates (declared, not inline) |
| `workflow.upsert_search_attributes([key.value_set(v)])` | run updates its own queryable metadata mid-flight | **MISSING** |
| `client.list_workflows('WorkflowType="X" AND CustomerId="c1"')` | SQL-ish List Filter over runs | Run Home queries (designed, narrower) |
| `client.create_schedule(id, Schedule(action=..., spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=...)])))` | managed recurring starts | **MISSING** (scheduled answers only) |
| schedule handle: `backfill / pause(note) / trigger / update(cb) / describe / delete` | schedule ops incl. run-now and re-run-a-window | **MISSING** |
| `Worker(client, task_queue=, workflows=[...], activities=[...])` | worker loop | `host.work_forever(worker_id=)`; declare-list convention already adopted (landscape doc) |

## Observation

`handle.describe()`, `@workflow.query` handlers, `list_workflows` + typed
`SearchAttributeKey.for_keyword/for_text` filters, Web UI over event history. No
push/stream verb on the client — observation is poll/query shaped; `host.watch`'s
durable-replay-plus-live-tail has no Temporal client equivalent (the UI plays that role).

## HITL / pause

Covered in hitl.md: signal (no reply) vs query (read-only, sync `def`) vs update
(validated, tracked; `@set_language.validator` rejects **before** anything enters
history). `workflow.wait_condition` + `workflow.all_handlers_finished` for draining.
Async handlers may call activities; docs push `asyncio.Lock` for handler/main-loop races.

## Failure & retry

`RetryPolicy(initial_interval, backoff_coefficient, maximum_interval, maximum_attempts,
non_retryable_error_types)` per activity/workflow; three timeout knobs per activity;
heartbeats for long activities. Operator verbs are CLI-first: `workflow reset` (rewind to
a history point), `activity reset/pause/unpause`, batch ops (engines.md §2).

## Concurrency & flow control

Scope = task queue + worker: `Worker(max_concurrent_activities=...)`, server-side
per-task-queue rate limits. No per-key/tenant fairness primitive at the API level.

## Scheduling & timers

Schedules API (above) with `ScheduleOverlapPolicy`; legacy `cron_schedule=`;
`start_delay=`; durable `workflow.sleep()`/timers inside workflows.

## Steals

1. **Uniform `execute_*` / `start_*` verb morphology** — every operation comes as a
   blocking form and a handle form with identical args: `execute_workflow/start_workflow`,
   `execute_activity/start_activity`, `execute_child_workflow/start_child_workflow`,
   `execute_update/start_update`. One rule teaches the whole surface. hypergraph's
   `run` vs `start_run` matches for runs but not for anything else.
2. **`get_workflow_handle(workflow_id)` as a universal re-attach** — any process turns an
   id into a full verb set (result/signal/query/update/cancel/terminate/describe). The
   host design has `watch`/`stop`/`answer` as separate host methods; a handle object would
   bundle them: `handle = client.get_workflow_handle(workflow_id="your-workflow-id")`.
3. **Run-authored search attributes** — `workflow.upsert_search_attributes([customer_id_key.value_set("customer_2")])`
   + `client.list_workflows('CustomerId="customer_2"')`: the run itself publishes typed,
   queryable facts mid-flight; operators filter fleets by them without a mirror table.
4. **`is_continue_as_new_suggested()`** — the engine advising the workflow about its own
   resource state is a lovely inversion, even though hypergraph doesn't need CAN itself.

## Warnings

- The **three-way signal/query/update split** is admitted accidental complexity: signals
  can't return values, queries must be sync `def` and can't mutate, updates need a live
  worker — docs spend pages on which to pick, plus `asyncio.Lock` guidance because
  handlers interleave with the main loop.
- **Update-with-Start is not atomic** (hitl.md): worker unavailable → workflow started,
  update lost; reconcile via workflow-id + update-id yourself.
- **Continue-as-new vs handlers footgun**, verbatim: "If you use Updates or Signals,
  don't call Continue-as-New from the handlers" — wait for `all_handlers_finished`.
- The **sandbox** is a real tax: normal imports break until wrapped in
  `imports_passed_through()`; escape hatch is turning protection off.
- `parent_close_policy` default `TERMINATE` silently kills children with the parent.
- CAN itself exists only because replay accumulates history — a whole verb users must
  learn to work around the engine's own storage model.

## Verdict vs hypergraph

Temporal's client surface is the most complete of the four: universal handle re-attach,
hard-terminate, staged updates, fleet-query via search attributes, and a full Schedules
verb set are all things the host design lacks or under-specifies. What hypergraph does
better: no determinism sandbox, no CAN ceremony, interrupt gates are graph-declared
rather than inline `wait_condition` code, and `watch` streams where Temporal polls.
Net: adopt the verb morphology, the handle object, and run-authored queryable metadata;
skip everything that exists to serve replay.

---

# 2. Trigger.dev v3

## Identity

TypeScript-native background-task platform; queue-dispatch + CRIU checkpoint/restore
(waits freeze the machine, no replay, no determinism contract). Apache-2.0; heaviest
self-host stack of the group and **no native Python** (engines.md §Trigger.dev).
Docs: trigger.dev/docs.

## Authoring

[tasks/overview](https://trigger.dev/docs/tasks/overview):

```ts
export const helloWorld = task({
  id: "hello-world",
  retry: { maxAttempts: 10, factor: 1.8, minTimeoutInMs: 500, maxTimeoutInMs: 30_000, randomize: false },
  queue: { concurrencyLimit: 1 },
  machine: { preset: "large-1x" },   // 4 vCPU, 8 GB RAM
  maxDuration: 300,
  run: async (payload: { message: string }, { ctx }) => { ... },
});
```

Lifecycle hooks on the same object: `onStartAttempt` (per attempt), `onSuccess`,
`onFailure` (after retries exhausted), `onComplete` (either way), `onWait`/`onResume`
(around checkpoints), `onCancel`, `catchError` (control retry decisions), `middleware`
(wraps `run` via `next()`). `init`/`cleanup`/`onStart` are **deprecated** in favor of
`middleware`/`onStartAttempt`. `schemaTask` adds payload validation.

## Run verbs

Sources: [triggering](https://trigger.dev/docs/triggering), [wait](https://trigger.dev/docs/wait),
[wait-for-token](https://trigger.dev/docs/wait-for-token),
[realtime/subscribe-to-run](https://trigger.dev/docs/realtime/subscribe-to-run),
[runs/metadata](https://trigger.dev/docs/runs/metadata),
[queue-concurrency](https://trigger.dev/docs/queue-concurrency),
[tasks/scheduled](https://trigger.dev/docs/tasks/scheduled),
[errors-retrying](https://trigger.dev/docs/errors-retrying).

| Trigger.dev verb | What it does | hypergraph equivalent |
|---|---|---|
| `await myTask.trigger(payload, opts)` → handle | enqueue, don't wait | `host.submit` → receipt |
| `await myTask.triggerAndWait(payload)` → `{ok, output \| error}`; `.unwrap()` throws (`SubtaskUnwrapError` has `runId`/`taskId`) | run child, get result envelope | `runner.run` (raises; no envelope) |
| `await myTask.batchTrigger([{payload}...])` → batch handle | enqueue batch | `host.submit(..., map_over=)` |
| `await myTask.batchTriggerAndWait([...])` → per-item `{ok,...}[]` | batch + per-item partial-failure results | `runner.map` → `MapResult` (similar) |
| `tasks.trigger<typeof emailSequence>("email-sequence", payload)` | backend trigger by id, typed by `typeof` import | `host.submit("graph", ...)` (string id, untyped) |
| `batch.trigger<typeof t1 \| typeof t2>([{id, payload}...])` / `batch.triggerByTaskAndWait` | one batch across **different** tasks | **MISSING** (batch is one graph) |
| trigger opts: `delay: "1h"`, `ttl: "2h"`, `idempotencyKey`, `maxAttempts`, `tags`, `metadata`, `queue`, `concurrencyKey` | per-call overrides | partial (workflow_id dedup; no delay/ttl/tags) |
| `runs.retrieve(runId)` / `runs.list(filters)` / `runs.cancel` / `runs.replay` ([management API](https://trigger.dev/docs/management/overview)) | inspect / operate on runs | Run Home read; `host.redrive` (new id + `retry_of` — more honest than in-place replay) |
| `runs.subscribeToRun<typeof myTask>(runId)` async iterator | push every change until finished; typed payload/output | `host.watch(wf)` (same shape; ours adds durable replay-from-start) |
| `runs.subscribeToRunsWithTag(tag)` / `subscribeToBatch(batchId)` | fleet / batch tails (never auto-complete) | batch child outcomes in `host.watch` |
| `wait.for({days: 1})` / `wait.until({date})` | durable sleep; >5s checkpoints, no compute billed | **MISSING as user verb** (host has `wake_at` on records, not a node-facing sleep) |
| `wait.createToken({timeout: "10m", idempotencyKey, tags})` → `{id, url, publicAccessToken}`; `wait.forToken<T>(tokenId)` → `{ok, output}`; `wait.completeToken<T>(tokenId, output)` | typed pause token; completable via SDK, REST, or the token's own webhook `url` | interrupt gate + `host.answer` (validated; no webhook URL, no timeout-as-result) |
| `wait.listTokens({status, tags})` / `retrieveToken(id)` | enumerate open pauses | pause slots on RunResult / Run Home |
| `metadata.set/append/increment/decrement/del/replace/flush`; `metadata.parent.set(...)`, `metadata.root.increment(...)` | run-authored live progress, writable **upward** from children; 256KB; read via `runs.retrieve` or Realtime | **MISSING** (progress is engine events only) |
| `queue({name, concurrencyLimit})`, task `queue:`, trigger-time `queue: "paid-users"` + `concurrencyKey: userId` | named queues, per-call routing, per-tenant queue copies | `max_active_runs` (host-global only) |
| `schedules.task({id, cron: {pattern, timezone}})`; `schedules.create({task, cron, externalId, deduplicationKey})`; payload has `timestamp/lastTimestamp/upcoming` | declarative + dynamic cron | **MISSING** |
| `retry.onThrow(fn, {maxAttempts})`, `retry.fetch(url, {retry: {byStatus: {"429": {strategy: "headers"}, "500-599": {strategy: "backoff"}}}})`, `AbortTaskRunError` | sub-block retries without redoing the task; non-retryable abort | **MISSING** (retry is node-scoped) |

## Observation

Realtime (`subscribeToRun`) + user-authored `metadata` is the progress story — the
subscription pushes the merged run object (status, metadata, output) on every change;
React hooks mirror it. Dashboard for the rest. This is the strongest *user-facing* batch
progress surface of the four.

## HITL / pause

`wait.forToken` (engines.md §3): free-form JSON typed only by TS generics — hypergraph's
graph-declared answer schema validated before settling is stronger; but the token's
auto-generated webhook `url` and `{ok:false}` timeout-as-value are better ergonomics.

## Failure & retry

Task-level `retry` config; per-call `maxAttempts`; sub-block `retry.onThrow`/`retry.fetch`;
`AbortTaskRunError`; `onFailure`/`catchError` hooks; per-item batch envelopes; dashboard
bulk replay by filter (engines.md).

## Concurrency & flow control

Scopes, explicitly: env-wide limit → named queue `concurrencyLimit` → `concurrencyKey`
(one queue **copy** per key value). "Only actively executing runs count towards
concurrency limits" — checkpointed waiters release their slot.

## Scheduling & timers

`schedules.task` (cron + timezone + per-env), dynamic `schedules.create` with
`externalId`/`deduplicationKey` for multi-tenant, `delay:` at trigger, `wait.for/until`.

## Steals

1. **`{ok, output | error}` result envelope + `.unwrap()`** — one shape for single,
   batch-item, and token results; caller chooses branching or throwing:
   `const result = await childTask.triggerAndWait("data"); if (result.ok) {...}` /
   `await childTask.triggerAndWait("data").unwrap()`.
2. **`metadata.parent` / `metadata.root`** — children push progress up the ancestor
   chain, observers read one object: `metadata.root.increment("totalProcessed", 1)`.
   The single best batch-progress idiom seen in this atlas.
3. **`concurrencyKey`** — per-tenant fairness as one trigger-time param: "creates a copy
   of the queue for each unique value of the key."
4. **Waits release concurrency slots** — a policy worth stating explicitly in the host
   ADRs: parked runs must not count against `max_active_runs`.

## Warnings

- **Lifecycle-hook churn**: `init`, `cleanup`, `onStart` all deprecated in-place
  (→ `middleware`/`onStartAttempt`) — a young surface still renaming itself.
- `subscribeToRunsWithTag`/`subscribeToBatch` iterators **never complete**; forgetting
  `break` leaks subscriptions (documented behavior).
- Metadata is **not propagated** to children automatically and silently no-ops outside a
  run context — two easy mis-assumptions called out in docs.
- Python is a Node-proxy build extension, not an SDK; self-host = Postgres + Redis +
  ElectricSQL + ClickHouse (engines.md) — the API's polish is fenced inside TS + their infra.

## Verdict vs hypergraph

Trigger.dev is the ergonomics benchmark: result envelopes, upward-writable metadata,
typed realtime subscriptions, per-tenant concurrency keys, sub-block retries, and cron —
each a user-facing feature hypergraph's design lacks or hides in engine events. What we
do better: Python-native, graph-declared typed pauses with server-side validation
(vs free-form token JSON), honest redrive lineage (`retry_of`) vs in-place replay, and a
one-store host vs their four-service stack. Net: steal surface shapes aggressively
(envelope, metadata rollup, concurrencyKey), keep our validation and lineage semantics.

---

# 3. pgflow

## Identity

Postgres-embedded DAG engine for Supabase: TS DSL compiles to SQL; state IS tables in
your database; workers are Supabase Edge Functions polling pgmq. Apache-2.0, pre-1.0
(v0.14.x), TS-only (engines.md §pgflow). Docs: pgflow.dev.

## Authoring

[understanding-flows](https://www.pgflow.dev/concepts/understanding-flows/),
[map-steps](https://www.pgflow.dev/concepts/map-steps/):

```ts
new Flow<Input>({ slug: 'myFlow' })
  .step({ slug: 'scrape' }, async (flowInput) => await scrapeWebsite(flowInput.url))
  .step({ slug: 'analyze', dependsOn: ['scrape'] },
        async (deps, ctx) => analyzeContent(deps.scrape.content))
  .array({ slug: 'searchResults' }, async (input) => await searchAPI(input))
  .map({ slug: 'processResults', array: 'searchResults' },
       (result) => extractRelevantData(result));
```

Root steps receive flow input; dependent steps receive `deps.<slug>` outputs — full TS
inference across the chain. Map handlers get the single element. Lifecycle: "Definition
(TypeScript) → Compilation [to SQL migration] → Registration [in the database] →
Execution." Per-flow/per-step `maxAttempts`, `baseDelay`, `timeout`.

## Run verbs

Source: [starting-flows](https://www.pgflow.dev/build/starting-flows/),
[monitor-execution](https://www.pgflow.dev/deploy/monitor-execution/) (via engines.md).

| pgflow verb | What it does | hypergraph equivalent |
|---|---|---|
| `SELECT pgflow.start_flow(flow_slug => 'sendEmail', input => '{"to":"a@b.c"}'::jsonb)` | start a run from **any SQL client**; returns a `run_id` row | `host.submit` (Python-only) |
| `const run = await pgflow.startFlow('process_video', { url })` (`PgflowClient(supabase)`) | typed client start | `host.submit` |
| `run.on('*', (event) => ...)` | live event subscription per run | `host.watch` / `runner.iter` |
| `await run.waitForStatus('completed')` | block until a target status | `handle.result()` (result-shaped, not status-shaped) |
| `pgflow.getRun(runId)` polling | re-attach/inspect by id | Run Home read |
| `SELECT ... FROM pgflow.runs / pgflow.step_states / pgflow.step_tasks` | state queries — taught as the primary monitoring interface | Run Home (private store; **no documented SQL contract**) |
| `cron.schedule('daily-sync', '0 2 * * *', $$ SELECT pgflow.start_flow('sync_data','{}') $$)` | recurring starts via pg_cron | **MISSING** |
| DB triggers calling `start_flow` | reactive starts on data change | **MISSING** (no event-source verb) |
| `.map({slug, array: 'dep'})` per-item tasks | fan-out with independent retry counters per element | `map_over=` children |
| Edge Function worker (deploy; no user loop verb) | execution plane | `host.work_forever` |

No HITL verb of any kind (engines.md: only durable `startDelay`). No manual
single-item retry / redrive verb (engines.md gap).

## Observation

SQL is the observation API — docs ship verbatim monitoring queries against
`pgflow.runs`/`step_states`/`step_tasks`; plus per-run client events. No dashboard.

## HITL / pause — none (engines.md §3).

## Failure & retry

Per-step `maxAttempts`/`baseDelay` (exponential); map items retry independently, **but**:
"if exhausted, fails the entire step" and "the entire run is marked as failed" — no
tolerance knob, no quarantine, no redrive.

## Concurrency & flow control

Worker-level polling batch size only; no queues/keys/priorities at the API surface.

## Scheduling & timers

pg_cron composition (above) + durable `startDelay` per step (engines.md). No wake-at verb.

## Steals

1. **Start = a SQL RPC** — `SELECT pgflow.start_flow(...)` makes every language, trigger,
   and cron client a first-class starter with zero SDK. The host equivalent would be a
   documented "insert a submit row / call one SQL function" contract on the Run Home.
2. **State tables as documented API** — pgflow versions its state schema and teaches
   `SELECT`s against it as the monitoring interface; the strongest "no mirror table"
   story reviewed (engines.md dimension 4). Run Home could publish a read-view contract.
3. **`array:` names the fan-out source** — `.map({ slug: 'processResults', array: 'searchResults' })`
   binds map-to-producer declaratively, order preserved by `task_index`; compare
   hypergraph's runtime `map_over="doc"` — pgflow's is graph-visible and type-checked.
4. **`run.waitForStatus('completed')`** — status-addressed awaiting (wait for `paused`,
   not just terminal) is a nice verb hypergraph handles only implicitly via `result.paused`.

## Warnings

- Map-item retry exhaustion **fails the whole run** — the docs' own retry-isolation pitch
  ends at a cliff; no per-item tolerance (hypergraph's `BatchTolerance` is exactly the
  missing knob).
- No HITL, no manual retry, no Python; worker story outside Supabase Edge Functions
  undocumented (engines.md) — the elegant surface is fenced inside one vendor's runtime.

## Verdict vs hypergraph

pgflow's contribution is not the DSL (hypergraph's graph model is richer) but the
**substrate honesty**: start-by-SQL and query-by-SQL make the engine's whole surface
usable from anything that can reach Postgres. hypergraph's Run Home is one store by
design already — publishing a stable read (and maybe submit) SQL contract would buy
pgflow's best property. We are better on literally every lifecycle dimension that
matters to Panda-style ingestion: tolerance, redrive, pauses, language. Net: steal the
substrate contract, ignore the engine.

---

# 4. Resonate Python

## Identity

"Distributed async await": plain async functions made durable via journaled context
calls and Durable Promises (replay-to-checkpoint, promise id = identity). Single Rust
binary or **no server at all**; Apache-2.0, pre-1.0, Python ≥3.12 (engines.md §Resonate).
Docs: [docs.resonatehq.io/develop/python](https://docs.resonatehq.io/develop/python).

## Authoring

```python
resonate = Resonate()                                  # local: in-process, zero infra
resonate = Resonate(url="http://localhost:8001")       # remote: durable via server

@resonate.register
async def foo(ctx, arg):
    r = await ctx.run(bar, arg)          # local child, journaled
    return r
```

Same function code in both modes — the constructor is the durability switch.

## Run verbs

| Resonate verb | What it does | hypergraph equivalent |
|---|---|---|
| `handle = resonate.run("invocation-id", foo, arg)`; `await handle.result()` | invoke locally; **id is the first positional arg**, promise-id dedup = idempotency | `runner.run(graph, ..., workflow_id=)` (id optional!) |
| `resonate.rpc("id", "foo", arg)` → handle | invoke on a remote worker by function name | `host.submit("graph", ...)` |
| `.begin_run` / `.begin_rpc` (handle-now variants; options chain on `.run/.beginRun/.rpc/.beginRpc/.detached`) | fire, don't await | `runner.start_run` / submit |
| `handle = await resonate.get("invocation-id")` | **subscribe to an existing invocation by id** | `host.watch` (no result-handle form) |
| `resonate.schedule("scheduled-foo", "0 * * * *", "foo", args=(1,))` | cron-start a durable function | **MISSING** |
| `ctx.run(bar, arg)` / `ctx.rpc("bar", arg)` | awaited child, local / remote | nested graph call |
| `ctx.detached("bar", arg)` → future with `.id()` | fire-and-forget child that outlives the parent | **MISSING** (parent-close semantics implicit) |
| `await ctx.sleep(timedelta(seconds=5))` | durable sleep | MISSING as node verb (host `wake_at` only) |
| `approval = ctx.promise(); await approval` | durable promise pause (HITL) | interrupt gate |
| `resonate.promises.create(id=, timeout_at=, param=Value(...))` / `.resolve(id=, value=Value(data={"approved": True}))` / `.reject(...)` | external settle, SDK or bare HTTP | `host.answer` (ours validates against declared schema; theirs is free-form) |
| `.options(timeout=timedelta(60), target="workers", version=1, retry_policy=Exponential(delay=1, factor=2, max_delay=30, max_retries=10))` | per-call policy chain; `target` routes to a worker group | per-call config nest; MISSING: target routing |
| `resonate.stop()` | graceful shutdown | host lifecycle |
| worker = registering functions + staying alive (remote mode) | execution plane | `host.work_forever` |

## Observation

`handle.result()`, `resonate.get(id)`, CLI `resonate promises get/search` (engines.md
dimension 4). No event stream, no progress surface, no dashboard.

## HITL / pause

`ctx.promise()` + external resolve — the cleanest pause primitive of the four (one
concept for child calls, sleeps, and human input: everything is a promise). Free-form
payload; no answer-schema validation (hypergraph stronger there).

## Failure & retry

`retry_policy=Exponential/Linear/Constant/Never` via `.options()`; `timeout=` per call;
recovery = replay-to-checkpoint. No operator force-retry/redrive verb (engines.md).

## Concurrency & flow control

`target=` group routing only — no concurrency caps, rate limits, or fairness keys.

## Scheduling & timers

`resonate.schedule(id, cron, fn)` + durable `ctx.sleep`.

## Steals

1. **Identity-first calling convention** — every invocation names its id up front
   (`resonate.run("invocation-id", foo, arg)`), so dedup, recovery, and attach all fall
   out of one habit. hypergraph's optional `workflow_id` invites anonymous runs that
   later can't be watched or redriven; the host tier could require it (it does, via
   submit) — Tier 0 could at least auto-surface it.
2. **`resonate.get(id)` → handle** — attaching to any invocation and awaiting its result
   is one verb; the host design has `watch` (stream) but no "give me a result handle for
   an existing workflow_id."
3. **The constructor is the durability tier** — `Resonate()` vs `Resonate(url=...)`,
   same user code: the cleanest expression of hypergraph's own Tier-0-to-host promise;
   worth mirroring as the story for `runner` vs `serve(graph.with_runner(...))`.
4. **One primitive, many verbs** — sleep, child call, HITL are all durable promises;
   the API teaches one concept instead of three.

## Warnings

- **Naming churn across generations**: older docs/examples use `ctx.lfc/lfi/rfc/rfi` and
  `.begin_run/.begin_rpc`; current pages teach `ctx.run/ctx.rpc/ctx.detached` — both
  vocabularies live in the wild (their FaaS example still explains `ctx.rfc`/`ctx.rfi`),
  plus a generator→asyncio migration and a Python ≥3.12 floor. Renaming core verbs
  pre-1.0 burned their example corpus; a caution for hypergraph's own verb renames.
- Pre-1.0 maturity (server 0.9.x, SDK 0.7.x, 39 issues / 31 stars — engines.md); no
  fan-out/map primitive, no tolerance, no operator surface.

## Verdict vs hypergraph

Resonate is a philosophy peer more than a feature peer: identity-first invocation,
attach-by-id, and the constructor-as-tier story are all shapes hypergraph should own at
the API level, and its promise-unification is a benchmark for conceptual economy. It
has nothing to teach us about batches, tolerance, observation, or operator verbs — the
areas where hypergraph's design is strongest. Net: steal the calling conventions,
not the engine.

---

# Combined verdict vs hypergraph

Across the four, the recurring gaps in hypergraph's user-facing surface are: (1) **no
universal re-attach handle** — Temporal's `get_workflow_handle` and Resonate's
`resonate.get(id)` both turn an id into the full verb set, where the host design
scatters watch/stop/answer as host methods and has no "result handle for an existing
run"; (2) **no user-authored progress channel** — Trigger.dev's `metadata.root.increment`
rollup is the best batch-progress idiom reviewed, and hypergraph offers only
engine-emitted events; (3) **no scheduling verb anywhere** (all four have cron; the host
has only `wake_at`-style scheduled answers); (4) **no per-tenant fairness**
(`concurrencyKey`) and no sub-node retry (`retry.onThrow`); (5) **no documented substrate
contract** — pgflow's start-by-SQL/query-by-SQL shows what a stable Run Home read
contract would buy. Where hypergraph is ahead of all four: graph-declared, typed,
server-validated answers (only Temporal's update validators compare), honest
redrive-with-lineage instead of in-place replay, batch tolerance as a first-class
submit parameter (only Step Functions has an analog, none of these four), and a
zero-ceremony local tier that three of the four cannot offer in Python at all.
Recommendation: keep the state-based core and validation semantics untouched; adopt
Temporal's execute/start verb morphology and handle object, Trigger.dev's result
envelope + metadata rollup + concurrencyKey, Resonate's identity-first convention, and
publish a pgflow-style read contract on the Run Home.
