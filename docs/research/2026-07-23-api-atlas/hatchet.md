# API Atlas — Hatchet (Python SDK, v1)

Sources: docs.hatchet.run (v1 docs, fetched 2026-07-23; three doc trees are live —
`/home/*`, `/v1/*`, `/sdks` + `/reference`, plus legacy `v0-docs.hatchet.run`).
Lifecycle/infra/DAG-retry strengths already covered in
`superposition/docs/research/2026-07-16-ingestion-lifecycle-primitives/engines.md` (Hatchet section)
and `hypergraph/docs/research/2026-07-21-durable-execution-landscape.md` — not repeated here.

## 1. Identity

Postgres-backed task orchestration engine (MIT): durable queue + DAG scheduler by default,
with an *opt-in* replay-based durable-execution layer (`durable_task`) on top — queue-first,
replay-second philosophy. SDKs: Python / TypeScript / Go (Ruby appearing); v1 API is a 2025
ground-up SDK rewrite (v0 fully deprecated). Server is a separate process + its own Postgres DB.

## 2. Authoring

Two shapes: a standalone task, or a workflow object that collects tasks into a DAG.
([docs.hatchet.run/home/your-first-task](https://docs.hatchet.run/home/your-first-task))

```python
class SimpleInput(BaseModel):
    message: str

@hatchet.task(name="first-task", input_validator=SimpleInput)
def first_task(input: SimpleInput, ctx: Context) -> SimpleOutput:
    return SimpleOutput(transformed_message=input.message.lower())
```

DAG: `parents=` takes **task object references** (not strings — a v1 fix), and parent output
is read back typed via `ctx.task_output(step1)`.
([docs.hatchet.run/home/dags](https://docs.hatchet.run/home/dags))

```python
dag_workflow = hatchet.workflow(name="DAGWorkflow")

@dag_workflow.task(execution_timeout=timedelta(seconds=5))
def step1(input: EmptyModel, ctx: Context) -> StepOutput: ...

@dag_workflow.task(parents=[step1, step2])
async def step3(input: EmptyModel, ctx: Context) -> RandomSum:
    one = ctx.task_output(step1).random_number
    two = ctx.task_output(step2).random_number
    return RandomSum(sum=one + two)
```

Extras on the workflow object: `@wf.on_failure_task()` (runs last if any task failed; reads
`ctx.task_run_errors`) and `@wf.on_success_task()`; one of each per workflow.
([docs.hatchet.run/home/on-failure-tasks](https://docs.hatchet.run/home/on-failure-tasks))
Durable authoring is a separate decorator + context type: `@hatchet.durable_task()` with
`ctx: DurableContext` ([docs.hatchet.run/v1/durable-tasks](https://docs.hatchet.run/v1/durable-tasks)).
Sync and async task functions both allowed; async client methods are uniformly `aio_`-prefixed.

## 3. Run verbs

The task/workflow **object is the client**: `.run()`, `.schedule()`, `.create_cron()` all hang
off the thing you defined. ([docs.hatchet.run/home/running-your-task](https://docs.hatchet.run/home/running-your-task),
[/sdks/python/runnables](https://docs.hatchet.run/sdks/python/runnables))

| Hatchet verb | What it does | hypergraph equivalent |
|---|---|---|
| `task.run(input)` / `await task.aio_run(input)` | Enqueue, block until done, return typed result | `await runner.run(graph, inputs)` (ours is in-process) |
| `task.run(input, wait_for_result=False)` / `task.run_no_wait()` / `aio_run_no_wait()` | Enqueue, return `WorkflowRunRef` immediately | `runner.start_run(...)` handle / designed `host.submit(...)` receipt |
| `ref.result()` / `ref.aio_result()`, `ref.workflow_run_id` | Later join on a detached run | `handle.result()` / `handle.done` |
| `task.run_many([task.create_bulk_run_item(input=...), ...])` / `aio_run_many` | Bulk fan-out, waits for **all**, returns list of results ([/home/bulk-run](https://docs.hatchet.run/home/bulk-run)) | `runner.map(...)` (ours has `map_over` sugar; theirs is explicit item list) |
| `run_many_no_wait` / `aio_run_many_no_wait` | Bulk fan-out, detached, list of refs | `runner.start_map(...)` / designed `host.submit(map_over=...)` |
| `create_bulk_run_item(input, key=None, options...)` | Per-item trigger config: input + dedup `key` + metadata/priority | MISSING as a typed per-item config (our per-item identity = item_keys, designed) |
| child spawn: call `child_wf.run(...)` / `aio_run_many(...)` **inside a task** | Runs are auto-associated to parent in dashboard ([/home/child-spawning](https://docs.hatchet.run/home/child-spawning)) | Tier-0 nodes can call runner again, but no tracked parent/child lineage |
| `task.schedule(run_at_datetime, input)` → schedule id | One-shot future run ([/home/scheduled-runs](https://docs.hatchet.run/home/scheduled-runs)) | MISSING (host has scheduled *answers* only, in design) |
| `task.create_cron(cron_name, expression, input, additional_metadata=...)` / declarative `on_crons=[...]` | Recurring runs ([/home/cron-runs](https://docs.hatchet.run/home/cron-runs)) | MISSING |
| `hatchet.event.push(key, payload, scope=..., additional_metadata=...)` + `on_events=["user:create", "subscription:*"]` | Event-triggered runs, wildcards, scoped CEL filters ([/v1/external-events/run-on-event](https://docs.hatchet.run/v1/external-events/run-on-event)) | MISSING (host is submit-only by design) |
| `hatchet.runs.create(workflow_name, input, ...)` | Trigger **by name string**, documented as an escape hatch ([/reference/python/feature-clients/runs](https://docs.hatchet.run/reference/python/feature-clients/runs)) | `host.submit("graph", ...)` — ours is by-name as the primary verb |
| `worker = hatchet.worker(name, slots=10, workflows=[...]); worker.start()` | Worker entrypoint ([/home/workers](https://docs.hatchet.run/home/workers)) | `host.work_forever(worker_id=...)` (designed) |
| `task.mock_run(input=..., parent_outputs=...)` | Unit-test a task without an engine | in-process Tier 0 *is* this — no mock needed |
| — | Backpressured streaming of batch results as they finish | `runner.map_iter(...)` — MISSING in Hatchet (`run_many` is all-or-list) |
| — | Live event iterator of one run's node events | `runner.iter(...)` — MISSING (closest: log/stream subscribe) |

Sync/async duplication is total and mechanical: every verb exists as `x` and `aio_x`.

## 4. Observation

- **Status/result pulls**: `hatchet.runs.get(run_id)`, `get_status(run_id)`, `get_result(run_id)`,
  `runs.list(since=..., statuses=[...], workflow_ids=[...], additional_metadata=..., worker_id=...,
  parent_task_external_id=...)` ([/reference/python/feature-clients/runs](https://docs.hatchet.run/reference/python/feature-clients/runs)).
  Also `workflow.list_runs(...)` directly on the object.
- **Streaming**: inside a task `await ctx.aio_put_stream(chunk)`; consumer:
  `async for chunk in hatchet.runs.subscribe_to_stream(ref.workflow_run_id)` — shown proxied
  straight into a FastAPI `StreamingResponse` ([/home/streaming](https://docs.hatchet.run/home/streaming)).
  **Live-only**: "You must begin consuming the stream before any events are published. Any events
  published before a consumer is initialized will be dropped." (Our designed `host.watch()` is
  durable-replay + live tail — strictly stronger.)
- **Dashboard**: run DAG view, per-task I/O, logs, metrics; manual trigger with custom input,
  metadata, and scheduled time; filter runs by `additional_metadata` key-values.
  Quirk admitted in docs: metadata filters match runs with **OR**, events with **AND**
  ([/home/additional-metadata](https://docs.hatchet.run/home/additional-metadata)).
- Batch watching = the parent's run page (children auto-associated) or `runs.list` polling with
  `parent_task_external_id`. No push-based batch outcome feed (our designed `watch` emits child
  outcomes).
- Side clients: `hatchet.metrics`, `hatchet.logs`, `hatchet.workers`
  ([/sdks/python/client](https://docs.hatchet.run/sdks/python/client)).

## 5. HITL / pause

Two mechanisms, both event-based — there is no first-class "pause slot / answer" object:

1. **Durable wait** (imperative, inside `durable_task`)
   ([/v1/durable-event-waits](https://docs.hatchet.run/v1/durable-event-waits)):

```python
event = await ctx.aio_wait_for_event(
    key="user:create",
    expression=f"input.user_id == {input.user_id}",   # CEL over event payload
    scope=f"user_id:{input.user_id}",                  # narrows candidates; required for lookback
    lookback_window=timedelta(minutes=1),              # match events pushed BEFORE the wait started
    payload_validator=LookbackEventPayload,            # Pydantic-validate the resolved payload
)
```

   The "answer" is `hatchet.event.push(key, payload, scope=...)`. `payload_validator` types the
   received payload, but validation failure is a task error — there is no reject-and-keep-waiting
   semantics like our designed `host.answer` (validate against declared schema *before* settling;
   pause survives rejection).
2. **Declarative wait conditions on DAG tasks** — approval gates drawn into the graph shape
   ([/home/conditional-workflows](https://docs.hatchet.run/home/conditional-workflows)):

```python
@task_condition_workflow.task(
    parents=[start],
    wait_for=[or_(
        SleepCondition(duration=timedelta(minutes=1)),
        UserEventCondition(event_key="wait_for_event:start"),
    )],
)
def wait_for_event(input: EmptyModel, ctx: Context) -> StepOutput: ...
```

   Plus `skip_if=[ParentCondition(parent=..., expression="output.random_number > 50")]`,
   `cancel_if`, downstream `ctx.was_skipped(task)`. Multiple `or_()` groups AND together.

Waits free the worker slot (durable tasks are evicted while waiting and replayed to the
checkpoint on wake). Durable-task code must be deterministic between checkpoints: "Durable tasks
must only either call methods on the durable context or spawn children"
([/v1/durable-tasks](https://docs.hatchet.run/v1/durable-tasks)).

## 6. Failure & retry surface

User-configured, per task ([/home/retry-policies](https://docs.hatchet.run/home/retry-policies),
[/home/timeouts](https://docs.hatchet.run/home/timeouts)):

```python
@backoff_workflow.task(retries=10, backoff_max_seconds=10, backoff_factor=2.0)
def backoff_task(input: EmptyModel, ctx: Context) -> dict[str, str]:
    if ctx.retry_count < 3:
        raise Exception("backoff task failed")
```

- `raise NonRetryableException(...)` short-circuits the policy.
- `execution_timeout` (default 60s) vs `schedule_timeout` (default 5m, queue-wait budget) — both
  `timedelta`; timeout = failure = retryable. `ctx.refresh_timeout(timedelta(...))` is **additive**
  from inside the task.
- Error routing: `@wf.on_failure_task()` (reads `ctx.task_run_errors`); DAG semantics
  (failed task retries alone, downstream stays blocked) covered in engines.md.
- No batch tolerance concept (our designed `BatchTolerance(count=10)`) — bulk runs have
  `return_exceptions=True` at the collect site, that's all.

Operator verbs ([/home/bulk-retries-and-cancellations](https://docs.hatchet.run/home/bulk-retries-and-cancellations)):

```python
hatchet.runs.replay(run_id); hatchet.runs.cancel(run_id)
hatchet.runs.bulk_cancel(BulkCancelReplayOpts(ids=workflow_run_ids))
hatchet.runs.bulk_replay(BulkCancelReplayOpts(filters=RunFilter(
    since=..., until=..., statuses=[V1TaskStatus.RUNNING],
    workflow_ids=[...], additional_metadata={"key": "value"},
)))
```

The same `RunFilter` vocabulary drives `runs.list` — select-then-act symmetry. Replay **with
edited input** is a dashboard affordance (edit the JSON on the run page, replay from the failed
step; documented since v0 — [v0-docs manual retries](https://v0-docs.hatchet.run/home/features/retries/manual));
the API-level `replay(run_id)` takes no new input — programmatic "redrive with fix" is
`runs.create` with new input, losing lineage (our designed `host.redrive` keeps `retry_of` lineage
+ `item_keys` subset — stronger).
Cancellation delivery: sync tasks poll `ctx.exit_flag`; async tasks get cooperative cancel /
`await ctx.aio_cancel()` ([/home/cancellation](https://docs.hatchet.run/home/cancellation)).

## 7. Concurrency & flow control

- **Workflow-level concurrency keys** — CEL over input, with an explicit overflow strategy
  ([/home/concurrency](https://docs.hatchet.run/home/concurrency)):

```python
concurrency_workflow = hatchet.workflow(
    name="WorkflowName",
    concurrency=ConcurrencyExpression(
        expression="input.user_id",
        max_runs=1,
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)
```

  Strategies: `GROUP_ROUND_ROBIN` (fair round-robin across key groups, excess queues),
  `CANCEL_IN_PROGRESS` (new run preempts the running one — debounce-latest),
  `CANCEL_NEWEST` (running run wins, new one is cancelled).
  `concurrency=[expr1, expr2]` ANDs multiple dimensions (e.g. per-user AND per-resource).
- **Rate limits, static**: `hatchet.rate_limits.put("external-api", 100, RateLimitDuration.MINUTE)`
  declared upfront, consumed by tasks; **dynamic**: keyed by CEL at run time
  ([/home/rate-limits](https://docs.hatchet.run/home/rate-limits)):

```python
@rate_limit_workflow.task(rate_limits=[RateLimit(
    dynamic_key="input.user_id", units=1, limit=10, duration=RateLimitDuration.MINUTE,
)])
def step_2(input: RateLimitInput, ctx: Context) -> None: ...
```

  Scope note from docs: "the same rendered cel on multiple steps will be treated as one global
  rate limit" — limits are engine-global per rendered key, not per-task.
- **Worker capacity**: `slots=` per worker (default 100), "slots are a local limit"; durable
  waits release slots. **Priorities**: 1/2/3 via `default_priority=` on the workflow or
  `priority=` at trigger time — but "priority only affects multiple runs of a single workflow"
  ([/home/priority](https://docs.hatchet.run/home/priority)).
- **Placement**: worker `labels={"model": "fancy-ai-model-v2", "memory": 512}` +
  per-task `desired_worker_labels` with `comparator` / `required` / `weight`
  ([/v1/advanced-assignment/worker-affinity](https://docs.hatchet.run/v1/advanced-assignment/worker-affinity));
  sticky assignment `sticky=StickyStrategy.SOFT|HARD` on the workflow, `sticky=True` on child
  spawn — beta, HARD can stall runs if the worker dies
  ([/v1/advanced-assignment/sticky-assignment](https://docs.hatchet.run/v1/advanced-assignment/sticky-assignment)).

Scopes in one line: concurrency = workflow (keyed groups, engine-wide); rate limits = engine-wide
per key; slots = per worker; priority = per workflow queue; affinity/sticky = per assignment.

## 8. Scheduling & timers

- One-shot: `simple.schedule(datetime(2025, 3, 14, 15, 9, 26))`; managed via
  `hatchet.scheduled.list/update/delete/bulk_delete/bulk_update` — bulk delete accepts filters
  (`workflow_id`, `statuses`, `additional_metadata`). "The scheduled time is when Hatchet
  enqueues the task, not when the run starts."
- Cron: declarative `on_crons=["* * * * *"]` or dynamic `create_cron(cron_name=..., expression=...,
  input=...)` with enforced name uniqueness; UTC only; missed schedules are skipped, not made up;
  deleting a cron does not cancel in-flight runs.
- In-run timers: `await ctx.aio_sleep_for(timedelta(seconds=5))` in durable tasks — "guaranteed
  to respect the original duration across interruptions … worker crashes"
  ([/v1/durable-sleep](https://docs.hatchet.run/v1/durable-sleep)); `SleepCondition` for
  declarative DAG delays.

## 9. The 3 steals

1. **The task object is the client.** Every verb — `run`, `aio_run`, `run_no_wait`, `run_many`,
   `schedule`, `create_cron`, `list_runs`, `mock_run` — lives on the object you defined; child
   spawning is *the same call* made inside a task, and lineage is captured implicitly. One noun,
   one verb family, zero client plumbing. Our runner/host split is principled but makes the user
   learn two nouns; a `graph.on(host).run(...)`-shaped affordance (or verbs on a bound handle)
   would keep zero-ceremony feel in the durable tier.
   ```python
   result = await child_wf.aio_run(ChildInput(a="b"))   # same line inside or outside a task
   ```
2. **Select-then-act operator symmetry.** `RunFilter` is one vocabulary for querying
   (`runs.list`) and for bulk mutation (`bulk_cancel`, `bulk_replay`) — the operator "select by
   time/status/workflow/metadata, then cancel or replay the selection" needs no new concepts.
   Our designed host has per-run `stop`/`redrive` but no filterable bulk surface at all.
   ```python
   hatchet.runs.bulk_cancel(BulkCancelReplayOpts(filters=RunFilter(
       since=..., statuses=[V1TaskStatus.RUNNING], additional_metadata={"key": "value"})))
   ```
3. **Declarative wait/skip conditions in the graph shape.** `wait_for=or_(SleepCondition(...),
   UserEventCondition(...))` + `skip_if=[ParentCondition(...)]` + `ctx.was_skipped(task)` turn
   "approval with a 24h timeout escape" into DAG structure that the dashboard can render, instead
   of imperative code inside a node. Our interrupt gates are imperative-only; a declared
   sleep-or-answer race per gate would make pause topology visible to tooling.
4. **One expression language everywhere (CEL).** Concurrency keys, dynamic rate-limit keys, event
   filters, wait-event matching, skip conditions — all the same small language over
   `input`/`output`/`additional_metadata`/`event_key`. Learn once, reuse in five features; and
   `ConcurrencyExpression(expression="input.user_id", max_runs=1, limit_strategy=...)` is the
   most compact per-key fairness/debounce API in this space.

## 10. The warnings

- **The v0 → v1 rewrite is a confession list** ([/home/v1-sdk-improvements](https://docs.hatchet.run/home/v1-sdk-improvements),
  [/home/migration-guide-python](https://docs.hatchet.run/home/migration-guide-python)):
  magic-string parent refs (`parents=["step1"]`), untyped `context.workflow_input()` dicts,
  `context.spawn_workflow()` as a special child verb, string durations (`"10s"`), protobuf enums
  leaking into user code, `sync_to_async` helpers, `max_runs`→`slots` rename. Every one of these
  had to be breaking-changed away. Lesson for hypergraph: object references over names in
  authoring APIs, typed inputs from day one, and *no special verb for child work* — those are the
  mistakes a queue-first API makes when typing arrives late.
- **Live-only streams drop early events.** "Any events published before a consumer is initialized
  will be dropped" ([/home/streaming](https://docs.hatchet.run/home/streaming)) — the docs
  work around it with sleeps. Validates our watch-as-durable-replay+tail decision; do not ship a
  live-only observation channel.
- **Durable tasks import Temporal's determinism contract.** "Durable tasks must only either call
  methods on the durable context or spawn children, and they must be deterministic given the
  event history" ([/v1/durable-tasks](https://docs.hatchet.run/v1/durable-tasks)) — so the
  "durable" decorator silently changes what code is legal inside the function, a trap the base
  `@hatchet.task` never has. Two decorators, two execution semantics, one-word difference.
- Smaller but real: metadata filtering is OR for runs and AND for events (admitted in
  [/home/additional-metadata](https://docs.hatchet.run/home/additional-metadata)); priority is
  meaningless across workflows; sticky assignment is beta and `HARD` pins runs to dead workers;
  API `replay(run_id)` cannot edit input (dashboard-only); three overlapping doc trees
  (`/home`, `/v1`, `/sdks`+`/reference`, plus v0-docs) with many dead cross-links — the sprawl of
  two API generations documented at once.

## 11. Verdict vs hypergraph

At the user-facing API level Hatchet is ahead of our *designed* durable tier on the whole
operator-and-flow-control belt: filterable bulk cancel/replay with list/act symmetry, keyed
concurrency with three explicit overflow strategies, static+dynamic rate limits, priorities,
scheduled-run and cron management clients, scoped event triggers with CEL filters, worker
labels/affinity/sticky, and object-as-client ergonomics where triggering, scheduling, child
spawning, and testing are all verbs on the thing you defined. We are ahead on exactly the things
their docs apologize for: state-based recovery with **no determinism contract** (their durable
decorator forbids ordinary code inside the function), a true in-process Tier 0 (their `mock_run`
exists because nothing runs without the engine), backpressured `map_iter`/`iter` result streaming
(their `run_many` blocks for the full list and their stream drops pre-subscribe events, versus
our durable-replay `watch`), typed answer validation with reject-and-keep-pending (their
`payload_validator` fails the wait), submit fingerprint dedup, and lineage-preserving `redrive`
(their programmatic re-run with new input is `runs.create`, which forgets ancestry).
Net: keep the recovery model and evidence-honesty decisions untouched; steal the operator belt —
a `RunFilter`-style select-then-act bulk surface and a `ConcurrencyExpression`-style keyed
gate/fairness config are the two highest-value additions, followed by declarative
sleep-or-answer races on interrupt gates, and consider verb-on-the-graph ergonomics so the host
tier does not cost the notebook user a second mental model.
