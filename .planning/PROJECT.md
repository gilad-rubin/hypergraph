# Hypergraph - Type Validation Feature

## What This Is

A computation graph framework for Python that separates structure definition from execution. Users define nodes (wrapped functions) and compose them into graphs with automatic edge inference from parameter names. **Now with type annotation validation** to catch type mismatches at graph construction time.

## Core Value

Catch type errors early - before execution, at graph construction time. If `strict_types=True`, incompatible connections fail immediately with clear error messages.

## Current Milestone: v1.3 Execution Runtime

**Goal:** Execute graphs with SyncRunner and AsyncRunner supporting `.run()` and `.map()` methods.

## Current State

**v1.0 Shipped:** 2026-01-16 — Type validation system
**v1.1 Shipped:** 2026-01-16 — Documentation polish
**v1.2 Shipped:** 2026-01-16 — Comprehensive test coverage
**v1.3 In Progress:** Execution runtime with runners

Type validation is complete for existing features:
- `strict_types=True` validates all edge connections at construction time
- FunctionNode and GraphNode expose type annotations
- Supports Union, generics, forward references
- Clear error messages with "How to fix" guidance

v1.3 adds execution runtime:
- SyncRunner and AsyncRunner with `.run()` and `.map()`
- Parallel execution via asyncio.gather (supersteps)
- Nested graph execution
- 8 phases (13-20), 40 requirements

**Codebase:** 2,151 lines Python (src), 382 tests passing

## Requirements

### Validated

- ✓ Graph construction with automatic edge inference — existing
- ✓ FunctionNode with @node decorator — existing
- ✓ GraphNode for nested composition — existing
- ✓ Build-time validation (duplicates, identifiers, defaults) — existing
- ✓ InputSpec computation (required/optional/seeds) — existing
- ✓ Immutable bind/unbind operations — existing
- ✓ Definition hashing (Merkle-tree) — existing
- ✓ `strict_types` parameter on Graph constructor — v1.0
- ✓ Type annotation extraction from FunctionNode — v1.0
- ✓ GraphNode exposes output type from inner graph — v1.0
- ✓ Type compatibility checking (Union, generics, forward refs) — v1.0
- ✓ Error when `strict_types=True` and nodes lack annotations — v1.0
- ✓ Clear error messages showing types and how to fix — v1.0
- ✓ Documentation examples execute without errors — v1.1
- ✓ Getting-started.md covers type validation with strict_types — v1.1
- ✓ Graph class API reference with constructor, methods, strict_types — v1.1
- ✓ GraphNode API reference with nested composition — v1.1
- ✓ InputSpec API reference with required/optional/bound/seeds — v1.1
- ✓ GraphNode forwarding methods tested (has_default_for, get_default_for, get_input/output_type) — v1.2
- ✓ Graph topologies tested (diamond, cycles, isolated, nested) — v1.2
- ✓ Function signatures tested (*args, **kwargs, keyword-only, positional-only) — v1.2
- ✓ Advanced type compatibility tested (Literal, Protocol, TypedDict, NamedTuple) — v1.2
- ✓ Binding edge cases tested (None, multiple, seeds) — v1.2
- ✓ Name validation tested (keywords, unicode, empty, long) — v1.2

### Active (v1.3)

- ⏳ SyncRunner with `.run()` returning RunResult — Phase 17
- ⏳ AsyncRunner with `.run()` returning RunResult — Phase 17
- ⏳ Parallel execution via asyncio.gather (supersteps) — Phase 17
- ⏳ `.map()` method for batch processing — Phase 18
- ⏳ Nested graph execution through runners — Phase 19
- ⏳ FunctionNode with @node decorator — Phase 14
- ⏳ GraphNode with .as_node() and map_over() — Phase 15

### Out of Scope

- Warning mode (`strict_types='warn'`) — keep it simple: error or nothing
- Runtime type checking — this is static/construction-time only
- Custom type validators — use Python's typing system as-is
- Events/EventProcessor — deferred to v1.4
- Caching (DiskCache, MemoryCache) — deferred to v1.4
- Checkpointing/persistence — deferred to v1.4
- Control flow nodes (GateNode, RouteNode, BranchNode) — deferred
- InterruptNode (human-in-the-loop) — requires checkpointing
- DaftRunner (distributed) — specialized runner
- DBOSAsyncRunner (durable) — DBOS integration
- .iter() streaming — requires event infrastructure

## Context

**Reference implementation:** pipefunc's type validation system (used for design)

**Type validation files:**
- `src/hypergraph/_typing.py` — Type compatibility engine
- `src/hypergraph/graph.py` — `_validate_types()` method
- `src/hypergraph/nodes/function.py` — `parameter_annotations`, `output_annotation` properties
- `src/hypergraph/nodes/graph_node.py` — `output_annotation` property

## Constraints

- **Compatibility**: Python 3.10+ (match existing requirement)
- **Dependencies**: Standard library only (no new dependencies added)
- **API**: `strict_types=False` default to avoid breaking existing code

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Error on mismatch, not warn | Keep it simple - one behavior | ✓ Good |
| Require annotations when strict | Forces explicit types, catches more errors | ✓ Good |
| Full pipefunc-style type handling | Union, generics, forward refs - comprehensive | ✓ Good |
| Use get_type_hints() | Resolves forward references automatically | ✓ Good |
| Graceful degradation | Empty dict on failure, not exceptions | ✓ Good |
| Union directionality | Incoming ALL must satisfy required type | ✓ Good |
| Tests document expected behavior | Even when implementation is incomplete | ✓ Good |
| Bound values excluded from GraphNode.inputs | Cleaner interface | ✓ Good |
| Both runners return RunResult | Consistent API, SyncRunner doesn't need dict | v1.3 |
| Parallel via asyncio.gather | Supersteps batch independent nodes | v1.3 |
| Defer events to v1.4 | Focus on core execution first | v1.3 |
| Defer caching to v1.4 | Adds complexity, not essential for MVP | v1.3 |

---
*Last updated: 2026-01-16 after v1.3 roadmap creation*
