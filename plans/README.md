# Plans

This directory stores implementation plans created by the Claude-Planner agent
as part of the OpenClaw dev team workflow.

## Naming Convention

Plans are named with a zero-padded sequential number followed by a kebab-case
feature name:

```
00001-add-async-caching.md
00002-improve-viz-edge-routing.md
00003-add-type-validation-nodes.md
```

## Plan Format

Each plan file follows this structure:

```markdown
# Plan 00001: Add Async Caching

## Goal
One-sentence description of the feature.

## Files to Change
- `src/hypergraph/runners/async_/runner.py` — reason

## TDD Specification
### Tests to Write First
- [ ] `test_cache_hit_returns_cached_result`: ...
- [ ] `test_cache_miss_executes_node`: ...

## Implementation Stages
### Stage 1: Add cache interface
...
[ ] Stage 1 complete

### Stage 2: Implement LRU cache
...
[ ] Stage 2 complete
```

## Lifecycle

1. Created by Claude-Planner during `/orchestrate-feature`
2. Used by Codex-Agent as the implementation specification
3. Updated by Codex-Agent as stages are completed
4. Archived (kept in git history) after the PR is merged

Plans are committed to the repository so they serve as a record of design
decisions and can be referenced in PR descriptions.
