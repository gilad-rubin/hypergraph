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
| 1 | Mutable defaults shared | CRITICAL | ✅ **RESOLVED** in PR #25 |
| 2 | Cycle off-by-one | HIGH | ✅ **RESOLVED** in PR #25 |
| 3 | Runtime type checking | MEDIUM-HIGH | ⏸️ **DEFERRED** — by design |
| 4 | Intermediate injection | HIGH | ✅ **RESOLVED** in PR #25 |
| 5 | Disconnected nodes | HIGH | ⏸️ **DEFERRED** — design decision |
| 6 | Rename collision | MEDIUM | ✅ **RESOLVED** in PR #25 |
| 7 | Invalid gate target | MEDIUM | ✅ **ALREADY FIXED** — validation exists |
| 8 | Mutex branch outputs | MEDIUM | ✅ **ALREADY FIXED** — in PR #8 |
| 9 | Control-only cycles | MEDIUM | ✅ **RESOLVED** in PR #25 |
| 10 | Deep nesting loops | HIGH | 🔴 **OPEN** — needs investigation |
| 11 | Rename propagation | MEDIUM | ✅ **RESOLVED** in PR #25 |
| 12 | kwargs detection | LOW | ⏸️ **DEFERRED** — low priority |
| 13 | GraphNode output leakage | MEDIUM | 🔵 **NEW** — from PR #23 |
| 14 | Stale branch values persist | MEDIUM | 🔵 **NEW** — from PR #23 |
| 15 | Type subclass compatibility | LOW | 🔵 **NEW** — from PR #23 |
| 16 | DiGraph drops parallel edges | MEDIUM | 🔵 **NEW** — from PR #23 |
| 17 | Empty/edge-case graphs | LOW | 🔵 **NEW** — from PR #23 |
| 18 | Routing edge cases (None, multi-target) | MEDIUM | 🔵 **NEW** — from PR #23 |
| 19 | Bind/unbind edge cases | LOW | 🔵 **NEW** — from PR #23 |
| 20 | Generator node behavior | LOW | 🔵 **NEW** — from PR #23 |
| 21 | Complex type validation (Optional, generics) | LOW | 🔵 **NEW** — from PR #23 |
| 22 | Superstep determinism | LOW | 🔵 **NEW** — from PR #23 |
| 23 | Max iterations boundary | LOW | 🔵 **NEW** — from PR #23 |
| 24 | GateNode property edge cases | LOW | 🔵 **NEW** — from PR #23 |
| 25 | Select parameter filtering | LOW | 🔵 **NEW** — from PR #23 |
| 26 | Map with empty list / zip mismatch | MEDIUM | 🔵 **NEW** — from PR #18 extended |
| 27 | Nested graph bindings persistence | LOW | 🔵 **NEW** — from PR #18 extended |
| 28 | Async exception propagation | MEDIUM | 🔵 **NEW** — from PR #18 extended |

---

## ✅ RESOLVED Issues

### 1. Mutable Default Arguments Shared Across Runs

