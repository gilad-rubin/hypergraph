# Requirements: Hypergraph v1.1

**Defined:** 2026-01-21
**Core Value:** Pure functions connect automatically with build-time validation

## v1.1 Requirements

Fix visualization edge routing regression and implement unified algorithm for nested graphs.

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
| EDGE-01 | Phase 1 | Pending |
| EDGE-02 | Phase 1 | Pending |
| EDGE-03 | Phase 1 | Pending |
| EDGE-04 | Phase 1 | Pending |
| TEST-01 | Phase 1 | Pending |
| TEST-02 | Phase 1 | Pending |
| TEST-03 | Phase 1 | Pending |
| TEST-04 | Phase 1 | Pending |

**Coverage:**
- v1.1 requirements: 8 total
- Mapped to phases: 8
- Unmapped: 0

---
*Requirements defined: 2026-01-21*
*Last updated: 2026-01-21 after initial definition*
