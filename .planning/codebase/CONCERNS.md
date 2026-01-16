# Codebase Concerns

**Analysis Date:** 2026-01-16

## Tech Debt

**Design Mode - No Runtime Implementation:**
- Issue: Project is in "design mode" per `CLAUDE.md`. Graph definition and type checking exist, but no execution/runner implementation yet.
- Files: `src/hypergraph/graph.py`, `specs/reviewed/runners.md`
- Impact: Cannot actually run graphs - only structural validation works. The specs describe runners, checkpointers, state management, but none are implemented.
- Fix approach: Follow the reviewed specs in `specs/reviewed/` to implement runners (likely next major milestone)

**tmp/ Directory Contains Working Code:**
- Issue: `tmp/pipefunc_typing.py` contains a more complete type checking implementation that was used as reference, but it depends on numpy
- Files: `tmp/pipefunc_typing.py`
- Impact: Reference code is in tmp/ but should probably be cleaned up or moved to deprecated/
- Fix approach: Either delete or move to `deprecated/` once `_typing.py` is fully validated

**hash_depth Parameter Not Implemented:**
- Issue: `specs/reviewed/graph.md` describes `hash_depth` parameter for controlling import depth in definition hashing, but current implementation only hashes function source
- Files: `src/hypergraph/_utils.py`, `src/hypergraph/graph.py`
- Impact: Changes to helper functions in the same package won't invalidate the graph hash, which could cause stale cache issues
- Fix approach: Implement the hash_depth feature as described in the spec when implementing runners/caching

**complete_on_stop Parameter Not Implemented:**
- Issue: Spec describes `complete_on_stop` behavior for graceful shutdown, but Graph constructor doesn't accept it
- Files: `src/hypergraph/graph.py`, `specs/reviewed/graph.md`
- Impact: No impact yet since runners aren't implemented, but will need to be added
- Fix approach: Add parameter when implementing runner execution

## Known Bugs

No known bugs at this time. All 276 tests pass.

## Security Considerations

**No Runtime Input Validation:**
- Risk: Type checking is build-time only (via `strict_types`). No runtime type checking exists.
- Files: `src/hypergraph/graph.py` (`_validate_types`)
- Current mitigation: Users must enable `strict_types=True` explicitly; type mismatches caught at Graph construction
- Recommendations: Consider adding optional runtime type checking when implementing runners

**Function Source Hashing:**
- Risk: `hash_definition` uses `inspect.getsource()` which could fail silently on certain function types
- Files: `src/hypergraph/_utils.py`
- Current mitigation: Raises `ValueError` if source can't be retrieved
- Recommendations: Document limitation for C extensions and built-ins

## Performance Bottlenecks

No significant performance concerns identified. The codebase is small and focused on graph definition (not execution).

**Potential Future Concern - Graph Validation:**
- Problem: `_validate_types()` iterates all edges and calls `is_type_compatible()` for each
- Files: `src/hypergraph/graph.py` (lines 528-575)
- Cause: Linear scan with recursive type checking
- Improvement path: Not a concern until graphs reach thousands of nodes; NetworkX handles graph operations efficiently

## Fragile Areas

**Type Compatibility for TypeVars:**
- Files: `src/hypergraph/_typing.py` (lines 394-428, 479-482)
- Why fragile: Incoming TypeVars return True unconditionally (can't know concrete type without runtime info). This matches pipefunc behavior but could cause false positives.
- Safe modification: Any changes should be accompanied by comprehensive test cases in `tests/test_typing.py`
- Test coverage: Good - see `TestTypeVarCompatibility` class

**Python Version-Specific ForwardRef Handling:**
- Files: `src/hypergraph/_typing.py` (lines 110-129)
- Why fragile: `_evaluate_forwardref()` has version-specific code for Python 3.12 vs 3.13+
- Safe modification: Test on all supported Python versions (3.10-3.13)
- Test coverage: Forward reference tests exist but version-specific branches may not all be covered

**Graph Copy Operation:**
- Files: `src/hypergraph/graph.py` (`_shallow_copy` lines 372-383)
- Why fragile: Uses `copy.copy()` for shallow copy, but only creates new `_bound` dict. If new mutable attributes are added to Graph, they need explicit handling.
- Safe modification: Document mutable attributes and update `_shallow_copy()` when adding new ones
- Test coverage: Tested via bind/unbind tests

## Scaling Limits

**No Current Limits:**
- NetworkX can handle millions of nodes
- Type checking is O(edges * type_complexity)
- No known scaling issues for the current scope

## Dependencies at Risk

**networkx:**
- Risk: None - stable, well-maintained library
- Impact: Core dependency for graph operations
- Migration plan: N/A - appropriate choice

**No Other Runtime Dependencies:**
- Project has minimal dependencies (just networkx for core)
- Optional dependencies (pyarrow, daft, etc.) are properly isolated

## Missing Critical Features

**Execution Engine:**
- Problem: No way to actually execute graphs
- Blocks: Real-world usage
- Note: This is intentional - project is in design phase per `CLAUDE.md`

**RouteNode, BranchNode, InterruptNode:**
- Problem: Specs describe these node types for conditional execution and human-in-the-loop, but only FunctionNode and GraphNode are implemented
- Blocks: Cyclic graph execution, conditional branching, HITL workflows
- Files: `specs/reviewed/node-types.md`, `src/hypergraph/nodes/`

**Observability/Telemetry:**
- Problem: No logging, tracing, or metrics infrastructure
- Blocks: Production debugging and monitoring
- Note: `pyproject.toml` lists optional telemetry deps (logfire, tqdm, rich)

## Test Coverage Gaps

**Missing pytest-cov:**
- What's not tested: Cannot measure exact coverage - pytest-cov not in dev dependencies
- Files: `pyproject.toml` (dev dependencies)
- Risk: Unknown coverage percentage
- Priority: Low - test file count and organization suggests good coverage

**Forward Reference Resolution Edge Cases:**
- What's not tested: Complex nested forward references with version-specific behavior
- Files: `src/hypergraph/_typing.py`
- Risk: Edge case failures on specific Python versions
- Priority: Medium

**GraphNode Type Propagation:**
- What's not tested: Deep nesting (3+ levels) of GraphNode type annotations
- Files: `src/hypergraph/nodes/graph_node.py` (`get_input_type`, `output_annotation`)
- Risk: Type info may not propagate correctly through multiple nesting levels
- Priority: Low - basic nesting is tested

---

*Concerns audit: 2026-01-16*
