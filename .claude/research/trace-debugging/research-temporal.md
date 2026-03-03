# Temporal Debugging & Inspection Research

(Temporal research agent still running — capturing what we have from specs and prior research)

## Core Model
- Immutable append-only event history per workflow execution
- All workflow/activity args & return values are "payloads"
- DataConverter: PayloadConverter (types) + PayloadCodec (compression/encryption)
- Hard size limits: 2MB per payload, 4MB per event history transaction

## Event Types
- WorkflowExecutionStarted, WorkflowExecutionCompleted, WorkflowExecutionFailed
- ActivityTaskScheduled, ActivityTaskStarted, ActivityTaskCompleted, ActivityTaskFailed
- TimerStarted, TimerFired
- WorkflowTaskScheduled, WorkflowTaskCompleted
- SignalExternalWorkflowExecutionInitiated

## Visibility API
- List Filters / Advanced Visibility (Elasticsearch)
- Filter on: WorkflowId, RunId, WorkflowType, TaskQueue, StartTime, ExecutionStatus
- Basic visibility vs advanced visibility (ES-backed)

## Debugging Tools
- `tctl workflow show` — full event history
- `tctl workflow list` — list workflows with filters
- `tctl workflow query` — run queries against running workflows
- `tctl workflow describe` — workflow metadata
- Temporal Web UI — visual event history, activity details

## Workflow Statuses
- Running, Completed, Failed, Cancelled, Terminated, TimedOut, ContinuedAsNew

## Replay
- Deterministic replay from event history
- SDK's workflow replayer for debugging
- `temporal workflow replay` command
- Can replay specific workflow from event history JSON

## Key Pattern
- "Portable by default": avoid pickle, encode to JSON/protobuf/bytes
- "Large outputs" with real budgets: store blobs elsewhere, pass pointers
- Codec layer for encryption/compression without changing business logic

## From specs/references/serialization.md
- Max payload per request: 2 MB
- Max Event History transaction size: 4 MB
- Workflow Event History limited (events/total size)
- Needs `continue-as-new` for long workflows
