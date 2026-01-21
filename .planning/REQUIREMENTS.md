# Requirements: Hypergraph v1.1

**Defined:** 2026-01-21
**Core Value:** Pure functions connect automatically with build-time validation

## v1.1 Requirements

Refactor visualization code to add missing abstractions, then fix edge routing regression.

### Refactoring (Abstractions)

- [ ] **REFAC-01**: Hierarchy traversal abstraction — eliminate manual depth tracking
- [ ] **REFAC-02**: Coordinate transformation types — explicit transforms between 4 coordinate spaces
- [ ] **REFAC-03**: Unify edge routing logic — single source of truth (not split Python/JS)

### Edge Routing

- [ ] **EDGE-01**: Edges route around nodes, never through them (regression fix)
- [ ] **EDGE-02**: Collapsed nested graphs connect edges flush to node boundary (no gap)
- [ ] **EDGE-03**: Deeply nested graphs (2+ levels) route edges to correct inner nodes
- [ ] **EDGE-04**: Single unified algorithm handles all nesting depths (no special cases)

### Verification Test Cases

These specific scenarios must render correctly:

- [ ] **TEST-01**: `complex_rag` graph from `test_viz_layout` — edges never cross nodes
- [ ] **TEST-02**: Single nested graph, collapsed state — edge connects to outer node boundary
- [ ] **TEST-03**: Single nested graph, expanded state — edge routes to inner node
- [ ] **TEST-04**: Double nested graph (graph inside graph inside graph) — edges route correctly at all levels

### Automated Verification

- [ ] **VERIFY-01**: Python script extracts node/edge coordinates from rendered output
- [ ] **VERIFY-02**: Geometric tests verify edge paths don't intersect node bounding boxes
- [ ] **VERIFY-03**: CI runs visual regression tests with Playwright screenshots

## Future Requirements

Deferred to later milestones. Not in current roadmap.

### Checkpointing

- **CHKPT-01**: Save execution state to disk at configurable intervals
- **CHKPT-02**: Resume execution from saved state

### Event Streaming

- **STREAM-01**: `.iter()` method yields execution events in real-time
- **STREAM-02**: Events include node start, node complete, value produced

### Human-in-the-Loop

- **HITL-01**: `InterruptNode` pauses execution and awaits user input
- **HITL-02**: Runner resumes with user-provided value

## Out of Scope

| Feature | Reason |
|---------|--------|
| New node types | Not related to viz fix |
| Runner performance | Not related to viz fix |
| API changes | Fix should be internal to viz layer |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| REFAC-01 | Phase 1 | Pending |
| REFAC-02 | Phase 1 | Pending |
| REFAC-03 | Phase 2 | Pending |
| EDGE-01 | Phase 3 | Pending |
| EDGE-02 | Phase 3 | Pending |
| EDGE-03 | Phase 3 | Pending |
| EDGE-04 | Phase 3 | Pending |
| VERIFY-01 | Phase 4 | Pending |
| VERIFY-02 | Phase 4 | Pending |
| VERIFY-03 | Phase 4 | Pending |
| TEST-01 | Phase 4 | Pending |
| TEST-02 | Phase 4 | Pending |
| TEST-03 | Phase 4 | Pending |
| TEST-04 | Phase 4 | Pending |

**Coverage:**
- v1.1 requirements: 14 total
- Mapped to phases: 14
- Unmapped: 0

---
*Requirements defined: 2026-01-21*
*Last updated: 2026-01-21 after initial definition*
