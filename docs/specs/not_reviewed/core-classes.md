# Core Classes Specification

## Class Hierarchy Overview

```
Graph                 # Pure structure definition
├── wraps nx.DiGraph  # NetworkX graph internally
├── nodes: list       # Registered hypergraph
└── validation        # Build-time checks

GraphState            # Runtime value storage
├── values: dict      # name → value
├── versions: dict    # name → version number
└── node_history      # Which nodes ran, when

SyncRunner            # Sync execution
├── cache             # Optional cache backend
├── callbacks         # Optional callbacks
└── run(graph, inputs) → outputs

AsyncRunner           # Async execution
├── cache             # Optional cache backend
├── callbacks         # Optional callbacks
├── run(graph, inputs) → outputs
└── iter(graph, inputs) → AsyncIterator[Event]

DaftRunner            # Distributed execution (DAG-only)
├── cache             # Optional cache backend
└── map(graph, inputs, map_over) → DataFrame
```

## Graph Class

### Purpose

Pure graph structure definition. No execution logic, no state, no cache. Just structure + validation.

### Constructor

```python
class Graph:
    def __init__(
        self,
        nodes: list[HyperNode],
        *,
        strict_types: bool = False,  # Opt-in type congruence checking
    ) -> None:
```

### Key Properties

```python
@property
def has_cycles(self) -> bool:
    """True if graph contains any cycles."""

@property
def inputs(self) -> set[str]:
    """All params that can accept external values."""

@property
def required(self) -> set[str]:
    """Must provide: no edge, no default, not bound."""

@property
def optional(self) -> set[str]:
    """Has fallback: no edge, has default OR bound."""

@property
def seeds(self) -> set[str]:
    """Cycle initialization: params with self/cycle edge."""

@property
def bound(self) -> dict[str, Any]:
    """Currently bound values."""

@property
def nx_graph(self) -> nx.DiGraph:
    """Access underlying NetworkX graph for visualization."""

@property
def nodes(self) -> dict[str, HyperNode]:
    """Map of node name → node object."""
```

### Key Methods

```python
def bind(self, **values) -> Graph:
    """
    Return new Graph with values pre-bound.

    Bound values are used when:
    - Parameter has no incoming edge, AND
    - No runtime input provided

    Attempting to bind a value that has an edge raises ValueError.

    Returns:
        New Graph instance with bound values
    """

def as_node(
    self,
    *,
    name: str | None = None,
    runner: SyncRunner | AsyncRunner | None = None,
) -> GraphNode:
    """
    Wrap graph as a node for composition.

    IMPORTANT: Returns a NEW GraphNode. Does NOT modify this Graph.

    Args:
        name: Node name (required if not set in Graph constructor)
        runner: SyncRunner or AsyncRunner for nested execution

    Returns:
        GraphNode that wraps this graph
    """
```

## GraphState Class

### Purpose

Tracks all values, their versions, and execution history during a run. Used internally by runners.

### Constructor

```python
class GraphState:
    def __init__(self, initial_values: dict[str, Any] | None = None) -> None:
        """
        Initialize state with optional starting values.
        Initial values get version 0.
        """
```

### Key Properties

```python
@property
def values(self) -> dict[str, Any]:
    """Current values (read-only view)."""

@property
def versions(self) -> dict[str, int]:
    """Version numbers for each value (read-only view)."""
```

### Key Methods

```python
def get(self, name: str) -> Any:
    """Get value by name. Raises KeyError if not present."""

def get_version(self, name: str) -> int:
    """Get version number for a value."""

def set(self, name: str, value: Any) -> GraphState:
    """
    Return new state with updated value.
    Increments version number.
    State is immutable - returns new instance.
    """

def is_stale(self, node_name: str, input_versions: dict[str, int]) -> bool:
    """
    Check if node needs re-execution.
    True if any input version > version when node last ran.
    """

def to_checkpoint(self) -> bytes:
    """Serialize state for persistence."""

@classmethod
def from_checkpoint(cls, data: bytes) -> GraphState:
    """Restore state from checkpoint."""
```

## Runner Classes

### Architecture

All runners inherit from `BaseRunner` and implement capability protocols:

```python
from abc import ABC, abstractmethod

class BaseRunner(ABC):
    """Abstract base runner - minimal shared interface."""
    
    def __init__(
        self,
        cache: Cache | None = None,
        callbacks: list[Callback] | None = None,
    ):
        self.cache = cache
        self.callbacks = callbacks or []
    
    @abstractmethod
    def run(self, graph: Graph, inputs: dict[str, Any], **kwargs):
        """All runners must support single execution."""
        pass
    
    @property
    @abstractmethod
    def capabilities(self) -> RunnerCapabilities:
        """Declare what this runner supports."""
        pass
```

