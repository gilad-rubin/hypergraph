# HyperNodes v0.5 - Specifications

**Complete technical specification for implementing HyperNodes v0.5: a graph-native execution system supporting cycles, multi-turn interactions, and complex control flow.**

---

## Quick Start

### For AI Context
- **Full context:** `@specs/` - loads all specifications
- **Specific area:** `@specs/api/decorators` - loads just that file
- **Navigation:** Start with [Overview](overview.md) → [Design Principles](design-principles.md)

### For Implementation
1. Read [Overview](overview.md) - understand the vision
2. Check [Design Principles](design-principles.md) - for making good decisions
3. Reference specific specs as needed during implementation

---

## Document Structure

### 📖 Foundation

**[Overview](overview.md)** - The journey from DAGs to reactive graphs
- Why HyperNodes v0.5 exists
- Key differentiators from LangGraph/Pydantic-Graph
- What this enables
- Implementation priorities

**[Design Principles](design-principles.md)** - Philosophy and decision framework
- Pure functions over state objects
- Fail fast, explicit over implicit
- Unified execution model
- Error messages as documentation
- Anti-patterns to avoid

---

### 🏗️ Architecture

**[Execution Model](architecture/execution-model.md)** - Reactive dataflow with versioning
- Values and versions
- Staleness detection
- Sole producer rule
- Ready set computation
- Execution loop
- Conflict detection
- Generator handling
- Cache interaction

**[Control Flow](architecture/control-flow.md)** - Gates and routing
- `@route` decorator for multi-way routing
- `@branch` decorator for binary decisions
- `END` sentinel for cycle termination
- Gate semantics and validation
- Control edges vs data edges

**[Edge Cancels Default](architecture/edge-cancels-default.md)** - Input resolution rules
- The three cases (edge, default, required)
- Why this eliminates entrypoints
- Cycle initialization through inputs
- Conflict prevention

**[Core Classes](architecture/core-classes.md)** - Class hierarchy and responsibilities
- `Graph` - pure structure definition
- `GraphState` - runtime value storage
- `SyncRunner` / `AsyncRunner` / `DaftRunner`
- Separation of concerns

---

### 🔧 API Reference

**[Core Types](api/types.md)** - Complete type reference ⭐ NEW
- `HyperNode` - base protocol for all nodes
- `Node`, `RouteNode`, `BranchNode`, `InterruptNode`
- `Graph`, `GraphState`, `GraphResult`, `RunResult`
- Event types
- Type hierarchy and common patterns

**[Decorators](api/decorators.md)** - Node and gate decorators
- `@node` - wrap functions as graph nodes
- `@route` - multi-way routing decisions
- `@branch` - binary routing (syntactic sugar)
- `InterruptNode` - human-in-the-loop pause points
- Common patterns (accumulators, cycles, etc.)

**[Graph](api/graph.md)** - Graph construction and introspection
- Constructor and validation
- Properties (nodes, cycles, gates, etc.)
- Methods (`.bind()`, `.as_node()`, `.visualize()`)
- Composition and nesting

**[Runners](api/runners.md)** - Execution runtime
- `SyncRunner` - synchronous execution
- `AsyncRunner` - async + streaming + interrupts
- `DaftRunner` - distributed (DAG-only)
- Runner compatibility matrix
- Event types
- Identity model (`session_id` / `run_id`)

**[Observability](api/observability.md)** - Events and integrations ⭐ NEW
- `EventProcessor` interface (single method)
- Span hierarchy for nested graphs
- Pull-based (`.iter()`) vs push-based (processors)
- OpenTelemetry integration
- Integration examples (Langfuse, Logfire, custom logging)

**[State](api/state.md)** - GraphState API
- Constructor and properties
- Value access (immutable)
- Execution history
- Methods (`.get()`, `.set()`, `.has()`)
- Staleness checking
- Checkpointing

**[Errors](api/errors.md)** - Error handling
- Error hierarchy
- Error message format
- Specific error types with examples
- Human-friendly guidance

---

