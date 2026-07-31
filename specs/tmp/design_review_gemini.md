# Design Review: Hypergraph Framework

## Executive Summary

The `hypergraph` design presents a clean, "pure python" approach to graph-based workflows, differentiating itself with the **"Outputs ARE State"** philosophy. This significantly reduces boilerplate compared to LangGraph's explicit `TypedDict` state schemas. The separation of **Structure**, **State** (Runtime), and **Durability** is architecturally sound.

However, the current design harbors specific risks regarding **long-running processes (infinite loops)**, **type safety/refactoring**, and **production schema evolution**. While the API is elegant for "one-off" complex flows (like RAG), it may struggle with "daemon" agents or evolving production systems without additional mechanisms.

---

## 🚨 Critical Flaws & Risks

### 1. The "Infinite History" Problem (Storage Leak)
**Issue:** The design relies on `StepRecords` as the "implicit cursor" and source of truth (`checkpointer.md`). For cyclical graphs (agents loops) or long-running daemons, the list of `StepRecords` grows monotonically.
**Impact:** A "monitor and alert" agent running for weeks will generate millions of step records.
*   **Performance:** `get_state()` (even with snapshots) requires querying the index.
*   **Storage:** Database bloat.
*   **Context Window:** If "history" is passed to LLMs, it must be manually truncated, but the *system* history remains forever.
**Recommendation:** Introduce **Step Pruning / Log Compaction**. Allow a graph to define a "Window" or "TTL" for steps, or a mechanism to "squash" history into a new baseline snapshot, discarding old steps.

### 2. The "Deadlock by Skipping" Edge Case
**Issue:** Nodes execute when *all* inputs are available. `GateNode`s route flow, effectively "skipping" the non-selected paths.
**Scenario:**
*   Node D requires inputs from Node B and Node C.
*   A Gate routes execution to Node B (skipping Node C).
*   Node B executes. Node C *never* executes.
*   **Result:** Node D waits forever for Node C's output. The workflow hangs in a "running" state but makes no progress.
**Recommendation:** Introduce **Optional Inputs** or **Skip Propagation**. If a branch is skipped, the runner must propagate a "Signal: Skipped" token to downstream consumers so they know not to wait (or receive `None`).

### 3. "String-ly" Typed Wiring & Refactoring Fragility
**Issue:** The API relies heavily on string literals for wiring: `node.with_inputs(param="upstream_name")`.
**Impact:**
*   **Refactoring:** Renaming a node function doesn't automatically update the string references in `with_inputs`.
*   **Type Safety:** No build-time check ensures that `upstream_name` (Output: `int`) matches `param` (Input: `str`).
**Recommendation:**
*   Investigate **Typed References**: Allow passing `Node` objects directly to inputs (e.g., `node_b.bind(x=node_a.outputs.result)`).
*   Add a static analysis tool or "Graph Linter" that validates wiring types before execution.

---

## ⚠️ Reliability & Production Readiness

### 4. Schema Evolution & Versioning (The "Graph Hash" Trap)
**Issue:** `Workflow` includes a `graph_hash` to detect mismatches. The spec implies that if the code changes, the hash changes, triggering a `VersionMismatchError`.
**Impact:** In production, you *will* change code (fix a prompt, optimize a function) while workflows are in-flight. Failing/Blocking all resumes on a minor code change is unacceptable.
**Recommendation:**
*   **Semantic Versioning:** Allow developers to explicitly version graphs (`v1`, `v2`).
*   **Allow-list Changes:** Distinguish between "Logic Changes" (safe to resume) and "Topology/IO Changes" (unsafe).
*   **Migration Strategies:** Define how to map old state to new nodes if the topology changes.

### 5. Serialization Safety
**Issue:** Defaulting to JSON is safe but limited. Allowing `Pickle` is a security non-starter for many enterprises.
**Impact:** Users will inevitably return custom classes, `numpy` arrays, or `datetime` objects, causing JSON serialization failures or silent data corruption (e.g., datetime becoming string).
**Recommendation:**
*   **Enforce Pydantic:** Strongly recommend or enforce that node inputs/outputs be Pydantic models. This handles serialization/validation automatically and provides schema stability.
*   **Pluggable Codecs:** Explicitly support safe binary serializers (e.g., `msgpack`, `protobuf`) rather than generic Pickling.

---

## 💡 Missing Features ("Zooming Out")

### 1. "Batteries Included" Agent Memory
**Observation:** The spec says "For cross-workflow memory... use an external store."
**Gap:** Most users want a simple way to "remember user name" across sessions without spinning up a separate Redis/Postgres table manually.
**Idea:** Add a `Store` or `Memory` interface (distinct from Checkpointer) for long-term, semantic, or KV storage available to all nodes.

### 2. Rate Limiting & Throttling
**Observation:** LLM-based graphs hit API limits immediately.
**Gap:** No mention of rate limiting in `runners.md`.
**Idea:** Add `@node(rate_limit="10/minute")` or runner-level throttling policies.

### 3. Testing Harness
**Observation:** How does a user test a 10-step graph without running all 10 steps (and paying LLM costs)?
**Gap:** No mocking utilities mentioned.
**Idea:** Provide a `MockRunner` or `graph.test_mode()` that allows injecting mock outputs for specific nodes (`values={"node_name": mock_val}`).

### 4. Priority Queues
**Observation:** `AsyncRunner` processes tasks.
**Gap:** In a busy system, "User Interrupt Response" should take precedence over "Background Vector Indexing".
**Idea:** Priority levels for Runs or Nodes.

---

## 🧩 Comparison & Inspiration

| Feature | Hypergraph (Current) | LangGraph | PydanticAI / Mastra |
| :--- | :--- | :--- | :--- |
| **State** | Implicit (Outputs) | Explicit (`TypedDict`) | Models / Context |
| **Wiring** | String keys | Edges / Channels | Dependency Injection |
| **Durability**| Atomic Steps | Checkpoints | Varies |
| **DX** | Decorators | Class-based | Type-safe Builders |

**Inspiration from PydanticAI:**
*   Use **Dependency Injection** for context/state. Instead of mapping strings, allow nodes to request `ctx: RunContext` to access run-level info, limiting the need for "magic" string passing.

**Inspiration from Temporal.io:**
*   **Signal Channels:** Instead of just "InterruptNode", allow sending *signals* to a running workflow at any time (e.g., "Cancel", "UpdateConfig") without it explicitly waiting at a node.

---

## ✅ Action Plan

1.  **Solve the Deadlock:** Update `Runner` logic to handle "Skipped" propagation.
2.  **Fix Storage Leak:** Design a "History Pruning" policy for `Checkpointer`.
3.  **Harden Serialization:** Integrate Pydantic serialization by default.
4.  **Improve DX:** Add `graph.visualize()` (Mermaid export) to help debug string-wiring.
5.  **Refine Versioning:** Move away from strict `graph_hash` to a more lenient compatibility check.
