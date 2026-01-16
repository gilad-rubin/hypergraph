# Requirements: Hypergraph v1.1 Documentation Polish

**Defined:** 2026-01-16
**Core Value:** Catch type errors early - documentation must help users understand and use strict_types

## v1.1 Requirements

Requirements for documentation milestone. Each maps to roadmap phases.

### Documentation Audit

- [ ] **AUDIT-01**: All code examples in getting-started.md execute without errors against current API
- [ ] **AUDIT-02**: getting-started.md follows progressive complexity (simple → advanced)
- [ ] **AUDIT-03**: getting-started.md covers type validation with strict_types

### API Reference

- [ ] **API-01**: Graph class reference documents constructor parameters, methods, and strict_types behavior
- [ ] **API-02**: FunctionNode reference documents @node decorator, properties, and rename API
- [ ] **API-03**: GraphNode reference documents nested composition and .as_node() usage
- [ ] **API-04**: InputSpec reference documents required/optional/bound/seeds categorization

### Style

- [ ] **STYLE-01**: Guides use step-by-step, human-centered language
- [ ] **STYLE-02**: API reference uses technical, comprehensive format
- [ ] **STYLE-03**: All documentation uses consistent example patterns (show code, then explain)

## v2 Requirements

Deferred to future release. Tracked but not in current roadmap.

### Future Audit

- **AUDIT-V2-01**: Remove "Coming soon" sections after features ship
- **AUDIT-V2-02**: Update comparison.md with competitive positioning

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| Runner documentation | Feature not implemented |
| RouteNode/BranchNode docs | Feature not implemented |
| Checkpointing docs | Feature not implemented |
| Control flow (@route, @branch) docs | Feature not implemented |

## Traceability

Which phases cover which requirements. Updated by create-roadmap.

| Requirement | Phase | Status |
|-------------|-------|--------|
| AUDIT-01 | — | Pending |
| AUDIT-02 | — | Pending |
| AUDIT-03 | — | Pending |
| API-01 | — | Pending |
| API-02 | — | Pending |
| API-03 | — | Pending |
| API-04 | — | Pending |
| STYLE-01 | — | Pending |
| STYLE-02 | — | Pending |
| STYLE-03 | — | Pending |

**Coverage:**
- v1.1 requirements: 10 total
- Mapped to phases: 0
- Unmapped: 10 (pending roadmap)

---
*Requirements defined: 2026-01-16*
*Last updated: 2026-01-16 after initial definition*
