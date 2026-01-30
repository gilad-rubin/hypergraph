# Consolidated Red-Team Findings

> **Sources**: Jules ([PR #18](https://github.com/gilad-rubin/hypergraph/pull/18)), Claude Opus ([PR #23](https://github.com/gilad-rubin/hypergraph/pull/23)), Gemini (`red_team_gemini.md`), Codex (`new_codex.md`), AMP subfolder (`How to Red-Team a Codebase for Potential Issues/`)
>
> **Test files**: `test_critical_flaws.py`, `codex_tests_might_not_fail.py`, subfolder `test_red_team*.py`, `test_nodes_gate.py`, `test_routing.py`, `test_red_team_audit.py` (PR #23)
>
> **Resolution PRs**: [#25](https://github.com/gilad-rubin/hypergraph/pull/25) — fixes #1, #2, #4, #6, #9, #11 | [#27](https://github.com/gilad-rubin/hypergraph/pull/27) — fixes mutex rename

---

## Status Summary

| # | Issue | Severity | Status |
|---|-------|----------|--------|
| 1 | Mutable defaults shared | CRITICAL | ✅ Resolved (PR #25) |
| 2 | Cycle off-by-one | HIGH | ✅ Resolved (PR #25) |
| 3 | Runtime type checking | MEDIUM-HIGH | ⏸️ Deferred — by design |
| 4 | Intermediate injection | HIGH | ✅ Resolved (PR #25) |
| 5 | Disconnected nodes | HIGH | ⏸️ Deferred — design decision |
| 6 | Rename collision | MEDIUM | ✅ Resolved (PR #25) |
| 7 | Invalid gate target | MEDIUM | ✅ Already fixed |
| 8 | Mutex branch outputs | MEDIUM | ✅ Already fixed (PR #8) |
| 9 | Control-only cycles | MEDIUM | ✅ Resolved (PR #25) |
| 10 | Deep nesting / infinite loops | HIGH | 🔴 **OPEN** |
| 11 | Rename propagation | MEDIUM | ✅ Resolved (PR #25) |
| 12 | kwargs detection | LOW | ⏸️ Deferred — low priority |
| 13 | GraphNode output leakage | MEDIUM | 🔵 **OPEN** — from PR #23 |
| 14 | Stale branch values persist | MEDIUM | 🔵 **OPEN** — from PR #23 |
| 15 | Type subclass compatibility | LOW | 🔵 **OPEN** — from PR #23 |
| 16 | DiGraph drops parallel edges | MEDIUM | 🔵 **OPEN** — from PR #23 |
| 17 | Empty/edge-case graphs | LOW | 🔵 **OPEN** — from PR #23 |
| 18 | Routing edge cases (None, multi-target) | MEDIUM | 🔵 **OPEN** — from PR #23 |
| 19 | Bind/unbind edge cases | LOW | 🔵 **OPEN** — from PR #23 |
| 20 | Generator node behavior | LOW | 🔵 **OPEN** — from PR #23 |
| 21 | Complex type validation (Optional, generics) | LOW | 🔵 **OPEN** — from PR #23 |
| 22 | Superstep determinism | LOW | 🔵 **OPEN** — from PR #23 |
| 23 | Max iterations boundary | LOW | 🔵 **OPEN** — from PR #23 |
| 24 | GateNode property edge cases | LOW | 🔵 **OPEN** — from PR #23 |
| 25 | Select parameter filtering | LOW | 🔵 **OPEN** — from PR #23 |
| 26 | Map with empty list / zip mismatch | MEDIUM | 🔵 **OPEN** — from PR #18 extended |
| 27 | Nested graph bindings persistence | LOW | 🔵 **OPEN** — from PR #18 extended |
| 28 | Async exception propagation | MEDIUM | 🔵 **OPEN** — from PR #18 extended |
| 29 | Cyclic gate→END infinite loop | HIGH | 🔵 **OPEN** — from AMP (CY-005) |
| 30 | Type checking with renamed inputs | MEDIUM | 🔵 **OPEN** — from AMP (TC-010) |
| 31 | GraphNode name collision not detected | MEDIUM | 🔵 **OPEN** — from AMP (GN-008) |

---

## 🔴 OPEN — High Severity

### 10. Deep Nesting / Multiple GraphNodes — Infinite Loops

| | |
|---|---|
| **Severity** | HIGH |
| **Sources** | AMP (GN-001, GN-003, SM-007, GN-008) |

Several nesting scenarios cause `InfiniteLoopError` or hang:
- 4+ level nested graphs
- Two `GraphNode` instances from the same inner graph in one outer graph
- A node whose output has the same name as its input (staleness loop — SM-007)
- A `GraphNode` with the same name as another node in the outer graph (GN-008 — see also #31)

**Next steps**:
1. Write isolated failing tests for each scenario
2. Add tracing/logging to staleness detection during nested execution
3. Investigate if `GraphState.copy()` needs deeper isolation for nested runs

---

### 29. Cyclic Gate→END Infinite Loop

| | |
|---|---|
| **Severity** | HIGH |
| **Sources** | AMP (CY-005) |

**Problem**: A cyclic graph where a gate routes to `END` enters an infinite loop instead of terminating. Distinct from #2 (off-by-one, now fixed) — this is about the gate→END path hanging entirely in certain topologies.

---

## 🔵 OPEN — Medium Severity

### 13. GraphNode Output Leakage

| | |
|---|---|
| **Severity** | MEDIUM |
| **Sources** | PR #23 `TestGraphNodeOutputLeakage` |

**Problem**: GraphNode exposes all intermediate outputs to the outer graph, not just its declared leaf outputs. This can cause unintended wiring.

---

### 14. Stale Branch Values Persist

| | |
|---|---|
| **Severity** | MEDIUM |
| **Sources** | PR #23 `TestStaleBranchValues` |

**Problem**: When a routing gate deactivates a branch, values produced in a previous iteration by that branch remain in state, potentially being consumed by downstream nodes.

---

### 16. DiGraph Drops Parallel Edges

| | |
|---|---|
| **Severity** | MEDIUM |
| **Sources** | PR #23 `TestMultipleOutputs` |

**Problem**: NetworkX DiGraph drops parallel edges — when a node produces multiple outputs consumed by the same downstream node, only one edge is recorded.

---

### 18. Routing Edge Cases

| | |
|---|---|
| **Severity** | MEDIUM |
| **Sources** | PR #23 `TestRoutingEdgeCases` |

**Problem**: Route returning `None`, IfElse with non-bool values, END termination semantics, and multi-target routing have under-specified behavior.

---

### 26. Map with Empty List / Zip Length Mismatch

| | |
|---|---|
| **Severity** | MEDIUM |
| **Sources** | PR #18 `test_red_team_extended.py` |

**Problem**: `runner.map()` behavior with empty input lists and mismatched zip lengths may not be well-defined.

---

### 28. Async Exception Propagation

| | |
|---|---|
| **Severity** | MEDIUM |
| **Sources** | PR #18 `test_red_team_extended.py` |

**Problem**: Exception propagation in async map operations may not correctly surface errors from individual items.

---

### 30. Type Checking with Renamed Inputs

| | |
|---|---|
| **Severity** | MEDIUM |
| **Sources** | AMP (TC-010) |

**Problem**: `strict_types=True` type checking fails or produces false results when inputs/outputs have been renamed via `with_inputs()`/`with_outputs()`.

---

### 31. GraphNode Name Collision Not Detected

| | |
|---|---|
| **Severity** | MEDIUM |
| **Sources** | AMP (GN-008) |

**Problem**: A GraphNode with the same name as another node in the outer graph causes an infinite loop instead of a validation error at build time. Related to #10 but specifically about missing name-collision detection.

---

## 🔵 OPEN — Low Severity

| # | Area | Test Class | Notes |
|---|------|-----------|-------|
| 15 | Type subclass compatibility | `TestTypeCompatibilitySubclass` | `bool` → `int` not handled by strict_types |
| 17 | Empty/edge-case graphs | `TestEdgeCaseGraphs` | Empty graphs, side-effect-only nodes silently accepted |
| 19 | Bind/unbind | `TestBindEdgeCases` | Bind override precedence, unbind restoration |
| 20 | Generator nodes | `TestGeneratorNodes` | Sync generators collected as list |
| 21 | Complex types | `TestComplexTypeValidation` | Optional, generics with strict_types |
| 22 | Superstep determinism | `TestSuperstepDeterminism` | Parallel nodes see same input state |
| 23 | Max iterations | `TestMaxIterations` | Boundary conditions at exact max |
| 24 | GateNode properties | `TestGateNodeProperties` | Gate has no outputs, get_output_type returns None |
| 25 | Select parameter | `TestSelectParameter` | select filters outputs, warns on non-existent |
| 27 | Nested graph bindings | `test_red_team_extended.py` | Bindings may persist/be lost when wrapped as GraphNode |

---

## ⏸️ DEFERRED — By Design or Low Priority

### 3. Runtime Inputs Not Type-Checked (`strict_types`)

| | |
|---|---|
| **Severity** | MEDIUM-HIGH |
| **Sources** | Jules PR #18 (B3), Gemini (T1), Codex (issue 6) |

**Problem**: `strict_types=True` only validates node-to-node edges at build time. Values passed via `runner.run()` are never checked.

**Why deferred**: Runtime type checking adds overhead. Current behavior matches many frameworks. Could use `beartype` for opt-in validation.

**Future**: Add an optional `validate_inputs=True` parameter to `runner.run()`.

---

### 5. Disconnected / Unreachable Nodes Force All Inputs

| | |
|---|---|
| **Severity** | HIGH |
| **Sources** | Jules PR #18 (D1, D2), Gemini (SPLIT-1, UNR-1), Codex (issue 3) |

**Problem**: The runner demands inputs for every node, even if unreachable from provided inputs.

**Why deferred**: Arguably correct — a graph should be self-contained. Computing reachability adds complexity.

**Future**: Add a `select_subgraph(outputs=[...])` method to extract runnable subgraphs.

---

### 12. `**kwargs` Not Detected as Graph Inputs

| | |
|---|---|
| **Severity** | LOW |
| **Sources** | Jules PR #18 (D4) |

**Problem**: Nodes using `**kwargs` can't receive extra inputs via the graph.

**Why deferred**: Rare use case. Workaround: use explicit parameters or a dict parameter.

---

## Confirmed Working (Not Bugs)

| Area | Detail |
|---|---|
| Async parallel execution | `AsyncRunner` correctly runs independent async nodes concurrently |
| Mixed sync/async | `AsyncRunner` handles both sync and async nodes in one graph |
| Rename swap | `with_inputs(a='b', b='a')` swaps atomically |
| Chained renames | `with_inputs(x='y').with_inputs(y='z')` composes correctly |
| Union type compatibility | `int` → `int \| str` passes `strict_types` |
| State isolation between runs | Basic state (non-mutable-default) is isolated |
| Duplicate node name detection | Caught at build time |
| Mutex branch-local consumers | Validation correctly handles same-name outputs in exclusive branches |
| Gate invalid target detection | Runtime validation catches undeclared targets |
| Map exception handling | `runner.map` returns `RunResult` with `status=FAILED` for failed items |
| Recursive graph construction | Graph copies nodes at construction, preventing self-reference |

---

## Ideas: Future Red-Team Techniques

### Testing Strategies

| Technique | Description |
|---|---|
| **Property-based testing** | Use Hypothesis to generate random graph topologies, rename sequences, and input values. Verify: termination, type safety, state isolation, determinism. |
| **Mutation testing** | Run `mutmut` or `cosmic-ray` on validation, rename mapping, and gate decision logic. |
| **Capability matrix expansion** | Add dimensions to `tests/capabilities/matrix.py`: routing type (none/route/ifelse/nested), reachability (connected/disconnected/gated-off), defaults (immutable/mutable), output collisions. |
| **Differential testing** | Build same workflow in hypergraph and LangGraph/Pydantic-Graph — compare edge-case behavior. |
| **Fuzzing inputs** | Test with `None`, empty collections, very large values, unicode, nested structures at graph boundaries. |
| **Concurrency stress** | Many parallel executions with random delays to expose race conditions. |
| **Cross-framework mining** | Search GitHub issues of LangGraph, Pydantic-Graph, Mastra for keywords: "routing", "seed", "cycle", "async", "branch". |

### What Worked in This Red-Team

| Technique | Why it helped |
|---|---|
| **`xfail(strict=True)` tests** | "Living spec" — tests auto-transition from xfail to passing as bugs are fixed. |
| **Outside-in examples** | User-visible failures keep findings grounded in real usage. |
| **Cross-framework research** | Mining issues from other frameworks revealed common failure patterns. |
| **Systematic ID schemes** | Prefixed IDs (B1, D2, GT-003, RN-005) make issues trackable across docs. |
| **Iterative deepening** | Confirming V1 (mutable defaults) led to probes V1a (dict), V1b (nested mutable), V1e (class instance). |

---

## File Index

| File | Source | Description |
|---|---|---|
| `red-team/pr-23/test_red_team_audit.py` | PR #23 | 20 test classes, 1149 lines — comprehensive edge-case audit |
| `red-team/pr-18/RED_TEAM_FINDINGS.md` | PR #18 | 3 confirmed bugs, 4 design flaws, recommendations |
| `red-team/pr-18/test_red_team_plan.py` | PR #18 | Core regression tests — cycles, types, mutex, nesting |
| `red-team/pr-18/test_red_team_extended.py` | PR #18 | Extended tests — map, kwargs, async, nested bindings |
| `tmp/red-team/jules_red_team.md` | — | Points to PR #18 — original Jules analysis |
| `tmp/red-team/red_team_gemini.md` | — | Gemini findings — 6 confirmed, 2 not reproduced |
| `tmp/red-team/new_codex.md` | — | Codex red-team plan — 7 hypotheses with suggested tests |
| `tmp/red-team/test_critical_flaws.py` | — | 8 tests covering core issues |
| `tmp/red-team/codex_tests_might_not_fail.py` | — | 3 exploratory tests |
| `tmp/red-team/How to Red-Team…/*.md` | AMP | Multi-phase reports — 218 tests, 68 failures, cross-framework research |
| `tmp/red-team/How to Red-Team…/test_red_team*.py` | AMP | Batched test suites (gates, renames, types, nesting, etc.) |
