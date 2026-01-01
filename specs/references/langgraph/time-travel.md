# LangGraph Time Travel Reference

> LangGraph v1.0 (October 2025) - Reference for HyperNodes design

## Overview

Time travel in LangGraph allows you to navigate through execution history, replay prior executions, and fork from any checkpoint to explore alternative paths. This is essential for debugging non-deterministic LLM-based systems.

---

## Core Concepts

### What Time Travel Enables

1. **Understand reasoning:** Analyze steps that led to a successful result
2. **Debug mistakes:** Identify where and why errors occurred
3. **Explore alternatives:** Test different paths from any point
4. **Reproduce issues:** Replay exact execution sequence

### How It Works

LangGraph persistently records the agent's state at each "super-step" via checkpointers. Each checkpoint is a complete snapshot that can be:
- **Retrieved:** Get state at any point
- **Replayed:** Re-execute from a checkpoint
- **Forked:** Create new branch from any point

---

## Checkpoint Identification

Every checkpoint has two identifiers:

```python
config = {
    "configurable": {
        "thread_id": "conversation-123",    # Groups related checkpoints
        "checkpoint_id": "0c62ca34-..."     # Specific checkpoint (UUID v6)
    }
}
```

- **`thread_id`:** Groups all checkpoints for a conversation/workflow
- **`checkpoint_id`:** Unique ID for specific checkpoint (time-ordered UUID)

---

## Retrieving State History

### Get Latest State

```python
config = {"configurable": {"thread_id": "1"}}
state = graph.get_state(config)

print(state.values)    # Current state values
print(state.next)      # Next nodes to execute
print(state.config)    # Checkpoint config
```

### Get Specific Checkpoint

```python
config = {
    "configurable": {
        "thread_id": "1",
        "checkpoint_id": "0c62ca34-abc1-..."
    }
}
state = graph.get_state(config)
```

### Get Full History

```python
config = {"configurable": {"thread_id": "1"}}

# Returns chronologically (newest first)
for state in graph.get_state_history(config):
    print(f"Step {state.metadata['step']}: {state.values}")
    print(f"  Checkpoint: {state.config['configurable']['checkpoint_id']}")
    print(f"  Next nodes: {state.next}")
```

---

## StateSnapshot Structure

Each checkpoint returns a `StateSnapshot`:

```python
@dataclass
class StateSnapshot:
    values: dict              # State channel values
    config: dict              # Checkpoint configuration
    metadata: dict            # Source, step, writes info
    next: tuple[str, ...]     # Next nodes to execute
    tasks: tuple[PregelTask, ...]  # Pending/errored tasks
    parent_config: dict | None     # Link to parent checkpoint
```

### Metadata Fields

```python
state.metadata = {
    "source": "loop",        # "input", "loop", "update", "fork"
    "step": 3,               # Execution step (-1 for initial)
    "writes": {...},         # What this step wrote
    "parents": {...},        # Parent checkpoints (for subgraphs)
}
```

---

## Replay: Re-Execute from Checkpoint

Replay executes the graph from a specific checkpoint, skipping already-executed steps:

```python
# Get checkpoint to replay from
history = list(graph.get_state_history(config))
target_checkpoint = history[2]  # Third from latest

# Replay from that checkpoint
replay_config = {
    "configurable": {
        "thread_id": "1",
        "checkpoint_id": target_checkpoint.config["configurable"]["checkpoint_id"]
    }
}

# Pass None as input - uses checkpoint state
result = graph.invoke(None, config=replay_config)
```

### What Happens During Replay

1. **Load checkpoint state:** Restore values from checkpoint
2. **Skip executed steps:** Don't re-run nodes before checkpoint
3. **Execute remaining steps:** Run nodes after checkpoint
4. **Create new checkpoints:** New execution creates new checkpoint branch

**Key insight:** Replay doesn't modify history - it creates a new branch.

---

## Fork: Branch from Any Point

Forking creates a new execution path from any checkpoint:

### Method 1: update_state + invoke

```python
# Fork by updating state at a specific checkpoint
config = {
    "configurable": {
        "thread_id": "1",
        "checkpoint_id": "target-checkpoint-id"
    }
}

# Modify state to create fork
graph.update_state(
    config,
    {"messages": [{"role": "user", "content": "Try different approach"}]},
    as_node="user_input"  # Attribute change to this node
)

# Execute from forked state
result = graph.invoke(None, config)
```

