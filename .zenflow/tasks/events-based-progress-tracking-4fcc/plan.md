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

### [ ] Step: SyncRunner event emission

Integrate event emission into SyncRunner and its executors.

- Add `event_processors: list[EventProcessor] | None = None` parameter to `BaseRunner.run()` and `BaseRunner.map()` signatures
- In `SyncRunner.run()`: create `EventDispatcher`, emit `RunStartEvent`/`RunEndEvent`, pass dispatcher to `_execute_graph()`
- In `run_superstep_sync()`: accept dispatcher, emit `NodeStartEvent` before executor call and `NodeEndEvent`/`NodeErrorEvent` after
- In `SyncRouteNodeExecutor` and `SyncIfElseNodeExecutor`: emit `RouteDecisionEvent` after decision
- In `SyncGraphNodeExecutor`: pass `event_processors` to nested `runner.run()`/`runner.map()` calls with correct `parent_span_id`
- In `SyncRunner.map()`: emit `RunStartEvent(is_map=True, map_size=...)` and `RunEndEvent`, pass processors to each inner `run()`
- Write integration tests in `tests/events/test_sync_events.py` using a `ListProcessor` to assert event sequences for: simple DAG, cyclic graph, nested graph, map, error cases
- Run `uv run pytest tests/events/`

### [ ] Step: AsyncRunner event emission

Mirror sync event emission for the async runner.

- In `AsyncRunner.run()`: same pattern as SyncRunner but use `dispatcher.emit_async()` for `AsyncEventProcessor` instances
- In `run_superstep_async()`: accept dispatcher, emit node events
- In async executors (`AsyncRouteNodeExecutor`, `AsyncIfElseNodeExecutor`, `AsyncGraphNodeExecutor`): emit events matching sync counterparts
- Write integration tests in `tests/events/test_async_events.py` mirroring sync scenarios
- Run `uv run pytest tests/events/`

### [ ] Step: RichProgressProcessor

Implement the Rich-based hierarchical progress bar.

- Create `events/rich_progress.py` with `RichProgressProcessor(TypedEventProcessor)`
- Implement internal state tracking: `_tasks` (span_id -> Rich TaskID), `_depth` (nesting level), `_parents` (span hierarchy), `_node_tasks` (aggregation for map), `_map_total`
- Implement all 7 visual scenarios from requirements: single run, map, nested graph, outer map + nested, nested with inner map, both maps, 3-level nesting
- Handle cyclic graphs with indeterminate/dynamic totals
- Lazy `import rich` — fail with clear error only on instantiation if rich not installed
- Write unit tests in `tests/events/test_rich_progress.py` with mocked Rich Progress object, verifying correct task creation/advancement for each scenario
- Run `uv run pytest tests/events/`

### [ ] Step: Public API and packaging

Finalize exports, dependencies, and documentation.

- Add `EventProcessor`, `AsyncEventProcessor`, `TypedEventProcessor`, `RichProgressProcessor`, and all event types to `src/hypergraph/__init__.py` exports
- Add `rich` optional dependency group in `pyproject.toml` (e.g., `progress = ["rich>=13.0.0"]`) — note: `rich` is already in `telemetry` extras but deserves its own group
- Update `AGENTS.md` with event system info
- Update `README.md` with a progress bar example in the features section
- Run full test suite: `uv run pytest`
- Run lint: `uv run ruff check src/hypergraph/`

### [ ] Step: Edge Case detection

find edge cases, look at capabilities matrix and fix issues in the implementation or surface dillemas
