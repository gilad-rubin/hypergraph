# Research Summary: Edge Routing in Nested Graphs

**Domain:** Graph visualization with hierarchical/compound graphs
**Researched:** 2026-01-21
**Overall confidence:** HIGH

## Executive Summary

The problem of routing edges to deeply nested nodes in compound graphs is well-studied in the graph visualization literature. **But the root cause of hypergraph's regression is code smells, not algorithm choice.** Professional libraries (ELK, Cytoscape, Graphviz) handle this through four main algorithmic patterns:

1. **LCA-based routing** - Route through lowest common ancestor containers
2. **Recursive DFS** - Recursively descend into expanded containers to find leaf nodes
3. **Iterative unwrapping** - Loop until reaching non-container node
4. **Hierarchy-aware edges** - Edges carry metadata about containment levels

Hypergraph previously implemented a recursive DFS approach (commit efd7504) that successfully handled double-nesting cases like `outer → middle → inner → process`. This implementation was later reverted, suggesting the special-case logic didn't fully generalize.

The recursive approach is the most suitable for hypergraph because:
- Works directly with existing graph structure (no tree building required)
- Naturally handles arbitrary nesting depth
- Expansion-state aware (only recurses into open containers)
- Returns multiple targets when needed (parameter consumed by multiple nodes)
- Low implementation complexity

The reverted implementation had the right algorithmic structure but likely failed due to incomplete edge type coverage, depth parameter confusion, or missing fallback handling for edge cases.

## Key Findings

**Stack:** Python backend rendering to React Flow JSON; JavaScript layout using custom constraint-based algorithm
**Architecture:** Recursive depth-first search to resolve logical node IDs to visual node IDs for edge rendering
**Critical pitfall:** Mixing logical IDs (from graph definition) with visual IDs (for rendering) causes edges to terminate at container boundaries instead of inner nodes

## Code Smells (Root Cause)

The regression stems from systemic code quality issues:

1. **Recursive depth as magic number** — Manual `remaining_depth` parameter threading instead of tree traversal abstraction
2. **Coordinate system confusion** — 4 different coordinate spaces with manual arithmetic scattered throughout
3. **Python/JavaScript duplication** — Hierarchy traversal implemented twice differently
4. **Special-case proliferation** — Each nesting level adds new conditional branches

**Why single-level fix failed at double-level:** The fix was a special case (`if depth > 1`) not a general solution. Adding depth=2 required another special case.

**Refactoring priority:**
1. Hierarchy traversal abstraction (eliminate manual depth)
2. Coordinate transformation types (eliminate arithmetic scatter)
3. Edge routing unification (single source of truth)

## Implications for Roadmap

The fix requires a **unified algorithm** that works at any depth, not special cases for depth=1 vs depth=2.

### Recommended Implementation Phases

**Phase 1: Core Recursive Resolution**
- Implement `find_visual_target(logical_id, expansion_state, max_depth)` function
- Implement `find_visual_source(logical_id, output_name, expansion_state, max_depth)` function
- Add safety limits (max recursion depth)
- Handle empty results (fallback to container node)

**Phase 2: Integration with All Edge Types**
- Apply to INPUT → node edges
- Apply to node → node edges
- Apply to node → OUTPUT edges (if rendered)
- Store both logical and visual IDs in edge data

**Phase 3: Expansion State Handling**
- Only recurse into expanded GraphNodes
- Test partial expansion (outer open, inner closed)
- Handle dynamic expansion/collapse correctly

**Phase 4: Test Coverage**
- Test depth=0 (collapsed), depth=1, depth=2, depth=3+
- Test partial expansion scenarios
- Test multiple targets (parameter consumed by multiple deep nodes)
- Test edge cases (empty graphs, circular references)

**Phase ordering rationale:**
- Phase 1 establishes the core algorithm pattern (recursive + safety limits)
- Phase 2 ensures no edge type is left with special-case logic
- Phase 3 handles the dynamic nature (expansion changes visual routing)
- Phase 4 validates correctness across all scenarios

**Research flags for phases:**
- Phase 1: Unlikely to need deeper research (algorithm pattern is clear from examples)
- Phase 2: May need research if OUTPUT edges have special rendering logic
- Phase 3: May need research into React Flow's expansion state management
- Phase 4: Standard testing patterns, no additional research needed

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Algorithm Pattern | HIGH | Multiple professional implementations demonstrate recursive DFS works |
| Implementation Approach | HIGH | Previous commit efd7504 shows working Python implementation |
| Root Cause of Reversion | MEDIUM | Commit message doesn't specify exact failure mode, but likely edge type coverage |
| React Flow Integration | HIGH | Existing codebase shows clear separation of logical vs visual node IDs |

## Gaps to Address

1. **Why exactly was efd7504 reverted?** - Need to test original implementation to identify failure modes
2. **OUTPUT edge handling** - Does hypergraph render OUTPUT nodes? If so, do they need special visual resolution logic?
3. **Performance at depth=5+** - Recursive algorithm should handle it, but test performance with deeply nested graphs
4. **Circular reference detection** - Max depth provides safety, but should we explicitly detect cycles?

These gaps should be addressed during implementation, not as blocking research questions. The core algorithm pattern is well-established.
