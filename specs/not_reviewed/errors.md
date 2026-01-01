# Error Handling Specification

## Philosophy

**Errors should be helpful to someone who has never seen the framework before.**

Principles:
1. **Use simple terms** - Avoid jargon like "producer", "consumer", "parallel execution"
2. **Explain the crux** - What's actually wrong, in plain English
3. **Show the conflict** - Make it obvious WHY this is a problem
4. **Give concrete options** - Actionable fixes, not just "fix your code"
5. **Suggest typo fixes** - Use fuzzy matching to catch common mistakes

## Error Hierarchy

```python
class HyperNodesError(Exception):
    """Base class for all Hypernodes errors."""

class GraphConfigError(HyperNodesError):
    """Build-time validation failure."""

class ConflictError(HyperNodesError):
    """Two nodes produce same output simultaneously."""

class MissingInputError(HyperNodesError):
    """Required input not provided."""

class InvalidRouteError(HyperNodesError):
    """Route returned invalid target."""

class InfiniteLoopError(HyperNodesError):
    """Cycle exceeded max iterations."""

class DeadlockError(HyperNodesError):
    """Cycle cannot start - no node is ready."""

class IncompatibleRunnerError(HyperNodesError):
    """Runner doesn't support graph features."""

class CheckpointError(HyperNodesError):
    """Checkpoint serialization/restore failed."""
```

## Error Message Format

All errors follow this structure:

```
ErrorType: One-line summary

  → Specific detail 1
  → Specific detail 2

The problem: Plain English explanation of WHY this is wrong.

How to fix:

  Option A: First fix suggestion
            → Example code if helpful

  Option B: Alternative fix
            → Example code if helpful

[Optional hint for typos]
```

---

## GraphConfigError

Raised at `Graph()` construction for structural problems.

### Invalid Route Target

```python
GraphConfigError: @route target 'retreive' doesn't exist

  → should_continue() declares targets=["retreive", "generate", END]
  → No node named 'retreive' in this graph
  → Available nodes: ['retrieve', 'generate', 'add_message']

Did you mean 'retrieve'?
```

### No Termination Path

```python
GraphConfigError: Cycle has no termination path

  → Cycle: generate → evaluate → generate
  → No @route returns END
  → No path to a leaf node

The problem: This cycle will run forever because there's no way out.

How to fix:

  Option A: Add END to your route
            
            @route(targets=["generate", END])  # Add END
            def quality_check(score: float) -> str:
                return END if score > 0.9 else "generate"

  Option B: Add a path to a node outside the cycle
```

### Parallel Producer Conflict

```python
GraphConfigError: Multiple nodes produce 'result'

  → process_a creates 'result'
  → process_b creates 'result'

The problem: If both nodes run, which 'result' should we use?
The framework can't decide for you.

How to fix:

  Option A: Rename one output
            
            @node(outputs="result_a")  # Different name
            def process_a(x: int) -> int: ...

  Option B: Make them mutually exclusive with @branch
            
            @branch(when_true="process_a", when_false="process_b")
            def choose_path(x: int) -> bool: ...
```

### Self-Reference Without Gate

```python
GraphConfigError: Node references itself without a gate

  → accumulate() has parameter 'messages' AND outputs='messages'
  → This creates a self-loop

The problem: Without a gate to break the loop, this would run forever.

How to fix:

  Add a @route that can return END:

    @route(targets=["accumulate", END])
    def should_continue(messages: list) -> str:
        return END if done(messages) else "accumulate"
```

---

## ConflictError

Raised at runtime when inputs create parallel ready nodes with same output.

```python
ConflictError: Two nodes create 'messages' at the same time

  → add_user creates 'messages' (ready because you provided 'user_input')
  → add_assistant creates 'messages' (ready because you provided 'response')

The problem: If add_user sets messages=[A] and add_assistant sets messages=[B],
which one should we use? The framework can't decide for you.

How to fix (pick ONE):

  Option A: Remove 'response' from inputs
            → add_user runs first, then add_assistant follows naturally
            
            runner.run(graph, inputs={"user_input": "hello", "messages": []})

  Option B: Remove 'user_input' from inputs
            → Start from add_assistant instead

  Option C: Make add_assistant depend on add_user
            → Forces add_user to always run first
```

---

## MissingInputError

Raised when required input isn't available.

### Cycle Initialization

```python
MissingInputError: 'messages' needs a starting value

  → add_response wants to read 'messages', but nothing has created it yet
  → This is a cycle - add_response creates 'messages' for the NEXT iteration,
    but what about the FIRST iteration?

How to fix:

  Provide an initial value in your inputs:
  
    runner.run(graph, inputs={..., "messages": []})
```

