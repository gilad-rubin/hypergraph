# Hypergraph - Type Validation Feature

## What This Is

A computation graph framework for Python that separates structure definition from execution. Users define nodes (wrapped functions) and compose them into graphs with automatic edge inference from parameter names. Currently adding type annotation validation to catch type mismatches at graph construction time.

## Core Value

Catch type errors early - before execution, at graph construction time. If `strict_types=True`, incompatible connections fail immediately with clear error messages.

## Requirements

### Validated

- ✓ Graph construction with automatic edge inference — existing
- ✓ FunctionNode with @node decorator — existing
- ✓ GraphNode for nested composition — existing
- ✓ Build-time validation (duplicates, identifiers, defaults) — existing
- ✓ InputSpec computation (required/optional/seeds) — existing
- ✓ Immutable bind/unbind operations — existing
- ✓ Definition hashing (Merkle-tree) — existing

### Active

- [ ] `strict_types` parameter on Graph constructor
- [ ] Type annotation extraction from FunctionNode (parameter + return types)
- [ ] Type compatibility checking (Union, generics, forward refs - like pipefunc)
- [ ] Error when `strict_types=True` and connected nodes lack type annotations
- [ ] Clear error messages showing which types conflict and how to fix

### Out of Scope

- Warning mode (`strict_types='warn'`) — keep it simple: error or nothing
- Runtime type checking — this is static/construction-time only
- GraphNode type validation — complex, defer to future if needed
- Custom type validators — use Python's typing system as-is

## Context

**Reference implementation:** pipefunc's type validation system
- Files saved to `tmp/pipefunc_typing.py` and `tmp/pipefunc_validation.py`
- Implementation plan in `tmp/type_validation_plan.md`

**Existing codebase patterns:**
- Fail-fast validation in `Graph.__init__()` via `_validate()` method
- GraphConfigError for structural issues with helpful "How to fix" messages
- FunctionNode already uses `get_type_hints()` for return annotation warnings

## Constraints

- **Compatibility**: Must work with Python 3.10+ (match existing requirement)
- **Dependencies**: Prefer standard library only (no new dependencies)
- **API**: `strict_types=False` default to avoid breaking existing code

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Error on mismatch, not warn | Keep it simple - one behavior | — Pending |
| Require annotations when strict | Forces explicit types, catches more errors | — Pending |
| Full pipefunc-style type handling | Union, generics, forward refs - comprehensive | — Pending |

---
*Last updated: 2026-01-16 after initialization*
