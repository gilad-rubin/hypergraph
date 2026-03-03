# Hatchet Debugging & Inspection Research (Complete)

## Core Architecture
- PostgreSQL as single source of truth for ALL execution state
- **Persistence and observability are the same layer** — no separate trace store
- Every task invocation durably logged to PostgreSQL
- Dashboard, API, replay all use same tables

## Data Model

### V1WorkflowRunDetails (workflow level)
- Internal run ID (UUID)
- External ID (user-assigned, for cross-referencing)
- Workflow name + version
- Status: QUEUED, RUNNING, SUCCEEDED, FAILED, CANCELLED
- Input data (full JSON payload)
- Result output (JSON-serializable)
- Timestamps: created_at, started_at, completed/failed_at
- `additional_metadata: dict[str, str]` (arbitrary tags for filtering)
- Failure info: which step failed, error message, failure timestamp

### V1TaskSummary (step level)
- Task name + definition version
- Step-specific inputs (from workflow input or parent task output)
- Status (V1TaskStatus)
- Retry count, attempt number
- Output data (stored per step after completion)
- Error message (if failed)
- Per-attempt timestamps
- Parent task reference (for DAG traversal)
- Worker ID

**Key: Both inputs and outputs stored per step — this is what makes replay possible.**

### RunsClient API
```python
runs.get(workflow_run_id: str) -> V1WorkflowRunDetails
runs.get_status(workflow_run_id: str) -> V1TaskStatus
runs.get_result(run_id: str) -> JSONSerializableMapping
runs.get_task_run(task_run_id: str) -> V1TaskSummary
runs.list(
    since, until, statuses, workflow_ids, worker_id,
    parent_task_external_id, triggering_event_external_id,
    additional_metadata, offset, limit, only_tasks, include_payloads
) -> V1TaskSummaryList
runs.bulk_replay(opts) -> None
runs.bulk_cancel(opts) -> None
# All have aio_* async variants
```

### LogsClient
```python
logs.list(task_run_id: str, limit=1000, since=None, until=None) -> V1LogLineList
# 1000-line cap per task is a hard limit
```

### Context Methods (during execution)
```python
ctx.log(line: str|dict) -> None         # Send log line to Hatchet API
ctx.put_stream(data: str|bytes) -> None  # Stream data to consumers
ctx.task_output(task: Task) -> R         # Get parent task output (type-checked)
ctx.task_run_errors() -> dict[str, str]  # Error map in on-failure handler
ctx.was_skipped(task: Task) -> bool
ctx.retry_count: int                     # 0 on first attempt
ctx.attempt_number: int                  # retry_count + 1
```

## Timing Model
Four timestamps per step:
- `created_at` — when task was created/queued
- `assigned_at` — when assigned to a worker
- `started_at` — when execution began
- `completed_at` — when execution finished

Distinguishes queue latency vs execution time. Also Prometheus histograms.

## Persistence Architecture
- PostgreSQL transactions for all state changes (not eventual consistency)
- Event log is append-only, per-workflow-run
- Each subtask has unique idempotency key (workflow_run_id + subtask_index)
- **Replay logic**: Replays event log, skips steps with completion records, resumes at first incomplete step
- **Durable tasks vs DAG tasks**: Different persistence models under same UI
  - DAG: Individual step records (input, output, status per node)
  - Durable: Checkpoint-based event log (append-only, replayed on resume)

## Debugging Workflow
1. Dashboard filter: status=FAILED + workflow name + time range
2. Or SDK: `hatchet.runs.list(statuses=[V1TaskStatus.FAILED], since=...)`
3. Click red node in DAG view → see error, input, retry count, timestamps
4. Navigate upstream to see parent outputs (all persisted)
5. Step-level log viewer: all ctx.log() + Python logging output
6. on_failure handler: `task_run_errors() -> {"step_name": "error message"}`
7. Fix code, use Replay button or `bulk_replay()` (reuses completed outputs)
8. Monitor replayed run — completed steps reused, only failed step re-executes

## Key Design Observations for Hypergraph
1. **Same layer for persistence and observability** — no separate trace store
2. **Step outputs are the unit of replay** — storing per-step I/O enables partial replay
3. **`additional_metadata` for cross-system correlation** — simple but requires discipline
4. **No dedicated CLI** — all querying through REST API/SDK + dashboard
5. **30-day default retention**
6. **Standard Python logging captured** — pass logger to Hatchet client, no ctx.log() needed
7. **OTel export supported** for external observability (Honeycomb, Jaeger, Datadog)
