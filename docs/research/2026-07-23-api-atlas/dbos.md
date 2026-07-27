# API Atlas — DBOS Transact Python

Sources: docs.dbos.dev fetched 2026-07-23. Python SDK `dbos` **2.28.0** (PyPI, released
2026-07-21, Python >=3.10, MIT, Development Status: Production/Stable). Where a docs page
was ambiguous, verified against `dbos-inc/dbos-transact-py` source at `main`.
Lifecycle strengths and the singleton/recovery-scope limits are already covered in
`superposition/docs/research/2026-07-16-ingestion-lifecycle-primitives/engines.md` (DBOS
section) and `hypergraph/docs/research/2026-07-21-durable-execution-landscape.md` — not
repeated here; this file is the user-facing verb surface.

## 1. Identity

Postgres-backed (now also SQLite-backed) durable execution as an **in-process library**,
no server. Replay-family: workflows must be deterministic; steps checkpoint results;
recovery re-executes the workflow feeding back recorded step outputs
([workflow tutorial](https://docs.dbos.dev/python/tutorials/workflow-tutorial)). Python SDK
mature and fast-moving (2.0.0 Sep 2025 → 2.28.0 Jul 2026, ~weekly minors).

## 2. Authoring

Decorators on plain functions; a process-singleton `DBOS()` object; `DBOS.launch()` arms
recovery, queues, schedules ([dbos-class](https://docs.dbos.dev/python/reference/dbos-class)).

```python
@DBOS.workflow()            # (*, name=None, max_recovery_attempts=100, serialization_type=None, validate_args=None)
def example_workflow(friend: str):
    body = example_step()

@DBOS.step(retries_allowed=True, max_attempts=3, backoff_rate=2.0)   # + interval_seconds, should_retry, preemptible
def example_step(): ...

@DBOS.transaction()         # (isolation_level="SERIALIZABLE", *, name=None); use DBOS.sql_session
def txn_step(): ...

DBOS(config={"name": "app"})   # singleton; config: DBOSConfig
DBOS.launch()                  # "initializing database connections and starting queues and scheduled workflows"
```

Also: `@DBOS.dbos_class()` + `DBOSConfiguredInstance` for methods; `DBOS.run_step(options,
func, *args)` runs an undecorated function as a step inline; `validate_args=
pydantic_args_validator` gives Pydantic input validation
([decorators](https://docs.dbos.dev/python/reference/decorators)). Notable config defaults
([configuration](https://docs.dbos.dev/python/reference/configuration)):
`system_database_url` defaults to **`sqlite:///[app_name].sqlite`** (zero-infra start);
`run_admin_server: True` on `admin_port: 3001`; `enable_patching`, `application_version`,
`executor_id`, `conductor_key` optional.

## 3. Run verbs (the complete surface)

Two invocation families coexist: the `Queue` object (`Queue(name, concurrency=...,
limiter=...)` + `queue.enqueue(func, *args)`, still exported) and the newer DB-persisted
`DBOS.register_queue(name, ...)` + `DBOS.enqueue_workflow("queue_name", func, *args)`
which the tutorials now teach ([queue tutorial](https://docs.dbos.dev/python/tutorials/queue-tutorial),
[queues reference](https://docs.dbos.dev/python/reference/queues)). Every verb below has a
`*_async` twin (omitted from the table).

| DBOS verb | What it does | hypergraph equivalent |
|---|---|---|
| `workflow_fn(args)` direct call | Durable run, blocks caller | `await runner.run(graph, inputs)` |
| `with SetWorkflowID("id"): workflow_fn()` | Assign idempotency-key workflow ID; same ID = single execution | `workflow_id="doc-42"` param (ours is a param, theirs a context manager) |
| `DBOS.start_workflow(fn, *args) -> WorkflowHandle` | Background start, in-process | `runner.start_run(graph, {...})` |
| `DBOS.enqueue_workflow(queue, fn, *args) -> WorkflowHandle` | Durable queued start, any worker may dequeue | `host.submit(...)` (designed) |
| `handle.get_result(polling_interval_sec=1.0)` | Block for result (DB polling for enqueued) | `handle.result()` / receipt await |
| `handle.get_status() -> WorkflowStatus` | Rich status record (see §4) | `RunResult` / host status (designed) |
| `DBOS.retrieve_workflow(wf_id) -> handle` | Re-attach to any workflow by ID | MISSING as a Tier-0 verb (host `watch` covers part) |
| `DBOS.get_result(wf_id) -> Optional[Any]` | Non-blocking result peek | MISSING |
| `DBOS.wait_first(handles)` | First-completed of a handle list | partial: `map_iter` streams completions |
| fan-out = loop of `enqueue_workflow` + `[h.get_result() for h in handles]` | Batch is hand-rolled; no batch object, no tolerance, no item keys | `runner.map / map_iter`, Batch manifest + `BatchTolerance` — **MISSING on their side** |
| `DBOSClient(system_database_url=...).enqueue(options, *args)` | Enqueue from a process with **no app code**, just the DB URL | MISSING (host client exists only via host object) |
| `client.retrieve_workflow / list_workflows / cancel / resume / fork / delete / read_stream / send / get_event / register_queue` | Full operator surface on the thin client ([client](https://docs.dbos.dev/python/reference/client)) | MISSING |
| `DBOS.send(dest_id, message, topic, idempotency_key=...)` / `DBOS.send_bulk([...])` | Durable message to a workflow; exactly-once from workflows | `host.answer(...)` (narrower, typed) |
| `DBOS.recv(topic, timeout_seconds=60) -> Any` | Workflow blocks for message; `None` on timeout | interrupt gate / pause slot |
| `DBOS.set_event(key, value)` / `DBOS.get_event(wf_id, key, timeout)` / `get_all_events` | Workflow-scoped durable KV, latest-wins, externally readable | MISSING (no run-scoped published KV) |
| `DBOS.write_stream(key, value)` / `close_stream` / `read_stream(wf_id, key, offset=0)` | Durable named streams per workflow; exactly-once from workflow body; tail with offset | `runner.iter` / `host.watch` (ours = engine events; theirs = user-pushed channels) |
| `DBOS.sleep(seconds)` | Durable sleep, survives restart | designed `wake_at` timers |
| `with SetWorkflowTimeout(10): ...` | Start-to-close timeout, cancels children too | MISSING as API (profile config only) |
| `with SetEnqueueOptions(deduplication_id=..., priority=..., delay_seconds=..., app_version=..., queue_partition_key=...)` | Per-enqueue options bundle | `submit` dedup is fingerprint-implicit; priority/delay/version MISSING |
| `Debouncer.create(wf, ...).debounce(key, period_sec, *args)` | Durable debounce by key | MISSING |
| `DBOS.create_schedule(schedule_name=, workflow_fn=, schedule=cron, automatic_backfill=, cron_timezone=, queue_name=)` + `apply_schedules` / `list/get/delete/pause/resume_schedule` / `backfill_schedule(name, start, end)` / `trigger_schedule(name)` | Runtime-managed cron schedules (replaces deprecated `@DBOS.scheduled`) | MISSING (scheduled answers ≠ cron) |
| `DBOS.list_workflows(...)` — ~27 filters: status, name, queue_name, app_version, `forked_from`, `parent_workflow_id`, `workflow_id_prefix`, `attributes`, `schedule_name`, time windows incl. `dequeued_after/before`, `load_input/load_output`, limit/offset/sort | The query verb; same filter set reused by CLI/Console | host list (designed, thinner); Tier 0 MISSING |
| `DBOS.list_queued_workflows` / `list_workflow_steps(wf_id)` / `get_workflow_status(wf_id)` | Queue introspection; per-step journal listing | step-level: ours = checkpoint/event journal |
| `DBOS.cancel_workflow(wf_id, cancel_children=False)` / `cancel_workflows([...])` | Preempts at next step boundary; bulk variant | `host.stop(wf)` (no bulk, no children flag) |
| `DBOS.resume_workflow(wf_id, queue_name=None)` / `resume_workflows([...])` | Resume from last completed step — also un-cancels and revives `MAX_RECOVERY_ATTEMPTS_EXCEEDED`; can re-target queue | resume via checkpointer; `redrive` (new Run) — different philosophy: theirs resumes the SAME run |
| `DBOS.fork_workflow(wf_id, start_step, *, application_version=None, queue_name=None, queue_partition_key=None, replacement_children=None)` | New workflow copying steps `< start_step`; can rebind to a new code version; can substitute child workflow results | `fork_from` / `retry_from` (node-addressed; no version rebinding, no child substitution) |
| `restart` | **Removed in 2.0** ("the workflow restart method and command — replaced with fork"); admin HTTP route remains | `retry_from` (start) |
| `DBOS.delete_workflow(wf_id, delete_children=False)` / bulk | GC a record | MISSING both sides as a designed verb |
| `DBOS.set_workflow_delay(wf_id, delay_seconds= / delay_until_epoch_ms=)` | Postpone an already-enqueued workflow (`DELAYED` status) | MISSING |
| `with SetWorkflowAttributes({...})` / `DBOS.update_workflow_attributes(wf_id, {...})` | Attach/patch operator metadata; filterable in `list_workflows(attributes=...)` (Postgres only) | MISSING |
| `DBOS.patch("name")` / `DBOS.deprecate_patch("name")` | In-code version gates for replay compatibility | N/A — state-based recovery doesn't need it |
| `DBOS.list_application_versions()` / `get_latest_application_version()` / `set_latest_application_version(name)` | Version registry verbs for blue-green drain | start-fingerprint only; no version routing verbs |
| `DBOS.register_queue(name, *, concurrency, worker_concurrency, limiter={"limit","period"}, priority_enabled, partition_queue, polling_interval_sec, on_conflict)` / `retrieve_queue` / `list_queues` / `delete_queue` / `queue.set_concurrency(...)` etc. | Queues are DB rows; **runtime reconfiguration** ("workers pick up changes on next polling cycle") | `max_active_runs` only |
| `DBOS.listen_queues(["gpu_queue"])` (pre-launch) | Worker pulls only named queues — heterogeneous worker pools | `work_forever(worker_id=...)` (no queue selection) |
| `DBOS.launch()` / `DBOS.destroy(workflow_completion_timeout_sec=0)` / `reset_system_database()` | Process lifecycle; launch IS the worker entrypoint | `serve(...)` + `work_forever` |
| `@DBOS.alert_handler` | Route framework alerts (rule_type, message, metadata) to user code | MISSING |
| CLI: `dbos workflow list/get/steps/cancel/resume/fork` (`-S/--step`, `-v/--application-version`), `dbos workflow queue list`, `dbos init/start/migrate/reset` — all take `-s/--sys-db-url` | Full operator CLI straight against the system DB ([cli](https://docs.dbos.dev/python/reference/cli)) | MISSING |
| Admin HTTP (port 3001, verified in `_admin_server.py`): `/dbos-healthz`, `/dbos-workflow-recovery` (POST executor-ID list → recovered wf IDs), `/deactivate`, `/dbos-workflow-queues-metadata`, `/dbos-garbage-collect`, `/dbos-global-timeout`, `/workflows`, `/workflows/:id/{cancel,resume,restart,fork,steps}`, `/queues` | Ops without importing the app | MISSING |

## 4. Observation

- `WorkflowStatus` (verified in `_sys_db.py`) is unusually rich: `status` ∈ {`PENDING`,
  `ENQUEUED`, `DELAYED`, `SUCCESS`, `ERROR`, `CANCELLED`,
  `MAX_RECOVERY_ATTEMPTS_EXCEEDED`}, plus `input`/`output`/`error`, `created_at/updated_at/
  dequeued_at/completed_at`, `queue_name`, `executor_id`, `app_version`,
  `deduplication_id`, `priority`, `queue_partition_key`, **`forked_from` /
  `was_forked_from` / `parent_workflow_id`** (lineage is queryable), `attributes`,
  `schedule_name`, `recovery_attempts`.
- Watching a run: poll `handle.get_status()`, or consume the workflow's **streams**
  (`DBOS.read_stream(wf_id, key, offset=0)` — generator that blocks until close;
  LISTEN/NOTIFY-backed since 2.23.0). "Every write is persisted, so if a server restarts
  mid-response the workflow recovers from where it left off and the reader keeps receiving
  values without dropping output" ([ai/streaming](https://docs.dbos.dev/ai/streaming)).
- Watching a batch: nothing first-class — you hold your own handle list and poll or
  `wait_first`. No progress events, no progress bars.
- Step-level inspection: `DBOS.list_workflow_steps(wf_id)`; Console shows a trace timeline
  ("Show Workflow Steps"); OTLP traces/logs exportable; plain SQL on `dbos.*` tables
  remains the escape hatch ([system tables](https://docs.dbos.dev/explanations/system-tables)).
- Dashboards: DBOS Console (hosted) with list/filter/cancel/resume/fork per workflow
  ([workflow management](https://docs.dbos.dev/production/workflow-management)); OSS gets
  CLI + admin HTTP + SQL.

## 5. HITL / pause

Covered comparatively in engines.md/hitl.md; the API shape:
`DBOS.recv(topic, timeout_seconds)` inside the workflow + `DBOS.send(wf_id, message,
topic)` from outside (webhook/operator); `set_event/get_event` for the reverse direction
(workflow publishes, human/UI polls) ([workflow communication](https://docs.dbos.dev/python/tutorials/workflow-communication)).
Payloads are `Any` — no schema, no validation before delivery (hypergraph's designed
`host.answer` validates against a graph-declared answer schema before settling; DBOS has
nothing equivalent). Timeout is signaled by returning `None`, indistinguishable from a
literal `None` message. `send` takes `idempotency_key` for exactly-once from plain Python;
`send_to_forks=True` mirrors a message to forked descendants — an admission that forking
breaks message addressing. No first-class "pause slot" object, no list of pending
questions; you build the inbox yourself ([agent inbox example](https://docs.dbos.dev/python/examples/agent-inbox)).

## 6. Failure & retry surface

- **User config:** per-step `retries_allowed/max_attempts/interval_seconds/backoff_rate` +
  `should_retry(exc) -> bool` predicate (sync or awaitable) — exhaustion raises
  `DBOSMaxStepRetriesExceeded` into the workflow, which may catch it (error routing =
  ordinary `try/except` in the workflow body). Workflow-level `max_recovery_attempts=100`
  brakes crash-loops → status `MAX_RECOVERY_ATTEMPTS_EXCEEDED` (the same brake hypergraph
  designed as `recovery_exhausted`). `SetWorkflowTimeout` cancels workflow + children.
  `preemptible=True` steps allow mid-step cancellation.
- **Operator verbs:** `cancel` (preempts at next step boundary), `resume` (same run, last
  completed step; also the un-cancel and un-brake verb; can move to another queue),
  `fork(start_step=N)` (new run, optionally on a new `application_version`, optionally
  substituting `replacement_children`), bulk `cancel_workflows`/`resume_workflows`, and
  the same verbs over CLI, admin HTTP, Console, and DBOSClient. No tolerance policy, no
  batch-level redrive — per-item ops only, batched by ID list.
- **No compensation/saga surface**; external side effects are at-least-once except
  `@DBOS.transaction` writes to your own Postgres (exactly-once, checkpoint shares the
  transaction).

## 7. Concurrency & flow control

All scoped to **queues**, which are durable DB objects ([queue tutorial](https://docs.dbos.dev/python/tutorials/queue-tutorial)):

- `concurrency` — global cap across all processes; `worker_concurrency` — per-process cap.
- `limiter={"limit": 50, "period": 30}` — rate limit on starts, global.
- `priority_enabled` + `SetEnqueueOptions(priority=n)` — 1..2^31-1, "a low number indicates
  a higher priority", FIFO within a level (unset = 0 = highest).
- `deduplication_id` — one active workflow per ID per queue; **violation raises
  `DBOSQueueDeduplicatedError`** rather than returning the existing handle.
- `partition_queue=True` + `queue_partition_key` — flow-control limits apply **per
  partition key** (per-tenant fairness); `concurrency=1` partitioned queue = per-key
  serial execution.
- `delay_seconds` / `set_workflow_delay` — delayed start (`DELAYED` status).
- `Debouncer` — durable debounce by key with `debounce_timeout_sec` cap.
- Runtime reconfiguration of all queue knobs via `queue.set_*` / `retrieve_queue`,
  persisted in DB, picked up by workers on the next poll.
- Worker-scope: `DBOS.listen_queues([...])` restricts which queues a process dequeues.

## 8. Scheduling & timers

`DBOS.sleep(seconds)` durable sleep; `SetWorkflowTimeout`; `SetEnqueueOptions(delay_seconds=...)`.
Cron: the decorator form is **deprecated** —

```python
@DBOS.scheduled('* * * * *')   # DEPRECATED; croniter syntax, optional seconds field
@DBOS.workflow()
def example_scheduled_workflow(scheduled_time: datetime, actual_time: datetime): ...
```

— replaced by runtime-managed schedules: `DBOS.create_schedule(schedule_name=,
workflow_fn=, schedule="0 * * * *", context=, automatic_backfill=False, cron_timezone=,
queue_name=)`, `apply_schedules([...])` (declarative reconcile), `pause_schedule` /
`resume_schedule` / `delete_schedule`, `backfill_schedule(name, start, end)` (returns
handles for every missed tick), `trigger_schedule(name)` (run now). Exactly-once per tick
via idempotency key = schedule name + scheduled time
([scheduled workflows](https://docs.dbos.dev/python/tutorials/scheduled-workflows),
[contexts](https://docs.dbos.dev/python/reference/contexts)).

## 9. The 3 steals (+1)

1. **`fork_workflow(start_step=N, application_version=..., replacement_children=...)`** —
   fork is not just lineage, it is the *repair verb*: restart from a chosen point **onto
   fixed code**, optionally substituting child results, with `forked_from`/`was_forked_from`
   recorded and queryable. Hypergraph's `fork_from/retry_from` should grow a
   "target version/config" arm and record lineage as a first-class query filter.
   ```python
   DBOS.fork_workflow(workflow_id, start_step, *, application_version=None,
                      queue_name=None, replacement_children=None) -> WorkflowHandle[R]
   ```
2. **Durable named streams with offset tailing** — user-pushed, key-addressed channels on
   a run; exactly-once writes from the workflow body; any thin client can
   `read_stream(wf_id, key, offset=0)` and resume after a crash without dropping output.
   This is `host.watch`'s "durable replay + live tail" but *addressable per channel* and
   consumable with only a store URL. ([ai/streaming](https://docs.dbos.dev/ai/streaming))
   ```python
   DBOS.write_stream(stream_key, token); DBOS.close_stream("tokens")
   for token in DBOS.read_stream(handle.workflow_id, "tokens"): ...
   ```
3. **`DBOSClient` — the app-less operator client.** Everything an operator needs
   (enqueue by workflow *name*, list with the full filter set, cancel/resume/fork/delete,
   read streams/events, send messages) works from any process holding the system-DB URL —
   no graph import, no host object. Hypergraph's Run Home should get exactly this thin
   client so UIs and scripts don't need the graph code loaded.
   ```python
   client = DBOSClient(system_database_url=os.environ["DBOS_SYSTEM_DATABASE_URL"])
   handle = client.enqueue({"queue_name": "pipeline_queue", "workflow_name": "data_pipeline"}, task)
   ```
4. *(bonus)* **Queues as runtime-mutable DB objects + `SetEnqueueOptions` bundle** —
   dedup/priority/delay/version/partition-key ride one context manager; queue knobs
   (`set_concurrency`, `set_limiter`) change live without redeploy; `attributes` +
   `update_workflow_attributes` give operators a mutable tag surface that `list_workflows`
   can filter on. The whole flow-control story is per-queue, per-partition-key — richer
   than a single `max_active_runs`.

## 10. The warnings

1. **The determinism contract taxes the whole API.** Because recovery is replay, workflow
   bodies must be deterministic ("if called multiple times with the same inputs, it should
   invoke the same steps with the same inputs in the same order"), async steps must *start*
   in deterministic order before awaiting, and an entire second machinery exists solely to
   manage code change under replay: `DBOS.patch`/`deprecate_patch` (two-phase, with a
   mandatory transition period), `enable_patching` config, app-version pinning, version
   registry verbs, blue-green drain guidance — and a 2.24.0 release item literally titled
   "Detect Patching Non-Determinism". Hypergraph's state-based philosophy deliberately
   avoids this entire surface; do not import any of it.
2. **Surface churn and doubled/split verbs.** v2.0.0 removed `restart_workflow`
   ("replaced with fork") and requires a staged upgrade (1.12 → 1.14 → 2.0) because old
   migrations were deleted; `@DBOS.scheduled` is deprecated mid-2.x for
   `create_schedule/apply_schedules`; the `Queue` class and `DBOS.register_queue`
   coexist as two ways to make a queue (with different `on_conflict` defaults between
   `DBOS.register_queue` — `"update_if_latest_version"` — and `client.register_queue` —
   `"always_update"`); and every single verb ships a `*_async` twin, roughly doubling the
   API. Design async-first with one spelling.
3. *(also noted)* `recv` returns `None` on timeout (ambiguous with a `None` message) and
   payloads are untyped `Any`; dedup conflict is an exception (`DBOSQueueDeduplicatedError`)
   rather than a use-existing result — hypergraph's receipt-with-reason design is better;
   `send_to_forks` exists because forking silently breaks message addressing; handles
   resolve by DB polling (`polling_interval_sec=1.0`), not push; priority "unset = 0 =
   highest" while the documented range starts at 1.

## 11. Verdict vs hypergraph

DBOS is the strongest evidence that our missing layer is the **operator plane, not the
engine**: they expose one coherent verb set (list with ~27 filters incl. lineage and
custom attributes, cancel/resume/fork with bulk variants, delete, delay, queue metadata)
identically through Python, a thin app-less `DBOSClient`, a CLI, an admin HTTP server, and
a console — while hypergraph today has none of that beyond in-process handles, and even
the designed host lacks delete/delay/attributes/bulk verbs, an app-less client, per-key
partitioned flow control, runtime-mutable queue config, cron schedules with
backfill/pause/trigger, debounce, and per-run published KV/streams. What we do better is
exactly what their warts prove costly: no determinism contract (so no patch/versioning
machinery, no replay footguns), first-class batch (`map`, manifest, tolerance,
`redrive(item_keys=...)` — DBOS still makes you hand-roll fan-out with a handle list and
no batch object), typed validated answers instead of `Any` messages with `None`-timeout
ambiguity, dedup as an honest receipt instead of an exception, and an engine event stream
with progress rather than poll-the-status-row. Net: keep our execution philosophy and
batch/HITL design untouched; steal their operator plane — step/version-rebinding fork,
offset-tailed named streams, the thin store-URL client, enqueue-options
(dedup/priority/delay/partition-key), and runtime-tunable queue limits — as the checklist
for what `serve()`/Run Home must expose before it can claim operational parity with a
library that has shipped all of it.
