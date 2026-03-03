# Synthesis: What Hypergraph Should Build

## The Core Realization

The previous session (838b) designed an in-memory "RunLog" that's always-on for timing metadata.
The current session pushed further: **persistence is the key enabler for the AI agent use case.**

The in-memory RunLog is necessary but not sufficient. The real story:

1. **In-memory trace** (RunLog): Always-on, zero-config, cheap (timing metadata only)
2. **Persistent trace** (Checkpointer): Durable, cross-process, queryable — the real debugging story

## Cross-Framework Patterns Applied to Hypergraph

### Pattern 1: Same data serves multiple purposes
- LangGraph: checkpoints power resume AND time-travel AND debugging
- Hatchet: PostgreSQL records power execution AND dashboard AND replay
- **Hypergraph**: StepRecord should power resume AND debugging AND trace inspection

### Pattern 2: Execution data is always captured by default
- Every framework captures execution metadata by default
- **Hypergraph**: RunLog (in-memory) is always-on. StepRecord (when checkpointer configured) captures everything.

### Pattern 3: Progressive disclosure
- Summary → detail → raw data
- **Hypergraph**: `result.log.summary()` → `result.log.steps` → `checkpointer.get_steps(workflow_id)`

### Pattern 4: Structured output for AI agents
- Inngest has MCP server; Hatchet has REST API; LangGraph has Platform SDK
- **Hypergraph**: `result.log.to_dict()` + `checkpointer.get_steps()` returns structured data

## What StepRecord Needs for Trace

Current StepRecord spec fields:
- workflow_id, superstep, node_name, index, status
- input_versions, values, error, pause
- partial, created_at, completed_at, child_workflow_id

**Missing for trace/debugging:**
- `duration_ms: float` — explicit timing (can also derive from created_at/completed_at)
- `cached: bool` — was this a cache hit?
- `decision: str | list[str] | None` — route/gate decision

These are the same 3 fields that the in-memory StepTrace/NodeRecord was going to have.

## The Two-Layer Architecture

```
┌─────────────────────────────────────────────────┐
│  Layer 1: In-Memory RunLog (always-on)           │
│  - Timing metadata per node                      │
│  - Always available on RunResult.log             │
│  - Zero config, zero IO                          │
│  - Lost on process exit                          │
│  - API: result.log.summary(), .steps, .errors    │
└─────────────────┬───────────────────────────────┘
                  │ same data
                  ▼
┌─────────────────────────────────────────────────┐
│  Layer 2: Persistent StepRecord (checkpointer)   │
│  - Full StepRecord with values + timing          │
│  - Cross-process queryable                       │
│  - Survives crashes                              │
│  - Requires checkpointer config                  │
│  - API: checkpointer.get_steps(workflow_id)      │
└─────────────────────────────────────────────────┘
```

## Proposed Implementation Order

### Phase 1: In-Memory RunLog (the 838b plan)
- NodeRecord, NodeStats, RunLog, _RunLogCollector
- Always-on, zero-config
- result.log on every RunResult
- 12 files total (1 new + 11 modified)

### Phase 2: Extend StepRecord with trace fields
- Add duration_ms, cached, decision to StepRecord spec
- These fields exist whether or not a checkpointer is configured
- When checkpointer is present, they're persisted automatically

### Phase 3: Implement SqliteCheckpointer
- save_step(), get_steps(), get_state(), list_workflows()
- Runner calls save_step() after each node
- This gives the full durable debugging story

### Phase 4: Analysis layer
- MapResult wrapper for cross-item analysis
- Time-travel queries: get_state(workflow_id, superstep=N)
- Structured JSON output for AI agents

## Key Design Decisions

1. **RunLog and Checkpointer are independent** — RunLog works without checkpointer (in-memory only). Checkpointer works without RunLog (persistence only). Together they cover all use cases.

2. **StepRecord is the superset** — NodeRecord (in-memory) is a subset of StepRecord (persistent). Both have node_name, duration_ms, error, cached, decision. StepRecord adds values, input_versions, etc.

3. **No new abstractions needed** — TraceCollector, ExecutionTrace, StepTrace from the v4 plan are unnecessary. RunLog (in-memory) + StepRecord (persistent) cover everything.

4. **The checkpointer IS the trace store** — `get_steps()` is the trace query. `get_state()` is the values query. No separate trace API needed.

## Comparison: v4 Plan vs This Approach

| Aspect | v4 (in-memory only) | This (RunLog + Checkpointer) |
|--------|---------------------|-------------------------------|
| Cross-process query | No | Yes (checkpointer) |
| Survives crash | No | Yes (checkpointer) |
| Multiple run history | No (last_trace only) | Yes (all workflows) |
| Always-on timing | Yes (TraceCollector) | Yes (RunLog) |
| Zero-config | Yes | Yes (RunLog layer) |
| AI agent debugging | Partial | Full (persistent + queryable) |
| Types needed | 6 new types | 2 new types (NodeRecord, RunLog) |
| Complexity | Medium | Lower (reuses existing specs) |