### ✅ Test Specifications

#### Unit Tests
- **[Graph Construction](tests/unit/graph-construction.md)** - Validation and edge inference
- **[Route Validation](tests/unit/route-validation.md)** - Build-time target checking
- **[Staleness Detection](tests/unit/staleness-detection.md)** - Version tracking correctness

#### Integration Tests
- **[Multi-Turn RAG](tests/integration/multi-turn-rag.md)** - Conversational retrieval with cycles
- **[Agentic Loop](tests/integration/agentic-loop.md)** - LLM-driven tool selection
- **[Iterative Refinement](tests/integration/iterative-refinement.md)** - Quality-gated generation
- **[Human-in-the-Loop](tests/integration/human-in-the-loop.md)** - Interrupt and resume

---

## Reading Paths

### 🚀 I want to see working examples first
```
overview.md                      # Complete multi-turn RAG example
api/decorators.md                # @node, @route, @branch examples
api/node-configuration.md        # Node(), .rename(), .map_over() examples
```
*Start here! See real code first, understand concepts through examples.*

### 📚 I want to understand the API surface
```
overview.md (API at a Glance)    # All decorators and methods in one place
api/types.md                      # Core types: HyperNode, Graph, GraphResult, etc.
api/graph.md                      # Graph construction and properties
api/runners.md                    # SyncRunner, AsyncRunner, DaftRunner
api/observability.md              # EventProcessor, events, integrations
api/decorators.md                 # @node, @route, @branch, InterruptNode
api/node-configuration.md         # Node configuration and builder pattern
```
*For getting oriented: what can I do with HyperNodes?*

### 🎯 I'm building a specific feature

**Multi-turn conversational workflows:**
```
overview.md (Quick Example)
api/decorators.md (@route decorator)
architecture/control-flow.md (gates and cycles)
architecture/conflict-resolution.md (sole producer rule)
```

**Human-in-the-loop approval flows:**
```
api/decorators.md (InterruptNode section)
api/runners.md (AsyncRunner and .iter())
tests/integration/human-in-the-loop.md
```

**Composing and nesting graphs:**
```
api/graph.md (.as_node() method)
api/node-configuration.md (.rename() and .map_over())
api/graph.md (Nested Graph Results)
```

**Batch processing at scale:**
```
api/runners.md (DaftRunner section)
api/node-configuration.md (.map_over())
```

**Observability and integrations:**
```
api/observability.md (complete guide)
api/execution-types.md (event types)
api/runners.md (event_processors parameter)
```

### 🏗️ I'm implementing the system

**Core execution engine:**
```
architecture/execution-model.md
architecture/edge-cancels-default.md
architecture/conflict-resolution.md
api/graph.md
api/state.md
```

**Runner pattern and async:**
```
api/runners.md (complete)
architecture/control-flow.md
```

**Validation logic:**
```
architecture/core-classes.md (validation section)
architecture/conflict-resolution.md
api/errors.md
tests/unit/graph-construction.md
```

### 🤔 I want to understand the philosophy
```
overview.md (The Journey section)
design-principles.md (complete)
architecture/execution-model.md (concepts)
```
*Why HyperNodes exists, what makes it different.*

### ✅ I'm writing tests
```
tests/unit/                      # Unit test specifications
tests/integration/               # Integration test examples
architecture/conflict-resolution.md  # Edge cases to test
```

---

## What's New in This Spec Update

### New API Documentation
- **[Core Types](api/types.md)** ⭐ - Complete reference for HyperNode, Node, RouteNode, Graph, GraphResult, and all event types
- **[Node Configuration](api/node-configuration.md)** - `Node()` constructor, `.rename()`, `.map_over()`, builder pattern
- **[Conflict Resolution](architecture/conflict-resolution.md)** - Parallel producers, sole producer rule, input conflicts

