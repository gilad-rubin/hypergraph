# Adversarial Design Review: Hypergraph Specifications

**Date:** 2025-12-24
**Scope:** Review of `specs/reviewed/*.md` (11 files)
**Objective:** Identify flaws, loopholes, and risks in the current design that could hinder production readiness for complex data/scientific workflows.

---

## 1. Executive Summary

Hypergraph presents a compelling "outputs are state" model that significantly simplifies the developer experience compared to explicit state-machine frameworks like LangGraph. The strict separation of concerns—Checkpointer for durability, EventProcessor for observability, and Runners for execution—is designed well.

However, the **"All-or-Nothing" persistence policy** and the **lack of a defined versioning strategy** are critical risks for production systems, especially in data-heavy or long-lived workflow contexts. Furthermore, the feature disparity between `AsyncRunner` and `DaftRunner` creates a portability trap where "working locally" does not guarantee "scaling distributedly."

---

## 2. Critical Flaws & Risks

### 2.1. The "All-or-Nothing" Persistence Trap
**Severity:** Critical
**Context:** `EXECUTION-TYPES-FIXES.md` explicitly states: "Added explicit policy: 'All outputs are persisted'. Clarified no selective persistence."
**The Problem:**
In scientific and data pipelines, nodes often pass large objects (DataFrames, Tensors, huge JSON blobs) that are ephemeral intermediate states. Persisting *everything* is untenable:
1.  **Performance:** Serializing/writing GBs of intermediate data to Postgres/SQLite for every step will destroy throughput.
2.  **Storage Costs:** Massive storage bloat for data that is never needed for resumption (only the final result or specific checkpoints matters).
3.  **Privacy/Compliance:** You may strictly *not* want to persist PII/sensitive data that flows through intermediate nodes, even if you want to checkpoint the final workflow state.

**Adversarial Scenario:** A user builds a pipeline processing 4K video frames. Each node outputs a processed frame. The Checkpointer attempts to serialize and write every frame to the DB. The system halts due to I/O bottlenecks or the DB explodes.

**Recommendation:** Revert the "no selective persistence" decision. Allow `FunctionNode(..., persist=False)`. Most users need "Resume on Crash", which only requires persisting the *inputs* of the crashed node (recoupling from previous outputs) or explicit checkpoints, not every single intermediate value.

### 2.2. The "Schema-less" State Evolution Nightmare
**Severity:** High
**Context:** `state-model.md`: "Outputs ARE State... No explicit state schema."
**The Problem:**
While convenient for prototyping, implicit contracts are dangerous in long-lived production systems.
1.  **Refactoring Safety:** If Node A changes output `x` to `x_v2`, Node B (independent component) expecting `x` breaks at runtime. There is no central definition to audit.
2.  **Versioning:** If I change Node A's output format today, what happens to a workflow `order-123` currently "paused" at Step 1? When it resumes and runs Step 2, does Step 2 receive the old checkpointed format or is it expected to handle the new one?
3.  **Type drift:** Without a schema, `Strict` typing relies entirely on runtime checks, which is too late for production pipelines.

**Adversarial Scenario:** A long-running "Approval" workflow is paused for 2 weeks. In the meantime, the developer renames a field in the output of the node *preceding* the interrupt. When the user clicks "Approve", the workflow resumes, loads the *old* output from the checkpoint, passes it to the *new* code, and crashes with a `KeyError`.

**Recommendation:** Introduce a `GraphSchema` or `Interface` contract for critical boundaries, or at minimum, a "Migration/Version" strategy for handling in-flight workflows when code changes.

### 2.3. Runner Parity & The Portability Illusion
**Severity:** High
**Context:** `runners-api-reference.md`: `DaftRunner` supports "Distributed" but NO cycles, NO gates, NO interrupts, NO streaming.
**The Problem:**
The distinct feature sets imply that `Graph` definitions are not actually portable. A graph written with `InterruptNode` or a logical feedback loop (cycle) *cannot* be scaled to `DaftRunner`. This bifurcates the ecosystem: "Complex Logic Graphs" (AsyncRunner) vs "Data Pipelines" (DaftRunner).
If a user writes a complex agentic loop locally (`AsyncRunner`), they cannot simply "swap runner" to scale it up.

