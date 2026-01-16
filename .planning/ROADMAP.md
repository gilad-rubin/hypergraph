# Roadmap: Hypergraph

## Milestones

- ✅ **v1.0 MVP** - Phases 1-4 (shipped 2026-01-16)
- 📋 **v1.1 Documentation** - Phases 5-6 (planned)
- 🚧 **v1.2 Test Coverage** - Phases 7-12 (in progress)

## Phases

<details>
<summary>✅ v1.0 MVP (Phases 1-4) - SHIPPED 2026-01-16</summary>

Type validation system shipped with:
- `strict_types` parameter on Graph constructor
- Type annotation extraction from FunctionNode
- GraphNode output type exposure
- Type compatibility checking (Union, generics, forward refs)
- Clear error messages with "How to fix" guidance

</details>

### 📋 v1.1 Documentation (Planned)

**Milestone Goal:** Polish documentation for type validation system

- [ ] **Phase 5: Getting Started Audit** - Verify and polish getting-started.md
- [ ] **Phase 6: API Reference Documentation** - Create comprehensive API reference

### 🚧 v1.2 Test Coverage (In Progress)

**Milestone Goal:** Close all test coverage gaps for comprehensive quality assurance

- [ ] **Phase 7: GraphNode Capabilities** - Test forwarding methods
- [ ] **Phase 8: Graph Topologies** - Test diamond, cycles, isolated components
- [ ] **Phase 9: Function Signatures** - Test *args, **kwargs, keyword-only, positional-only
- [ ] **Phase 10: Type Compatibility** - Test Literal, Protocol, TypedDict, NamedTuple
- [ ] **Phase 11: Binding Edge Cases** - Test None values, seed interaction
- [ ] **Phase 12: Name Validation** - Test underscore, keywords, unicode

## Phase Details

### Phase 5: Getting Started Audit
**Goal**: getting-started.md is accurate, progressive, and teaches strict_types
**Depends on**: Nothing (first phase of v1.1)
**Requirements**: AUDIT-01, AUDIT-02, AUDIT-03, STYLE-01
**Success Criteria** (what must be TRUE):
  1. Every code example runs without error against current API
  2. Examples progress from simple → binding → composition → type validation
  3. strict_types usage demonstrated with working examples
  4. Language is step-by-step and human-centered
**Research**: Unlikely (internal documentation, existing guide to polish)
**Plans**: TBD

Plans:
- [ ] 05-01: TBD

### Phase 6: API Reference Documentation
**Goal**: Complete technical reference for all public APIs
**Depends on**: Phase 5
**Requirements**: API-01, API-02, API-03, API-04, STYLE-02, STYLE-03
**Success Criteria** (what must be TRUE):
  1. Graph class reference covers constructor, methods, strict_types
  2. FunctionNode reference covers @node decorator and properties
  3. GraphNode reference covers nested composition and .as_node()
  4. InputSpec reference explains all categorization types
  5. Reference format is technical and comprehensive
  6. Examples follow consistent patterns (code → explanation)
**Research**: Unlikely (documenting existing code, established patterns)
**Plans**: TBD

Plans:
- [ ] 06-01: TBD

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
- [ ] 07-01: GraphNode forwarding methods tests

### Phase 8: Graph Topologies
**Goal**: Test complex graph topologies work correctly
**Depends on**: Nothing (independent test phase)
**Requirements**: TOPO-01, TOPO-02, TOPO-03, TOPO-04, TOPO-05
**Success Criteria** (what must be TRUE):
  1. Diamond dependency pattern (A→B, A→C, B→D, C→D) executes correctly
  2. Multi-node cycles (A→B→C→A) detected and seeds computed correctly
  3. Multiple independent cycles in one graph work correctly
  4. Isolated subgraphs (disconnected components) work correctly
  5. Deeply nested graphs (3+ levels) work correctly
**Research**: Unlikely (testing existing code, patterns established)
**Plans**: 1 plan

Plans:
- [ ] 08-01: Graph topology tests

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
- [ ] 09-01: Function signature tests

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
- [ ] 10-01: Advanced type compatibility tests

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
- [ ] 11-01: Binding edge case tests

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
- [ ] 12-01: Name validation edge case tests

## Progress

**Execution Order:**
Phases execute in numeric order: 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 5. Getting Started Audit | v1.1 | 0/TBD | Not started | - |
| 6. API Reference Documentation | v1.1 | 0/TBD | Not started | - |
| 7. GraphNode Capabilities | v1.2 | 0/1 | Not started | - |
| 8. Graph Topologies | v1.2 | 0/1 | Not started | - |
| 9. Function Signatures | v1.2 | 0/1 | Not started | - |
| 10. Type Compatibility | v1.2 | 0/1 | Not started | - |
| 11. Binding Edge Cases | v1.2 | 0/1 | Not started | - |
| 12. Name Validation | v1.2 | 0/1 | Not started | - |
