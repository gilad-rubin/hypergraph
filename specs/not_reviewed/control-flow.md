# Control Flow Specification

## Overview

Control flow in Hypernodes is managed through **gates** - special nodes that decide which downstream nodes execute. There are two gate types:

- `@route` - Multi-way routing (returns target node name)
- `@branch` - Binary routing (returns bool, maps to true/false targets)

## The END Sentinel

`END` is a special sentinel value that explicitly terminates cycles.

```python
from hypernodes import END

@route(targets=["process", "validate", END])
def decide_next(result: str) -> str:
    if is_complete(result):
        return END  # Stop the cycle
    if needs_validation(result):
        return "validate"
    return "process"
```

### END Semantics

- `END` is a valid route target (must be declared in `targets`)
- Returning `END` sets `state.terminated = True`
- Execution loop exits on next iteration
- Leaf outputs are collected and returned

## @route Decorator

### Purpose

Multi-way routing based on runtime decisions. Enables cycles by routing back to earlier nodes.

### Signature

```python
def route(
    targets: list[str | END],
    *,
    cache: bool = False,  # Routes should not be cached by default
) -> Callable:
    """
    Mark function as a routing decision node.
    
    Args:
        targets: List of valid target node names and/or END.
                 REQUIRED - no default, must be explicit.
        cache: Whether to cache route decisions (default False).
    
    Returns:
        Decorated function that returns target name as string.
    
    Raises:
        GraphConfigError: At Graph() construction if any target doesn't exist.
    """
```

### Usage

```python
@route(targets=["retrieve", "generate", END])
def should_continue(messages: list, max_turns: int = 10) -> str:
    if len(messages) > max_turns:
        return END
    if needs_more_context(messages):
        return "retrieve"
    return "generate"
```

### Validation Rules

1. **targets is required** - Must explicitly declare valid targets
2. **All targets must exist** - Validated at `Graph()` construction
3. **Return value must match** - Runtime check that returned value is in targets
4. **END must be declared** - Can only return END if it's in targets

### How Routes Create Cycles

```python
@node(outputs="response")
def generate(messages: list) -> str:
    return llm.generate(messages)

@node(outputs="messages")
def add_response(messages: list, response: str) -> list:
    return messages + [{"role": "assistant", "content": response}]

@route(targets=["generate", END])
def check_done(messages: list) -> str:
    if user_satisfied(messages):
        return END
    return "generate"  # ← Creates cycle back to generate

# Flow: generate → add_response → check_done → generate (cycle!)
```

### Control Edges

Routes create **control edges** in the graph (distinct from data edges):

```python
# Data edge: parameter name matches output name
# Control edge: route decision activates target node

graph.edges = [
    # Data edges (implicit from signatures)
    Edge("generate", "add_response", type="data", value="response"),
    Edge("add_response", "check_done", type="data", value="messages"),
    
    # Control edges (from route)
    Edge("check_done", "generate", type="control", condition="generate"),
    Edge("check_done", END, type="control", condition="END"),
]
```

## @branch Decorator

### Purpose

Simplified routing for boolean decisions. Syntactic sugar over `@route` with exactly two targets.

### Signature

```python
def branch(
    when_true: str | HyperNode,
    when_false: str | HyperNode,
    *,
    cache: bool = False,
) -> Callable:
    """
    Mark function as binary routing decision.
    
    Args:
        when_true: Target when function returns True (node name or node object)
        when_false: Target when function returns False
        cache: Whether to cache (default False)
    
    Returns:
        Decorated function that returns bool.
    """
```

### Usage

```python
@branch(when_true="use_cache", when_false="compute_fresh")
def check_cache(query: str, cache: dict) -> bool:
    return query in cache

@node(outputs="result")
def use_cache(query: str, cache: dict) -> str:
    return cache[query]

@node(outputs="result")  # Same output name OK - mutually exclusive
def compute_fresh(query: str) -> str:
    return expensive_computation(query)
```

### Equivalence to @route

```python
# These are equivalent:

@branch(when_true="node_a", when_false="node_b")
def decide(x: int) -> bool:
    return x > 0

@route(targets=["node_a", "node_b"])
def decide(x: int) -> str:
    return "node_a" if x > 0 else "node_b"
```

## Mutual Exclusivity

### The Rule

Nodes on different branches of the same gate can produce the same output name.

```python
@branch(when_true="path_a", when_false="path_b")
def gate(x: int) -> bool:
    return x > 0

@node(outputs="result")  # Same name
def path_a(x: int) -> str:
    return "positive"

@node(outputs="result")  # Same name - OK because mutually exclusive
def path_b(x: int) -> str:
    return "non-positive"
```

### Validation

At build time, check if same-output producers are mutually exclusive:

```python
def mutually_exclusive(node_a, node_b, graph):
    """
    Check if two nodes are on mutually exclusive branches.
    
    Returns True if there exists a gate where:
    - node_a is reachable only from one branch
    - node_b is reachable only from a different branch
    """
    for gate in graph.gates:
        a_branches = branches_reaching(node_a, gate)
        b_branches = branches_reaching(node_b, gate)
        if a_branches and b_branches and not (a_branches & b_branches):
            return True
    return False
```

## Gate Signals

Gates emit signals when they decide, useful for observability:

```python
class GateDecisionEvent:
    gate_name: str      # Name of the gate node
    decision: str       # The target chosen
    inputs: dict        # Inputs that led to decision
    timestamp: float
```

### Callback Integration

```python
class MyCallback(Callback):
    def on_route_decision(self, gate_name: str, target: str):
        print(f"Gate {gate_name} chose {target}")
```

## Cycle Termination Validation

At build time, verify that all cycles have a termination path:

```python
def validate_cycle_termination(graph):
    """
    Ensure every cycle can terminate.
    
    A cycle can terminate if:
    - It contains a @route with END in targets, OR
    - It has a path to a leaf node (node with no outgoing edges)
    
    Raises:
        GraphConfigError: If cycle has no termination path
    """
    for cycle in graph.cycles:
        has_end_route = any(
            END in node.targets 
            for node in cycle 
            if isinstance(node, RouteNode)
        )
        has_leaf_path = any(
            graph.has_path(node, leaf)
            for node in cycle
            for leaf in graph.leaf_nodes
        )
        
        if not has_end_route and not has_leaf_path:
            raise GraphConfigError(
                f"Cycle has no termination path: {cycle}\n\n"
                "How to fix:\n"
                "  Add a @route with END in targets, or\n"
                "  Add a path from the cycle to a leaf node"
            )
```

## Deadlock Detection

A deadlock occurs when a cycle has no valid starting input:

```python
def validate_no_deadlock(graph):
    """
    Ensure cycles can start.
    
    A cycle can start if:
    - At least one node in the cycle can be made ready via inputs
    
    The "edge cancels default" rule means cyclic parameters MUST
    be initialized via inputs.
    """
    for cycle in graph.cycles:
        startable = False
        for node in cycle:
            # Can this node start if we provide its non-cyclic inputs?
            non_cyclic_inputs = [
                p for p in node.parameters 
                if not graph.is_in_cycle(p, cycle)
            ]
            if can_be_ready_with_inputs(node, non_cyclic_inputs):
                startable = True
                break
        
        if not startable:
            raise GraphConfigError(
                f"Cycle cannot start - no node can be made ready: {cycle}\n\n"
                "This usually means every node in the cycle depends on "
                "another node in the cycle, with no external entry point."
            )
```
