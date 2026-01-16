# Hypergraph - Type Validation Feature

## What This Is

A computation graph framework for Python that separates structure definition from execution. Users define nodes (wrapped functions) and compose them into graphs with automatic edge inference from parameter names. **Now with type annotation validation** to catch type mismatches at graph construction time.

## Core Value

Catch type errors early - before execution, at graph construction time. If `strict_types=True`, incompatible connections fail immediately with clear error messages.

## Current Milestone: v1.1 Documentation Polish

**Goal:** Update all documentation to reflect current implementation using the clear, example-driven README.md style.

**Target features:**
- Documentation audit of all docs/ files
- Complete API reference for implemented features
- Consistent writing style across all docs

## Current State

**v1.0 Shipped:** 2026-01-16

Type validation is complete for existing features:
- `strict_types=True` validates all edge connections at construction time
- FunctionNode and GraphNode expose type annotations
- Supports Union, generics, forward references
- Clear error messages with "How to fix" guidance

**Codebase:** 3,142 lines Python, 263 tests passing

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

### Active

- [ ] Documentation audit — review and update all docs/ directory files
- [ ] API reference — complete reference for Graph, FunctionNode, GraphNode, InputSpec, type checking
- [ ] Style consistency — apply README.md writing style across all documentation

### Out of Scope

- Warning mode (`strict_types='warn'`) — keep it simple: error or nothing
- Runtime type checking — this is static/construction-time only
- Custom type validators — use Python's typing system as-is
- `map_over` type transformation — deferred until feature exists

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

---
*Last updated: 2026-01-16 after starting v1.1 milestone*
