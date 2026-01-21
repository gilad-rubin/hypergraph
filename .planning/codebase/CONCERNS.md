# Codebase Concerns

**Analysis Date:** 2026-01-21

## Performance Bottlenecks

**Infinite Loop Detection (DEFAULT_MAX_ITERATIONS = 1000):**
- Problem: Cyclic graphs can iterate up to 1000 supersteps before raising `InfiniteLoopError`
- Files: `src/hypergraph/runners/sync/runner.py` (line 44), `src/hypergraph/runners/async_/runner.py` (line 50)
- Cause: Hard-coded limit without performance introspection. In production with large graphs, each iteration can be expensive
- Improvement path: Make configurable, add iteration timing metrics, provide early warnings at 50% and 75% thresholds. Store intermediate state snapshots for debugging runaway loops

**Visualization Renderer Complexity:**
- Problem: `src/hypergraph/viz/renderer.py` and `src/hypergraph/viz/html_generator.py` contain intricate edge routing and layout calculations
- Files: `src/hypergraph/viz/renderer.py` (451 lines), `src/hypergraph/viz/html_generator.py` (204 lines), `src/hypergraph/viz/layout_estimator.py` (298 lines)
- Cause: Browser-side layout using custom constraint solver with hierarchical nesting support requires complex Python↔JavaScript coordination
- Improvement path: Profile edge routing algorithm performance on large graphs (>100 nodes). Consider caching hierarchy trees between renders

**Graph Validation at Construction Time:**
- Problem: `_validate_types()` runs deep type introspection on all edges when `strict_types=True`
- Files: `src/hypergraph/graph/validation.py` (lines 222-269)
- Cause: Full graph traversal for validation at every Graph construction
- Improvement path: For large graphs, parallelize edge validation or defer to first run

## Test Coverage Gaps

**Visualization System Testing:**
- What's not tested: Edge routing algorithm correctness for complex nested graphs, viewport centering edge cases
- Files: `src/hypergraph/viz/renderer.py`, `src/hypergraph/viz/html_generator.py`, assets stored in JavaScript
- Risk: Visualization changes can break silently without Python test coverage
- Priority: Medium - visualization issues are UX problems, not data correctness problems

**Generator Function Handling:**
- What's not tested: Interaction between generator functions (sync/async) and gate routing, streaming with multi-target gates
- Files: `src/hypergraph/nodes/function.py` (generator detection), `src/hypergraph/runners/sync/executors/function_node.py`, `src/hypergraph/runners/async_/executors/function_node.py`
- Risk: Generator state may not be properly cleaned up on gate branching, could leak resources
- Priority: High - generators are power users feature and current execution model treats all outputs equally

**Async Error Propagation:**
- What's not tested: Exception handling in async generators with concurrent execution
- Files: `src/hypergraph/runners/async_/runner.py`, `src/hypergraph/runners/async_/superstep.py`
- Risk: Concurrent task failures may not cancel other tasks properly, leading to incomplete state
- Priority: Medium - error handling works for normal functions but untested with async generators

## Fragile Areas

**Gate Routing Decision Caching:**
- Files: `src/hypergraph/nodes/gate.py` (lines 98-103, 200, 251-256, 426)
- Why fragile: Gates have `cache=False` by default, but if enabled, there's no invalidation logic. Stale routing decisions could persist across iterations
- Safe modification: Add routing decision invalidation hooks in state manager. Test that routing decisions update when inputs change
- Test coverage: No tests for cached gate behavior across multiple iterations

**Multi-target Gate Output Conflict Detection:**
- Files: `src/hypergraph/graph/validation.py` (lines 327-364)
- Why fragile: Validation only catches duplicate outputs at graph construction time. If targets are dynamically computed (future feature), this breaks
- Safe modification: Move validation to run time for dynamic gates. Add per-superstep output conflict detection
- Test coverage: Only tested with static target lists

**Graph Composition with Nested Graphs:**
- Files: `src/hypergraph/nodes/graph_node.py` (378 lines), `src/hypergraph/graph/core.py` (line 103-137 for namespace collision)
- Why fragile: GraphNode names cannot collide with output names, but this isn't enforced if GraphNode is renamed after composition
- Safe modification: Add runtime validation when GraphNode is renamed. Test graph reuse with different names
- Test coverage: Covered for initial composition, not for post-construction renames

**Rename History Tracking:**
- Files: `src/hypergraph/nodes/_rename.py` (142 lines), `src/hypergraph/nodes/function.py` (lines 56-80 for forward rename map)
- Why fragile: Complex logic mapping original param names through rename chains. Error in `_build_forward_rename_map` could silently map wrong names
- Safe modification: Add exhaustive tests for chained renames (a→b, b→c, c→d). Test parallel renames in same batch
- Test coverage: Basic tests exist but complex rename chains untested

## Tech Debt

**Hardcoded Visualization Constants:**
- Issue: Multiple magic numbers in visualization code without explanation
- Files: `src/hypergraph/viz/layout_estimator.py`, `src/hypergraph/viz/html_generator.py`, assets/constraint-layout.js
- Current state: Constants like GRAPH_PADDING (40), HEADER_HEIGHT (32), SHADOW_OFFSET (14), stemMinTarget (6-12px) scattered across files
- Fix approach: Create `src/hypergraph/viz/config.py` to centralize all layout constants with documentation. Update asset generation to reference config values

**Incomplete Streaming Support:**
- Issue: `AsyncRunner.capabilities.supports_streaming = False` - streaming is marked as Phase 2
- Files: `src/hypergraph/runners/async_/runner.py` (line 94)
- Current state: Generator functions can be used but results are buffered, not streamed to caller
- Fix approach: Implement `runner.stream()` method that yields results as they become available. Requires significant async coordination changes

