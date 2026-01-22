# Pour Preview: Fix Visualization Layout and Edge Routing

**Spec Tasks:** 16
**Implementation Tasks:** 32 (after breakdown)
**Formula:** choo-choo-ralph
**Mode:** Workflow formula (6 steps per task)

---

## Phase 1: Collapse Edge Routing (Priority 0-1) - 18 tasks

### From: verify-collapse-tests-fail (Priority 0, infrastructure)
| # | Task | Test Steps |
|---|------|------------|
| 1 | Run existing collapse tests and document failures | 3 steps |

### From: test-node-to-parent-map (Priority 0, infrastructure)
| # | Task | Test Steps |
|---|------|------------|
| 2 | Create failing test for node_to_parent map in renderer | 4 steps |

### From: implement-node-to-parent-map (Priority 0, functional)
| # | Task | Test Steps |
|---|------|------------|
| 3 | Implement _build_node_to_parent_map function | 5 steps |
| 4 | Integrate node_to_parent map into render_graph meta output | 4 steps |

### From: test-node-to-parent-in-js (Priority 0, infrastructure)
| # | Task | Test Steps |
|---|------|------------|
| 5 | Create Playwright test for node_to_parent in debug API | 4 steps |

### From: implement-node-to-parent-in-js (Priority 0, functional)
| # | Task | Test Steps |
|---|------|------------|
| 6 | Add node_to_parent to routingData in app.js | 4 steps |
| 7 | Expose node_to_parent in debug API | 3 steps |

### From: implement-collapse-fix (Priority 1, functional)
| # | Task | Test Steps |
|---|------|------------|
| 8 | Extract nodeToParent from routingData in layout.js | 3 steps |
| 9 | Implement findVisibleAncestor helper function | 5 steps |
| 10 | Fix INPUT edge target routing for collapse case | 6 steps |
| 11 | Fix data edge source routing for collapse case | 6 steps |
| 12 | Verify all collapse tests pass | 4 steps |

### From: test-multi-level-collapse (Priority 1, infrastructure)
| # | Task | Test Steps |
|---|------|------------|
| 13 | Add TestOuterInteractiveCollapse test class | 5 steps |
| 14 | Add test_outer_collapse_inner_routes_to_container | 5 steps |
| 15 | Add test_outer_collapse_middle_routes_to_container | 5 steps |

### From: fix-multi-level-collapse (Priority 1, functional)
| # | Task | Test Steps |
|---|------|------------|
| 16 | Verify multi-level collapse works or fix if needed | 5 steps |

---

## Phase 2: Graph Clipping (Priority 2) - 5 tasks

### From: test-clipping (Priority 2, infrastructure)
| # | Task | Test Steps |
|---|------|------------|
| 17 | Create test_viewport_clipping.py with large graph test | 6 steps |

### From: diagnose-clipping (Priority 2, infrastructure)
| # | Task | Test Steps |
|---|------|------------|
| 18 | Add debug logging to fitWithFixedPadding | 4 steps |
| 19 | Analyze bounds calculation and document root cause | 5 steps |

### From: fix-clipping (Priority 2, functional)
| # | Task | Test Steps |
|---|------|------------|
| 20 | Implement viewport bounds safety checks | 6 steps |
| 21 | Verify clipping fix works for all graph sizes | 5 steps |

---

## Phase 3: Edge Crossing (Priority 3) - 5 tasks

### From: test-edge-crossing (Priority 3, infrastructure)
| # | Task | Test Steps |
|---|------|------------|
| 22 | Add test_no_edge_node_crossings in test_edge_connections | 6 steps |

### From: diagnose-edge-crossing (Priority 3, infrastructure)
| # | Task | Test Steps |
|---|------|------------|
| 23 | Add debug logging to routing function | 4 steps |
| 24 | Analyze edge routing patterns and document findings | 5 steps |

### From: fix-edge-crossing (Priority 3, functional)
| # | Task | Test Steps |
|---|------|------------|
| 25 | Update blocking detection in routing algorithm | 6 steps |
| 26 | Verify edge routing fix and no regressions | 5 steps |

---

## Phase 4: Validation and Cleanup (Priority 4) - 6 tasks

### From: run-all-tests (Priority 4, functional)
| # | Task | Test Steps |
|---|------|------------|
| 27 | Run full collapse test suite | 4 steps |
| 28 | Run full viz test suite for regression check | 4 steps |

### From: cleanup-debug-logging (Priority 4, infrastructure)
| # | Task | Test Steps |
|---|------|------------|
| 29 | Remove debug logging from layout.js | 3 steps |
| 30 | Remove debug logging from app.js | 3 steps |
| 31 | Remove debug logging from constraint-layout.js | 3 steps |
| 32 | Final verification - all tests pass, no console spam | 4 steps |

---

## Summary

| Phase | Spec Tasks | Implementation Tasks | Priority |
|-------|------------|---------------------|----------|
| 1: Collapse | 8 | 16 | 0-1 |
| 2: Clipping | 3 | 5 | 2 |
| 3: Edge Crossing | 3 | 5 | 3 |
| 4: Validation | 2 | 6 | 4 |
| **Total** | **16** | **32** | - |

**Total beads to create:** 32 tasks × 6 formula steps = 192 beads
