# Roadmap: Hypergraph

## Milestones

- **v1.0 MVP** — Phases 1-4 (shipped 2026-01-16)
- **v1.1 Documentation** — Phases 5-6 (shipped 2026-01-16)
- **v1.2 Test Coverage** — Phases 7-12 (in progress)

## Phases

<details>
<summary>v1.0 MVP (Phases 1-4) - SHIPPED 2026-01-16</summary>

Type validation system shipped with:
- `strict_types` parameter on Graph constructor
- Type annotation extraction from FunctionNode
- GraphNode output type exposure
- Type compatibility checking (Union, generics, forward refs)
- Clear error messages with "How to fix" guidance

</details>

<details>
<summary>v1.1 Documentation (Phases 5-6) — SHIPPED 2026-01-16</summary>

- [x] Phase 5: Getting Started Audit (1/1 plans) — completed 2026-01-16
- [x] Phase 6: API Reference Documentation (1/1 plans) — completed 2026-01-16

</details>

### v1.2 Test Coverage (In Progress)

**Milestone Goal:** Close all test coverage gaps for comprehensive quality assurance

- [x] **Phase 7: GraphNode Capabilities** - Test forwarding methods ✓
- [x] **Phase 8: Graph Topologies** - Test diamond, cycles, isolated components ✓
- [x] **Phase 9: Function Signatures** - Test *args, **kwargs, keyword-only, positional-only ✓
- [x] **Phase 10: Type Compatibility** - Test Literal, Protocol, TypedDict, NamedTuple ✓
- [x] **Phase 11: Binding Edge Cases** - Test None values, seed interaction ✓
- [x] **Phase 12: Name Validation** - Test underscore, keywords, unicode ✓

## Phase Details

### Phase 7: GraphNode Capabilities
**Goal**: Test GraphNode forwarding methods work correctly
**Depends on**: Nothing (independent test phase)
**Requirements**: GNODE-01, GNODE-02, GNODE-03, GNODE-04, GNODE-05
**Success Criteria** (what must be TRUE):
  1. GraphNode.has_default_for() returns correct value from inner graph
  2. GraphNode.get_default_for() retrieves default from inner graph
  3. GraphNode.get_input_type() returns correct type from inner graph node
  4. GraphNode.get_output_type() returns correct type from inner graph node
  5. GraphNode with bound values from inner graph handled correctly
**Research**: Unlikely (testing existing code, patterns established)
**Plans**: 1 plan

Plans:
- [x] 07-01: GraphNode forwarding methods tests

### Phase 8: Graph Topologies
**Goal**: Test complex graph topologies work correctly
**Depends on**: Nothing (independent test phase)
**Requirements**: TOPO-01, TOPO-02, TOPO-03, TOPO-04, TOPO-05
**Success Criteria** (what must be TRUE):
  1. Diamond dependency pattern (A->B, A->C, B->D, C->D) executes correctly
  2. Multi-node cycles (A->B->C->A) detected and seeds computed correctly
  3. Multiple independent cycles in one graph work correctly
  4. Isolated subgraphs (disconnected components) work correctly
  5. Deeply nested graphs (3+ levels) work correctly
**Research**: Unlikely (testing existing code, patterns established)
**Plans**: 1 plan

Plans:
- [x] 08-01: Graph topology tests

### Phase 9: Function Signatures
**Goal**: Test FunctionNode handles all Python parameter types
**Depends on**: Nothing (independent test phase)
**Requirements**: FUNC-01, FUNC-02, FUNC-03, FUNC-04, FUNC-05
**Success Criteria** (what must be TRUE):
  1. FunctionNode handles *args parameter correctly
  2. FunctionNode handles **kwargs parameter correctly
  3. FunctionNode handles keyword-only parameters (*, name) correctly
  4. FunctionNode handles positional-only parameters (param, /) correctly
  5. FunctionNode handles mixed argument types correctly
**Research**: Unlikely (testing existing code, patterns established)
**Plans**: 1 plan

Plans:
- [x] 09-01: Function signature tests

### Phase 10: Type Compatibility
**Goal**: Test type validation handles advanced Python types
**Depends on**: Nothing (independent test phase)
**Requirements**: TYPE-01, TYPE-02, TYPE-03, TYPE-04, TYPE-05, TYPE-06, TYPE-07
**Success Criteria** (what must be TRUE):
  1. Literal types validated correctly (Literal["a", "b"])
  2. Protocol types validated correctly (structural typing)
  3. TypedDict types validated correctly
  4. NamedTuple types validated correctly
  5. ParamSpec types handled correctly
  6. Self type (Python 3.11+) handled correctly
  7. Recursive types handled without infinite loop
**Research**: Unlikely (testing existing code, patterns established)
**Plans**: 1 plan

Plans:
- [x] 10-01: Advanced type compatibility tests

### Phase 11: Binding Edge Cases
**Goal**: Test bind/unbind edge cases work correctly
**Depends on**: Nothing (independent test phase)
**Requirements**: BIND-01, BIND-02, BIND-03, BIND-04
**Success Criteria** (what must be TRUE):
  1. bind(x=None) correctly binds None as a value (not unbind)
  2. bind() with multiple values at once works correctly
  3. bind() interaction with cycle seeds handled correctly
  4. unbind() restores correct required vs optional status
**Research**: Unlikely (testing existing code, patterns established)
**Plans**: 1 plan

Plans:
- [x] 11-01: Binding edge case tests

### Phase 12: Name Validation
**Goal**: Test name validation handles edge cases correctly
**Depends on**: Nothing (independent test phase)
**Requirements**: NAME-01, NAME-02, NAME-03, NAME-04, NAME-05
**Success Criteria** (what must be TRUE):
  1. Names starting with underscore (_private) handled correctly
  2. Names that are Python keywords rejected with clear error
  3. Empty string names rejected with clear error
  4. Unicode characters in names handled correctly
  5. Very long names (1000+ chars) handled correctly
**Research**: Unlikely (testing existing code, patterns established)
**Plans**: 1 plan

Plans:
- [x] 12-01: Name validation edge case tests

## Progress

**Execution Order:**
Phases execute in numeric order: 7 -> 8 -> 9 -> 10 -> 11 -> 12

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 7. GraphNode Capabilities | v1.2 | 1/1 | Complete | 2026-01-16 |
| 8. Graph Topologies | v1.2 | 1/1 | Complete | 2026-01-16 |
| 9. Function Signatures | v1.2 | 1/1 | Complete | 2026-01-16 |
| 10. Type Compatibility | v1.2 | 1/1 | Complete | 2026-01-16 |
| 11. Binding Edge Cases | v1.2 | 1/1 | Complete | 2026-01-16 |
| 12. Name Validation | v1.2 | 1/1 | Complete | 2026-01-16 |