**Exception Types Need Consolidation:**
- Issue: Multiple exception types defined but not consistently used
- Files: `src/hypergraph/exceptions.py` (MissingInputError, InfiniteLoopError, IncompatibleRunnerError, GraphConfigError)
- Current state: Some exceptions raised from validation (`GraphConfigError`), others from runners
- Fix approach: Add base `HypergraphException` class. Define when each exception type should be raised. Add tests for exception contracts

**Type Validation is Optional:**
- Issue: `strict_types=False` by default - type mismatches not caught
- Files: `src/hypergraph/graph/core.py` (line 56, 63-66), `src/hypergraph/graph/validation.py` (lines 222-269)
- Current state: Type validation requires opt-in. Most users won't catch type errors at graph build time
- Fix approach: Add warning when types are present but strict_types=False. Consider making it True by default in future version

## Known Bugs

**Double-Wiggle Edge Routing (Documented but Unresolved):**
- Symptoms: Some left-to-right edges in visualization have two bends instead of one smooth curve
- Files: `src/hypergraph/viz/CLAUDE.md` (lines 296-337 documents extensive investigation), assets/constraint-layout.js (corridor selection logic)
- Trigger: Edges from left-to-center or center-to-right nodes, particularly in nested layouts
- Current status: Attempted fixes reverted because they caused edges to route over blocking nodes. Requires architectural rethinking of blocking detection and corridor selection

**Nested Graph Expansion State Not Persisted:**
- Symptoms: User expands a collapsed pipeline, but if graph re-renders, expansion state is lost
- Files: `src/hypergraph/viz/html_generator.py` (generates React Flow state)
- Workaround: User must re-expand after refresh. No persistence mechanism implemented
- Fix approach: Add localStorage or URL parameter support for expansion state

## Scaling Limits

**Maximum Iterations for Cyclic Graphs:**
- Current capacity: 1000 iterations per run
- Limit: Graphs taking longer than 1000 iterations fail with `InfiniteLoopError`
- Scaling path: Make limit configurable (already done via `max_iterations` param), but no adaptive scaling based on graph complexity

**Visualization Rendering for Large Graphs:**
- Current capacity: Tested up to ~50-100 nodes (inferred from CLAUDE.md examples)
- Limit: JavaScript constraint solver may become slow on graphs with >200 nodes
- Scaling path: Implement graph clustering/grouping at rendering layer. Consider server-side layout pre-computation

**Concurrency Limiting in AsyncRunner:**
- Current capacity: `max_concurrency` parameter available but defaults to unlimited
- Limit: Running all independent nodes concurrently in superstep can exhaust resources
- Scaling path: Add sensible default limit (e.g., 10). Document memory implications of large concurrent task pools

## Security Considerations

**Graph Validation Type Introspection:**
- Risk: `get_type_hints()` and `inspect.signature()` used on user functions. Malicious functions could exploit introspection
- Files: `src/hypergraph/nodes/function.py` (lines 35-53), `src/hypergraph/graph/validation.py` (lines 239-269)
- Current mitigation: `try/except` blocks catch exceptions from introspection failures
- Recommendations: Add sandboxing for type introspection. Limit recursion depth in type checking. Add timeout to prevent denial-of-service via complex type hints

**Function Source Code Hashing:**
- Risk: Uses `hashlib.sha256(inspect.getsource(func))` to create definition hashes
- Files: `src/hypergraph/_utils.py`, referenced in `src/hypergraph/nodes/function.py`, `src/hypergraph/nodes/gate.py`
- Current mitigation: None - hashes not used for security-critical operations
- Recommendations: Document that hashes are NOT for integrity verification. Use different hash strategy if future caching relies on this

**Exception Messages Leak Internal State:**
- Risk: GraphConfigError messages include all node names and node structure
- Files: `src/hypergraph/graph/validation.py` (all validation functions)
- Current mitigation: None - this is by design for debugging
- Recommendations: For production deployments, add option to sanitize error messages. Document that exception messages are intended for developers

## Dependencies at Risk

**No External API Dependencies (Positive):**
- Observation: Hypergraph has no external service dependencies (no APIs, no databases)
- Impact: Good - no operational risk from third-party services
- Concern: Visualization uses React Flow (npm package) which is a peer dependency, not bundled

**Visualization JavaScript Assets Not Bundled:**
- Issue: HTML generation references external `constraint-layout.js` and layout algorithms
- Files: `src/hypergraph/viz/assets/` (JavaScript files are committed but how they're served is unclear)
- Risk: Circular dependency if Python changes HTML generation but JavaScript assets drift
- Fix approach: Either bundle assets into Python wheel, or document clear versioning between Python and JavaScript

## Missing Critical Features

**No Checkpointing/Resumption:**
- Problem: If a long-running graph fails at iteration 500, no way to resume from checkpoint
- Blocks: Complex ETL pipelines, expensive ML training workflows
- Roadmap: Listed as Phase 2+ feature in runner capabilities

**No Observability/Telemetry:**
- Problem: No built-in logging, tracing, or metrics for graph execution
- Blocks: Production deployments need visibility into execution
- Current state: Users must add print statements or override runner methods
- Roadmap: Listed as Phase 2+ feature

**No Human-in-the-Loop Support:**
- Problem: No way to pause execution and ask user for input
- Blocks: Interactive workflows, approval gates
- Files: `src/hypergraph/runners/base.py` (capabilities mention InterruptNode as "Coming soon")
- Roadmap: Mentioned in README but not implemented

**No Dynamic Node Registration:**
- Problem: All nodes must be defined before Graph construction
- Blocks: Runtime-determined workflows, plugin systems
- Current state: Graph is static after construction
- Fix approach: Could add graph.add_node() method with re-validation

---

*Concerns audit: 2026-01-21*
