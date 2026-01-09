# GraphState API Specification

## Overview

`GraphState` tracks all values, their versions, and execution history during a graph run. It is **immutable** - all operations return new state instances.

## Constructor

```python
class GraphState:
    def __init__(
        self,
        initial_values: dict[str, Any] | None = None,
    ) -> None:
        """
        Initialize graph state.
        
        Args:
            initial_values: Starting values (get version 0).
        
        Example:
            state = GraphState({"query": "hello", "messages": []})
        """
```

## Properties

### Value Access

```python
@property
def values(self) -> Mapping[str, Any]:
    """
    Read-only view of current values.
    
    Returns:
        Immutable mapping of name → value.
    """

@property
def versions(self) -> Mapping[str, int]:
    """
    Read-only view of version numbers.
    
    Returns:
        Immutable mapping of name → version.
    """
```

### Execution History

```python
@property
def executed_nodes(self) -> frozenset[str]:
    """Set of node names that have executed."""

@property
def node_history(self) -> Mapping[str, NodeExecution]:
    """
    Execution history per node.
    
    Returns:
        Mapping of node_name → NodeExecution record.
    """

@dataclass(frozen=True)
class NodeExecution:
    """Record of a node execution."""
    node_name: str
    input_versions: dict[str, int]  # Versions of inputs when run
    output_version: int             # Version of output produced
    timestamp: float
    duration_ms: float
    cached: bool
```

### Termination State

```python
@property
def terminated(self) -> bool:
    """True if execution should stop (END was returned)."""

@property
def gate_decisions(self) -> Mapping[str, str]:
    """
    Gate decisions made during execution.
    
    Returns:
        Mapping of gate_name → chosen_target.
    """
```

## Methods

### Value Operations

```python
def get(self, name: str) -> Any:
    """
    Get value by name.
    
    Args:
        name: Value name.
    
    Returns:
        The value.
    
    Raises:
        KeyError: If value doesn't exist.
    """

def get_version(self, name: str) -> int:
    """
    Get version number for a value.
    
    Args:
        name: Value name.
    
    Returns:
        Version number (0 for inputs, increments on updates).
    
    Raises:
        KeyError: If value doesn't exist.
    """

def has(self, name: str) -> bool:
    """Check if value exists."""

def set(self, name: str, value: Any) -> GraphState:
    """
    Return new state with updated value.
    
    Args:
        name: Value name.
        value: New value.
    
    Returns:
        New GraphState with updated value and incremented version.
        Original state is unchanged (immutable).
    
    Example:
        new_state = state.set("messages", messages + [new_msg])
    """

def set_multiple(self, values: dict[str, Any]) -> GraphState:
    """
    Return new state with multiple updated values.
    
    Args:
        values: Dict of name → value.
    
    Returns:
        New GraphState with all updates applied.
    """
```

### Staleness Detection

```python
def is_stale(
    self,
    node_name: str,
    input_names: list[str],
) -> bool:
    """
    Check if node needs re-execution.
    
    Args:
        node_name: Name of the node to check.
        input_names: Parameter names the node reads.
    
    Returns:
        True if:
        - Node has never run, OR
        - Any input version > version when node last ran
        
        Implements sole producer rule: excludes node's own outputs
        from staleness check.
    """

def get_input_versions(self, input_names: list[str]) -> dict[str, int]:
    """
    Get current versions for a set of inputs.
    
    Args:
        input_names: Parameter names.
    
    Returns:
        Dict of name → current version.
    """
```

### Execution Recording

