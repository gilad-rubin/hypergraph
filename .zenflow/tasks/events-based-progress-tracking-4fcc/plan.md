# Full SDD workflow

## Configuration
- **Artifacts Path**: {@artifacts_path} → `.zenflow/tasks/{task_id}`

---

## Workflow Steps

### [x] Step: Requirements
<!-- chat-id: 4176d99b-dedc-4e40-ae4a-6887d570675b -->

Create a Product Requirements Document (PRD) based on the feature description.

1. Review existing codebase to understand current architecture and patterns
2. Analyze the feature definition and identify unclear aspects
3. Ask the user for clarifications on aspects that significantly impact scope or user experience
4. Make reasonable decisions for minor details based on context and conventions
5. If user can't clarify, make a decision, state the assumption, and continue

Save the PRD to `{@artifacts_path}/requirements.md`.

### [x] Step: Technical Specification
<!-- chat-id: dc98052a-6c80-484d-b5a0-207e8cbf0272 -->

Create a technical specification based on the PRD in `{@artifacts_path}/requirements.md`.

1. Review existing codebase architecture and identify reusable components
2. Define the implementation approach

Save to `{@artifacts_path}/spec.md` with:
- Technical context (language, dependencies)
- Implementation approach referencing existing code patterns
- Source code structure changes
- Data model / API / interface changes
- Delivery phases (incremental, testable milestones)
- Verification approach using project lint/test commands

### [x] Step: Planning
<!-- chat-id: 9836273a-88f6-4224-a1c0-2bc458242119 -->

Create a detailed implementation plan based on `{@artifacts_path}/spec.md`.

### [x] Step: Event types and processor interfaces
<!-- chat-id: 861f1fb2-b63d-4e8f-a737-f392a8a09d89 -->

Create `src/hypergraph/events/` package with core types and interfaces.

- Create `events/types.py` with all 8 event dataclasses (`BaseEvent`, `RunStartEvent`, `RunEndEvent`, `NodeStartEvent`, `NodeEndEvent`, `NodeErrorEvent`, `RouteDecisionEvent`, `InterruptEvent`, `StopRequestedEvent`) and `Event` union type
- Create `events/processor.py` with `EventProcessor`, `AsyncEventProcessor`, `TypedEventProcessor` base classes
- Create `events/dispatcher.py` with `EventDispatcher` (manages processor list, best-effort emit/emit_async, shutdown)
- Create `events/__init__.py` with public exports
- Write unit tests in `tests/events/test_types.py` and `tests/events/test_dispatcher.py` covering: TypedEventProcessor auto-dispatch, EventDispatcher best-effort error handling, event immutability
- Run `uv run ruff check src/hypergraph/events/` and `uv run pytest tests/events/`

### [x] Step: SyncRunner event emission
<!-- chat-id: f622a887-a6c5-4ba4-b55d-b7cfc0ba259b -->

Integrate event emission into SyncRunner and its executors.

- Add `event_processors: list[EventProcessor] | None = None` parameter to `BaseRunner.run()` and `BaseRunner.map()` signatures
- In `SyncRunner.run()`: create `EventDispatcher`, emit `RunStartEvent`/`RunEndEvent`, pass dispatcher to `_execute_graph()`
- In `run_superstep_sync()`: accept dispatcher, emit `NodeStartEvent` before executor call and `NodeEndEvent`/`NodeErrorEvent` after
- In `SyncRouteNodeExecutor` and `SyncIfElseNodeExecutor`: emit `RouteDecisionEvent` after decision
- In `SyncGraphNodeExecutor`: pass `event_processors` to nested `runner.run()`/`runner.map()` calls with correct `parent_span_id`
- In `SyncRunner.map()`: emit `RunStartEvent(is_map=True, map_size=...)` and `RunEndEvent`, pass processors to each inner `run()`
- Write integration tests in `tests/events/test_sync_events.py` using a `ListProcessor` to assert event sequences for: simple DAG, cyclic graph, nested graph, map, error cases
- Run `uv run pytest tests/events/`

### [x] Step: AsyncRunner event emission
<!-- chat-id: 5d61b882-7c58-458b-ab12-03adae04db79 -->

Mirror sync event emission for the async runner.

- In `AsyncRunner.run()`: same pattern as SyncRunner but use `dispatcher.emit_async()` for `AsyncEventProcessor` instances
- In `run_superstep_async()`: accept dispatcher, emit node events
- In async executors (`AsyncRouteNodeExecutor`, `AsyncIfElseNodeExecutor`, `AsyncGraphNodeExecutor`): emit events matching sync counterparts
- Write integration tests in `tests/events/test_async_events.py` mirroring sync scenarios
- Run `uv run pytest tests/events/`

### [x] Step: RichProgressProcessor
<!-- chat-id: f0e663a2-624d-4c2a-8ca8-85b066643e29 -->

Implement the Rich-based hierarchical progress bar.

- Create `events/rich_progress.py` with `RichProgressProcessor(TypedEventProcessor)`
- Implement internal state tracking: `_tasks` (span_id -> Rich TaskID), `_depth` (nesting level), `_parents` (span hierarchy), `_node_tasks` (aggregation for map), `_map_total`
- Implement all 7 visual scenarios from requirements: single run, map, nested graph, outer map + nested, nested with inner map, both maps, 3-level nesting
- Handle cyclic graphs with indeterminate/dynamic totals
- Lazy `import rich` — fail with clear error only on instantiation if rich not installed
- Write unit tests in `tests/events/test_rich_progress.py` with mocked Rich Progress object, verifying correct task creation/advancement for each scenario
- Run `uv run pytest tests/events/`

### [x] Step: Public API and packaging
<!-- chat-id: ef369419-7b8f-4a70-8f4c-59cb7926615f -->

Finalize exports, dependencies, and documentation.

- Add `EventProcessor`, `AsyncEventProcessor`, `TypedEventProcessor`, `RichProgressProcessor`, and all event types to `src/hypergraph/__init__.py` exports
- Add `rich` optional dependency group in `pyproject.toml` (e.g., `progress = ["rich>=13.0.0"]`) — note: `rich` is already in `telemetry` extras but deserves its own group
- Update `AGENTS.md` with event system info
- Update `README.md` with a progress bar example in the features section
- Run full test suite: `uv run pytest`
- Run lint: `uv run ruff check src/hypergraph/`

### [x] Step: Edge Case detection
<!-- chat-id: 6b04b82d-dcfc-445f-9644-4c9a6300cf35 -->

find edge cases, look at capabilities matrix and fix issues in the implementation or surface dillemas

### [x] Step: Improve code
<!-- chat-id: be9505dc-3ee2-418f-b430-6362bcba2f65 -->

Make sure all functions have docstrings, type hints. Read CLAUDE.md, "flat structure" rule, "code smells" skills and improve implementation;

### [x] Step: Update the docs, README, api reference;
<!-- chat-id: 473568f5-9178-48a5-9f02-99e7720f7120 -->

Update the docs, README, api reference; Add examples for real life usage, like the rest of the docs
