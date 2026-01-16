# Roadmap: Hypergraph

## Milestones

- **v1.0 MVP** — Phases 1-4 (shipped 2026-01-16)
- **v1.1 Documentation** — Phases 5-6 (shipped 2026-01-16)
- **v1.2 Test Coverage** — Phases 7-12 (shipped 2026-01-16)
- **v1.3 Execution Runtime** — Phases 13-20 (in progress)

## Phases

<details>
<summary>v1.0 MVP (Phases 1-4) — SHIPPED 2026-01-16</summary>

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

<details>
<summary>v1.2 Test Coverage (Phases 7-12) — SHIPPED 2026-01-16</summary>

- [x] Phase 7: GraphNode Capabilities (1/1 plans) — completed 2026-01-16
- [x] Phase 8: Graph Topologies (1/1 plans) — completed 2026-01-16
- [x] Phase 9: Function Signatures (1/1 plans) — completed 2026-01-16
- [x] Phase 10: Type Compatibility (1/1 plans) — completed 2026-01-16
- [x] Phase 11: Binding Edge Cases (1/1 plans) — completed 2026-01-16
- [x] Phase 12: Name Validation (1/1 plans) — completed 2026-01-16

</details>

### v1.3 Execution Runtime (Phases 13-20)

**Milestone Goal:** Execute graphs with SyncRunner and AsyncRunner supporting `.run()` and `.map()` methods.

#### Phase 13: Runtime Types
**Goal**: Foundation types for execution runtime
**Depends on**: Nothing (first v1.3 phase)
**Requirements**: TYPE-01, TYPE-02, TYPE-03, TYPE-04, TYPE-05
**Success Criteria** (what must be TRUE):
  1. RunResult dataclass exists with values, status, run_id, error fields
  2. RunStatus enum has COMPLETED and FAILED values
  3. RunnerCapabilities dataclass declares runner features
  4. InputSpec dataclass has required/optional/seeds/bound properties
  5. GraphState class tracks values with versions internally
**Plans**: TBD

Plans:
- [ ] 13-01: Runtime types implementation

#### Phase 14: FunctionNode
**Goal**: Wrap Python functions as graph nodes
**Depends on**: Phase 13
**Requirements**: NODE-01, NODE-02, NODE-03, NODE-04
**Success Criteria** (what must be TRUE):
  1. @node decorator creates FunctionNode from function
  2. FunctionNode detects async/generator from function signature
  3. Single output_name stores return value as-is
  4. Multiple output_names unpack and validate return tuple
  5. with_inputs/with_outputs/with_name return new instances (immutable)
**Plans**: TBD

Plans:
- [ ] 14-01: FunctionNode implementation

#### Phase 15: GraphNode
**Goal**: Enable graph composition via nesting
**Depends on**: Phase 14
**Requirements**: NODE-05, NODE-06, NODE-07
**Success Criteria** (what must be TRUE):
  1. Graph.as_node() returns GraphNode wrapping the graph
  2. GraphNode exposes inner graph's inputs and outputs
  3. with_inputs/with_outputs renames propagate correctly
  4. map_over() configures iteration parameters
  5. Rename propagation updates _map_over list
**Plans**: TBD

Plans:
- [ ] 15-01: GraphNode implementation

#### Phase 16: Graph Class
**Goal**: Graph construction with edge inference and validation
**Depends on**: Phase 15
**Requirements**: GRAPH-01, GRAPH-02, GRAPH-03, GRAPH-04, GRAPH-05, GRAPH-06, GRAPH-07, GRAPH-08, GRAPH-09
**Success Criteria** (what must be TRUE):
  1. Graph infers edges from parameter name matching
  2. Graph.inputs returns InputSpec with required/optional categorization
  3. Graph.bind() returns new graph with pre-set values
  4. Graph validates no conflicting output names
  5. Graph detects cycles and computes definition_hash
**Plans**: TBD

Plans:
- [ ] 16-01: Graph class implementation

#### Phase 17: Runners
**Goal**: SyncRunner and AsyncRunner execute graphs
**Depends on**: Phase 16
**Requirements**: RUN-01, RUN-02, RUN-03, RUN-04, RUN-05, RUN-06, RUN-07, RUN-08, RUN-09
**Success Criteria** (what must be TRUE):
  1. SyncRunner.run() executes graph and returns RunResult
  2. AsyncRunner.run() executes graph asynchronously with parallel nodes
  3. Independent async nodes run via asyncio.gather (supersteps)
  4. max_concurrency limits parallel operations
  5. Runners validate graph compatibility before execution
**Plans**: TBD

Plans:
- [ ] 17-01: Runners implementation

#### Phase 18: Map Method
**Goal**: Batch processing over iterable inputs
**Depends on**: Phase 17
**Requirements**: MAP-01, MAP-02, MAP-03, MAP-04, MAP-05, MAP-06
**Success Criteria** (what must be TRUE):
  1. runner.map() iterates over specified inputs
  2. zip mode iterates parameters in parallel
  3. product mode produces Cartesian product
  4. Non-mapped inputs broadcast to all executions
  5. map() returns list of RunResult
**Plans**: TBD

Plans:
- [ ] 18-01: Map method implementation

#### Phase 19: Nested Graph Execution
**Goal**: Execute nested graphs through runners
**Depends on**: Phase 18
**Requirements**: NEST-01, NEST-02, NEST-03, NEST-04, NEST-05, NEST-06
**Success Criteria** (what must be TRUE):
  1. GraphNode executes inner graph when parent runs
  2. Nested graph inherits parent runner by default
  3. runner= parameter overrides nested runner
  4. Nested RunResult appears in parent result.values
  5. Cross-runner execution works (sync in async, async in sync)
**Plans**: TBD

Plans:
- [ ] 19-01: Nested graph execution

#### Phase 20: Error Handling
**Goal**: Clear error messages for execution failures
**Depends on**: Phase 19
**Requirements**: ERR-01, ERR-02, ERR-03, ERR-04, ERR-05
**Success Criteria** (what must be TRUE):
  1. MissingInputError raised when required input missing
  2. GraphConfigError raised for invalid graph structure
  3. IncompatibleRunnerError raised when runner can't handle graph
  4. ConflictError raised when parallel nodes produce same output
  5. All errors include helpful "How to fix" guidance
**Plans**: TBD

Plans:
- [ ] 20-01: Error handling implementation

## Progress

| Milestone | Phases | Status | Shipped |
|-----------|--------|--------|---------|
| v1.0 MVP | 1-4 | Complete | 2026-01-16 |
| v1.1 Documentation | 5-6 | Complete | 2026-01-16 |
| v1.2 Test Coverage | 7-12 | Complete | 2026-01-16 |
| v1.3 Execution Runtime | 13-20 | In Progress | - |

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 13. Runtime Types | 0/1 | Not started | - |
| 14. FunctionNode | 0/1 | Not started | - |
| 15. GraphNode | 0/1 | Not started | - |
| 16. Graph Class | 0/1 | Not started | - |
| 17. Runners | 0/1 | Not started | - |
| 18. Map Method | 0/1 | Not started | - |
| 19. Nested Graph Execution | 0/1 | Not started | - |
| 20. Error Handling | 0/1 | Not started | - |
