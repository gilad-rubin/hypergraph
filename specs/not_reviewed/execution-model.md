# Execution Model Specification

## Overview

Hypernodes uses **reactive dataflow with versioning**. Nodes execute when their inputs are ready and stale. The same unified algorithm handles DAGs, branches, AND cycles.

## Core Concepts

### Values and Versions

Every value in the graph has:
- **Value**: The actual data
- **Version**: Monotonically increasing integer (starts at 0 for inputs)

```python
# Conceptual state structure
state = {
    "values": {
        "query": "What is RAG?",
        "messages": [{"role": "user", "content": "What is RAG?"}],
    },
    "versions": {
        "query": 0,      # Input, never changes
        "messages": 3,   # Updated 3 times (accumulator)
    }
}
```

### Staleness Detection

A node is **stale** if any of its inputs have changed since it last ran.

```python
def is_stale(node, state):
    """
    Check if node needs re-execution.
    
    Returns True if:
    - Node has never run, OR
    - Any input version > version when node last ran
    """
    if node not in state.node_history:
        return True
    
    last_run = state.node_history[node]
    for input_name in node.inputs:
        if state.versions[input_name] > last_run.input_versions[input_name]:
            return True
    return False
```

### Sole Producer Rule

**Critical for accumulators**: A node does NOT re-trigger from its own output.

```python
@node(outputs="messages")
def add_response(messages: list, response: str) -> list:
    return messages + [{"role": "assistant", "content": response}]
```

Without sole producer rule:
1. `add_response` runs → updates `messages` (v1)
2. `messages` changed! → `add_response` is stale
3. `add_response` runs again → infinite loop

With sole producer rule:
1. `add_response` runs → updates `messages` (v1)
2. Check staleness: skip own output when checking
3. `response` hasn't changed → not stale → stop

**Implementation**: When checking staleness, exclude outputs that this node itself produces.

## Ready Set Computation

The **ready set** is the set of nodes that can execute given current state.

```python
def compute_ready_set(graph, state):
    """
    Find all nodes that are ready to execute.
    
    A node is ready if:
    1. All required inputs are available in state
    2. Node is stale (inputs changed since last run)
    3. Any controlling gate has activated this node
    """
    ready = set()
    for node in graph.nodes:
        if all_inputs_available(node, state):
            if is_stale(node, state):
                if not blocked_by_gate(node, state):
                    ready.add(node)
    return ready
```

### Gate Blocking

Nodes downstream of gates are blocked until the gate fires and activates them.

```python
def blocked_by_gate(node, state):
    """
    Check if node is waiting for a gate decision.
    
    Returns True if:
    - Node has an incoming control edge from a gate
    - That gate hasn't fired yet, OR
    - Gate fired but chose a different path
    """
    for gate in get_controlling_gates(node):
        if gate not in state.gate_decisions:
            return True  # Gate hasn't decided yet
        if state.gate_decisions[gate] != node.name:
            return True  # Gate chose different path
    return False
```

## Execution Loop

### Main Loop

```python
def execute(graph, inputs, max_iterations=1000):
    state = GraphState(inputs)
    iteration = 0
    
    while True:
        ready = compute_ready_set(graph, state)
        
        if not ready:
            # No nodes ready - check if we're done
            if reached_termination(graph, state):
                break
            else:
                raise DeadlockError("No nodes ready but not at termination")
        
        # Execute all ready nodes (deterministic order)
        for node in sorted(ready, key=lambda n: n.name):
            state = execute_node(node, state)
            
            # Check for END signal from routes
            if state.terminated:
                break
        
        iteration += 1
        if iteration > max_iterations:
            raise InfiniteLoopError(f"Exceeded {max_iterations} iterations")
    
    return extract_outputs(graph, state)
```

### Node Execution

```python
def execute_node(node, state):
    # 1. Gather inputs
    inputs = {}
    for param in node.parameters:
        inputs[param] = state.get(param)
    
    # 2. Check cache
    if node.cache and cache_hit(node, inputs):
        outputs = get_cached(node, inputs)
        fire_callback("on_node_cached", node.name)
    else:
        # 3. Execute function
        fire_callback("on_node_start", node.name, inputs)
        outputs = node.func(**inputs)
        fire_callback("on_node_end", node.name, outputs)
        
        # 4. Handle generators (accumulate)
        if is_generator(outputs):
            outputs = accumulate_generator(outputs)
        
        # 5. Cache result
        if node.cache:
            set_cached(node, inputs, outputs)
    
    # 6. Update state with outputs
    state = state.set(node.outputs, outputs)
    
    # 7. Record execution history
    state = state.record_execution(node.name, current_input_versions)
    
    return state
```

### Termination Conditions

Execution terminates when:

