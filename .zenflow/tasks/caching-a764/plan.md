# Spec and build

## Configuration
- **Artifacts Path**: {@artifacts_path} → `.zenflow/tasks/{task_id}`

---

## Agent Instructions

Ask the user questions when anything is unclear or needs their input. This includes:
- Ambiguous or incomplete requirements
- Technical decisions that affect architecture or user experience
- Trade-offs that require business context

Do not make assumptions on important decisions — get clarification first.

---

## Workflow Steps

### [x] Step: Technical Specification
<!-- chat-id: 1cebdb2d-1cdd-4d25-aadf-44fe927c2450 -->

Assess the task's difficulty, as underestimating it leads to poor outcomes.
- easy: Straightforward implementation, trivial bug fix or feature
- medium: Moderate complexity, some edge cases or caveats to consider
- hard: Complex logic, many caveats, architectural considerations, or high-risk changes

Create a technical specification for the task that is appropriate for the complexity level:
- Review the existing codebase architecture and identify reusable components.
- Define the implementation approach based on established patterns in the project.
- Identify all source code files that will be created or modified.
- Define any necessary data model, API, or interface changes.
- Describe verification steps using the project's test and lint commands.

Save the output to `{@artifacts_path}/spec.md` with:
- Technical context (language, dependencies)
- Implementation approach
- Source code structure changes
- Data model / API / interface changes
- Verification approach

If the task is complex enough, create a detailed implementation plan based on `{@artifacts_path}/spec.md`:
- Break down the work into concrete tasks (incrementable, testable milestones)
- Each task should reference relevant contracts and include verification steps
- Replace the Implementation step below with the planned tasks

Rule of thumb for step size: each step should represent a coherent unit of work (e.g., implement a component, add an API endpoint, write tests for a module). Avoid steps that are too granular (single function).

Save to `{@artifacts_path}/plan.md`. If the feature is trivial and doesn't warrant this breakdown, keep the Implementation step below as is.

---

### [x] Step: Cache backend and event types
<!-- chat-id: a97c9e17-0394-4bff-a6f7-68a202b79b0c -->

Create the cache module and add the cache event:
- Create `src/hypergraph/cache.py` with `CacheBackend` protocol, `InMemoryCache` (with optional `max_size` LRU), `DiskCache`, and `compute_cache_key()` helper
- Add `CacheHitEvent` to `src/hypergraph/events/types.py` and update the `Event` union
- Add `cached: bool = False` field to `NodeEndEvent`
- Add `on_cache_hit` to `TypedEventProcessor` in `src/hypergraph/events/processor.py`
- Add `[cache]` optional dependency for `diskcache` in `pyproject.toml`
- Update `src/hypergraph/__init__.py` exports
- Add build-time validation: `cache=True` disallowed on `GateNode`, `InterruptNode`, `GraphNode`

### [x] Step: Runner and superstep integration
<!-- chat-id: 918db6c6-c8b8-4e10-9b60-317ea2d9b388 -->

Wire caching into the execution path:
- Add `cache` parameter to `SyncRunner.__init__` and `AsyncRunner.__init__` (not BaseRunner — cache is concrete)
- Pass cache backend through to superstep functions
- Add cache lookup/store logic in `run_superstep_sync` and `run_superstep_async` around `execute_node`
- On cache hit: emit `NodeStartEvent` → `CacheHitEvent` → `NodeEndEvent(cached=True)`, skip execution
- Cache propagation to nested graphs is automatic (executors use `self.runner`)

### [x] Step: Tests and verification
<!-- chat-id: 7c2a44ee-5d7e-482c-8675-71e89f06c8c0 -->

Update existing tests and add new ones:
- Update `tests/test_cache_behavior.py` to use `SyncRunner(cache=InMemoryCache())` and verify cache hit counts
- Create `tests/test_cache_events.py` for `CacheHitEvent` emission and `NodeEndEvent.cached` field
- Add `DiskCache` tests for cross-run persistence
- Add build-time validation tests (cache=True on gates, InterruptNode, GraphNode)
- Add `Caching` dimension to `tests/capabilities/matrix.py`
- Run `uv run pytest` and `uv run ruff check`
- Write report to `{@artifacts_path}/report.md`

# Full SDD workflow

## Configuration
- **Artifacts Path**: {@artifacts_path} → `.zenflow/tasks/{task_id}`

### [x] Step: Edge Cases & Red-Team
<!-- chat-id: 33dd9abb-09fb-451e-9f05-5ea711e09c2d -->

find edge cases, look at capabilities matrix and fix issues in the implementation or surface dilemmas; "Red Team" the implemenation and try to find and create failing tests. use subagents

### [x] Step: Improve Code quality
<!-- chat-id: 9a4901c5-6851-429b-aa18-7cd029d78300 -->

Make sure all functions have docstrings, type hints. Read CLAUDE.md, "flat structure" rule, "code smells" skills and improve implementation;

### [x] Step: Update Documentation
<!-- chat-id: 7cb74301-9c16-42a4-a05b-fed02fe5210e -->

Update the docs, README, api reference; Add examples for real life usage, like the rest of the docs


### [ ] Step: PR
<!-- chat-id: 51dbee68-8386-4c01-b51d-cc60643ed8eb -->

Create a PR push; Wait 15 minutes and read the comments on the PR, make improvements (add TDD) until no further comments are made
