## Workflow

- Use `uv run X` for scripts (ensures consistent dependencies)
- Use `trash X` instead of `rm X` (allows file recovery)
- Commit frequently and autonomously using `Conventional Commits` format. Use this like a "save" button.

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

## Testing

- Capability tests use pairwise combinations locally (~21 tests) for speed.
- Run `pytest -m full_matrix` for comprehensive coverage (~8K tests, CI only).
- Tests run in parallel via pytest-xdist. Graph builds are cached.
- Add new capability dimensions to `tests/capabilities/matrix.py`.

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

## Maintaining Instructions

After making significant code structure changes, update the AGENTS.md and README.md markdown files

=========

@README.md