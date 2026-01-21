---
phase: 01-core-abstractions
plan: 01
subsystem: viz
tags: [networkx, visualization, coordinates, traversal, duck-typing]

# Dependency graph
requires:
  - phase: research
    provides: viz edge routing research and roadmap
provides:
  - Graph.to_viz_graph() method for NetworkX conversion
  - traversal.py with graph traversal utilities
  - coordinates.py with coordinate space transformations
affects: [02-layout-algorithm, 03-edge-routing]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - Duck typing for node type classification (hasattr checks)
    - Immutable dataclasses for coordinate transformations
    - Recursive flattening of nested graphs

key-files:
  created:
    - src/hypergraph/viz/traversal.py
    - src/hypergraph/viz/coordinates.py
  modified:
    - src/hypergraph/graph/core.py
    - src/hypergraph/viz/__init__.py

key-decisions:
  - "Use duck typing (hasattr) instead of isinstance for node type classification to avoid import dependencies"
  - "Frozen dataclasses for Point and CoordinateSpace to ensure immutability"
  - "Recursive flattening embeds children with parent references in single NetworkX graph"

patterns-established:
  - "Node type classification: hasattr('graph') → PIPELINE, hasattr('targets') → BRANCH, else FUNCTION"
  - "Coordinate transformations: local → parent → absolute → viewport"
  - "Traversal with predicates: depth-based expansion control"

# Metrics
duration: 4min
completed: 2026-01-21
---

# Phase 1 Plan 1: Create foundation abstractions for viz decoupling

**NetworkX conversion with duck-typed node classification, hierarchical traversal, and coordinate space transformations**

## Performance

- **Duration:** 4 min
- **Started:** 2026-01-21T13:46:04Z
- **Completed:** 2026-01-21T13:50:01Z
- **Tasks:** 3
- **Files modified:** 4

## Accomplishments

- Added `Graph.to_viz_graph()` method that converts hypergraph to NetworkX DiGraph with full visualization metadata
- Created traversal utilities for depth-based graph expansion with predicates
- Implemented coordinate space transformations for hierarchical layout positioning

## Task Commits

Each task was committed atomically:

1. **Task 1: Add to_viz_graph() method to Graph class** - `36f81a8` (feat)
   - Added method returning nx.DiGraph with viz attributes
   - Duck-typed node classification (hasattr checks)
   - Recursive flattening of nested graphs with parent references

2. **Task 2: Create traversal.py** - `2457bf4` (feat)
   - get_children() for finding direct children
   - traverse_to_leaves() for recursive traversal
   - build_expansion_predicate() for depth limits

3. **Task 3: Create coordinates.py** - `38ebcdd` (feat)
   - Point dataclass with add/subtract operations
   - CoordinateSpace with hierarchical transformations
   - layout_to_absolute() utility function

4. **Update viz module exports** - `048b64f` (feat)

## Files Created/Modified

- `src/hypergraph/graph/core.py` - Added to_viz_graph() and helper methods
- `src/hypergraph/viz/traversal.py` - Graph traversal utilities
- `src/hypergraph/viz/coordinates.py` - Coordinate space transformations
- `src/hypergraph/viz/__init__.py` - Export new modules

## Decisions Made

1. **Duck typing for node classification**: Used `hasattr(node, 'graph')` instead of `isinstance(node, GraphNode)` to avoid import dependencies and maintain loose coupling between graph core and node types.

2. **Frozen dataclasses**: Made Point and CoordinateSpace immutable to prevent accidental modifications during coordinate transformations.

3. **Single flattened graph**: Recursive flattening embeds all nested children in one NetworkX graph with parent references, rather than maintaining separate graph objects.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None

## Next Phase Readiness

Foundation abstractions complete and tested:
- ✅ to_viz_graph() tested with simple and nested graphs
- ✅ All viz tests passing (24/24)
- ✅ Ready for Phase 2: Unified Layout Algorithm

---
*Phase: 01-core-abstractions*
*Completed: 2026-01-21*
