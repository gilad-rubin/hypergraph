# Session 838b79a4 Summary — Observability Design Research

## Origin
Ran `.map()` over LLM calls, got 504 errors from Azure OpenAI, things were slow, no way to know which nodes were slow, which items failed, or what intermediate values were.

## Frameworks Analyzed

| Framework | Core Abstraction | Storage |
|-----------|-----------------|---------|
| **Hatchet** | PostgreSQL event log (inputs, outputs, timing, logs per task step) | PostgreSQL (same DB as workflow state) |
| **LangGraph** | "The checkpoint IS the trace" — powers debugging AND persistence AND time-travel | Checkpointer backends (SQLite, Postgres) |
| **Prefect** | Task states + artifacts; results opt-in via `persist_result=True` | Server DB + configurable result storage |
| **Temporal** | Immutable append-only event history per workflow | Cassandra/MySQL |
| **Inngest** | Per-step memoized outputs; SQL queries via "Insights"; MCP server for AI agents | Internal event store |

## Key Patterns

### Storage unit determines debugging capabilities

| Framework | What's stored | What that enables |
|-----------|--------------|-------------------|
| Temporal | Every event (append-only log) | Deterministic replay |
| LangGraph | Full state snapshots per superstep | Fork from any point, mutate, re-run |
| Inngest | Per-step memoized outputs | Skip completed steps on replay |
| Hatchet | Step input/output records | Re-run from original input |
| Prefect | State transitions (results opt-in) | Cache-key based idempotency |

### Four universal cross-framework patterns
1. Execution data always captured by default (not opt-in)
2. Same data serves multiple purposes (debugging + replay + monitoring)
3. Progressive disclosure: summary -> detail -> raw data
4. Structured output (JSON, CLI flags) for AI agent consumption

### Hatchet timing specifics
Four timestamps per step: `created_at`, `assigned_at`, `started_at`, `completed_at` — distinguishes queue latency vs execution time.

### LangGraph key quirk
Has NO native per-node timing — requires LangSmith or manual OTel. Hypergraph's `NodeEndEvent.duration_ms` is already better.

## Hypergraph Split-Brain Problem

| Layer | Has timing | Has values | Always-on |
|-------|-----------|-----------|-----------|
| Events (`NodeEndEvent.duration_ms`) | Yes | No | Only with processors |
| Checkpointer (`StepRecord.values`) | No | Yes | Requires explicit config |

### Spec vs implementation gap
`runners-api-reference.md` spec defines `NodeStartEvent.inputs` and `NodeEndEvent.outputs`, but actual `events/types.py` has neither. The spec anticipated value capture — never implemented.

### No checkpointer exists yet — StepRecord is spec-only

### `dispatcher.active` guard
When no event processors are passed, `active=False` and zero events fire.

## Design Options Explored

**Option A: RunResult carries full execution history**
- Zero config, natural API
- Dangerous for memory (10K map items x 5 nodes x LLM response = non-trivial)
- Muddies RunResult's role

**Option B: Everything through processors**
- Clean separation, composable
- Severe discoverability problem: by the time you need it, run is already over

**Option C (chosen): Minimal metadata always-on + opt-in for values**
- Timing metadata is O(nodes), not O(data) — negligible
- Values could be LLM responses, embeddings, DataFrames — memory unsafe to assume
- Always-on means `result.log` is always available

## Use Cases Designed

### UC1: "Why was my run slow?"
```python
result.log.summary()
# "50 items, 4m12s | embed: avg 0.2s | llm_call: avg 4.1s (p95: 12.3s)"
result.log.stats("llm_call")
# NodeStats(count=50, avg_ms=4100, p50_ms=2800, p95_ms=12300)
```

### UC2: "What failed and why?"
```python
result.log.errors
# [MapItemError(index=3, node="llm_call", error="504 Gateway Timeout"), ...]
```

### UC3: "What path did execution take?"
```python
result.log.execution_trace
# [Step(superstep=0, node="classify", duration_ms=120), ...]
```

### UC4: "What were the intermediate values?" — deferred, opt-in ValueTracer

## Final Design: "RunLog" — Always-On Execution Trace

```
Runner.run()
  create _RunLogCollector (internal TypedEventProcessor)
  prepend to processor list before creating dispatcher
  after execution: result.log = collector.build()
```

### API surface
```python
result.log.summary()              # "3 nodes, 2.1s, 0 errors | slowest: llm_call (1.8s)"
result.log.timing                 # {"fetch": 0.5, "llm_call": 1.8, "format": 0.3}
result.log.node_stats["llm_call"] # NodeStats(count=1, total_ms=1800, errors=0, cached=0)
result.log.steps                  # [NodeRecord(...), ...]
result.log.errors                 # failed steps only
result.log.to_dict()              # JSON-serializable for AI agents
```

### Naming
Used `NodeRecord` (not `StepRecord`) to avoid collision with persistence spec's `StepRecord`.

## Deferred
- Value capture (`log_values=True`)
- StructuredLogProcessor for AI/CI
- Map-level aggregate log
- Visual overlay (coloring viz nodes)
- Event field extensions (`inputs`/`outputs` on events)

## Key Takeaway
The plan was finalized but no code was written. The plan file is at:
`/Users/giladrubin/python_workspace/hypergraph/.claude/worktrees/hidden-sprouting-wreath/.claude/plans/hidden-sprouting-wreath.md`
