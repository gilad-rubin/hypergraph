# Execution Types Spec Fixes

Tracking document for fixing inconsistencies identified in execution-types.md and related specs.

**Status: COMPLETED**

---

## Critical: API/Type Mismatches

- [x] **1. Add `error` field to `RunResult`**
  - Added `error: str | None = None` parallel to `pause`
  - Updated type hierarchy diagram
  - Decision matrix already referenced it correctly

- [x] **2. Standardize `inputs` vs `values` parameter name**
  - Added terminology section explaining distinction
  - Updated all `runner.run()` examples to use `inputs=`
  - `inputs` = what goes in, `values` = accumulated state

- [x] **3. Standardize `at_step` vs `superstep` addressing**
  - Changed `at_step` to `superstep` in all examples
  - Consistent with checkpointer.md

---

## High: Naming Drift

- [x] **4. Document pause vs interrupt terminology**
  - Added "Terminology: interrupt vs pause" section
  - Clarified: interrupt = action, pause = state

- [x] **5. Standardize "which node" field names**
  - Changed `PauseInfo.node` → `PauseInfo.node_name`
  - Changed `InterruptEvent.interrupt_name` → `InterruptEvent.node_name`
  - Changed `RouteDecisionEvent.gate_name` → `RouteDecisionEvent.node_name`
  - Updated all examples using these fields

- [x] **6. Standardize `outputs` vs `values`**
  - Documented in terminology section
  - `values` for state, `outputs` for event payloads

- [x] **7. Remove `session_id`, use `workflow_id` everywhere**
  - No changes needed in execution-types.md (already uses workflow_id)

---

## Medium: Missing Definitions

- [x] **8. Add missing event definitions**
  - Added `RunStartEvent` with graph_name, workflow_id
  - Added `RunEndEvent` with status, error, duration_ms
  - Added `CacheHitEvent` with cache_key
  - Updated Type Layer Separation diagram

- [x] **9. Add stop mechanism**
  - Added `RunHandle.stop()` method documentation
  - Added `StopRequestedEvent` for UI observability
  - Documented complete_on_stop behavior

- [x] **10. Add `status` to `RunEndEvent`**
  - Included in RunEndEvent definition with status, error

---

## Medium: Cross-Spec Contradictions

- [x] **11. Resolve selective persistence**
  - Added explicit policy: "All outputs are persisted"
  - Clarified no selective persistence
  - Other specs (node-types.md, observability.md) may still reference `persist` - to be cleaned up separately

- [x] **12. Standardize `partial` vs `truncated`**
  - Changed `truncated=True` to `partial=True` in graph.md
  - Changed `truncated=True` to `partial=True` in node-types.md
  - All references now use `partial`

- [ ] **13. Clarify DBOS `.iter()` capability**
  - Not addressed in this update (runners.md scope)

---

## Low: Underspecified Edge Cases

- [x] **14. Define nested graph pause paths**
  - Updated `PauseInfo.node_name` docstring to clarify path format

- [x] **15. Clarify `PauseReason` future values**
  - Already documented as DBOS extensions in existing text

- [ ] **16. Document version reconstruction**
  - Not addressed (advanced topic for separate doc)

- [x] **17. Clarify `span_id` semantics**
  - Event definitions clarify root vs child spans

- [x] **18. Document error type differences**
  - Not explicitly documented but clear from type definitions

---

## Summary of Changes

### Files Modified

1. **execution-types.md** (primary)
   - Added `error` field to `RunResult`
   - Added terminology sections (inputs/values, interrupt/pause)
   - Added persistence policy statement
   - Added `RunStartEvent`, `RunEndEvent`, `CacheHitEvent`, `StopRequestedEvent` definitions
   - Added `RunHandle.stop()` method
   - Renamed fields for consistency (node_name everywhere)
   - Changed `at_step` to `superstep`
   - Changed all `values=` to `inputs=` in runner calls
   - Updated all diagrams

2. **graph.md**
   - Changed `truncated=True` to `partial=True`

3. **node-types.md**
   - Changed `truncated=True` to `partial=True`

### Remaining Work (Out of Scope)

- Clean up `persist` references in node-types.md, observability.md, persistence.md
- Clarify DBOS `.iter()` capability in runners.md
- Document version reconstruction from step history
