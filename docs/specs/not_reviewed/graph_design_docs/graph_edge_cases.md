# Graph Execution Edge Cases

> **Status**: Design Document  
> **Type**: Edge Cases & Conflict Resolution  
> **Related**: [Design Philosophy](graph_design_philosophy.md) | [Implementation Guide](graph_implementation_guide.md)

---

## Table of Contents

1. [Multiple Producers](#multiple-producers)
2. [Parallel Conflicts](#parallel-conflicts)
3. [Sequential Accumulation](#sequential-accumulation)
4. [Mutual Exclusivity](#mutual-exclusivity)
5. [Input Conflicts](#input-conflicts)
6. [Checkpointing Edge Cases](#checkpointing-edge-cases)
7. [Cycle Detection](#cycle-detection)

---

## Multiple Producers

When multiple nodes declare the same `output_name`, the framework must determine if this is valid or an error.

### The Problem

```
Node A produces "result"
Node B produces "result"
             ↓
    Which one wins?
```

### Three Valid Scenarios

| Scenario | When It's Valid | How Framework Handles |
|----------|-----------------|----------------------|
| **Mutually Exclusive** | Nodes are in different branches of a gate | Only one runs; gate ensures exclusivity |
| **Sequential** | One node depends (transitively) on the other | Dependency chain enforces order; versioning tracks updates |
| **Merge Pattern** | User wants to combine results | Use different names + explicit merge node |

### Resolution Strategy

1. **At build time**: Check if producers are mutually exclusive (different gate branches)
2. **At runtime**: If both become ready simultaneously → `ParallelProducersError`
3. **User action required**: Either add dependency, use different names, or declare exclusivity

---

## Parallel Conflicts

When two nodes that produce the same value become ready in the same iteration.

### Detection

Before executing any ready nodes, the framework checks:

```
For each value V:
  producers_of_V = {nodes ready to produce V}
  if |producers_of_V| > 1:
    raise ParallelProducersError
```

### Error Message Pattern

```
ParallelProducersError: Value 'result' has multiple producers ready simultaneously:
  - process_valid (inputs: data)
  - handle_error (inputs: error)

Cannot determine execution order. Options:
  1. Use different output names + explicit merge node
  2. Use @gate to make branches mutually exclusive
  3. Add dependencies to enforce sequential execution
```

### Resolution Options

| Option | When to Use | Example |
|--------|-------------|---------|
| **Different names + merge** | Both results are needed | `db_results` + `api_results` → `merge_results` |
| **Gate exclusivity** | Only one should run | `@branch(when_true="valid_path", when_false="error_path")` |
| **Add dependency** | One should wait for other | Make node B depend on node A's output |

---

## Sequential Accumulation

The "accumulator pattern" where a node reads and writes the same value.

### The Pattern

```
messages v1 → [add_response] → messages v2 → [add_response] → messages v3
```

### The Problem

Without special handling, `add_response` would re-trigger infinitely:
- Runs with messages v1 → produces v2
- Sees messages changed (v1→v2) → thinks it's stale → runs again
- Produces v3 → sees change → runs again → ∞

### The Solution: Sole Producer Rule

When checking staleness, skip inputs where **this node is the only producer**:

```
is_stale(add_response):
  for each input:
    if sole_producer(input) == add_response:
      skip  ← "messages" skipped because add_response is sole producer
    else:
      check if version changed
```

### When Sole Producer Rule Applies

| Situation | Sole Producer? | Staleness Check |
|-----------|----------------|-----------------|
| Only one node produces value | Yes | Skip this input |
| Multiple nodes produce value | No | Check version normally |
| Value is external input only | N/A | No producer in graph |

### Multi-Producer Accumulation

When multiple nodes can produce the same value (e.g., `add_user_msg` and `add_assistant_msg` both produce `messages`):

- Neither is "sole producer"
- Dependencies must enforce order
- Versioning tracks each update

---

## Mutual Exclusivity

When nodes in different gate branches can safely share an output name.

### Determining Exclusivity

Two nodes are mutually exclusive if:
1. They require **different decisions from the same gate**
2. This is true **transitively** (through dependencies)

### Transitive Exclusivity

```
          ┌→ [A] → [C] ─┐
[gate] ───┤             ├→ both produce "result"
          └→ [B] → [D] ─┘
```

- A and B are exclusive (direct gate targets)
- C and D are also exclusive (inherit from A and B)
- C producing "result" and D producing "result" is **valid**

### Validation at Build Time

```
For each value with multiple producers:
  For each pair of producers (P1, P2):
    if not mutually_exclusive(P1, P2):
      raise ConfigError("Shared output without exclusivity")
```

### Gate Requirements Propagation

```
gate_requirements(node):
  direct = control edges pointing to node
  inherited = union of gate_requirements(data dependencies)
  return direct ∪ inherited
```

---

## Input Conflicts

When user-provided inputs cause parallel producers to become ready.

### The Scenario

```python
graph.run(inputs={
    "messages": [],
    "user_input": "hi",      # Makes add_user ready
    "response": "hello"      # Makes add_assistant ready
})
```

Both `add_user` and `add_assistant` produce `messages` → conflict!

### Detection

Same as parallel conflict detection, but with enhanced error message showing **which inputs caused it**:

```
ParallelProducersError: Value 'messages' has multiple producers ready simultaneously:
  - add_user (inputs: messages, user_input)
  - add_assistant (inputs: messages, response)

This conflict was caused by providing both 'user_input' and 'response' as inputs.
Options:
  1. Remove 'response' from inputs to run normal flow
  2. Remove 'user_input' from inputs to skip to assistant
  3. Use graph.run_sequence([...]) to run both in order
```

### Resolution: run_sequence

For explicit ordering when inputs would conflict:

```python
graph.run_sequence([
    {"messages": [], "user_input": "hi"},   # First iteration
    {"response": "hello"}                    # Second iteration
])
```

Each dict is a separate execution step, allowing controlled sequencing.

---

## Checkpointing Edge Cases

### Checkpoint Contains Intermediate Values

When resuming, intermediate computed values should skip recomputation:

```
Checkpoint: {enriched_q: "...", docs: [...], response: "..."}

Resume behavior:
  - enrich: output in checkpoint → SKIP
  - retrieve: output in checkpoint → SKIP
  - respond: output in checkpoint → SKIP
  - route: no output, inputs available → RUN
```

### Checkpoint Invalidation on New Input

When new input overrides a checkpoint value:

```
Checkpoint: {messages: [...], response: "old"}
New input: {response: "new"}

Behavior:
  - response marked as "not external" (user wants processing)
  - nodes depending on response become stale
  - route runs with new response value
```

### Partial Checkpoint

What if checkpoint is missing values needed for resume?

```
Checkpoint: {response: "..."}  # Missing: messages, docs

Resume:
  - respond needs messages, docs → NOT READY
  - route needs response → READY → runs
  - route decides "more" → retrieve needs enriched_q → ???
```

**Solution**: Checkpoint must include transitive dependencies. Framework validates this at checkpoint time.

### Human-in-the-Loop Pause

Special checkpoint that includes "paused at" marker:

```
checkpoint = {
    "state": {...},
    "paused_at": "approval_gate",
    "pending_decision": ["approve", "reject", "edit"]
}
```

Resume requires explicit decision:

```python
graph.resume(checkpoint, decision="approve")
```

---

## Cycle Detection

### At Build Time

Use standard graph algorithm (DFS) to detect cycles in the graph structure.

### Cycles Are Valid When

1. Graph is explicitly declared as cyclic (has gates that loop back)
2. Termination is guaranteed (gate can reach END)

### Infinite Loop Prevention

Even in valid cyclic graphs, runtime protection:

1. **Max iterations**: Configurable limit on loop count
2. **Staleness convergence**: If no node is stale, execution stops
3. **END sentinel**: Gate explicitly terminates

### Nested Graph Cycles

Cycles can exist in nested graphs:

```
Outer graph (acyclic):
  [A] → [inner_graph] → [B]

Inner graph (cyclic):
  [X] → [Y] → [gate] → [X] (loop)
```

This is valid: the inner graph handles its own cycles, outer graph sees it as single node.

---

## Summary: Edge Case Decision Tree

```
Multiple producers for same value?
├─ Are they mutually exclusive (gate branches)?
│   ├─ Yes → Valid, only one runs
│   └─ No → Continue checking
│
├─ Are they in a dependency chain?
│   ├─ Yes → Valid, versioning handles order
│   └─ No → Continue checking
│
└─ Do they become ready simultaneously?
    ├─ Yes → ParallelProducersError
    └─ No → Valid, whichever is ready first runs
```

---

> **See also**: 
> - [Design Philosophy](graph_design_philosophy.md) for why these rules exist
> - [Implementation Guide](graph_implementation_guide.md) for code implementation
