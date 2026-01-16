# Requirements: Hypergraph v1.3 Execution Runtime

**Defined:** 2026-01-16
**Core Value:** Execute graphs with structure/execution separation - SyncRunner and AsyncRunner with `.run()` and `.map()`.

## v1.3 Requirements

Requirements for execution runtime. Each maps to roadmap phases.

### Types (TYPE)

- [ ] **TYPE-01**: RunResult dataclass with values, status, run_id, error fields
- [ ] **TYPE-02**: RunStatus enum with COMPLETED and FAILED values
- [ ] **TYPE-03**: RunnerCapabilities dataclass declaring runner features
- [ ] **TYPE-04**: InputSpec dataclass with required, optional, seeds, bound properties
- [ ] **TYPE-05**: GraphState internal class with values and versions tracking

### Nodes (NODE)

- [ ] **NODE-01**: HyperNode base class with name, inputs, outputs, with_* methods
- [ ] **NODE-02**: FunctionNode wrapping functions with @node decorator
- [ ] **NODE-03**: FunctionNode detects is_async and is_generator from function
- [ ] **NODE-04**: FunctionNode handles single and multiple output names
- [ ] **NODE-05**: GraphNode wrapping Graph via .as_node() method
- [ ] **NODE-06**: GraphNode supports with_inputs(), with_outputs(), with_name()
- [ ] **NODE-07**: GraphNode.map_over() configures iteration parameters

### Graph (GRAPH)

- [ ] **GRAPH-01**: Graph constructor accepts nodes list and optional name
- [ ] **GRAPH-02**: Graph infers edges from parameter name matching
- [ ] **GRAPH-03**: Graph.inputs property returns InputSpec (required/optional/seeds)
- [ ] **GRAPH-04**: Graph.outputs property returns all output names
- [ ] **GRAPH-05**: Graph.bind() returns new graph with pre-set values
- [ ] **GRAPH-06**: Graph.as_node() returns GraphNode for nesting
- [ ] **GRAPH-07**: Graph validates no duplicate output names (unless mutually exclusive)
- [ ] **GRAPH-08**: Graph.has_cycles property detects cycles
- [ ] **GRAPH-09**: Graph.definition_hash property for structure hashing

### Runners (RUN)

- [ ] **RUN-01**: BaseRunner abstract class with run() and map() signatures
- [ ] **RUN-02**: SyncRunner executes graph synchronously
- [ ] **RUN-03**: AsyncRunner executes graph asynchronously
- [ ] **RUN-04**: Both runners return RunResult from run()
- [ ] **RUN-05**: SyncRunner executes nodes in topological order
- [ ] **RUN-06**: AsyncRunner uses asyncio.gather for independent nodes (supersteps)
- [ ] **RUN-07**: AsyncRunner handles both sync and async nodes in same graph
- [ ] **RUN-08**: max_concurrency parameter limits parallel operations
- [ ] **RUN-09**: Runners validate graph compatibility via capabilities

### Map (MAP)

- [ ] **MAP-01**: runner.map() executes graph over iterable inputs
- [ ] **MAP-02**: map_over parameter specifies which inputs to iterate
- [ ] **MAP-03**: map_mode="zip" iterates parameters in parallel
- [ ] **MAP-04**: map_mode="product" produces Cartesian product
- [ ] **MAP-05**: Non-mapped inputs broadcast to all executions
- [ ] **MAP-06**: map() returns list of RunResult

### Nested Graphs (NEST)

- [ ] **NEST-01**: GraphNode executes inner graph when outer graph runs
- [ ] **NEST-02**: Nested graph inherits parent runner by default
- [ ] **NEST-03**: GraphNode can override runner via runner= parameter
- [ ] **NEST-04**: Nested RunResult appears in parent result.values
- [ ] **NEST-05**: Dot notation routes values to nested graphs
- [ ] **NEST-06**: Cross-runner execution (sync in async, async in sync) works

### Errors (ERR)

