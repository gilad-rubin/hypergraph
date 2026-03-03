# LangGraph Debugging & Inspection Research

## Architecture: Two Separate Systems

Checkpointing and tracing are **completely separate** in LangGraph.

| Dimension | Checkpointing | LangSmith Tracing |
|-----------|--------------|-------------------|
| Purpose | State persistence, resume, time travel | Observability, debugging, monitoring |
| Data stored | Full graph state snapshots | Inputs/outputs/latency/errors per span |
| Storage | Your backend (memory, SQLite, Postgres) | LangSmith cloud |
| Correlation key | `thread_id` / `checkpoint_id` | `run_id` / `thread_id` |
| Activation | Compile with `checkpointer=...` | `LANGSMITH_TRACING=true` env var |

## Checkpoint Data Model

```python
CheckpointTuple(
    config=dict,           # thread_id, checkpoint_ns, checkpoint_id
    checkpoint=dict,       # channel_values, channel_versions, versions_seen
    metadata=dict,         # source, writes, step, parents
    parent_config=dict,    # Parent checkpoint (linked list)
    pending_writes=list    # In-flight state updates
)
```

### Metadata
```python
metadata = {
    "source": "loop",    # "input" | "loop" | "fork" | "update"
    "writes": {"node_a": {"foo": "val"}},  # What changed this step
    "step": 1,           # Superstep index (-1 = initial)
    "parents": {}
}
```

### StateSnapshot (get_state returns)
```python
StateSnapshot(
    values=dict,         # Channel values
    config=dict,         # Full config with checkpoint_id
    next=list[str],      # Next nodes to invoke
    metadata=dict,       # source/writes/step
    created_at=datetime,
    parent_config=dict
)
```

## Time Travel
- Every `invoke()` creates a checkpoint
- History is a linked list (or DAG when forked)
- Replay from any checkpoint via `graph.invoke(None, config=past_config)`
- `update_state()` creates fork, not overwrite
- Original history preserved alongside new branches

## Failed Run Handling
- Checkpoint from last SUCCESSFUL superstep is preserved
- `state.next` points to the failing node
- Error visible in LangSmith traces (red span with stack trace)
- Recovery: fix state via `update_state()`, then `invoke(None, config)`

## Key Insight: NO native per-node timing
LangGraph has no built-in `duration_ms` — requires LangSmith for that.
The `writes` field in metadata IS the diff (what changed at each step).

## Platform Entities
| ID | What |
|---|---|
| `thread_id` | Persistent session, holds all checkpoints |
| `run_id` | Single graph execution on a thread |
| `checkpoint_id` | Specific snapshot in history |
| `checkpoint_ns` | Namespace for subgraph isolation |

## Streaming Debug Mode
```python
stream_mode="debug"  # Emits checkpoint, task, task_result events
```

## Design Insights for Hypergraph
1. Checkpointing saves after every superstep, not just graph boundaries
2. `writes` field is the diff — what changed at each step
3. Time travel creates forks, not overwrites
4. Traces and checkpoints are independent — LangSmith is optional/external
5. Failed runs leave checkpoints intact — last successful checkpoint survives
