# Consolidated Adversarial Review: Hypergraph Specifications

**Synthesized from:** Gemini, GPT-4, and Opus reviews
**Date:** 2026-01-03
**Purpose:** Early-stage design issue detection
**Status:** PARTIALLY RESOLVED - Updated 2026-01-06

---

## Resolution Summary

The following issues have been **resolved** via recent commits:

| Issue | Resolution | Commit |
|-------|------------|--------|
| Persistence policy contradiction | Removed `persist` parameter, all outputs persisted | 5214d84, 16d2f1f |
| Cycles + resume semantics | Added `input_versions` tracking, unified algorithm | 5214d84 |
| Schema versioning | Added `force_resume` and `VersionMismatchError` | f95d9d2 |
| Step persistence atomicity | Merged Step + StepResult into atomic `StepRecord` | cda9b34 |
| Terminology inconsistency | Unified naming (values, superstep) | 70df943, 09dc566 |
| complete_on_stop issues | Added `complete_on_stop` to Graph | 09dc566, ae8c4ba |
| Event ordering underspecified | Added "Event Ordering Guarantees" section | (pending) |
| EventProcessor failure semantics | Defined failure/backpressure semantics for EventProcessor | (pending) |

---

## Remaining Issues

### Consensus Issues (Found by 2+ Reviewers)

| Issue                                    | Gemini | GPT | Opus |   Severity   |
| ---------------------------------------- | :----: | :-: | :--: | :----------: |
| Workflow ID locking/concurrency          |   ✓   | ✓  |  -   |   **HIGH**   |
| DBOS streaming limitations               |   -   | ✓  |  ✓  |  **MEDIUM**  |
| ~~Event ordering underspecified~~        |   -   | ✓  |  ✓  |  ~~MEDIUM~~  |
| State reconstruction scalability         |   -   | ✓  |  -   |   **HIGH**   |

---

## Part 1: High-Severity Issues (Remaining)

### 1.1 Workflow ID Concurrency/Locking

**Consensus:** Gemini and GPT
**Location:** `persistence.md`

**The Problem:**
"Just run with same workflow_id to resume" is too magical:

1. **No locking specified**: Two workers calling `run(..., workflow_id="same")` concurrently → undefined behavior
2. **No intent distinction**: Accidental ID reuse mutates existing workflow instead of creating new

**Questions That Need Answers:**

- Does the checkpointer lock the row?
- SQLite might lock file; Postgres needs explicit row locking
- What about optimistic concurrency (version numbers)?

**Recommendation:**

1. Specify locking semantics for each checkpointer implementation
2. Consider explicit mode: `mode="create|resume|create_or_resume"`
3. Or separate APIs: `runner.run()` vs `runner.resume()`

---

## Part 3: Unique Insights by Reviewer

### From Gemini (Production Experience Lens)

- **4K video frame adversarial scenario** - Excellent stress test for "persist everything"
- **Privacy/compliance angle** - PII in intermediate nodes may be legally unpersistable
- **SQLite vs Postgres locking difference** - Implementation-specific concern often missed

### From GPT (Spec Consistency Lens)

- **Nested RunResult reconstruction** - Does `get_state()` eagerly query children?
- **`strict_types` is placeholder** - What does it actually check?

### From Opus (Edge Case Lens)

- **`InputSpec.bound` mutable dict in frozen dataclass** - Subtle immutability leak
- **Generator accumulation for non-string chunks** - `yield {"metadata": "?"}` undefined
- **`history=` parameter for forking** - Two workflows sharing history prefix breaks append-only semantics conceptually

---

## Part 4: Prioritized Action Items

### ~~Immediate (Block v1.0)~~ ✅ RESOLVED

All critical v1.0 blockers have been addressed:
- ~~Resolve persistence policy~~ → Removed `persist` param (5214d84)
- ~~Fix cycle execution algorithm~~ → Added `input_versions` (5214d84)
- ~~Add graph versioning~~ → Added `force_resume` + `VersionMismatchError` (f95d9d2)
- ~~Specify step atomicity~~ → Atomic `StepRecord` type (cda9b34)
- ~~Unify terminology~~ → Standardized naming (70df943, 09dc566)

### Before Production Release (Remaining)

1. **Define workflow locking** - Per-checkpointer concurrency semantics
2. **Serialization guidance** - Pickle warning, production serializers, large blob strategy
3. **State materialization plan** - Snapshots for O(1) reconstruction
4. ~~**Event ordering rules** - Formal happens-before for parallel execution~~ ✅
5. **Nested interrupt namespacing** - Prevent resume key collisions

### Design Considerations (Future)

6. **Runner compatibility matrix** - Make feature gaps loud and early
7. **`map()` + interrupts** - Per-item lane pausing
8. **Schema evolution/migrations** - Beyond fail-fast detection
9. **Retry policies** - Node-level exponential backoff
10. **Transaction/saga support** - Compensating actions for side effects

---

## Appendix: Cross-Reference Matrix

| Spec File                  | Remaining Issues                           | Resolved Issues                              |
| -------------------------- | ------------------------------------------ | -------------------------------------------- |
| `durable-execution.md`     | DBOS streaming                             | ~~Persistence policy~~, ~~complete_on_stop~~ |
| `execution-types.md`       | Event payloads                             | ~~Terminology~~, ~~cycles~~                  |
| `checkpointer.md`          | Serialization, scalability                 | ~~Atomicity~~                                |
| `persistence.md`           | Workflow locking, history param            | ~~at_step terminology~~                      |
| `runners.md`               | Feature parity, map+interrupts, DBOS streaming |                                          |
| `graph.md`                 | Namespace collisions, nested access        | ~~Cycle validation~~                         |
| `node-types.md`            | TypeRouteNode precedence                   | ~~persist flag contradiction~~               |
| `state-model.md`           | Implicit state                             | ~~Schema-less evolution~~                    |
| `observability.md`         | —                                          | ~~persist contradiction~~, ~~processor failures~~ |
| `runners-api-reference.md` | DBOS streaming                             | ~~inputs/values~~                            |

---

## Conclusion

~~The three reviews converge on the same critical issues, which validates their severity.~~ **Update (2026-01-06):** All critical v1.0 blockers have been resolved.

The framework's core "outputs ARE state" philosophy is elegant and the specs are now internally consistent.

**Remaining work** focuses on production hardening:
- Workflow locking semantics
- Serialization guidance for large/sensitive data
- State materialization for performance
- ~~Event ordering guarantees~~ ✅
