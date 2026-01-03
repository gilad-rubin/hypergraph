# Graph Versioning Design

**Date:** 2026-01-03
**Status:** Approved
**Purpose:** Detect schema changes on workflow resume to prevent silent data corruption

---

## Overview

Add Merkle-tree style hashing to detect when a graph's structure or code has changed since a workflow was started. This enables safe workflow resume by catching breaking changes before they cause data integrity issues.

## Design Decisions

| Decision | Choice |
|----------|--------|
| Spec location | `graph.md` for hash, `runners-api-reference.md` for force_resume |
| Nested graphs | Recursive Merkle tree (changes bubble up) |
| Computation | Lazy (on first access), then cached |
| `graph.as_node()` | `GraphNode.definition_hash` = inner graph's hash |
| Mismatch behavior | Strict by default, `force_resume=True` escape hatch |
| Import depth | Configurable via `hash_depth` (default: 1) |

---

## 1. Graph.definition_hash

**Add to graph.md:**

```python
@property
def definition_hash(self) -> str:
    """
    Merkle-tree hash of the entire graph structure.

    Computed lazily on first access, then cached. Includes:
    - All node hashes (recursive for nested GraphNodes)
    - Graph structure (edges)
    - Node configuration that affects behavior

    Used by runners to detect schema changes on workflow resume.
    """
```

**Hashing algorithm:**

```python
def _compute_definition_hash(self) -> str:
    """Recursive Merkle-tree style hash."""
    node_hashes = []
    for node in sorted(self.nodes, key=lambda n: n.name):
        node_hashes.append(f"{node.name}:{node.definition_hash}")

    # Include structure (edges)
    edge_str = str(sorted((e.source, e.target) for e in self.edges))

    return sha256("|".join(node_hashes) + "|" + edge_str)
```

**What's included in each node's hash:**

| Node Type | Hash includes |
|-----------|---------------|
| `FunctionNode` | Function source + local imports (up to `hash_depth`) |
| `GraphNode` | Inner graph's `definition_hash` (recursive) |
| `InterruptNode` | `input_param`, `response_param`, `response_type` |
| `TypeRouteNode` | `input_param`, routes mapping |
| `BranchNode` | `condition_param`, `when_true`, `when_false` |
| `GateNode` | Node name only (structural) |
| `RouteNode` | Routing function hash |

---

## 2. GraphNode.definition_hash

**Update node-types.md** - Remove statement that GraphNode has no `definition_hash`. Add:

```python
@property
def definition_hash(self) -> str:
    """Hash of the nested graph (delegates to inner graph)."""
    return self.graph.definition_hash
```

This ensures changes inside nested graphs bubble up to the root hash.

---

## 3. Workflow.graph_hash and force_resume

**Add to checkpointer.md** - Extend the `Workflow` type:

```python
@dataclass
class Workflow:
    workflow_id: str
    graph_name: str
    graph_hash: str  # NEW: Stored at workflow creation
    created_at: datetime
    updated_at: datetime
    status: WorkflowStatus
```

**Add to runners-api-reference.md** - New parameter on `run()`:

```python
async def run(
    self,
    graph: Graph,
    values: dict[str, Any] | None = None,
    workflow_id: str | None = None,
    force_resume: bool = False,  # NEW
) -> RunResult:
```

**Behavior:**

1. **New workflow:** Store `graph.definition_hash` in `Workflow.graph_hash`
2. **Resume (hash matches):** Proceed normally
3. **Resume (hash mismatch, `force_resume=False`):** Raise `VersionMismatchError`
4. **Resume (hash mismatch, `force_resume=True`):** Log warning, proceed anyway

**Error message:**

```python
raise VersionMismatchError(
    f"Graph changed since workflow '{workflow_id}' started.\n"
    f"  Stored hash: {stored_hash[:12]}...\n"
    f"  Current hash: {current_hash[:12]}...\n"
    f"\n"
    f"Options:\n"
    f"  1. Start a new workflow with a different workflow_id\n"
    f"  2. Resume with force_resume=True (data integrity not guaranteed):\n"
    f"     runner.run(graph, workflow_id='{workflow_id}', force_resume=True)"
)
```

---

## 4. Local imports hashing with depth control

**Hash boundary rules:**

| Include in hash | Exclude from hash |
|-----------------|-------------------|
| Function source code | stdlib (`os`, `sys`, `json`) |
| Same-package imports (up to `hash_depth`) | pip packages (`pydantic`, `numpy`) |
| Graph structure (edges) | Runtime env vars |
| Node configuration | Dynamically loaded modules |

**Configuration on Graph:**

```python
graph = Graph(
    "my_graph",
    hash_depth=1,  # How deep to follow local imports (default: 1)
)
```

| `hash_depth` | Behavior |
|--------------|----------|
| `0` | Function source only (fastest, misses helper changes) |
| `1` | Function + direct local imports (default, good balance) |
| `2+` | Recursive up to N levels (thorough, slower) |
| `None` | Full transitive closure within package (most thorough) |

**Example:**

```python
# node.py imports helper.py imports utils.py

hash_depth=0  # Only node.py
hash_depth=1  # node.py + helper.py
hash_depth=2  # node.py + helper.py + utils.py
hash_depth=None  # Everything in same package
```

---

## Files to Update

1. **graph.md** - Add `Graph.definition_hash` property and `hash_depth` parameter
2. **node-types.md** - Add `GraphNode.definition_hash`, update note about missing properties
3. **checkpointer.md** - Add `graph_hash` to `Workflow` dataclass
4. **runners-api-reference.md** - Add `force_resume` parameter and `VersionMismatchError`
5. **execution-types.md** - Add `VersionMismatchError` exception type