### Missing Required Input

```python
MissingInputError: 'query' is required but not provided

  → embed() needs 'query' to run
  → No node produces 'query'
  → No default value defined

How to fix:

  Provide 'query' in your inputs:
  
    runner.run(graph, inputs={"query": "your search query"})
```

---

## InvalidRouteError

Raised at runtime when route returns unexpected value.

```python
InvalidRouteError: Route returned 'retreive' but that's not a valid target

  → should_continue() returned "retreive"
  → Valid targets are: ["retrieve", "generate", END]

Hint: Did you mean "retrieve"? (looks like a typo)
```

---

## InfiniteLoopError

Raised when cycle exceeds max iterations.

```python
InfiniteLoopError: Exceeded 1000 iterations

  → Cycle: generate → evaluate → generate
  → Ran 1000 times without reaching END

The problem: Either your termination condition is never met,
or you need more iterations for legitimate reasons.

How to fix:

  Option A: Check your termination logic
            → Is the route ever returning END?
            → Add logging to see what's happening

  Option B: Increase max_iterations if this is expected
            
            runner.run(graph, inputs={...}, max_iterations=5000)
```

---

## DeadlockError

Raised when cycle has no valid starting point.

```python
DeadlockError: Cycle cannot start - no node is ready

  → Cycle: node_a → node_b → node_a
  → node_a needs 'b' (from node_b)
  → node_b needs 'a' (from node_a)
  → Neither can run first!

The problem: Every node in this cycle depends on another node
in the same cycle. There's no entry point.

How to fix:

  Provide one of the cyclic values as input:
  
    runner.run(graph, inputs={"a": initial_value})  # Start from node_b
    # OR
    runner.run(graph, inputs={"b": initial_value})  # Start from node_a
```

---

## IncompatibleRunnerError

Raised when runner doesn't support graph features.

### Cycles with DaftRunner

```python
IncompatibleRunnerError: This graph has cycles, but DaftRunner doesn't support cycles.

  → DaftRunner uses Daft DataFrames for distributed execution
  → Distributed execution requires a DAG structure
  → Your graph has cycle: generate → evaluate → generate

How to fix:

  Option A: Use SyncRunner or AsyncRunner instead
            
            runner = AsyncRunner(cache=DiskCache("./cache"))
            result = await runner.run(graph, inputs={...})

  Option B: Restructure as a DAG (if possible)
```

### Async Nodes with Sync Runner

```python
IncompatibleRunnerError: Graph has async nodes but SyncRunner is synchronous.

  → Async nodes found: ['generate', 'stream_response']
  → SyncRunner can only execute sync functions

How to fix:

  Use AsyncRunner instead:
  
    runner = AsyncRunner(cache=DiskCache("./cache"))
    result = await runner.run(graph, inputs={...})
```

### InterruptNode with Sync Runner

```python
IncompatibleRunnerError: Graph has InterruptNode but SyncRunner doesn't support interrupts.

  → InterruptNode 'human_review' requires async execution
  → SyncRunner is synchronous

How to fix:

  Use AsyncRunner for human-in-the-loop workflows:
  
    runner = AsyncRunner()
    result = await runner.run(graph, inputs={...})
    
    if result.interrupted:
        # Handle interrupt...
```

---

## Implementation: Typo Detection

```python
from difflib import get_close_matches

def suggest_typo_fix(invalid: str, valid_options: list[str]) -> str | None:
    """
    Suggest a fix if the invalid value looks like a typo.
    
    Returns:
        Suggestion string or None if no close match.
    """
    matches = get_close_matches(invalid, valid_options, n=1, cutoff=0.6)
    if matches:
        return f"Did you mean '{matches[0]}'?"
    return None
```

---

## Implementation: Error Construction

```python
def build_error_message(
    summary: str,
    details: list[str],
    problem: str,
    fixes: list[tuple[str, str | None]],  # (description, code_example)
    hint: str | None = None,
) -> str:
    """Build consistently formatted error message."""
    lines = [summary, ""]
    
    for detail in details:
        lines.append(f"  → {detail}")
    
    lines.extend(["", f"The problem: {problem}", "", "How to fix:", ""])
    
    for i, (desc, code) in enumerate(fixes):
        option = chr(ord('A') + i)
        lines.append(f"  Option {option}: {desc}")
        if code:
            lines.append(f"            ")
            for code_line in code.strip().split('\n'):
                lines.append(f"            {code_line}")
        lines.append("")
    
    if hint:
        lines.append(hint)
    
    return '\n'.join(lines)
```