**See** [**Core Types - Runner Class Hierarchy**](../../../specs/api/types.md#runner-class-hierarchy) **for complete architecture.**

### SyncRunner

```python
class SyncRunner(BaseRunner, SupportsBatch):
    """Synchronous execution runner."""
    
    capabilities = RunnerCapabilities(
        supports_cycles=True,
        supports_gates=True,
        supports_interrupts=False,
        supports_async_nodes=False,
        supports_streaming=False,
    )
```

### Constructor

```python
    def __init__(
        self,
        *,
        cache: Cache | None = None,
        callbacks: list[Callback] | None = None,
    ) -> None:
        """
        Create a synchronous runner.
      
        Args:
            cache: Cache backend (e.g., DiskCache)
            callbacks: List of callbacks for observability
        """
        super().__init__(cache, callbacks)
```

### Key Methods

```python
def run(
    self,
    graph: Graph,
    inputs: dict[str, Any],
    *,
    select: list[str] | None = None,
    session_id: str | None = None,
    max_iterations: int = 1000,
) -> dict[str, Any]: #@should return GraphResult object. see graph_design_docs/ md files.
    """
    Execute graph synchronously.
  
    Args:
        graph: Graph to execute
        inputs: Input values (determines where cycles start)
        select: Optional list of output names to return (default: all leaf outputs)
        session_id: Optional session ID for grouping related runs
        max_iterations: Maximum loop iterations before InfiniteLoopError
  
    Returns:
        Dict of output name → value
  
    Raises:
        GraphConfigError: If graph is invalid
        ConflictError: If parallel producers conflict
        MissingInputError: If required input not provided
        InfiniteLoopError: If max_iterations exceeded
        IncompatibleRunnerError: If graph has async nodes
    """

def map(
    self,
    graph: Graph,
    inputs: dict[str, Any],
    *,
    map_over: str | list[str],
) -> list[dict[str, Any]]:
    """
    Execute graph for each item in map_over parameter(s).
  
    Args:
        graph: Graph to execute
        inputs: Input values (map_over params should be lists)
        map_over: Parameter name(s) to iterate over
  
    Returns:
        List of output dicts, one per input item
    """
```

### AsyncRunner

```python
class AsyncRunner(BaseRunner, SupportsBatch, SupportsStreaming, SupportsAsync):
    """Asynchronous runner with full feature support."""
    
    capabilities = RunnerCapabilities(
        supports_cycles=True,
        supports_gates=True,
        supports_interrupts=True,
        supports_async_nodes=True,
        supports_streaming=True,
    )
```

### Constructor

```python
    def __init__(
        self,
        *,
        cache: Cache | None = None,
        callbacks: list[Callback] | None = None,
    ) -> None:
        """Create an asynchronous runner."""
        super().__init__(cache, callbacks)
```

### Key Methods

```python
async def run(
    self,
    graph: Graph,
    inputs: dict[str, Any],
    *,
    select: list[str] | None = None,
    session_id: str | None = None,
    max_iterations: int = 1000,
    checkpoint: bytes | None = None,  # Resume from checkpoint
) -> dict[str, Any]:
    """
    Execute graph asynchronously.
  
    Additional args vs SyncRunner:
        checkpoint: Resume execution from saved state
  
    Returns:
        Dict of output name → value
        If interrupted, returns partial outputs + checkpoint
    """

async def iter(
    self,
    graph: Graph,
    inputs: dict[str, Any],
    *,
    session_id: str | None = None,
) -> AsyncIterator[Event]:
    """
    Execute graph and yield events as they occur.
  
    Yields:
        Event objects (see Events specification)
  
    Use for:
        - Token-by-token streaming
        - Real-time progress updates
        - Human-in-the-loop interrupts
    """
```

### DaftRunner

```python
class DaftRunner(BaseRunner, SupportsBatch):
    """Distributed execution runner (DAG-only)."""
    
    capabilities = RunnerCapabilities(
        supports_cycles=False,
        supports_gates=False,
        supports_interrupts=False,
        supports_async_nodes=True,
        supports_distributed=True,
    )
```

### Constructor

```python
    def __init__(
        self,
        *,
        cache: Cache | None = None,
    ) -> None:
        """
        Create distributed runner.
        Note: callbacks have limited support (no iteration events).
        """
        super().__init__(cache, callbacks=[])
```

### Key Methods

```python
def map(
    self,
    graph: Graph,
    inputs: dict[str, Any],
    *,
    map_over: str | list[str],
) -> "daft.DataFrame":
    """
    Execute graph in distributed fashion using Daft.
  
    Args:
        graph: Must be a DAG (no cycles)
        inputs: Input values
        map_over: Parameter(s) to distribute over
  
    Returns:
        Daft DataFrame with results
  
    Raises:
        IncompatibleRunnerError: If graph has cycles, gates, or interrupts
    """
```

### Validation

```python
# DaftRunner must validate at run time:
if graph.has_cycles:
    raise IncompatibleRunnerError(
        "This graph has cycles, but DaftRunner doesn't support cycles.\n\n"
        "The problem: DaftRunner uses Daft DataFrames for distributed execution, "
        "which requires a DAG structure.\n\n"
        "How to fix:\n"
        "  Option A: Use SyncRunner or AsyncRunner instead\n"
        "  Option B: Restructure as a DAG"
    )
```