### Method 2: Direct invoke with modified input

```python
config = {
    "configurable": {
        "thread_id": "1",
        "checkpoint_id": "target-checkpoint-id"
    }
}

# Provide new input to fork
result = graph.invoke(
    {"messages": [{"role": "user", "content": "Different question"}]},
    config
)
```

### Fork vs Replay

| Aspect | Replay | Fork |
|--------|--------|------|
| State | Uses exact checkpoint state | Modifies state before continuing |
| Use case | Debug exact execution | Explore alternatives |
| History | References original | Creates new branch |

---

## update_state Method

Modify graph state programmatically:

```python
graph.update_state(
    config,           # Must include thread_id, optionally checkpoint_id
    values,           # State updates to apply
    as_node="node_name"  # Node to attribute change to
)
```

### Parameters

- **config:** Target thread/checkpoint
- **values:** Updates applied through reducer functions
- **as_node:**
  - Controls which node appears to have made the change
  - Affects which nodes run next based on graph structure

### Reducer Behavior

```python
# If state has reducer (e.g., messages with append)
graph.update_state(config, {"messages": [new_message]})
# → new_message is APPENDED to existing messages

# If no reducer (direct value)
graph.update_state(config, {"count": 5})
# → count is REPLACED with 5
```

---

## Practical Debugging Workflow

### 1. Identify Problem Point

```python
# Find where things went wrong
for state in graph.get_state_history(config):
    print(f"Step {state.metadata['step']}:")
    print(f"  Writes: {state.metadata.get('writes', {})}")
    if "error" in str(state.values):
        print(f"  *** ERROR FOUND ***")
        problem_checkpoint = state.config
        break
```

### 2. Inspect State at That Point

```python
state = graph.get_state(problem_checkpoint)
print(f"State values: {state.values}")
print(f"Next nodes: {state.next}")
```

### 3. Fork with Fix

```python
# Modify state to fix issue
graph.update_state(
    problem_checkpoint,
    {"user_input": "Corrected input"},
    as_node="input_handler"
)

# Re-run from fixed state
result = graph.invoke(None, problem_checkpoint)
```

### 4. Compare Outcomes

```python
# Original path
original_result = list(graph.get_state_history(original_config))

# Forked path
forked_result = list(graph.get_state_history(forked_config))

# Compare
for orig, forked in zip(original_result, forked_result):
    if orig.values != forked.values:
        print(f"Divergence at step {orig.metadata['step']}")
```

---

## LangGraph Studio Integration

LangGraph Studio (visual IDE) provides:

1. **Timeline view:** See all checkpoints visually
2. **Click to inspect:** View state at any checkpoint
3. **Fork button:** Create new branch from any point
4. **Diff view:** Compare states between checkpoints
5. **Replay controls:** Step through execution

---

## Implications for HyperNodes

### Current HyperNodes approach:
- `RunResult.checkpoint` for interrupt resume
- No explicit time travel API
- History available via `GraphResult.history`

### What to adopt:

1. **get_state_history() equivalent:**
```python
# Retrieve execution history for a run
history = runner.get_history(run_id)
for snapshot in history:
    print(snapshot.step, snapshot.values)
```

2. **Replay from checkpoint:**
```python
# Replay execution from specific point
result = runner.run(
    graph,
    inputs=None,  # Use checkpoint state
    checkpoint=snapshot.checkpoint,
    mode="replay"  # Skip already-executed nodes
)
```

3. **Fork from checkpoint:**
```python
# Fork with modified state
result = runner.run(
    graph,
    inputs={"modified": "value"},
    checkpoint=snapshot.checkpoint,
    mode="fork"  # Create new branch
)
```

4. **StateSnapshot type:**
```python
@dataclass
class StateSnapshot:
    values: dict[str, Any]
    step: int
    checkpoint: bytes
    node_executions: list[NodeExecution]
    next_nodes: list[str]
    parent_checkpoint: bytes | None
```

---

## Sources

- [LangGraph Time Travel Concepts](https://langchain-ai.github.io/langgraph/concepts/time-travel/)
- [LangGraph Persistence Documentation](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Time Travel in Agentic AI](https://pub.towardsai.net/time-travel-in-agentic-ai-3063c20e5fe2)
- [Checkpointing Architecture (DeepWiki)](https://deepwiki.com/langchain-ai/langgraph/4.1-checkpointing)