1. **Leaf node reached**: A node with no outgoing edges completes
2. **END returned**: A `@route` explicitly returns `END`
3. **All requested outputs produced**: If `select` was specified

```python
def reached_termination(graph, state):
    # Check if any route returned END
    if state.terminated:
        return True
    
    # Check if we've reached leaf nodes
    for node in graph.leaf_nodes:
        if node.outputs in state.values:
            return True
    
    return False
```

## Conflict Detection

### What Is a Conflict?

Two nodes **conflict** if:
- They produce the same output name
- They can be ready simultaneously
- They are NOT mutually exclusive (different branches of same gate)

### When to Detect

1. **Build-time (static)**: Parallel producers with no gate separation
2. **Run-time (before execution)**: Input-dependent conflicts

```python
# Build-time check
def validate_no_static_conflicts(graph):
    for output in graph.all_outputs:
        producers = graph.producers_of(output)
        if len(producers) > 1:
            if not mutually_exclusive(producers, graph):
                raise GraphConfigError(
                    f"Multiple nodes produce '{output}': {producers}\n"
                    "Use @branch to make them mutually exclusive."
                )

# Run-time check (before first node executes)
def validate_no_dynamic_conflicts(graph, inputs):
    ready = compute_ready_set(graph, GraphState(inputs))
    outputs_produced = {}
    for node in ready:
        if node.outputs in outputs_produced:
            other = outputs_produced[node.outputs]
            raise ConflictError(
                f"Two nodes create '{node.outputs}' at the same time\n\n"
                f"  → {other} creates {node.outputs}\n"
                f"  → {node.name} creates {node.outputs}\n\n"
                "How to fix: Remove one from inputs or add dependency"
            )
        outputs_produced[node.outputs] = node.name
```

## Generator Handling

Modern LLM APIs return generators. The framework handles them automatically.

### Detection

```python
import inspect

def is_generator(value):
    return inspect.isgenerator(value) or inspect.isasyncgen(value)
```

### Accumulation

```python
def accumulate_generator(gen):
    """Consume generator and return accumulated result."""
    chunks = []
    for chunk in gen:
        chunks.append(chunk)
        fire_callback("on_streaming_chunk", chunk)
    
    # Concatenate based on type
    if all(isinstance(c, str) for c in chunks):
        return "".join(chunks)
    return chunks

async def accumulate_async_generator(gen):
    """Async version."""
    chunks = []
    async for chunk in gen:
        chunks.append(chunk)
        fire_callback("on_streaming_chunk", chunk)
    
    if all(isinstance(c, str) for c in chunks):
        return "".join(chunks)
    return chunks
```

### Streaming via `.iter()`

The `AsyncRunner.iter()` method yields events including streaming chunks:

```python
async def iter(self, graph, inputs):
    state = GraphState(inputs)
    
    async for event in execute_with_events(graph, state):
        yield event
        
        if isinstance(event, StreamingChunkEvent):
            # Chunk was already accumulated internally
            pass
        elif isinstance(event, InterruptEvent):
            # Execution paused, yield checkpoint
            yield CheckpointEvent(state.to_checkpoint())
            return
```

## Cache Interaction

### Signature Computation

Cache keys use **actual values**, not version numbers:

```python
def compute_signature(node, inputs):
    """
    Signature = hash(definition_hash + env_hash + input_values_hash)
    
    Key insight: Same inputs → same signature, regardless of iteration.
    This means multi-turn loops correctly recompute when messages change.
    """
    components = [
        hash_code(node.func),
        hash_env(node.env_vars),
        hash_values(inputs),  # Uses actual values!
    ]
    return hash_combine(components)
```

### Why Values, Not Versions

If we used versions:
- Iteration 1: `messages=[]` (v0) → signature_v0
- Iteration 2: `messages=[msg1]` (v1) → signature_v1
- Different signatures → correct, but...

If conversation restarts with same messages:
- New run: `messages=[msg1]` (v0) → signature_v0 ≠ signature_v1

Using actual values:
- Any run with `messages=[msg1]` → same signature → cache hit ✓

## Async vs Sync Execution

### SyncRunner (Sync)

- Executes nodes sequentially
- Raises error if any node is `async def`
- No streaming support (accumulates generators silently)

### AsyncRunner (Async)

- Can execute async nodes
- Supports `.iter()` for streaming
- Supports `InterruptNode` for human-in-the-loop
- Can resume from checkpoint

### Detection and Validation

```python
def validate_runner_compatibility(graph, runner):
    if isinstance(runner, SyncRunner):
        async_nodes = [n for n in graph.nodes if is_async(n.func)]
        if async_nodes:
            raise IncompatibleRunnerError(
                f"Graph has async nodes but SyncRunner is sync.\n"
                f"Async nodes: {async_nodes}\n"
                f"Use AsyncRunner instead."
            )
```