- [ ] **ERR-01**: MissingInputError when required input not provided
- [ ] **ERR-02**: GraphConfigError for invalid graph structure
- [ ] **ERR-03**: IncompatibleRunnerError when runner doesn't support graph features
- [ ] **ERR-04**: ConflictError when parallel nodes produce same output
- [ ] **ERR-05**: InfiniteLoopError when max_iterations exceeded

## v1.4 Requirements (Deferred)

### Events

- **EVT-01**: Event types (RunStartEvent, NodeEndEvent, etc.)
- **EVT-02**: EventProcessor interface
- **EVT-03**: .iter() streaming method on AsyncRunner

### Persistence

- **PERS-01**: Checkpointer interface
- **PERS-02**: workflow_id parameter on run()
- **PERS-03**: Resume from checkpoint

### Caching

- **CACHE-01**: cache= parameter on runners
- **CACHE-02**: DiskCache and MemoryCache implementations

## Out of Scope

| Feature | Reason |
|---------|--------|
| Control flow nodes (GateNode, RouteNode, etc.) | Requires routing infrastructure, defer |
| InterruptNode | Requires checkpointing for pause/resume |
| DaftRunner | Distributed execution, specialized |
| DBOSAsyncRunner | DBOS integration, specialized |
| Streaming via .iter() | Requires event infrastructure |
| Caching | Adds complexity, defer to v1.4 |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| TYPE-01 | Phase 13 | Pending |
| TYPE-02 | Phase 13 | Pending |
| TYPE-03 | Phase 13 | Pending |
| TYPE-04 | Phase 13 | Pending |
| TYPE-05 | Phase 13 | Pending |
| NODE-01 | Phase 14 | Pending |
| NODE-02 | Phase 14 | Pending |
| NODE-03 | Phase 14 | Pending |
| NODE-04 | Phase 14 | Pending |
| NODE-05 | Phase 15 | Pending |
| NODE-06 | Phase 15 | Pending |
| NODE-07 | Phase 15 | Pending |
| GRAPH-01 | Phase 16 | Pending |
| GRAPH-02 | Phase 16 | Pending |
| GRAPH-03 | Phase 16 | Pending |
| GRAPH-04 | Phase 16 | Pending |
| GRAPH-05 | Phase 16 | Pending |
| GRAPH-06 | Phase 16 | Pending |
| GRAPH-07 | Phase 16 | Pending |
| GRAPH-08 | Phase 16 | Pending |
| GRAPH-09 | Phase 16 | Pending |
| RUN-01 | Phase 17 | Pending |
| RUN-02 | Phase 17 | Pending |
| RUN-03 | Phase 17 | Pending |
| RUN-04 | Phase 17 | Pending |
| RUN-05 | Phase 17 | Pending |
| RUN-06 | Phase 17 | Pending |
| RUN-07 | Phase 17 | Pending |
| RUN-08 | Phase 17 | Pending |
| RUN-09 | Phase 17 | Pending |
| MAP-01 | Phase 18 | Pending |
| MAP-02 | Phase 18 | Pending |
| MAP-03 | Phase 18 | Pending |
| MAP-04 | Phase 18 | Pending |
| MAP-05 | Phase 18 | Pending |
| MAP-06 | Phase 18 | Pending |
| NEST-01 | Phase 19 | Pending |
| NEST-02 | Phase 19 | Pending |
| NEST-03 | Phase 19 | Pending |
| NEST-04 | Phase 19 | Pending |
| NEST-05 | Phase 19 | Pending |
| NEST-06 | Phase 19 | Pending |
| ERR-01 | Phase 20 | Pending |
| ERR-02 | Phase 20 | Pending |
| ERR-03 | Phase 20 | Pending |
| ERR-04 | Phase 20 | Pending |
| ERR-05 | Phase 20 | Pending |

**Coverage:**
- v1.3 requirements: 40 total
- Mapped to phases: 40
- Unmapped: 0 ✓

---
*Requirements defined: 2026-01-16*
*Last updated: 2026-01-16 after initial definition*
