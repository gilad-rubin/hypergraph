# Optimization PRs - Benchmark Report

## Summary

| PR | Optimization | Verdict | Key Finding |
|----|-------------|---------|-------------|
| #19 | Single-loop validation | **Flag** | Inconsistent results; negligible at real scale |
| #20 | Counter-based reachability | **Accept** | 35-73% speedup, scales with branch count |
| #21 | defaultdict vs setdefault | **Reject** | Mixed results; defaultdict sometimes slower |
| #22 | Worker pool for async map | **Accept** | 58-88% speedup, 87-99% memory reduction |
| #24 | Cache controlled_by map | **Accept** | ~99% speedup for repeated lookups |

## Detailed Results

### PR #20: Exclusive Reachability (Counter-based) - ACCEPT

The Counter-based approach shows clear algorithmic improvement that scales with graph complexity:

| Branches | Depth | Nodes | Old | New | Speedup |
|----------|-------|-------|-----|-----|---------|
| 5 | 10 | 55 | 0.028ms | 0.028ms | 0% |
| 10 | 20 | 210 | 0.108ms | 0.094ms | 13% |
| 20 | 30 | 620 | 0.433ms | 0.279ms | 36% |
| 50 | 20 | 1050 | 1.341ms | 0.527ms | 61% |
| 100 | 10 | 1100 | 2.354ms | 0.645ms | 73% |

The speedup grows with branch count, confirming the O(N) vs O(T*N) complexity difference. This is a legitimate algorithmic improvement.

**Recommendation:** Merge.

### PR #22: AsyncRunner.map Worker Pool - ACCEPT

Strong improvements in both time and memory:

| Items | Concurrency | Time Speedup | Memory Reduction |
|-------|------------|-------------|-----------------|
| 100 | 10 | 58% | 87% |
| 1,000 | 10 | 69% | 90% |
| 5,000 | 50 | 80% | 99% |
| 10,000 | 50 | 88% | 98% |

The worker pool avoids creating thousands of pending asyncio tasks. Both time and memory improvements are substantial and scale with input size.

**Recommendation:** Merge. Verify edge cases (max_concurrency=None, empty inputs).

### PR #24: Cache controlled_by Map - ACCEPT

Caching eliminates redundant recomputation across supersteps:

| Graph Size | Rebuild x100 | Cached | Speedup |
|-----------|-------------|--------|---------|
| 40 nodes | 0.215ms | 0.003ms | 99% |
| 110 nodes | 0.642ms | 0.007ms | 99% |
| 220 nodes | 1.699ms | 0.014ms | 99% |
| 500 nodes | 5.755ms | 0.044ms | 99% |

The per-superstep cost is eliminated entirely. While the absolute savings per execution may be small (single-digit ms), it's a clean architectural improvement - the map is graph-structure-dependent and belongs cached on Graph.

**Recommendation:** Merge. The benefit is primarily architectural cleanliness with a minor performance bonus.

### PR #19: Single-loop Validation - FLAG

Results are inconsistent across sizes:

| Size | Two-comp | Single-loop | Diff |
|------|----------|-------------|------|
| 5 | 22.5ms | 14.1ms | +37% |
| 10 | 36.4ms | 27.8ms | +24% |
| 20 | 45.6ms | 50.7ms | **-11%** |
| 50 | 191.8ms | 83.2ms | +57% |

At size=20 the single-loop is actually slower. These are 50K-iteration microbenchmarks on lists of 5-50 items - the total difference per real call is nanoseconds. The PR's claimed 24% speedup on 1M items is irrelevant since real graphs never have 1M shared parameters.

**Recommendation:** Reject. The optimization is inconsistent, provides no real-world benefit, and trades two readable list comprehensions for a less idiomatic loop.

### PR #21: defaultdict vs setdefault - REJECT

Results are mixed and contradict the PR's claims:

| Nodes | Outputs/Node | setdefault | defaultdict | Diff |
|-------|-------------|-----------|-------------|------|
| 5 | 2 | 26.3ms | 33.1ms | **-26%** (defaultdict slower) |
| 10 | 3 | 69.2ms | 62.8ms | +9% |
| 20 | 5 | 492.1ms | 299.6ms | +39% |

At the smallest (most realistic) scale, defaultdict is actually 26% **slower**. The PR claimed 41% improvement, but our benchmarks show inconsistent results. Real validation loops iterate over a handful of nodes - the difference is negligible.

**Recommendation:** Reject. The claimed 41% improvement is not reproducible. At realistic scales the results are mixed, with defaultdict sometimes slower.

## Overall Recommendations

1. **Merge PRs #20, #22, #24** - These provide genuine, measurable improvements with sound algorithmic reasoning.
2. **Reject PRs #19 and #21** - These are micro-optimizations with misleading benchmarks that provide no real-world benefit. The code changes trade readability for negligible (and inconsistent) performance gains.
