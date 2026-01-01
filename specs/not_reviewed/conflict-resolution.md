# Conflict Resolution

**How the framework handles multiple producers, parallel execution, and edge cases.**

---

## Quick Example

```python
# Problem: Both nodes produce "result"
@branch(when_true="process_valid", when_false="handle_error")
def check(data: dict) -> bool:
    return data.get("valid", False)

@node(outputs="result")  # ← Same output name
def process_valid(data: dict) -> str:
    return transform(data)

@node(outputs="result")  # ← Same output name
def handle_error(data: dict) -> str:
    return "ERROR: Invalid data"

# ✅ Valid! Branches are mutually exclusive
# Only one will ever run
```

---

## The Core Problem

When multiple nodes declare the same `outputs`, the framework must determine if this is valid or an error:

```
Node A produces "result"
Node B produces "result"
         ↓
    Which one wins?
```

---

## Three Valid Scenarios

| Scenario | When It's Valid | How Framework Handles |
|----------|-----------------|----------------------|
| **Mutually Exclusive Branches** | Nodes are in different branches of a gate | Only one runs; gate ensures exclusivity |
| **Sequential Dependency** | One node depends (transitively) on the other | Dependency chain enforces order; versioning tracks updates |
| **Different Names + Merge** | User explicitly merges results | Use different output names + explicit merge node |

---

## Parallel Producer Conflict

### When It Happens

Two nodes that produce the same value become ready simultaneously.

### Detection

Before executing any ready nodes, the framework checks:

```python
for each value V:
    producers_of_V = {nodes ready to produce V}
    if |producers_of_V| > 1:
        raise ParallelProducersError
```

### Error Message Format

```
ParallelProducersError: Value 'result' has multiple producers ready simultaneously:
  - process_valid (inputs: data)
  - handle_error (inputs: error)

Cannot determine execution order. Options:
  1. Use different output names + explicit merge node
  2. Use @branch to make branches mutually exclusive
  3. Add dependencies to enforce sequential execution
```

### Resolution Options

| Option | When to Use | Example |
|--------|-------------|---------|
| **Different names + merge** | Both results are needed | `db_results` + `api_results` → `merge_results` |
| **Gate exclusivity** | Only one should run | `@branch(when_true="valid_path", when_false="error_path")` |
| **Add dependency** | One should wait for other | Make node B depend on node A's output |

---

## Mutual Exclusivity

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

```python
for each value with multiple producers:
    for each pair of producers (P1, P2):
        if not mutually_exclusive(P1, P2):
            raise GraphConfigError("Shared output without exclusivity")
```

### Gate Requirements Propagation

```python
gate_requirements(node):
    direct = control edges pointing to node
    inherited = union of gate_requirements(data dependencies)
    return direct ∪ inherited
```

**Example:**

```python
@branch(when_true="path_a", when_false="path_b")
def check(x: int) -> bool:
    return x > 0

@node(outputs="intermediate")
def path_a(x: int) -> str:
    return "positive"

@node(outputs="intermediate")
def path_b(x: int) -> str:
    return "negative"

@node(outputs="result")
def process_a(intermediate: str) -> str:
    return f"A: {intermediate}"

@node(outputs="result")
def process_b(intermediate: str) -> str:
    return f"B: {intermediate}"

# ✅ Valid!
# process_a inherits gate requirement from path_a
# process_b inherits gate requirement from path_b
# They are transitively mutually exclusive
```

---

## Sequential Accumulation (Sole Producer Rule)

### The Pattern

The "accumulator pattern" where a node reads and writes the same value:

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

```python
def is_stale(node, state):
    for each input:
        if sole_producer(input) == node:
            skip  # ← Skip staleness check for this input
        else:
            check if version changed
```

**Example:**

```python
@node(outputs="messages")
def add_response(messages: list, response: str) -> list:
    """Accumulator: both reads and writes 'messages'"""
    return messages + [{"role": "assistant", "content": response}]

# add_response is the SOLE producer of "messages"
# → Staleness check for "messages" input is skipped
# → Only re-runs when "response" changes
# → No infinite loop!
```

### When Sole Producer Rule Applies

| Situation | Sole Producer? | Staleness Check |
|-----------|----------------|-----------------|
| Only one node produces value | Yes | Skip this input |
| Multiple nodes produce value | No | Check version normally |
| Value is external input only | N/A | No producer in graph |

### Multi-Producer Accumulation

