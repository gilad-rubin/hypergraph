---
stepsCompleted: [1]
inputDocuments:
  - '_bmad-output/prd.md'
  - '_bmad-output/index.md'
  - '_bmad-output/project-overview.md'
  - '_bmad-output/development-guide.md'
  - '_bmad-output/source-tree-analysis.md'
workflowType: 'architecture'
lastStep: 1
project_name: 'hypernodes'
user_name: 'Giladrubin'
date: '2025-12-23'
---

# Architecture Decision Document - HyperNodes Graph (v0.5.0)

_This document builds collaboratively through step-by-step discovery. Sections are appended as we work through architectural decisions for the Graph architecture rewrite._

## Context

This architecture document covers the **v0.5.0 rewrite** of HyperNodes:
- **From:** Pipeline (DAG-only) architecture
- **To:** Graph (reactive dataflow with cycles) architecture

Key changes being architected:
- `Graph` class replacing `Pipeline` for cyclic workflows
- `Runner`/`AsyncRunner` pattern separating definition from execution
- Reactive dataflow with versioning and staleness detection
- `@route` decorator for string-based routing
- NetworkX-native graph representation

Reference: See `prd.md` for complete requirements.
