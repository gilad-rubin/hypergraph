# Final Verification Report

## Test Suite Results

**928 tests passed, 0 failed** (5.77s)

4 warnings from `test_red_team_fixes.py` — expected `UserWarning` about providing values for internal parameters. These are intentional test behaviors, not regressions.

## Summary of Changes

1. **Partial values in run() on failure** — `RunResult.values` now contains successfully computed outputs when a graph execution fails mid-way, instead of returning an empty dict.

2. **error_handling parameter in runner.map()** — Both `SyncRunner.map()` and `AsyncRunner.map()` accept `error_handling="raise"` (default, fail-fast) or `"continue"` (collect all results including failures).

3. **error_handling in as_node().map_over()** — `GraphNode.map_over()` accepts `error_handling` parameter, propagated through executors to `runner.map()`. Failed items use `None` placeholders to preserve list length.

## No Regressions

All pre-existing tests continue to pass. The default `error_handling="raise"` preserves backward-compatible behavior.
