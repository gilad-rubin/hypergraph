# hypergraph Specification

Design specifications for hypergraph - a graph-based workflow framework.

---

## Document Structure

### Core Concepts (Foundation)

| File | Description |
|------|-------------|
| [graph.md](graph.md) | Graph API - structure definition, `InputSpec`, `bind()`, `as_node()`, nested graph results |
| [node-types.md](node-types.md) | All node types: `HyperNode`, `FunctionNode`, `GateNode`, `RouteNode`, `BranchNode`, `TypeRouteNode`, `InterruptNode`, `GraphNode` |
| [state-model.md](state-model.md) | "Outputs ARE state" philosophy, value resolution hierarchy, comparison with LangGraph |

### Execution Layer

| File | Description |
|------|-------------|
| [runners.md](runners.md) | Conceptual guide - `SyncRunner`, `AsyncRunner`, `DaftRunner`, choosing the right runner |
| [runners-api-reference.md](runners-api-reference.md) | Complete API reference - method signatures, `RunnerCapabilities`, cross-runner execution |
| [execution-types.md](execution-types.md) | Runtime types - `RunResult`, `RunStatus`, `PauseReason`, `GraphState`, events, persistence types |

### Persistence & Durability

| File | Description |
|------|-------------|
| [persistence.md](persistence.md) | User tutorial - workflow persistence, multi-turn conversations, human-in-the-loop patterns |
| [checkpointer.md](checkpointer.md) | `Checkpointer` interface definition, `Step`, `StepResult`, `Workflow` types, built-in implementations |
| [durable-execution.md](durable-execution.md) | Advanced durability - selective persistence, DBOS integration, parallel execution, retry configuration |

### Observability

| File | Description |
|------|-------------|
| [observability.md](observability.md) | Event system, `EventProcessor` interface, span hierarchy, OpenTelemetry integration |

---

## Reading Order

**For understanding the framework:**
1. `state-model.md` - Core philosophy
2. `graph.md` - Graph structure
3. `node-types.md` - Building blocks
4. `runners.md` - How graphs execute

**For implementing features:**
1. `execution-types.md` - Type definitions
2. `runners-api-reference.md` - Full API
3. `checkpointer.md` - Persistence interface
4. `observability.md` - Event system

**For production use:**
1. `persistence.md` - Practical patterns
2. `durable-execution.md` - DBOS and advanced features

---

## Architecture Overview

```
+------------------+     +------------------+     +------------------+
|   Graph Layer    |     |  Execution Layer |     | Persistence Layer|
|                  |     |                  |     |                  |
|  - Graph         | --> |  - Runners       | --> |  - Checkpointer  |
|  - Nodes         |     |  - Events        |     |  - Steps         |
|  - Edges         |     |  - RunResult     |     |  - Workflows     |
+------------------+     +------------------+     +------------------+
        |                        |                        |
        v                        v                        v
   Structure              State + Events             Durability
   (static)               (runtime)                  (persistent)
```

---

## Key Design Principles

1. **Outputs ARE state** - No separate state schema; node outputs are the state
2. **Graph code stays pure** - No durability imports in nodes
3. **Runners execute graphs** - Separation of structure from execution
4. **Events are data** - Unified event stream for observability
5. **Steps are the source of truth** - State is computed from steps, not stored separately
6. **Explicit over implicit** - No magic defaults
