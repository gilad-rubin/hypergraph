## Workflow

- Use `uv run X` for scripts (ensures consistent dependencies)
- Use `trash X` instead of `rm X` (allows file recovery)
- Commit frequently and autonomously using `Conventional Commits` format. Use this like a "save" button.

## Output format
- I prefer short sentences, simple examples that emphasize the issue at hand
- Formatted and presented as a markdown document

## Planning
- When discussing design, use 'outside-in' explanations with concrete, user-facing examples - one good example is worth a thousand words
- If you need to read a lot of content - use subagents (with haiku, sonnet) to summarize or answer questions in order to keep the context window clean
- Read the relevant code snippets and search online (using the tools) before answering

## Tools

- Use Context7 and MCP servers to query docs and understand unfamiliar libraries
- Use DeepWiki to query GitHub repos
- Use Perplexity to ask questions and perform research with LLM-powered search results (saves tokens and time)

## Coding Principles

- Follow SOLID principles.
- Use simple, readable functions rather than deeply nested ones.
- Split large functions into focused helpers when needed.
- Visualization measurements live in `src/hypergraph/viz/assets/constants.js`; update that file instead of duplicating constants.
- `Graph.visualize()` auto-sizes and uses `filepath=...` for HTML output.
- Visualization logic lives in Python: `render_graph` precomputes `nodesByState`/`edgesByState`; JS only selects a state and performs layout.

## Testing

- Capability tests use pairwise combinations locally (~21 tests) for speed.
- Run `pytest -m full_matrix` for comprehensive coverage (~8K tests, CI only).
- Tests run in parallel via pytest-xdist. Graph builds are cached.
- Add new capability dimensions to `tests/capabilities/matrix.py`.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## Event System

- Events are emitted during runner execution (`RunStartEvent`, `NodeStartEvent`, etc.)
- Pass `event_processors=[...]` to `runner.run()` or `runner.map()` to observe execution
- `EventProcessor` (sync) and `AsyncEventProcessor` (async) are the base interfaces
- `TypedEventProcessor` auto-dispatches to `on_run_start()`, `on_node_end()`, etc.
- `RichProgressProcessor` provides hierarchical Rich progress bars (requires `pip install 'hypergraph[progress]'`)
- Event types live in `src/hypergraph/events/types.py`; processors in `events/processor.py`
- Tests in `tests/events/`

## Caching System

- Nodes opt-in to result caching with `@node(cache=True)` or `@route(cache=True)`
- Pass `cache=InMemoryCache()` or `cache=DiskCache()` to runner constructor
- Cache keys combine node `definition_hash` + resolved input values (auto-invalidates on code changes)
- `CacheHitEvent` emitted on cache hits; `NodeEndEvent.cached` field tracks cache status
- `CacheBackend` protocol in `src/hypergraph/cache.py`; `DiskCache` requires `pip install 'hypergraph[cache]'`
- Gate nodes (route/ifelse) cache routing decisions; `InterruptNode` and `GraphNode` are never cacheable
- Tests in `tests/test_cache_*.py`; `Caching` dimension in `tests/capabilities/matrix.py`

## Runner Architecture

- Runner input normalization for `values` + kwargs lives in `src/hypergraph/runners/_shared/input_normalization.py`
- Sync runner lifecycle template is `src/hypergraph/runners/_shared/template_sync.py`
- Async runner lifecycle template is `src/hypergraph/runners/_shared/template_async.py`
- `SyncRunner` and `AsyncRunner` should keep execution internals (superstep/executors) in concrete runner files, while shared `run/map` orchestration stays in templates
- Reserved runner option names (like `select`, `map_over`, `max_concurrency`) are control args; colliding input names must be passed via `values={...}`

## Maintaining Instructions

After making significant code structure changes, update the AGENTS.md and README.md markdown files

=========

@README.md