```python
def record_execution(
    self,
    node_name: str,
    input_versions: dict[str, int],
    output_version: int,
    duration_ms: float,
    cached: bool = False,
) -> GraphState:
    """
    Record that a node executed.
    
    Args:
        node_name: Name of executed node.
        input_versions: Versions of inputs when run.
        output_version: Version of output produced.
        duration_ms: Execution duration.
        cached: Whether result came from cache.
    
    Returns:
        New GraphState with execution recorded.
    """

def record_gate_decision(
    self,
    gate_name: str,
    decision: str,
) -> GraphState:
    """
    Record a gate's routing decision.
    
    Args:
        gate_name: Name of gate node.
        decision: Chosen target (node name or "END").
    
    Returns:
        New GraphState with decision recorded.
    """

def mark_terminated(self) -> GraphState:
    """
    Mark execution as terminated (END reached).
    
    Returns:
        New GraphState with terminated=True.
    """
```

### Checkpointing

```python
def to_checkpoint(self) -> bytes:
    """
    Serialize state for persistence.
    
    Returns:
        Bytes that can be stored and later restored.
    
    Implementation notes:
        - Uses pickle with restricted unpickler for safety
        - Includes: values, versions, node_history, gate_decisions
        - Excludes: transient data, callbacks
    """

@classmethod
def from_checkpoint(cls, data: bytes) -> GraphState:
    """
    Restore state from checkpoint.
    
    Args:
        data: Bytes from to_checkpoint().
    
    Returns:
        Restored GraphState.
    
    Raises:
        CheckpointError: If data is corrupted or incompatible.
    """
```

## Immutability Pattern

All state modifications return new instances:

```python
# CORRECT
state = GraphState({"x": 1})
state2 = state.set("y", 2)
state3 = state2.set("x", 10)

# state.values == {"x": 1}           # Original unchanged
# state2.values == {"x": 1, "y": 2}  # Has y
# state3.values == {"x": 10, "y": 2} # Updated x

# WRONG - this does nothing useful
state.set("y", 2)  # Return value ignored, state unchanged
```

## Version Semantics

```python
# Initial values get version 0
state = GraphState({"query": "hello", "messages": []})
state.get_version("query")     # → 0
state.get_version("messages")  # → 0

# Each update increments version
state2 = state.set("messages", [{"role": "user", "content": "hi"}])
state2.get_version("messages")  # → 1

state3 = state2.set("messages", state2.get("messages") + [{"role": "assistant", "content": "hello"}])
state3.get_version("messages")  # → 2

# Version tracks HOW MANY TIMES value changed, not what it contains
```

## Sole Producer Rule Implementation

```python
def is_stale(self, node_name: str, input_names: list[str]) -> bool:
    """
    Check staleness, implementing sole producer rule.
    
    The sole producer rule: a node doesn't re-trigger from
    its own output. This prevents infinite loops in accumulators.
    """
    if node_name not in self.node_history:
        return True  # Never run → stale
    
    last_run = self.node_history[node_name]
    node_outputs = self._get_node_outputs(node_name)  # What this node produces
    
    for input_name in input_names:
        # SKIP our own outputs (sole producer rule)
        if input_name in node_outputs:
            continue
        
        # Check if input changed since we last ran
        current_version = self.versions.get(input_name, -1)
        last_seen_version = last_run.input_versions.get(input_name, -1)
        
        if current_version > last_seen_version:
            return True  # Input changed → stale
    
    return False  # All inputs same → not stale
```

## Usage Example

```python
# Initialize
state = GraphState({
    "query": "What is RAG?",
    "messages": [],
})

# Simulate execution
state = state.set("docs", ["doc1", "doc2"])
state = state.record_execution(
    node_name="retrieve",
    input_versions={"query": 0, "messages": 0},
    output_version=1,
    duration_ms=150.0,
)

# Check staleness
state.is_stale("retrieve", ["query", "messages"])  # → False

# Update messages (accumulator pattern)
state = state.set("messages", [{"role": "assistant", "content": "RAG is..."}])

# Now retrieve is stale (messages changed)
state.is_stale("retrieve", ["query", "messages"])  # → True

# Checkpoint for human-in-the-loop
checkpoint = state.to_checkpoint()
# ... later ...
restored = GraphState.from_checkpoint(checkpoint)
```
