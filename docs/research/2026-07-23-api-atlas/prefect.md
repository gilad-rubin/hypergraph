# API Atlas — Prefect 3 vs hypergraph

Researched 2026-07-23 against https://docs.prefect.io/v3 (docs are unversioned "v3
latest"; schedule classes note "Prefect >= 3.1.16"; the 2→3 upgrade guide is the
source for admitted breaking changes). Workspace research already covers Prefect's
pause/suspend HITL in depth (superposition `hitl.md` §2e) and its `serve()`
list-of-deployments convention (hypergraph `2026-07-21-durable-execution-landscape.md`,
"Framework API conventions") — referenced below, not repeated.

---

## 1. Identity

Python-native workflow orchestrator: plain functions become flows/tasks via
decorators; execution is **state-machine orchestration** (states + retries +
caching), NOT replay — no determinism contract, closest of the big engines to
hypergraph's state-based philosophy. Mature (3.x since 2024, huge install base),
server+UI optional: flows run fine with zero infrastructure and upgrade to a
server/work-pool world additively.

## 2. Authoring

Decorators on plain functions; config is decorator kwargs; the decorated object
carries all the verbs.

```python
from prefect import flow, task

@task(log_prints=True)
def explain_tasks():
    print("run any python code here!")

@flow
def my_workflow() -> str:
    return "Hello, world!"

my_workflow()   # a flow is called like a normal function
```
(https://docs.prefect.io/v3/how-to-guides/workflows/write-and-run)

- `@flow` params: `name, description, retries, retry_delay_seconds, flow_run_name`
  (template or callable), `task_runner` (default `ThreadPoolTaskRunner`),
  `timeout_seconds`, `validate_parameters` (Pydantic on by default), `version`.
- `@task` params (full table at
  https://docs.prefect.io/v3/api-ref/python/prefect-tasks.md): `name, description,
  tags, version, cache_key_fn, cache_policy, cache_expiration, task_run_name,
  retries, retry_delay_seconds, retry_jitter_factor, retry_condition_fn,
  timeout_seconds, log_prints, refresh_cache, persist_result, result_storage,
  result_serializer, result_storage_key, cache_result_in_memory, on_completion,
  on_failure, viz_return_value, asset_deps`.
- Subflows: call a `@flow` from a `@flow` — appears as a nested run. Generator
  flows (`yield`) and `@classmethod` flows are supported.
- `task.with_options(...)` / `flow.with_options(...)` clone a unit with changed
  config at call sites — authoring config is overridable per use, not frozen.
- Typed parameters: flow signature type hints → Pydantic validation → UI forms
  (`validate_parameters=True` default).

## 3. Run verbs (the complete surface)

The defining trait: **one authored unit, escalating verbs**. `fn()` →
`fn.submit()` → `fn.delay()` → `fn.serve()` — same signature, widening execution
scope, no re-declaration.

| Prefect verb | What it does | hypergraph equivalent |
|---|---|---|
| `my_flow(x=1)` | Run flow in-process, blocking; returns the return value | `await runner.run(graph, {...})` |
| `my_flow(x=1, return_state=True)` | Returns a `State` instead of raising/returning; `state.result()`, `state.is_completed()` | `RunResult` (ours richer: values, lineage, paused) |
| `await my_flow(...)` (async def flow) | Native async flow | native (runner is async-first) |
| `task(...)` direct call | Blocking in-flow task run | node execution (implicit) |
| `task.submit(*args, wait_for=[...])` → `PrefectFuture` | Concurrent task via task runner; `.result()`, `.wait()`, `.state` | MISSING as a verb — hypergraph's dataflow schedules automatically; no manual future wiring |
| `task.map(iterable, unmapped(const))` | Fan-out one task over an iterable → `PrefectFutureList`; `unmapped()` marks static args | `runner.map(graph, ..., map_over="doc")` (ours maps a whole graph, theirs one task) |
| `from prefect.futures import wait; done, not_done = wait(futures)` | Gather many futures | `MapResult` (structured) |
| `task.delay(*args)` → `PrefectDistributedFuture` | Background task run executed by a separate task worker; same signature as the function | `host.submit` (durable tier, designed) |
| `task.map(A, B, deferred=True)` | Background fan-out to task workers | `host.submit(..., map_over=...)` |
| `task.apply_async(args, kwargs, wait_for, dependencies)` | Celery-style explicit submission | MISSING (deliberately, on both: `delay` is sugar for it) |
| `task.serve()` / `serve(add, multiply)` | Start a **task worker** process executing `.delay()`ed runs (https://docs.prefect.io/v3/how-to-guides/workflows/run-background-tasks) | `host.work_forever(worker_id=...)` |
| `my_flow.serve(name=..., cron=...)` | Create a deployment and "immediately begins listening for scheduled runs to execute" — zero-infra daemon (https://docs.prefect.io/v3/how-to-guides/deployments/create-deployments) | `serve(graph.with_runner(...))` — convention already adopted |
| `serve(deploy_a, deploy_b)` (module-level) | One process serving many deployments | same |
| `my_flow.deploy(name, work_pool_name, image, push=False, cron=...)` | Register deployment onto a **work pool**; infra (Docker/K8s/serverless) provisioned per run | MISSING (host is single-store, single-tier by design) |
| `flow.from_source(repo_url, entrypoint).deploy(...)` | Deploy code that lives elsewhere (git/S3) | MISSING (out of scope) |
| `prefect deploy` (CLI, `prefect.yaml`) | Declarative deployment definitions | MISSING (no declarative surface) |
| `run_deployment(name="f/d", parameters={...}, timeout=0, scheduled_time=..., as_subflow=True)` | Trigger a registered deployment from code; `timeout=0` "returns immediately"; shows as a subflow of the caller by default (https://docs.prefect.io/v3/how-to-guides/deployments/run-deployments) | `host.submit(...)` — but Prefect has **no dedup/idempotency param here**; ours has start-fingerprint |
| `prefect deployment run 'f/d' --param x=1 --watch --start-at/--start-in` | CLI trigger with wait and future scheduling | MISSING (no CLI) |
| `prefect worker start --pool my-pool` | Infra worker loop polling a work pool | `host.work_forever` |
| `prefect flow-run execute <id>` | Execute a specific flow run in the current process | MISSING (no by-id execute verb) |
| `prefect flow-run retry <id> [--entrypoint ./f.py:my_flow]` | Re-execute a failed/cancelled run; "keeps its original ID and parameters, but the `run_count` increments" (https://docs.prefect.io/v3/how-to-guides/workflows/retry-flow-runs) | `host.redrive(...)` — ours mints a NEW run with `retry_of` lineage; theirs mutates in place |
| `prefect flow-run cancel <id>` / UI Cancel | Durable cancellation via Cancelling state + worker signal | `await host.stop(wf)` |
| `pause_flow_run` / `suspend_flow_run` / `resume_flow_run` | HITL pause (see §5; hitl.md §2e) | interrupt gates + `host.answer` |
| — (no equivalent) | — | `runner.iter(graph, ...)` live event stream — MISSING in Prefect's Python API |
| — (no equivalent) | — | `runner.map_iter(...)` backpressured streaming of batch results — MISSING |
| — (no equivalent) | — | `runner.start_run/start_map` handle (`done/stop()/result()`) — Prefect's nearest is `.delay()`+future or `run_deployment(timeout=0)`+poll |

Notable absence on their side: **no batch object**. `task.map` produces loose
task runs inside one flow run; `run_deployment` in a loop produces loose flow
runs. Nothing owns "the 500 of them" — no manifest, no tolerance contract, no
batch outcome stream (hypergraph's `Batch` + `BatchTolerance` have no Prefect
counterpart; cf. patterns.md §1a for Step Functions' tolerated-failure idea).

## 4. Observation

- **Python**: `future.state`, `state = fn(..., return_state=True)`,
  `state.is_completed()/.result()`. Past results: not on the run object — you
  reconstruct via `ResultStore(...).read(key=...)` (results are **not persisted
  by default**: "By default these results are not persisted and no reference to
  them is maintained in the API",
  https://docs.prefect.io/v3/advanced/results.md). Client queries via
  `PrefectClient` (https://docs.prefect.io/v3/advanced/api-client.md).
- **CLI**: `prefect flow-run ls --state FAILED`, `inspect --web`, `logs --tail`,
  `watch --timeout` ("Watch a flow run until it reaches a terminal state"),
  `prefect deployment run --watch --watch-interval`, `prefect events stream
  --format json` (live event firehose), `prefect events emit`.
- **UI dashboard verbs**: Quick run / Custom run (parameter form from the flow
  signature), Retry, Cancel, Resume (renders a `RunInput` form for paused runs),
  pause/resume schedules, per-run timeline + logs, task-run graph.
- **State hooks as push observation**: `on_completion/on_failure/on_crashed/
  on_cancellation/on_running` on flows and tasks, attachable as decorators:

```python
@failing_flow.on_failure
def notify_slack(flow, flow_run, state): ...
```
(https://docs.prefect.io/v3/how-to-guides/workflows/state-change-hooks) — caveat
quoted there: hooks "run in the same process as your workflow and execution
cannot be guaranteed."

Watching a **batch**: nothing first-class — you watch the parent flow run, or
subscribe to the event stream and filter. No progress-bar story (hypergraph's
`show_progress=True` has no equivalent). No Python `watch(run_id) -> stream`
(hypergraph `host.watch` durable replay+tail is strictly stronger).

## 5. HITL / pause

Covered in depth in hitl.md §2e (pause=process stays up vs suspend=infra torn
down; `wait_for_input` with `RunInput`/Pydantic → auto JSON-schema UI form;
`resume_flow_run(id, run_input={...})`; server-side validators run only AFTER
resume; feature marked experimental). Deltas worth adding here:

- Automations can `suspend-flow-run` / `resume-flow-run` as **actions** — a
  machine can operate the pause lever, not just humans
  (https://docs.prefect.io/v3/concepts/automations.md).
- Paused/Suspended are first-class state types (`PAUSED` group) visible in every
  `ls`/UI surface — the pause is an operator-legible state, not a hidden await.
- Contrast with hypergraph's design: answers validated against the declared
  schema **before** settling (pause survives rejection); Prefect validates after
  resume, so a bad answer becomes a flow-side error to handle.

## 6. Failure & retry surface

**User-configured, at the decorator (all on both @task and @flow):**

```python
from prefect.tasks import exponential_backoff

@task(retries=4, retry_delay_seconds=exponential_backoff(backoff_factor=2))
def melancholic_task():
    raise Exception("We used to see each other so much more often")
```
- `retry_delay_seconds` takes a scalar, a **list** (`[1, 2, 4, 8]` — explicit
  per-attempt schedule), or a callable; `retry_jitter_factor` adds jitter
  ("avoid thundering herd").
- `retry_condition_fn(task, task_run, state) -> bool` — predicate deciding
  retry vs final Failure (https://docs.prefect.io/v3/how-to-guides/workflows/retries).
- `timeout_seconds` on flow and task ("task marked failed if exceeded").
- Fleet defaults: `prefect config set PREFECT_TASKS_DEFAULT_RETRIES=2`,
  `PREFECT_TASKS_DEFAULT_RETRY_DELAY_SECONDS="1,10,100"`.
- Manual final states: `return Failed(message=...)` / `Completed(message=...)`;
  flow final state determined by **return value** (see warning #1).
- **Transactions** (https://docs.prefect.io/v3/advanced/transactions.md): group
  tasks atomically; `@task.on_rollback` / `@task.on_commit` hooks;
  `transaction(key="unique-id")` + `txn.is_committed()` for idempotent reruns;
  `IsolationLevel.SERIALIZABLE` with pluggable lock managers
  (Memory/File/Redis). "Rollbacks occur whenever the transaction a task is
  participating in fails, even if that failure is outside the task's local
  scope."

**Operator verbs:** `prefect flow-run retry` (in-place, run_count++; works for
non-deployment runs via `--entrypoint`), UI Retry button, `cancel`, automations
actions (`change-flow-run-state`, `cancel-flow-run`, `run-deployment` as
reactive repair). **No bulk retry/replay verb** (Hatchet/Trigger.dev have one;
engines.md row 2) and **no per-item tolerance** — a map has no "fail the batch
after N item failures" contract.

## 7. Concurrency & flow control

Four separate systems, each at a different scope (this sprawl is warning #3):

| Mechanism | Scope | API |
|---|---|---|
| Task runner (`ThreadPoolTaskRunner`, `DaskTaskRunner`) | Inside one flow-run process | `@flow(task_runner=...)` |
| Tag-based task concurrency | Server-wide, all task runs sharing a tag; "it must have available concurrency slots for **all** tags to run"; blocked tasks poll (`PREFECT_SERVER_TASKS_TAG_CONCURRENCY_SLOT_WAIT_SECONDS`) | `@task(tags=["database"])` + `prefect concurrency-limit create database 10` |
| **Global concurrency limits** — "can be applied to any Python-based operation in your codebase" | Any code block, named server-side limit; 5-min leases, `strict=True` to fail on lease loss; active/inactive toggle without code change | `with concurrency("database", occupy=1): ...` / `async with concurrency(...)`; `prefect gcl create my-limit --limit 5 --slot-decay-per-second 1.0` |
| `rate_limit("rate-limited-api")` | Same named limits with `slot_decay_per_second` → request **rate**, not occupancy | `from prefect.concurrency.sync import rate_limit` |
| Work pool / work queue | Flow-run scheduling: per-queue limits under a pool-level cap; integer priorities, "`1` being the highest," allocated "waterfall fashion" | pool/queue config + `prefect worker start --pool` |

(https://docs.prefect.io/v3/how-to-guides/workflows/global-concurrency-limits,
/v3/how-to-guides/workflows/tag-based-concurrency-limits, /v3/concepts/work-pools)

Hypergraph today: `max_concurrency` per map (local scope only); fleet-wide flow
control is a named deferred gap in the durable-host design (landscape doc §gap
analysis). Prefect's named-limit surface is the model to steal when that trigger
fires.

## 8. Scheduling & timers

- Inline on the run verbs: `my_flow.serve(cron="* * * * *")`; `cron`/`interval`/
  `rrule` each also accept **iterables** to create multiple schedules.
- Schedule objects bind more than time
  (https://docs.prefect.io/v3/how-to-guides/deployments/create-schedules):

```python
from prefect.schedules import Interval, Cron

Interval(timedelta(minutes=10), anchor_date=datetime(2026, 1, 1), timezone="America/Chicago")
Cron("0 8 * * *", slug="jim-email", parameters={"to": "jim.halpert@dundermifflin.com"})
```
  — per-schedule `parameters`, `slug` (stable identity; `replaces` renames
  without duplicating), `timezone`, `active: false`.
- CLI: `prefect deployment schedule create/ls/pause/resume/clear` (create
  **adds** by default; `--replace` to swap). UI can't author rrule.
- One-shot future runs: `run_deployment(scheduled_time=...)`, CLI `--start-at
  "5pm"` / `--start-in`.
- No durable in-flow sleep: the idiom is `pause_flow_run(timeout=...)` or
  scheduling the continuation — hypergraph's designed `wake_at` on the run
  record is cleaner for "escalate in 72h."

## 9. The 3 steals

1. **One unit, escalating verbs.** The same decorated function runs in-process,
   concurrently, or on remote workers by changing only the verb — `.delay()
   has the same signature as the @task decorated function.` Maps exactly onto
   Tier 0 → durable tier: `host.submit` should feel like `runner.run` with a
   different prefix, never a re-declaration.
   ```python
   add_integers(1, 2)                 # blocking
   add_integers.submit(1, 2)          # concurrent, PrefectFuture
   add_integers.delay(1, 2)           # background worker, distributed future
   ```
2. **Schedules that bind parameters and identity, not just time.** A schedule
   carries `parameters` and a `slug` — "which inputs, on what clock, under what
   stable name" as one object. Hypergraph's durable tier has `wake_at` but no
   schedule object; when cron lands, it should be
   `host.schedule("graph", Cron("0 8 * * *", slug="daily-reingest", inputs={...}))`.
   ```python
   Cron("0 8 * * *", slug="jim-email", parameters={"to": "jim.halpert@dundermifflin.com"})
   ```
3. **Named, code-attachable flow-control limits.** `with concurrency("database",
   occupy=1):` / `rate_limit("rate-limited-api")` — the limit is a named
   server-side resource, ops-tunable at runtime (`prefect gcl update --limit 10`,
   `--disable`) while the code only names what it contends for. The right shape
   for hypergraph's deferred fleet-wide flow control (and panda's hyperlimit
   gate could eventually target the same seam).
4. *(honorable)* **Lifecycle hooks as decorators on the authored object** —
   `@my_flow.on_failure` / `@task.on_rollback` — observers and compensation
   attach where the unit is defined, with a typed `(unit, run, state)` signature.

## 10. The warnings

1. **Flow final state is determined by the return value — and unresolved futures
   silently drop failures.** Their own upgrade guide: "Flows may now complete
   successfully even if they contain failed tasks, unless you explicitly handle
   task failures" (https://docs.prefect.io/v3/resources/upgrade-to-prefect-3).
   A fire-and-forget `.submit()` whose future is never returned/waited can make
   a run green while work failed. Lesson: an implicit aggregation rule tied to
   `return` + manual futures is a footgun; hypergraph's explicit
   `RunResult`/`MapResult` outcome model should never adopt "return value
   decides."
2. **Implicit auto-caching of tasks broke side-effecting code.** "By default,
   tasks in a flow run are automatically cached if they are called more than
   once with the same inputs," requiring `cache_policy=None` to opt out (same
   upgrade guide). A performance default that changes semantics — memoization
   must be opt-in.
3. **Concurrency-limit sprawl.** Four mechanisms (task runner, tag-based limits,
   global concurrency limits, work pool/queue limits) in four config places
   with different starvation behaviors (tag-blocked tasks poll on a
   server-side setting; gcl leases can silently continue on renewal failure
   unless `strict=True`). Two CLI names (`prefect concurrency-limit` vs
   `prefect gcl`) for two different systems.
4. **Operator verbs gated on deployments.** "Flow run cancellation requires
   that the flow run is associated with a deployment"
   (https://docs.prefect.io/v3/advanced/cancel-workflows.md); inline subflows
   can't be cancelled independently; UI Retry breaks on push pools
   (github.com/PrefectHQ/prefect/issues/17484). A run started the zero-ceremony
   way is a second-class citizen to the operator surface — exactly the honesty
   gap hypergraph's "no host.run() in v1" decision avoids.
5. **In-place retry erases history.** `flow-run retry` keeps the same ID and
   increments `run_count` — no lineage between attempts (community asked for it
   for years, github.com/PrefectHQ/prefect/issues/10767). `wait_for_input`
   remains flagged experimental (hitl.md §2e).

## 11. Verdict vs hypergraph

What Prefect has that our design lacks at the user-facing API level: a
**schedule object** binding cron/interval/rrule + parameters + slug to a served
graph; a **named global concurrency/rate-limit** surface (`with
concurrency("db")` / `rate_limit("api")`) that code names and operators tune
live; an **events → automations** rules layer (typed triggers, `for_each`,
proactive absence-detection, actions like run/cancel/suspend) that turns the
event stream into operator leverage; **lifecycle hook decorators**
(`@flow.on_failure`, `@task.on_rollback`) plus a **transactions** grouping with
commit/rollback semantics; and a full **operator CLI** (`ls/inspect/logs/watch/
retry/cancel`, `deployment run --watch`, `events stream`). What we do better:
the batch is a real object (manifest + tolerance + child outcomes vs loose task
runs with no aggregate), results are first-class and streamed
(`RunResult`/`map_iter` vs off-by-default `ResultStore` archaeology), retries
and redrives mint new runs with `retry_of`/`fork_from` lineage instead of
mutating `run_count` in place, submit dedup via start-fingerprint exists at all
(`run_deployment` has none), answers are schema-validated **before** settling
rather than after resume, and outcomes never depend on a return-value
convention or on remembering to resolve futures. Net: keep the core execution
and evidence model — it is honestly better — and steal the periphery: the
escalating-verb symmetry (`runner.run` → `host.submit` must feel like the same
unit), schedule objects with bound inputs, named flow-control limits, and hook
decorators; adopt automations only as a thin layer over `host.watch` rather
than a second rules engine.
