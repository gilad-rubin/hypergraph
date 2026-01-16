# Requirements: Hypergraph Type Validation

**Defined:** 2026-01-16
**Core Value:** Catch type errors early - before execution, at graph construction time

## v1 Requirements

### Type Validation

- [x] **TYPE-01**: Graph constructor accepts `strict_types` parameter (default False)
- [x] **TYPE-02**: FunctionNode extracts type annotations from parameter and return types
- [x] **TYPE-03**: GraphNode exposes output type from its output node (no recursive internal validation)
- [ ] **TYPE-04**: Type compatibility checking supports Union, generics, and forward refs
- [ ] **TYPE-05**: Graph raises error when `strict_types=True` and connected nodes lack type annotations
- [ ] **TYPE-06**: Error messages show which types conflict and how to fix
- [ ] **TYPE-07**: `map_over` transforms types to `list[T]` (both FunctionNode and GraphNode)

## v2 Requirements

(None currently deferred)

## Out of Scope

| Feature | Reason |
|---------|--------|
| Runtime type checking | Static/construction-time only |
| Custom type validators | Use Python's typing system |
| Warning mode (`strict_types='warn'`) | Error or nothing for v1 |
| Recursive nested graph validation | Nested graphs validate themselves |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| TYPE-01 | Phase 1 | Complete |
| TYPE-02 | Phase 1 | Complete |
| TYPE-03 | Phase 1 | Complete |
| TYPE-04 | Phase 2 | Pending |
| TYPE-05 | Phase 3 | Pending |
| TYPE-06 | Phase 3 | Pending |
| TYPE-07 | Phase 4 | Pending |

**Coverage:**
- v1 requirements: 7 total
- Mapped to phases: 7
- Unmapped: 0 ✓

---
*Requirements defined: 2026-01-16*
*Last updated: 2026-01-16 after Phase 1 completion*
