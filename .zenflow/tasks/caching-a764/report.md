# Tests and Verification Report

## Summary

All cache-related tests pass. 28 cache-specific tests + 59 capability matrix tests + 1076 existing tests = **1163 tests passing** (1 skipped: DiskCache requires optional `diskcache` dependency). 3 pre-existing failures in `test_rich_progress.py` are unrelated to caching.

## Tests Added/Updated

### `tests/test_cache_behavior.py` (updated)
- All tests now use `SyncRunner(cache=InMemoryCache())` instead of bare `SyncRunner()`
- Added `test_cached_node_skips_second_run` — verifies cache hit prevents re-execution
- Updated `test_cached_node_in_dag` — verifies cross-run caching with counter assertion
- Updated `test_multi_input_node_cache_hit` — verifies same-input deduplication
- Updated `test_map_over_with_repeated_values` — verifies `counter.count == 1` for repeated values
- Updated `test_generator_node_with_cache` — verifies generator results cached on second run

### `tests/test_cache_events.py` (new)
- `TestCacheHitEventEmission`: no hit on first run, hit on second run, no hit for uncached nodes
- `TestNodeEndEventCachedField`: `cached=False` on first run, `cached=True` on cache hit
- `test_cache_hit_emits_node_start_before_end`: verifies event order (NodeStart → CacheHit → NodeEnd)

### `tests/test_cache_disk.py` (new)
- `test_cross_runner_cache_hit`: DiskCache persists across separate runner instances
- `test_different_inputs_not_cached`: different inputs produce different keys on disk
- Skipped when `diskcache` not installed via `pytest.importorskip`

### `tests/test_cache_validation.py` (new)
- `test_route_with_cache_builds_successfully`: `cache=True` on `@route` gate is supported
- `test_interrupt_node_cache_raises`: InterruptNode.cache is always False
- `test_graph_node_cache_is_false`: GraphNode.cache is always False

### `tests/capabilities/matrix.py` (updated)
- Added `Caching` enum dimension (`NONE`, `IN_MEMORY`)
- Added `caching` field to `Capability` dataclass
- Updated `all_valid_combinations()` and `pairwise_combinations()` to include caching dimension
- Updated `__str__` to show "cached" suffix

### `tests/capabilities/test_matrix.py` (updated)
- Runner construction uses `InMemoryCache()` when `cap.caching == Caching.IN_MEMORY`

## Linting
`ruff check` passes with no errors on all modified files.