**Recommendation:** Make this constraint extremely loud. Fail fast at build time (which seems to be the case with `validate_runner_compatibility`). Ideally, implement a "Shim" or "Hybrid" runner that can execute "Data Parts" on Daft and "Control Flow Parts" locally, though that is complex.

---

## 3. Major Concerns

### 3.1. `map` vs Interrupts Incompatibility
**Context:** `runners.md`: "map() is incompatible with InterruptNode."
**The Problem:**
Many real-world "human-in-the-loop" use cases are batch-oriented. "Review these 50 flagged transactions." Parallel mapping is the natural way to process them. Disallowing interrupts inside `map` forces users to write manual `for` loops, killing concurrency and performance benefits of the Runner, or forces them to rethink their entire graph architecture to pull the "review" step out of the map (which is hard if the review depends on intermediate mapped computation).

**Adversarial Scenario:** User wants to use `HyperGraph` for a content moderation queue. They `map` over 1000 items. If the model is uncertain, it should `Interrupt` for human review. System throws `GraphConfigError`. User abandons framework.

**Recommendation:** Support `Interrupt` in `map` by pausing strictly the specific mapped item's "lane", while others continue. This is complex but high-value.

### 3.2. Implicit `workflow_id` Scoping
**Context:** `persistence.md`: "Just run with the same workflow_id... Automatic load-and-save."
**The Problem:**
It's too easy to accidentally "resume" (and mutate) an existing workflow when one intended to start fresh, or vice-versa, just by reusing a string ID.
Furthermore, there is no mention of **locking**. What if two processes call `runner.run(..., workflow_id="same")` simultaneously?
- Does the checkpointer lock the row?
- Do they overwrite each other's steps?
- `sqlite` might lock file, `postgres` needs explicit row locking.

**Recommendation:**
1.  Verify locking behavior in Checkpointer implementations.
2.  Consider explicit `resume=True` vs `create=True` flags for stricter safety, rather than purely implicit "create if missing, resume if exists".

### 3.3. Serialization Complexity
**Context:** `checkpointer.md`.
**The Problem:**
If "All outputs are state", then "All outputs must be serializable."
For `sqlite`/`postgres`, this usually means JSON or Pickling.
- **Pickle** is insecure and fragile across python versions.
- **JSON** restricts data types/requires custom encoders for everything (datetime, numpy, etc.).
- If a user passes a database connection object or a file handle between nodes (common in simple scripts), the Checkpointer will explode.

**Recommendation:** Clear error messages and "Best Practices" for data passing. Maybe supporting `CloudPickle` for development but forcing stricter serialization for production.

---

## 4. Minor Observations & Nits

1.  **Terminology:** "Superstep" vs "Step Index" vs "at_step". The specific mechanics of "folding" steps vs storing snapshots could use a diagram.
2.  **Ghost Runners:** `DBOSAsyncRunner` depends on an external `DBOS` object synchronization. It feels like "Action at a distance."
3.  **Observability hierarchy:** The distinction between `span_id` and `parent_span_id` is good, but does the `Checkpointer` store this? If I resume a workflow, is the span hierarchy preserved for the resumed/new steps to link back to old traces?

---

## 5. Conclusion & Verdict

**Verdict:** The core architecture is solid for **Application Logic** (Agents, Chatbots, simple automation). However, it is **currently immature for Heavy Data/Scientific Pipelines** due to the "Persist Everything" policy and Runner fragmentation.

**Immediate Actions Required:**
1.  **Allow Selective Persistence:** `persist=False` is a must-have for data inputs/outputs.
2.  **Define Schema/Versioning Story:** How to handle graph changes over time.
3.  **Locking Strategy:** ensure `workflow_id` collisions don't cause data corruption.