### Expanded Content
- **[Graph API](api/graph.md)** - Detailed `.bind()` / `.unbind()` documentation, nested graph results with `select` patterns
- **[Runners](api/runners.md)** - Async execution model, concurrency control, nested runner resolution
- **[Decorators](api/decorators.md)** - Complete InterruptNode guide with prompt/response types
- **[Overview](overview.md)** - Working multi-turn RAG example, API surface at a glance

### Navigation Improvements
- **Example-first approach** - Every major section starts with working code
- **Feature-focused paths** - "I'm building X" → "Read these docs"
- **Layered detail** - Quick example → API overview → Deep dive

---

## Key Concepts Quick Reference

| Concept | Where to Read | Why It Matters |
|---------|---------------|----------------|
| **Pure Functions** | [Overview](overview.md), [Design Principles](design-principles.md) | Core differentiator from LangGraph |
| **Reactive Dataflow** | [Execution Model](architecture/execution-model.md) | Core execution paradigm |
| **Edge Cancels Default** | [Edge Cancels Default](architecture/edge-cancels-default.md) | Eliminates entrypoint concept |
| **Sole Producer Rule** | [Conflict Resolution](architecture/conflict-resolution.md#sequential-accumulation-sole-producer-rule) | Prevents infinite loops in accumulators |
| **Staleness Detection** | [Execution Model](architecture/execution-model.md#staleness-detection) | When to re-execute nodes |
| **SyncRunner vs AsyncRunner** | [Runners](api/runners.md), [Core Classes](architecture/core-classes.md) | Pure graph vs execution |
| **Gates** | [Decorators](api/decorators.md), [Control Flow](architecture/control-flow.md) | Routing and cycles |
| **Build-Time Validation** | [Graph](api/graph.md), [Conflict Resolution](architecture/conflict-resolution.md) | Fail fast philosophy |
| **Node Configuration** | [Node Configuration](api/node-configuration.md) | `.rename()`, `.map_over()`, builder pattern |
| **Generator Handling** | [Runners](api/runners.md#async-execution-model) | LLM streaming support |
| **InterruptNode** | [Decorators](api/decorators.md#interruptnode) | Human-in-the-loop |
| **Runner Compatibility** | [Runners](api/runners.md#runner-compatibility-matrix) | What works where |
| **.bind() and Inputs** | [Graph](api/graph.md#bind) | Default values, input resolution |
| **Nested Graphs** | [Graph](api/graph.md#nested-graph-results) | Composition and result filtering |
| **Parallel Producers** | [Conflict Resolution](architecture/conflict-resolution.md#parallel-producer-conflict) | When conflicts occur |
| **Core Types** | [Types](api/types.md) | All classes and data structures |

---

## Specification Completeness

### ✅ Complete
- Overview and vision
- Design principles
- Execution model (reactive dataflow, staleness, conflicts)
- Control flow (@route, @branch, END)
- Edge cancels default rule
- Decorators API
- Runners API (SyncRunner, AsyncRunner, DaftRunner)
- Observability (EventProcessor, span hierarchy, integrations)
- State API
- Error handling

### 🔄 In Progress
- Test specifications (structure exists, examples partial)

### 📋 Not Yet Specified
- Checkpoint serialization format
- Visualization updates for cycles
- Type congruence validation (opt-in feature)

---

## Usage Guidelines

### When Designing
1. Check [Design Principles](design-principles.md) first
2. Ensure it follows "pure functions over state objects"
3. Make sure errors fail fast and are human-friendly

### When Implementing
1. Reference relevant spec file(s)
2. Follow the exact signatures and semantics
3. Write tests matching the test specifications

### When Stuck
1. Re-read [Overview](overview.md) - does this align with the vision?
2. Check [Design Principles](design-principles.md) - use the decision framework
3. Look for similar patterns in existing specs

---

## Version

**Specification Version:** v0.5.0-draft  
**Last Updated:** 2025-12-24  
**Status:** Active development

---

## Notes

- All specs are **implementation-focused** - they define what to build, not how to market it
- Examples in specs are **normative** - they define expected behavior
- Error messages are **part of the API** - they must match the spec
- Type hints are **optional for users** - validation happens at build time regardless


