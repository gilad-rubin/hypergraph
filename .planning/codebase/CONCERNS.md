# Codebase Concerns

**Analysis Date:** 2026-01-16

## Spec-Implementation Gap (Major)

**Large gap between specs and implementation:**
- Issue: The `specs/reviewed/` directory contains comprehensive specifications for a full graph workflow framework, but `src/` only implements ~15% of the design
- Files:
  - Specs: `specs/reviewed/runners.md`, `specs/reviewed/node-types.md`, `specs/reviewed/execution-types.md`, `specs/reviewed/checkpointer.md`
  - Implementation: `src/hypergraph/graph.py`, `src/hypergraph/nodes/`
- Impact: The codebase cannot execute graphs - only define structure. No runners, no execution, no state management exist yet
- Missing implementations:
  - **Runners**: `SyncRunner`, `AsyncRunner`, `DaftRunner`, `DBOSAsyncRunner` - none exist
  - **Node types**: `GateNode`, `RouteNode`, `BranchNode`, `TypeRouteNode`, `InterruptNode` - none exist
  - **Execution**: `RunResult`, `RunStatus`, `PauseInfo`, `GraphState` - none exist
  - **Persistence**: `Checkpointer`, `Step`, `StepResult`, `Workflow` - none exist
  - **Caching**: `DiskCache`, `MemoryCache` - none exist
  - **Events**: `NodeStartEvent`, `NodeEndEvent`, `StreamingChunkEvent` - none exist
- Fix approach: Prioritize implementation phases starting with SyncRunner (simplest), then gate nodes, then AsyncRunner with streaming

## hasattr/isinstance Usage (Code Smell)

**Feature detection via hasattr breaks abstraction:**
- Issue: Multiple uses of `hasattr()` and `isinstance()` for feature detection
- Files:
  - `src/hypergraph/graph.py:183` - `hasattr(node, "defaults")` to check if node has defaults
  - `src/hypergraph/graph.py:324` - `isinstance(node, FunctionNode) and node.is_async`
  - `src/hypergraph/graph.py:350` - `hasattr(node, "definition_hash")` for hash computation
  - `src/hypergraph/graph.py:442` - `hasattr(node, 'defaults')` in param collection
- Impact: Violates LSP (Liskov Substitution Principle) - indicates the base class interface is incomplete
- Fix approach:
  - Add `defaults` as abstract property on `HyperNode` with empty dict default for non-function nodes
  - Add `definition_hash` as abstract property on `HyperNode`
  - Add `is_async` property to `HyperNode` (returns False for non-function nodes)

## HyperNode Not Truly Abstract (Design Issue)

**HyperNode lacks proper abstract method enforcement:**
- Issue: `HyperNode` declares attributes via type annotations but doesn't enforce implementation via `@abstractmethod`
- Files: `src/hypergraph/nodes/base.py:15-37`
- Impact: Subclasses could forget to set required attributes; no compile-time checks
- Current workaround: Manual `__new__` check prevents direct instantiation
- Fix approach: Consider `@abstractproperty` for core attributes, or document/test the contract explicitly

## GraphNode Incomplete Implementation

**GraphNode lacks methods specified in design:**
- Issue: `GraphNode` is implemented but missing key methods from spec
- Files:
  - Implementation: `src/hypergraph/nodes/graph_node.py`
  - Spec: `specs/reviewed/node-types.md:1192-1599`
- Missing:
  - `map_over()` method for batch iteration configuration
  - `with_inputs()` override that propagates to `_map_over`
  - `complete_on_stop` property
  - `runner` property for nested execution
- Impact: Cannot use nested graphs for batch processing or configure execution behavior
- Fix approach: Implement missing methods following spec closely

## Test Coverage Gaps

**Critical functionality untested:**
- Files: `tests/` directory
- What's not tested:
  - Nested graph execution flow (only structure tested)
  - GraphNode `with_inputs`/`with_outputs` behavior
  - Cyclic graph detection edge cases
  - Error message formatting for complex scenarios
  - Multiple output unpacking validation
- Risk: Regressions could go unnoticed as more features are added
- Priority: High - tests should be added before implementing runners

## Deprecated/Obsolete Code Presence

**Multiple deprecated directories still present:**
- Issue: Old code directories exist but are excluded via pyproject.toml
- Files:
  - `src/hypergraph/old/` - excluded in pyproject.toml but may still exist
  - `specs/deprecated/` - old spec versions
  - `specs/not_reviewed/` - unreviewed specifications
  - `deprecated/Continuous-Claude-v3/` - appears in git status as untracked
- Impact: Confusion about what code is current; potential for importing wrong modules
- Fix approach: Consider removing deprecated directories entirely, or moving to separate archive

## Missing Public API Exports

**Some internal classes not exported:**
- Issue: `RenameEntry` not exported from main `__init__.py`
- Files:
  - `src/hypergraph/__init__.py` - exports `RenameError` but not `RenameEntry`
  - `src/hypergraph/nodes/__init__.py` - exports `RenameEntry`
- Impact: Inconsistent import paths for related types
- Fix approach: Either export `RenameEntry` from top-level or document that it's internal

## FunctionNode Default Output Behavior

**Spec-implementation mismatch on default output_name:**
- Issue: Spec says `output_name` defaults to function name, but implementation uses empty tuple
- Files:
  - Spec: `specs/reviewed/node-types.md:360` - "Default: function name"
  - Implementation: `src/hypergraph/nodes/function.py:138` - `outputs = ()` when no output_name
- Impact: Nodes without explicit `output_name` become side-effect only (no output captured)
- Current behavior: Emits warning if function has return annotation but no `output_name`
- Fix approach: Decide if current behavior is correct and update spec, or change implementation

## No Runtime Execution Path

**Graph defines structure but cannot execute:**
- Issue: There is no code path to actually run a graph
- Files: All of `src/hypergraph/`
- Impact: The framework is currently unusable for its intended purpose
- Current state: Only graph construction and validation works
- Fix approach: Implement `SyncRunner.run()` as first priority - simplest execution model

## Validation Gap for Nested Graphs

**GraphNode validation incomplete:**
- Issue: `_validate_no_namespace_collision` is a stub (pass)
- Files: `src/hypergraph/graph.py:422-425`
- Impact: Could create graphs where output names collide with GraphNode names, causing ambiguous results
- Risk: Medium - can cause confusing bugs when using nested graphs
- Fix approach: Implement the validation following the pattern in the spec

## Definition Hash Excludes Bindings Intentionally

**Potential cache invalidation issue:**
- Issue: `definition_hash` excludes bound values
- Files: `src/hypergraph/graph.py:329-368`
- Impact: Two graphs with same structure but different bound values have same hash
- Current behavior: By design - bindings are runtime values, not structure
- Consideration: May need separate "execution hash" that includes bindings for cache keys

## Dependencies

**No risky dependencies identified:**
- Only required dependency: `networkx>=3.2`
- Optional dependencies are well-scoped (daft, notebook, telemetry)
- No known security issues

## Scaling Limits

**Not applicable yet:**
- Framework is in early design phase
- No runtime execution to measure performance
- NetworkX handles graph structure - known to scale well for reasonable graph sizes

## Security Considerations

**No current security risks:**
- No network operations in current implementation
- No file I/O beyond source inspection
- No user input handling yet
- Risk will increase when runners with checkpointing are added (serialization)

---

*Concerns audit: 2026-01-16*