When multiple nodes can produce the same value:

```python
@node(outputs="messages")
def add_user_msg(messages: list, user_input: str) -> list:
    return messages + [{"role": "user", "content": user_input}]

@node(outputs="messages")
def add_assistant_msg(messages: list, response: str) -> list:
    return messages + [{"role": "assistant", "content": response}]
```

- Neither is "sole producer"
- Dependencies must enforce order
- Versioning tracks each update
- Framework validates no conflicts

---

## Input Conflicts

### The Scenario

When user-provided inputs cause parallel producers to become ready:

```python
graph.run(inputs={
    "messages": [],
    "user_input": "hi",      # Makes add_user_msg ready
    "response": "hello"      # Makes add_assistant_msg ready
})
```

Both `add_user_msg` and `add_assistant_msg` produce `messages` → conflict!

### Detection

Same as parallel conflict detection, but with enhanced error message showing **which inputs caused it**:

```
ParallelProducersError: Value 'messages' has multiple producers ready simultaneously:
  - add_user_msg (inputs: messages, user_input)
  - add_assistant_msg (inputs: messages, response)

This conflict was caused by providing both 'user_input' and 'response' as inputs.
Options:
  1. Remove 'response' from inputs to run normal flow
  2. Remove 'user_input' from inputs to skip to assistant
```

### Prevention

**Edge Cancels Default Rule** helps prevent many input conflicts:
- If a parameter has an incoming edge, its default is ignored
- Cyclic parameters have edges (by definition)
- Therefore, cyclic parameters must be initialized via input
- The input you provide determines where the cycle starts

```python
# Only provide ONE of these to avoid conflict:
runner.run(graph, inputs={"user_input": "hi", "messages": []})  # Start from user
# OR
runner.run(graph, inputs={"response": "hello", "messages": []})  # Start from assistant
# NOT BOTH!
```

---

## Checkpointing Edge Cases

### Checkpoint Contains Intermediate Values

When resuming, intermediate computed values should skip recomputation:

```python
checkpoint = {
    "enriched_q": "...", 
    "docs": [...], 
    "response": "..."
}

# Resume behavior:
# - enrich: output in checkpoint → SKIP
# - retrieve: output in checkpoint → SKIP
# - respond: output in checkpoint → SKIP
# - route: no output in checkpoint, inputs available → RUN
```

### Checkpoint Invalidation on New Input

When new input overrides a checkpoint value:

```python
checkpoint = {"messages": [...], "response": "old"}
new_input = {"response": "new"}

# Behavior:
# - response marked as "not from checkpoint" (user wants processing)
# - nodes depending on response become stale
# - route runs with new response value
```

### Partial Checkpoint Validation

**Framework validates** that checkpoint includes transitive dependencies:

```python
checkpoint = {"response": "..."}  # Missing: messages, docs

# Validation:
# - respond needs messages, docs → NOT in checkpoint
# - Framework raises: "Checkpoint missing required values: messages, docs"
```

---

## Validation Strategy

### Build-Time Validation

When `Graph()` is called:

✅ All `@route` targets reference existing nodes  
✅ Mutually exclusive branches can share output names (validated transitively)  
✅ Gates that can activate together don't produce conflicting outputs  
✅ Cycles have valid termination paths (route to `END` or reach leaf node)  
✅ Deadlock detection (cycle with no possible input to start it)  
✅ No structural impossibilities (self-loops without gates, etc.)

### Runtime Validation (Pre-Execution)

When `runner.run(graph, inputs={...})` is called, **before any node executes**:

✅ Input-dependent conflicts (user provided inputs that make parallel producers ready)  
✅ All required inputs are available  
✅ No dynamic conflicts in initial ready set

### During Execution

After each node completes:

✅ Check next ready set for conflicts before executing  
✅ Validate gate decisions reference valid targets

---

## Decision Tree

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

## Summary

| Concept | Key Rule |
|---------|----------|
| **Parallel Producers** | Multiple producers ready at once → Error |
| **Mutual Exclusivity** | Different gate branches can share output names |
| **Sole Producer Rule** | Accumulators don't re-trigger from own output |
| **Input Conflicts** | User inputs that activate parallel producers → Error |
| **Edge Cancels Default** | Parameters with edges can't use defaults (prevents conflicts) |
| **Build-Time First** | Validate structure before execution |
| **Runtime Second** | Validate inputs before first node |
| **During Execution** | Check each ready set for conflicts |