| | |
|---|---|
| **Severity** | CRITICAL |
| **Status** | ✅ **RESOLVED** in [PR #25](https://github.com/gilad-rubin/hypergraph/pull/25) |
| **Sources** | Jules PR #18 (B1), Gemini (V1), Codex (issue 7) |
| **Tests** | `tests/test_red_team_fixes.py::TestMutableDefaults` |

**Problem**: A node with a mutable default (list, dict) reuses the same object across runs.

**Fix**: Added `copy.deepcopy()` in `_resolve_input()` at `helpers.py:210` when returning function defaults.

---

### 2. Cycle Termination Off-by-One

| | |
|---|---|
| **Severity** | HIGH |
| **Status** | ✅ **RESOLVED** in [PR #25](https://github.com/gilad-rubin/hypergraph/pull/25) |
| **Sources** | Jules PR #18 (B2), Gemini (CYCLE-1) |
| **Tests** | `tests/test_red_team_fixes.py::TestCycleTermination` |

**Problem**: A loop governed by a `@route` gate executes one extra iteration after returning `END`.

**Fix**: Added `_clear_stale_gate_decisions()` helper in `helpers.py:50-64` that removes routing decisions for gates about to re-execute, preventing old decisions from prematurely activating targets.

---

### 4. Intermediate Value Injection Doesn't Work

| | |
|---|---|
| **Severity** | HIGH |
| **Status** | ✅ **RESOLVED** in [PR #25](https://github.com/gilad-rubin/hypergraph/pull/25) |
| **Sources** | Jules PR #18 (D3), Gemini (INT-1), Codex (issue 2) |
| **Tests** | `tests/test_red_team_fixes.py::TestIntermediateInjection` |

**Problem**: Providing an intermediate output value failed with `MissingInputError` for upstream inputs.

**Fix**: Added `_find_bypassed_inputs()` in `validation.py:189-222` to identify inputs exclusively needed by nodes whose outputs are user-provided, excluding them from required validation.

---

### 6. Rename Collision Silently Allowed

| | |
|---|---|
| **Severity** | MEDIUM |
| **Status** | ✅ **RESOLVED** in [PR #25](https://github.com/gilad-rubin/hypergraph/pull/25) |
| **Sources** | Codex (issue 5), AMP subfolder (RN-005) |
| **Tests** | `tests/test_red_team_fixes.py::TestRenameCollision` |

**Problem**: Renaming two outputs to the same name didn't raise an error.

**Fix**: Added `_check_rename_duplicates()` helper in `base.py:314-322` that raises `RenameError` when `with_inputs()`/`with_outputs()` would create duplicate names.

---

### 7. Gate Returning Invalid Target Not Caught at Runtime

| | |
|---|---|
| **Severity** | MEDIUM |
| **Status** | ✅ **ALREADY FIXED** — validation exists |
| **Sources** | AMP subfolder (GT-003) |

**Investigation**: Runtime validation already exists in `routing_validation.py:76-88`. The `_validate_single_target` function checks if a target is in `node.targets` and raises `ValueError` if not. Both sync and async route executors call `validate_routing_decision()`.

The red-team report was based on an older version of the codebase.

---

### 8. Mutex Branch-Local Consumers — Validation False Positive

| | |
|---|---|
| **Severity** | MEDIUM |
| **Status** | ✅ **ALREADY FIXED** in [PR #8](https://github.com/gilad-rubin/hypergraph/pull/8) |
| **Sources** | Jules PR #18 (G4), AMP subfolder (G4, G4c, GN-005) |
| **Tests** | `tests/test_runners/test_routing.py::TestMutexBranchOutputs` |

**Investigation**: The `feat/mutex-branch-outputs` branch was merged. Implementation in `core.py` includes:
- `_collect_output_sources` — gathers all nodes producing each output
- `_expand_mutex_groups` — identifies gate nodes and computes exclusive reachability
- `_are_all_mutex` — checks that duplicate-output producers fall in different branches
- `_validate_output_conflicts` — only errors if producers are NOT mutually exclusive

---

### 9. Control-Only Cycles Incorrectly Require Seed Inputs

| | |
|---|---|
| **Severity** | MEDIUM |
| **Status** | ✅ **RESOLVED** in [PR #25](https://github.com/gilad-rubin/hypergraph/pull/25) |
| **Sources** | Codex (issue 4) |
| **Tests** | `tests/test_red_team_fixes.py::TestControlOnlyCycles` |

**Problem**: Cycle detection included control edges, marking parameters as "seeds" even when only control (not data) flowed back.

**Fix**: Added `_data_only_subgraph()` helper in `input_spec.py:167-178` that filters to data edges before running `nx.simple_cycles()`.

---

### 11. Output Rename Propagation Failure

| | |
|---|---|
| **Severity** | MEDIUM |
| **Status** | ✅ **RESOLVED** in [PR #25](https://github.com/gilad-rubin/hypergraph/pull/25) |
| **Sources** | AMP subfolder (RN-006, RN-007, R6) |
| **Tests** | `tests/test_red_team_fixes.py::TestOutputRenamePropagation` |

**Problem**: `with_outputs()` on a GraphNode didn't translate inner output names to renamed external names in the non-map execution path.

**Fix**: Changed `return result.values` to `return node.map_outputs_from_original(result.values)` in both:
- `runners/sync/executors/graph_node.py:65`
- `runners/async_/executors/graph_node.py:71`

---

## 🔴 OPEN Issues — Needs Investigation

### 10. Deep Nesting / Multiple GraphNodes — Infinite Loops

| | |
|---|---|
| **Severity** | HIGH |
| **Status** | 🔴 **OPEN** — needs investigation |
| **Sources** | AMP subfolder (GN-001, GN-003, SM-007, GN-008) |

Several nesting scenarios cause `InfiniteLoopError` or hang:
- 4+ level nested graphs
- Two `GraphNode` instances from the same inner graph in one outer graph
- A node whose output has the same name as its input (staleness loop)
- A `GraphNode` with the same name as another node in the outer graph

**Investigation notes**:
- The planning agent hit API overload errors before completing analysis
- Likely related to staleness detection not properly handling nested graph boundaries
- May require changes to how `node_executions` tracks GraphNode execution vs inner node execution
- Consider adding tests with `--timeout=10` to catch hangs early

**Next steps**:
1. Write isolated failing tests for each scenario
2. Add tracing/logging to staleness detection during nested execution
3. Investigate if `GraphState.copy()` needs deeper isolation for nested runs

---

## ⏸️ DEFERRED Issues — By Design or Low Priority

### 3. Runtime Inputs Not Type-Checked (`strict_types`)

| | |
|---|---|
| **Severity** | MEDIUM-HIGH |
| **Status** | ⏸️ **DEFERRED** — by design |
| **Sources** | Jules PR #18 (B3), Gemini (T1), Codex (issue 6) |

**Problem**: `strict_types=True` only validates node-to-node edges at build time. Values passed via `runner.run()` are never checked.

**Why deferred**:
- Adding runtime type checking would add overhead to every run
- Could use `beartype` or similar for opt-in runtime validation
- Current behavior matches many frameworks (types as documentation, not enforcement)

**Future consideration**: Add an optional `validate_inputs=True` parameter to `runner.run()`.

---

### 5. Disconnected / Unreachable Nodes Force All Inputs

| | |
|---|---|
| **Severity** | HIGH |
| **Status** | ⏸️ **DEFERRED** — design decision |
| **Sources** | Jules PR #18 (D1, D2), Gemini (SPLIT-1, UNR-1), Codex (issue 3) |

**Problem**: The runner demands inputs for every node, even if unreachable from provided inputs.

**Why deferred**:
- This is arguably correct behavior: a graph should be self-contained
- "Library graph" pattern is unusual and could be achieved with separate graphs
- Computing reachability at runtime adds complexity and overhead
- Would need to define "reachable from" semantics clearly

**Future consideration**: Add a `select_subgraph(outputs=[...])` method to extract runnable subgraphs.

---

### 12. `**kwargs` Not Detected as Graph Inputs

| | |
|---|---|
| **Severity** | LOW |
| **Status** | ⏸️ **DEFERRED** — low priority |
| **Sources** | Jules PR #18 (D4) |

**Problem**: Nodes using `**kwargs` can't receive extra inputs via the graph.

**Why deferred**:
- Relatively rare use case
- Workaround: use explicit parameters or a dict parameter
- Would require significant changes to input spec computation

---

## 🔵 NEW Issues — From PR #23 and PR #18 Extended

### 13. GraphNode Output Leakage

| | |
|---|---|
| **Severity** | MEDIUM |
| **Status** | 🔵 **NEW** — needs investigation |
| **Sources** | PR #23 `TestGraphNodeOutputLeakage` |

**Problem**: GraphNode exposes all intermediate outputs to the outer graph, not just its declared leaf outputs. This can cause unintended wiring.

---

### 14. Stale Branch Values Persist

| | |
|---|---|
| **Severity** | MEDIUM |
| **Status** | 🔵 **NEW** — needs investigation |
| **Sources** | PR #23 `TestStaleBranchValues` |

**Problem**: When a routing gate deactivates a branch, values produced in a previous iteration by that branch remain in state, potentially being consumed by downstream nodes.

---

### 15. Type Subclass Compatibility

| | |
|---|---|
| **Severity** | LOW |
| **Status** | 🔵 **NEW** |
| **Sources** | PR #23 `TestTypeCompatibilitySubclass` |

**Problem**: `strict_types=True` may not correctly handle subclass relationships (e.g., `bool` → `int`).

---

### 16. DiGraph Drops Parallel Edges

| | |
|---|---|
| **Severity** | MEDIUM |
| **Status** | 🔵 **NEW** |
| **Sources** | PR #23 `TestMultipleOutputs` |

**Problem**: NetworkX DiGraph drops parallel edges — when a node produces multiple outputs consumed by the same downstream node, only one edge is recorded.

---

### 17. Empty / Edge-Case Graph Configurations

| | |
|---|---|
| **Severity** | LOW |
| **Status** | 🔵 **NEW** |
| **Sources** | PR #23 `TestEdgeCaseGraphs` |

**Problem**: Empty graphs, side-effect-only nodes, and self-referential configurations may be silently accepted or produce unexpected behavior.

---

### 18. Routing Edge Cases

| | |
|---|---|
| **Severity** | MEDIUM |
| **Status** | 🔵 **NEW** |
| **Sources** | PR #23 `TestRoutingEdgeCases` |

**Problem**: Route returning `None`, IfElse with non-bool values, END termination semantics, and multi-target routing have under-specified behavior.

---

### 19–25. Additional Edge Cases (Low Severity)

| # | Area | Test Class | Notes |
|---|------|-----------|-------|
| 19 | Bind/unbind | `TestBindEdgeCases` | Bind override precedence, unbind restoration |
| 20 | Generator nodes | `TestGeneratorNodes` | Sync generators collected as list |
| 21 | Complex types | `TestComplexTypeValidation` | Optional, generics with strict_types |
| 22 | Superstep determinism | `TestSuperstepDeterminism` | Parallel nodes see same input state |
| 23 | Max iterations | `TestMaxIterations` | Boundary conditions at exact max |
| 24 | GateNode properties | `TestGateNodeProperties` | Gate has no outputs, get_output_type returns None |
| 25 | Select parameter | `TestSelectParameter` | select filters outputs, warns on non-existent |

---

### 26. Map with Empty List / Zip Length Mismatch

| | |
|---|---|
| **Severity** | MEDIUM |
| **Status** | 🔵 **NEW** |
| **Sources** | PR #18 `test_red_team_extended.py` |

**Problem**: `runner.map()` behavior with empty input lists and mismatched zip lengths may not be well-defined.

---

### 27. Nested Graph Bindings Persistence

| | |
|---|---|
| **Severity** | LOW |
| **Status** | 🔵 **NEW** |
| **Sources** | PR #18 `test_red_team_extended.py` |

**Problem**: Bindings on inner graph nodes may persist or be lost when the graph is wrapped as a GraphNode.

---

### 28. Async Exception Propagation

| | |
|---|---|
| **Severity** | MEDIUM |
| **Status** | 🔵 **NEW** |
| **Sources** | PR #18 `test_red_team_extended.py` |

**Problem**: Exception propagation in async map operations may not correctly surface errors from individual items.

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

---

## Ideas & Meta: How to Red-Team in the Future

### What Worked

| Technique | Why it helped |
|---|---|
| **`xfail(strict=True)` tests** | Creates a "living spec" of known bugs. As bugs are fixed, tests auto-transition from xfail to passing — no manual cleanup. |
| **Outside-in examples** | Starting from user-visible failures (not implementation details) keeps findings grounded in real usage. |
| **Cross-framework research** | Mining GitHub issues from LangGraph, Pydantic-Graph, Mastra for keywords like "routing", "seed", "cycle" revealed common failure patterns to probe. |
| **Systematic ID schemes** | Prefixed IDs (B1, D2, GT-003, RN-005) make issues trackable across documents and conversations. |
| **Iterative deepening** | When V1 (mutable defaults) was confirmed, follow-up probes V1a (dict), V1b (nested mutable), V1e (class instance) tested variants. |

### What to Add Next

| Technique | Description |
|---|---|
| **Property-based testing** | Use Hypothesis to generate random graph topologies, rename sequences, and input values. Verify properties: termination, type safety, state isolation, determinism. |
| **Mutation testing** | Run `mutmut` or `cosmic-ray` on validation, rename mapping, and gate decision logic to verify tests catch real mutations. |
| **Capability matrix expansion** | Add dimensions to `tests/capabilities/matrix.py`: routing type (none/route/ifelse/nested), reachability (connected/disconnected/gated-off), defaults (immutable/mutable), output collisions. |
| **Differential testing** | Implement the same workflow in hypergraph and a competing framework — compare edge-case behavior. |
| **Fuzzing inputs** | Test with `None`, empty collections, very large values, unicode, nested structures at graph boundaries. |
| **Concurrency stress** | Run many parallel executions with random delays to expose race conditions in shared state. |

### File Index

| File | Source | Description |
|---|---|---|
| `red-team/pr-23/test_red_team_audit.py` | PR #23 | 20 test classes, 1149 lines — comprehensive edge-case audit |
| `red-team/pr-18/RED_TEAM_FINDINGS.md` | PR #18 | 3 confirmed bugs, 4 design flaws, recommendations |
| `red-team/pr-18/test_red_team_plan.py` | PR #18 | Core regression tests — cycles, types, mutex, nesting |
| `red-team/pr-18/test_red_team_extended.py` | PR #18 | Extended tests — map, kwargs, async, nested bindings |
| `jules_red_team.md` | — | Points to PR #18 — original Jules analysis |
| `red_team_gemini.md` | — | Gemini findings — 6 confirmed, 2 not reproduced |
| `new_codex.md` | — | Codex red-team plan — 7 hypotheses with suggested tests |
| `test_critical_flaws.py` | — | 8 tests covering core issues |
| `codex_tests_might_not_fail.py` | — | 3 exploratory tests |
| `How to Red-Team…/*.md` | — | AMP multi-phase reports — 218 tests, 68 failures |
| `How to Red-Team…/test_red_team*.py` | — | Batched test suites (gates, renames, types, nesting, etc.) |
