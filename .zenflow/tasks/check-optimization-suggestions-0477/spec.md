# Optimization PRs - Technical Specification

## Difficulty: Medium

## PR Analysis

### PR #19: Optimize default consistency check
**File:** `src/hypergraph/graph/validation.py` - `_check_defaults_consistency`
**Change:** Replace two list comprehensions with a single for-loop.
**Claimed improvement:** ~24% speedup on 1M items, ~9% on all-defaults case.

**Assessment: Questionable value.** The optimization is correct but practically irrelevant. `_check_defaults_consistency` operates on `info_list` which contains one entry per node that shares a parameter. Real graphs have at most dozens of nodes sharing a parameter, not millions. The 1M-item benchmark is artificial and misleading. At real-world sizes (5-50 items), the difference is in nanoseconds.

**Verdict:** Flag for consultation - technically correct but benchmark is misleading. The optimization provides no measurable real-world benefit and makes the code slightly less readable (6 lines of loop vs 2 list comprehensions).

### PR #20: Optimize mutex reachability calculation
**File:** `src/hypergraph/graph/core.py` - `_compute_exclusive_reachability`
**Change:** Replace O(N^2) set-difference approach with Counter-based O(N) approach.
**Claimed improvement:** Linear scaling instead of quadratic.

**Assessment: Valid and well-reasoned.** The original code computes `set().union(*(reachable[o] for o in targets if o != t))` for each target, which is O(T*N) where T is number of targets and N is total reachable nodes. The Counter approach flattens all reachable sets once (O(N)), then filters by count==1 per target. This is genuinely O(N) total vs O(T*N). The improvement matters for graphs with many branches.

**Verdict:** Makes sense - create benchmark tests to verify.

### PR #21: Optimized validation loop using defaultdict
**File:** `src/hypergraph/graph/validation.py` - `_validate_multi_target_output_conflicts`
**Change:** Replace `dict.setdefault(output, []).append()` with `defaultdict(list)[output].append()`.
**Claimed improvement:** ~41% speedup (28.42s -> 16.63s).

**Assessment: Highly suspicious benchmark.** The claimed 41% speedup for `setdefault` vs `defaultdict` is far beyond what's expected. In CPython, `defaultdict` is marginally faster than `setdefault` (~5-15% in microbenchmarks), not 41%. The 28s baseline suggests an absurdly large input or a flawed benchmark. In practice, this loop iterates over a few nodes and their outputs - tens of iterations at most. The real-world improvement is negligible.

**Verdict:** Flag for consultation - benchmark numbers are implausible. The change is harmless but the claimed improvement is misleading.

### PR #22: Optimize AsyncRunner.map with worker pool
**File:** `src/hypergraph/runners/async_/runner.py` - `map()`
**Change:** When `max_concurrency` is set, use a fixed worker pool instead of creating all tasks upfront.
**Claimed improvement:** ~92% memory reduction, ~25% speedup for 20K items.

**Assessment: Valid optimization.** The original code creates all asyncio.Tasks immediately via list comprehension, even when max_concurrency limits actual parallelism. With 20K items and max_concurrency=50, you'd have 20K pending tasks with only 50 running. The worker pool approach only creates `max_concurrency` workers that pull from a queue. This genuinely reduces memory overhead and task scheduling pressure.

**Verdict:** Makes sense - create benchmark tests to verify. Note: the implementation has a subtle issue - when `max_concurrency` is None, it still creates all tasks at once (same as before), which is correct for unbounded concurrency.

### PR #24: Optimize graph execution by caching controlled_by map
**Files:** `src/hypergraph/graph/core.py`, `src/hypergraph/runners/_shared/helpers.py`
**Change:** Move `controlled_by` map computation from `_get_activated_nodes` (called every superstep) to a lazy cached property on `Graph`.
**Claimed improvement:** ~2.5% execution time improvement for 100-node graph.

**Assessment: Valid but marginal.** The controlled_by map is indeed graph-structure-dependent and invariant during execution. Caching it on the Graph is correct and improves encapsulation. However, the 2.5% improvement is small. The real benefit is architectural cleanliness - the map belongs on Graph, not recomputed per superstep.

**Verdict:** Makes sense - create benchmark tests to verify (though improvement may be small).

## Summary

| PR | Optimization | Valid? | Action |
|----|-------------|--------|--------|
| #19 | Single-loop vs two list comprehensions | Technically yes, practically no | Flag - misleading benchmark |
| #20 | Counter-based exclusive reachability | Yes | Test |
| #21 | defaultdict vs setdefault | Technically yes, practically no | Flag - implausible benchmark |
| #22 | Worker pool for async map | Yes | Test |
| #24 | Cache controlled_by map | Yes (marginal) | Test |

## Implementation Approach

### Benchmarks to Create

Create `benchmarks/test_optimization_prs.py` with:

1. **PR #20 benchmark:** Create graphs with varying numbers of branches (5, 10, 20, 50) and measure `_compute_exclusive_reachability` with both old and new implementations.

2. **PR #22 benchmark:** Create async map operations with varying input sizes (100, 1000, 5000) and max_concurrency=10, measuring memory (via tracemalloc) and wall time for both implementations.

3. **PR #24 benchmark:** Create graphs with gates and measure execution time with controlled_by computed per-superstep vs cached, across multiple runs.

4. **PR #19 & #21 sanity checks:** Include micro-benchmarks at realistic scales (5-50 items) to demonstrate the negligible real-world difference.

### Test Structure

Each benchmark function should:
- Implement both old and new approaches inline (no branch switching)
- Use `time.perf_counter` for timing
- Run multiple iterations for statistical significance
- Print results with percentage improvement
- Assert correctness (both approaches produce identical results)

### Files to Create/Modify
- `benchmarks/test_optimization_prs.py` - All benchmarks
- `.zenflow/tasks/check-optimization-suggestions-0477/report.md` - Results and recommendations

### Verification
- Run benchmarks with `uv run python benchmarks/test_optimization_prs.py`
- Existing tests must still pass: `uv run pytest tests/ -x -q`
