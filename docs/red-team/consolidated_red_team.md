# Consolidated Red-Team Findings

> **Sources**: Jules ([PR #18](https://github.com/gilad-rubin/hypergraph/pull/18)), Claude Opus ([PR #23](https://github.com/gilad-rubin/hypergraph/pull/23)), Gemini (`red_team_gemini.md`), Codex (`new_codex.md`), AMP subfolder (`How to Red-Team a Codebase for Potential Issues/`)
>
> **Test files**: `test_critical_flaws.py`, `codex_tests_might_not_fail.py`, subfolder `test_red_team*.py`, `test_nodes_gate.py`, `test_routing.py`, `test_red_team_audit.py` (PR #23)
>
> **Resolution PRs**: [#25](https://github.com/gilad-rubin/hypergraph/pull/25) — fixes #1, #2, #4, #6, #9, #11 | [#27](https://github.com/gilad-rubin/hypergraph/pull/27) — fixes mutex rename

---

## Status Summary

| # | Issue | Severity | Source |
|---|-------|----------|--------|
| 10 | Deep nesting / infinite loops | HIGH | ✅ Investigated in PR #29 — not bugs |
| 29 | Cyclic gate→END infinite loop | HIGH | AMP (CY-005) |
| 13 | GraphNode output leakage | MEDIUM | PR #23 |
| 14 | Stale branch values persist | MEDIUM | PR #23 |
| 16 | DiGraph drops parallel edges | MEDIUM | PR #23 |
| 18 | Routing edge cases (None, multi-target) | MEDIUM | PR #23 |
| 26 | Map with empty list / zip mismatch | MEDIUM | PR #18 extended |
| 28 | Async exception propagation | MEDIUM | PR #18 extended |
| 30 | Type checking with renamed inputs | MEDIUM | AMP (TC-010) |
| 31 | GraphNode name collision not detected | MEDIUM | ✅ Validated in PR #29 — detected |
| 15 | Type subclass compatibility | LOW | PR #23 |
| 17 | Empty/edge-case graphs | LOW | PR #23 |
| 19 | Bind/unbind edge cases | LOW | PR #23 |
| 20 | Generator node behavior | LOW | PR #23 |
| 21 | Complex type validation (Optional, generics) | LOW | PR #23 |
| 22 | Superstep determinism | LOW | PR #23 |
| 23 | Max iterations boundary | LOW | PR #23 |
| 24 | GateNode property edge cases | LOW | PR #23 |
| 25 | Select parameter filtering | LOW | PR #23 |
| 27 | Nested graph bindings persistence | LOW | PR #18 extended |

**Deferred**: #3 runtime type checking (by design), #5 disconnected nodes (design decision), #12 kwargs detection (low priority)

**Resolved**: #1, #2, #4, #6, #9, #11 in PR #25; #7, #8 already fixed

---

## 🔴 OPEN — High Severity

### 10. Deep Nesting / Multiple GraphNodes — Infinite Loops

Investigated in [PR #29](https://github.com/gilad-rubin/hypergraph/pull/29):

| Scenario | Result |
|----------|--------|
| A: 4+ level nesting | **PASSES** (sync and async) |
| B: Same inner graph used twice | **PASSES** with renames, validates conflicts |
| C: Output name == input name (SM-007) | **NOT A BUG** — convergence loop pattern |
| D: GraphNode name collision (#31) | **PASSES** — validation catches it |

Scenario C was the most interesting: `transform(x) -> x+1` creates a self-loop that never converges, so `InfiniteLoopError` is correct. The same pattern with convergence (`clamp(val) -> min(val+1, 5)`) terminates correctly. This is the convergence loop feature used by `counter_stop`.

The Sole Producer Rule (from `specs/not_reviewed/conflict-resolution.md`) was attempted but reverted — it would break all convergence loops.

**Status**: Scenarios A/B/D are not bugs. Scenario C is working as designed. Remaining open sub-issue is #29 (gate→END topology).

---

### 29. Cyclic Gate→END Infinite Loop

```python
@node(output_name="count")
def increment(count: int) -> int: return count + 1

@route(targets=["increment", END])
def check(count: int) -> str:
    return END if count >= 3 else "increment"

graph = Graph(nodes=[increment, check])
runner.run(graph, {"count": 0})  # hangs in certain topologies
```

**Expected**: Loop runs 3 times, then terminates when `check` returns `END`.
**Actual**: In certain graph topologies, the gate→END path hangs entirely instead of terminating. Distinct from the fixed off-by-one (#2) — this is about specific topologies where the END signal is never processed.

---

## 🔵 OPEN — Medium Severity

### 13. GraphNode Output Leakage

```python
@node(output_name="intermediate")
def step1(x: int) -> int: return x * 2

@node(output_name="final")
def step2(intermediate: int) -> int: return intermediate + 1

inner = Graph(nodes=[step1, step2])
outer = Graph(nodes=[inner.as_node(), some_node_that_takes_intermediate])
```

**Expected**: Only `final` (the leaf output) should be visible to the outer graph.
**Actual**: Both `intermediate` and `final` are exposed. This means `some_node_that_takes_intermediate` would accidentally wire to the inner graph's intermediate value — breaking encapsulation.

---

### 14. Stale Branch Values Persist

```python
@route(targets=["branch_a", "branch_b"])
def decide(iteration: int) -> str:
    return "branch_a" if iteration % 2 == 0 else "branch_b"

# Iteration 0: branch_a runs, produces result_a="hello"
# Iteration 1: branch_b runs, but result_a="hello" is still in state
```

**Expected**: When the gate switches from `branch_a` to `branch_b`, values produced by `branch_a` should be cleared.
**Actual**: `result_a` stays in state. If a downstream node consumes `result_a`, it gets a stale value from a branch that didn't run this iteration.

---

### 16. DiGraph Drops Parallel Edges

```python
@node(output_name=("quotient", "remainder"))
def divmod_node(a: int, b: int) -> tuple[int, int]:
    return divmod(a, b)

@node(output_name="result")
def combine(quotient: int, remainder: int) -> str:
    return f"{quotient}r{remainder}"

graph = Graph(nodes=[divmod_node, combine])
```

**Expected**: Two edges: `divmod_node→combine` (for `quotient`) and `divmod_node→combine` (for `remainder`).
**Actual**: NetworkX `DiGraph` only keeps one edge between any two nodes. The second edge overwrites the first, so the graph may not correctly track that both `quotient` and `remainder` flow from `divmod_node` to `combine`.

---

### 18. Routing Edge Cases

```python
# What happens when a route function returns None?
@route(targets=["a", "b"])
def decide(x: int) -> str | None:
    return None  # not a valid target, not END — what should happen?

# What happens when IfElse gets a truthy non-bool?
@ifelse(when_true="a", when_false="b")
def check(x: int) -> int:
    return 42  # truthy, but not literally True
```

**Expected**: `None` should either raise a clear error or have documented fallback behavior. Non-bool truthy values in `ifelse` should be defined (coerce to bool? reject?).
**Actual**: Behavior is under-specified — depends on implementation details, may silently do the wrong thing or raise an unclear error.

---

### 26. Map with Empty List / Zip Length Mismatch

```python
# Empty input
runner.map(graph, values={"x": []}, map_over="x")

# Mismatched lengths
runner.map(graph, values={"a": [1, 2], "b": [1, 2, 3]}, map_over=["a", "b"])
```

**Expected**: Empty list should return empty results. Mismatched lengths should raise `ValueError` (like Python's `zip(..., strict=True)`).
**Actual**: Behavior is not well-defined — may silently truncate, produce unexpected results, or error with an unclear message.

---

### 28. Async Exception Propagation

```python
@node(output_name="result")
async def risky(x: int) -> int:
    if x == 0:
        raise ValueError("bad input")
    return x * 2

results = await async_runner.map(graph, values={"x": [1, 0, 3]}, map_over="x")
```

**Expected**: Item at index 1 should fail with `status=FAILED`, items 0 and 2 should succeed. The exception should be accessible on the failed result.
**Actual**: Exception propagation in async map may not correctly isolate failures per item — needs verification that one failure doesn't break the entire batch.

---

### 30. Type Checking with Renamed Inputs

```python
@node(output_name="doubled")
def double(x: int) -> int: return x * 2

@node(output_name="result")
def use_value(value: str) -> str: return f"got {value}"

renamed = double.with_outputs(doubled="value")
graph = Graph(nodes=[renamed, use_value], strict_types=True)
```

**Expected**: `strict_types` should catch that `double` produces `int` but `use_value` expects `str`, even though the output was renamed from `doubled` to `value`.
**Actual**: Type checking may look up the original name (`doubled`) instead of the renamed name (`value`), missing the type mismatch.

---

### 31. GraphNode Name Collision Not Detected

```python
@node(output_name="x")
def compute(a: int) -> int: return a + 1

inner = Graph(nodes=[compute], name="compute")  # same name as a node
outer = Graph(nodes=[inner.as_node(), compute])
```

**Expected**: Should raise `GraphConfigError` at build time — two nodes can't have the same name.
**Actual**: No validation error. Instead, execution enters an infinite loop because the runner confuses which "compute" it's tracking.

---

## 🔵 OPEN — Low Severity

### 15. Type Subclass Compatibility

```python
@node(output_name="flag")
def check(x: int) -> bool: return x > 0

@node(output_name="result")
def add_flag(flag: int) -> int: return flag + 1

graph = Graph(nodes=[check, add_flag], strict_types=True)
```

**Expected**: `bool` is a subclass of `int` in Python, so this should pass type checking.
**Actual**: `strict_types` may reject the `bool→int` edge because it checks exact type match rather than `issubclass`.

---

### 17. Empty / Edge-Case Graphs

```python
graph = Graph(nodes=[])
runner.run(graph, {})
```

**Expected**: Should raise `GraphConfigError` — an empty graph is almost certainly a mistake.
**Actual**: Silently accepted, returns empty results. No warning or error.

---

### 19. Bind/Unbind Edge Cases

```python
graph = Graph(nodes=[add]).bind(y=20)
result = runner.run(graph, {"x": 5, "y": 30})
```

**Expected**: Runtime input `y=30` should override the bound value `y=20` (resolution order: edge > input > bound > default).
**Actual**: Needs verification that the precedence order is consistently applied and that `unbind()` correctly restores the parameter as required.

---

### 20. Generator Node Behavior

```python
@node(output_name="items")
def generate(n: int) -> int:
    for i in range(n):
        yield i

result = runner.run(graph, {"n": 5})
```

**Expected**: Clear documentation on whether sync generators yield one-at-a-time (streaming) or are collected into a list.
**Actual**: The sync executor collects all yielded values into a list `[0, 1, 2, 3, 4]`. This works, but the behavior isn't obvious from the return type annotation (`-> int` vs actual `list[int]`).

---

### 21. Complex Type Validation (Optional, Generics)

```python
# Should int be compatible with Optional[int]?
@node(output_name="value")
def produce(x: int) -> int: return x

@node(output_name="result")
def consume(value: Optional[int]) -> int: return value or 0

graph = Graph(nodes=[produce, consume], strict_types=True)
```

**Expected**: `int` should be compatible with `Optional[int]` (since `Optional[int]` = `int | None`).
**Actual**: Needs verification that `is_type_compatible` correctly handles `Optional`, `Union`, and generic types like `list[int]` vs `list[str]`.

---

### 22. Superstep Determinism

```python
@node(output_name="a")
def slow(x: int) -> int: return x + 1

@node(output_name="b")
def fast(x: int) -> int: return x + 2

# Both consume "x" and run in the same superstep
```

**Expected**: Both nodes should see the same snapshot of `x` — neither should see the other's output mid-superstep.
**Actual**: Believed to work correctly, but needs explicit verification that the state is snapshotted before the superstep begins.

---

### 23. Max Iterations Boundary

```python
@route(targets=["increment", END])
def check(counter: int) -> str:
    return END if counter >= 5 else "increment"

# With max_iterations=5, the loop should have exactly enough room
result = runner.run(graph, {"counter": 0}, max_iterations=5)
```

**Expected**: If the loop naturally terminates at exactly `max_iterations`, it should succeed (not fail as "exceeded").
**Actual**: Needs verification of the boundary condition — does `max_iterations=5` allow 5 iterations, or does it fail at the 5th?

---

### 24. GateNode Property Edge Cases

```python
@route(targets=["a", END])
def decide(x: int) -> str: return "a"

decide.outputs        # should be ()
decide.get_output_type("anything")  # should be None
```

**Expected**: Gates route control flow, they don't produce data. `outputs` should be empty, `get_output_type` should return `None`.
**Actual**: Believed correct, but edge cases around `get_input_type` and interaction with `strict_types` need verification.

---

### 25. Select Parameter Filtering

```python
@node(output_name="a")
def f(x: int) -> int: return x

@node(output_name="b")
def g(x: int) -> int: return x * 2

result = runner.run(graph, {"x": 5}, select=["a"])
# Also: select=["nonexistent"] — should it warn or error?
```

**Expected**: `select=["a"]` returns only `{"a": 5}`. Selecting a nonexistent output should warn or raise.
**Actual**: Filtering works, but behavior on nonexistent names needs verification — silent ignore vs warning vs error.

---

### 27. Nested Graph Bindings Persistence

```python
@node(output_name="result")
def add(x: int, k: int) -> int: return x + k

inner = Graph(nodes=[add]).bind(k=10)
outer = Graph(nodes=[inner.as_node()])
result = runner.run(outer, {"x": 5})
```

**Expected**: `k=10` binding should persist through `as_node()` wrapping. Result should be `15`.
**Actual**: Needs verification that bindings survive the GraphNode wrapping and don't resurface as required inputs.

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
