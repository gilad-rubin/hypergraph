# Requirements: Hypergraph

**Defined:** 2026-01-16
**Core Value:** Catch type errors early - before execution, at graph construction time

## v1.1 Requirements

Requirements for documentation milestone. Each maps to roadmap phases.

### Documentation Audit

- [x] **AUDIT-01**: All code examples in getting-started.md execute without errors against current API
- [x] **AUDIT-02**: getting-started.md follows progressive complexity (simple → advanced)
- [x] **AUDIT-03**: getting-started.md covers type validation with strict_types

### API Reference

- [x] **API-01**: Graph class reference documents constructor parameters, methods, and strict_types behavior
- [x] **API-02**: FunctionNode reference documents @node decorator, properties, and rename API
- [x] **API-03**: GraphNode reference documents nested composition and .as_node() usage
- [x] **API-04**: InputSpec reference documents required/optional/bound/seeds categorization

### Style

- [x] **STYLE-01**: Guides use step-by-step, human-centered language
- [x] **STYLE-02**: API reference uses technical, comprehensive format
- [x] **STYLE-03**: All documentation uses consistent example patterns (show code, then explain)

## v1.2 Requirements

Requirements for comprehensive test coverage milestone.

### GraphNode

- [x] **GNODE-01**: GraphNode.has_default_for() correctly forwards to inner graph
- [x] **GNODE-02**: GraphNode.get_default_for() retrieves default from inner graph
- [x] **GNODE-03**: GraphNode.get_input_type() returns type from inner graph node
- [x] **GNODE-04**: GraphNode.get_output_type() returns type from inner graph node
- [x] **GNODE-05**: GraphNode with bound values from inner graph handled correctly

### Graph Topologies

- [ ] **TOPO-01**: Diamond dependency pattern (A→B, A→C, B→D, C→D) works correctly
- [ ] **TOPO-02**: Multi-node cycle (A→B→C→A) detected and seeds computed correctly
- [ ] **TOPO-03**: Multiple independent cycles in one graph handled correctly
- [ ] **TOPO-04**: Isolated subgraphs (disconnected components) work correctly
- [ ] **TOPO-05**: Deeply nested graphs (3+ levels) work correctly

### Function Signatures

- [ ] **FUNC-01**: FunctionNode handles *args parameter correctly
- [ ] **FUNC-02**: FunctionNode handles **kwargs parameter correctly
- [ ] **FUNC-03**: FunctionNode handles keyword-only parameters (*, name) correctly
- [ ] **FUNC-04**: FunctionNode handles positional-only parameters (param, /) correctly
- [ ] **FUNC-05**: FunctionNode handles mixed argument types correctly

### Type Compatibility

- [ ] **TYPE-01**: Literal types validated correctly (Literal["a", "b"])
- [ ] **TYPE-02**: Protocol types validated correctly (structural typing)
- [ ] **TYPE-03**: TypedDict types validated correctly
- [ ] **TYPE-04**: NamedTuple types validated correctly
- [ ] **TYPE-05**: ParamSpec types handled correctly
- [ ] **TYPE-06**: Self type (Python 3.11+) handled correctly
- [ ] **TYPE-07**: Recursive types handled without infinite loop

### Binding Edge Cases

- [ ] **BIND-01**: bind(x=None) correctly binds None as a value
- [ ] **BIND-02**: bind() with multiple values at once works correctly
- [ ] **BIND-03**: bind() interaction with cycle seeds handled correctly
- [ ] **BIND-04**: unbind() restores correct required vs optional status

### Name Validation Edge Cases

- [ ] **NAME-01**: Names starting with underscore (_private) handled correctly
- [ ] **NAME-02**: Names that are Python keywords rejected with clear error
- [ ] **NAME-03**: Empty string names rejected with clear error
- [ ] **NAME-04**: Unicode characters in names handled correctly
- [ ] **NAME-05**: Very long names (1000+ chars) handled correctly

## v2 Requirements

Deferred to future release. Tracked but not in current roadmap.

### Future Audit

- **AUDIT-V2-01**: Remove "Coming soon" sections after features ship
- **AUDIT-V2-02**: Update comparison.md with competitive positioning

### Stress Testing

- **STRESS-01**: Large graphs (100+ nodes) performance test
- **STRESS-02**: Memory usage with deeply nested graphs

### Error Quality

- **ERROR-01**: All error messages include "How to fix" hints
- **ERROR-02**: Error messages readable with long names

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Runner documentation | Feature not implemented |
| RouteNode/BranchNode docs | Feature not implemented |
| Checkpointing docs | Feature not implemented |
| Control flow (@route, @branch) docs | Feature not implemented |
| Runtime type checking | Framework is construction-time only |
| Custom type validators | Use Python's typing system as-is |
| Thread safety tests | Immutable design handles this |

## Traceability

Which phases cover which requirements. Updated by create-roadmap.

| Requirement | Phase | Status |
|-------------|-------|--------|
| AUDIT-01 | Phase 5 | Complete |
| AUDIT-02 | Phase 5 | Complete |
| AUDIT-03 | Phase 5 | Complete |
| STYLE-01 | Phase 5 | Complete |
| API-01 | Phase 6 | Complete |
| API-02 | Phase 6 | Complete |
| API-03 | Phase 6 | Complete |
| API-04 | Phase 6 | Complete |
| STYLE-02 | Phase 6 | Complete |
| STYLE-03 | Phase 6 | Complete |
| GNODE-01 | Phase 7 | Complete |
| GNODE-02 | Phase 7 | Complete |
| GNODE-03 | Phase 7 | Complete |
| GNODE-04 | Phase 7 | Complete |
| GNODE-05 | Phase 7 | Complete |
| TOPO-01 | Phase 8 | Pending |
| TOPO-02 | Phase 8 | Pending |
| TOPO-03 | Phase 8 | Pending |
| TOPO-04 | Phase 8 | Pending |
| TOPO-05 | Phase 8 | Pending |
| FUNC-01 | Phase 9 | Pending |
| FUNC-02 | Phase 9 | Pending |
| FUNC-03 | Phase 9 | Pending |
| FUNC-04 | Phase 9 | Pending |
| FUNC-05 | Phase 9 | Pending |
| TYPE-01 | Phase 10 | Pending |
| TYPE-02 | Phase 10 | Pending |
| TYPE-03 | Phase 10 | Pending |
| TYPE-04 | Phase 10 | Pending |
| TYPE-05 | Phase 10 | Pending |
| TYPE-06 | Phase 10 | Pending |
| TYPE-07 | Phase 10 | Pending |
| BIND-01 | Phase 11 | Pending |
| BIND-02 | Phase 11 | Pending |
| BIND-03 | Phase 11 | Pending |
| BIND-04 | Phase 11 | Pending |
| NAME-01 | Phase 12 | Pending |
| NAME-02 | Phase 12 | Pending |
| NAME-03 | Phase 12 | Pending |
| NAME-04 | Phase 12 | Pending |
| NAME-05 | Phase 12 | Pending |

**Coverage:**
- v1.1 requirements: 10 total (mapped to phases 5-6)
- v1.2 requirements: 31 total (mapped to phases 7-12)
- Unmapped: 0 ✓

---
*Requirements defined: 2026-01-16*
*Last updated: 2026-01-16 after mapping v1.2 requirements to phases*
