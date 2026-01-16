# Roadmap: Hypergraph Type Validation

## Overview

Add type annotation validation to hypergraph to catch type mismatches at graph construction time. Starting with type extraction from nodes, building a compatibility engine, adding strict mode enforcement with clear error messages, and finally handling `map_over` type transformations.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3, 4): Planned milestone work
- Decimal phases (e.g., 2.1): Urgent insertions (marked with INSERTED)

- [x] **Phase 1: Type Extraction Infrastructure** - Nodes expose type information
- [x] **Phase 2: Type Compatibility Engine** - Determine if types are compatible
- [ ] **Phase 3: Enforcement & Errors** - Strict mode catches problems with helpful messages
- [ ] **Phase 4: Map Over Transformation** - `map_over` transforms types to `list[T]`

## Phase Details

### Phase 1: Type Extraction Infrastructure
**Goal**: Nodes expose their type information for validation
**Depends on**: Nothing (first phase)
**Requirements**: TYPE-01, TYPE-02, TYPE-03
**Success Criteria** (what must be TRUE):
  1. Graph can be constructed with `strict_types=True` parameter
  2. FunctionNode exposes parameter types and return type
  3. GraphNode exposes its output node's return type
**Research**: Unlikely (Python's `get_type_hints()` - existing pattern in codebase)
**Plans**: TBD

Plans:
- [x] 01-01: Type extraction infrastructure (FunctionNode + GraphNode + Graph parameter)

### Phase 2: Type Compatibility Engine
**Goal**: System can determine if two types are compatible
**Depends on**: Phase 1
**Requirements**: TYPE-04
**Success Criteria** (what must be TRUE):
  1. Simple types (int, str) compatibility works
  2. Union types (int | str) are correctly handled
  3. Generic types (list[int]) are correctly handled
  4. Forward references resolve and compare
**Research**: Likely (complex type compatibility logic)
**Research topics**: pipefunc's type_validation.py patterns, typing module internals for Union/generics handling
**Plans**: TBD

Plans:
- [x] 02-01: Type compatibility engine (is_type_compatible with Union, generics, forward refs)

### Phase 3: Enforcement & Errors
**Goal**: Strict mode catches type problems with helpful messages
**Depends on**: Phase 2
**Requirements**: TYPE-05, TYPE-06
**Success Criteria** (what must be TRUE):
  1. Graph with `strict_types=True` raises error for missing annotations
  2. Graph with `strict_types=True` raises error for type mismatches
  3. Error messages identify which nodes/parameters conflict
  4. Error messages suggest how to fix the issue
**Research**: Unlikely (follows existing GraphConfigError patterns)
**Plans**: TBD

Plans:
- [ ] 03-01: TBD

### Phase 4: Map Over Transformation
**Goal**: `map_over` correctly transforms types for validation
**Depends on**: Phase 3
**Requirements**: TYPE-07
**Success Criteria** (what must be TRUE):
  1. FunctionNode with `map_over` parameter transforms type to `list[T]`
  2. GraphNode with `map_over` parameter transforms type to `list[T]`
  3. Type compatibility checking works with transformed types
**Research**: Unlikely (extends Phase 2 logic with `list[T]` wrapping)
**Plans**: TBD

Plans:
- [ ] 04-01: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Type Extraction Infrastructure | 1/1 | Complete | 2026-01-16 |
| 2. Type Compatibility Engine | 1/1 | Complete | 2026-01-16 |
| 3. Enforcement & Errors | 0/TBD | Not started | - |
| 4. Map Over Transformation | 0/TBD | Not started | - |
