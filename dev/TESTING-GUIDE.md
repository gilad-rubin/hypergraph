# Testing Guide

How to write and run tests in hypergraph.

## Running Tests

```bash
# Default: parallel, excludes full_matrix and slow
uv run pytest

# CI-equivalent (run before PR — catches warning-as-error failures)
uv run pytest -W error -W 'ignore::pytest.PytestUnraisableExceptionWarning'

# Single file
uv run pytest tests/test_specific.py

# Single test by name
uv run pytest -k "test_name"

# Include slow tests
uv run pytest -m slow

# Full capability matrix (~8K tests, CI only)
uv run pytest -m full_matrix

# Verbose output (overrides default -q)
uv run pytest -v tests/test_specific.py
```

**Note**: Tests run in parallel via `pytest-xdist` by default (`-n auto --dist loadfile`). This is configured in `pyproject.toml`.

### CI vs Local

CI runs `pytest -W error` (all warnings become errors). Plain `uv run pytest` does **not** — so tests can pass locally but fail in CI. Always run the CI-equivalent command before pushing a PR.

The `-W 'ignore::pytest.PytestUnraisableExceptionWarning'` suppresses GC-triggered cleanup warnings from `__del__` methods (sockets, event loops). These are non-deterministic and not real bugs. **Important**: `filterwarnings` in `pyproject.toml` does NOT suppress these — pytest's `collect_unraisable` hook fires after the `catch_warnings` context manager exits, so only global `-W` flags from the CLI take effect.

## Test Patterns

### Unit: Test Node Functions Directly

Nodes are pure functions. Test them without the framework:

```python
def test_embed():
    result = embed.func("hello world")
    assert len(result) == 768
```

No `Graph`, no `Runner`. Use `node.func(...)` to call the underlying function.

### Integration: Build Graph, Run, Assert Outputs

```python
def test_pipeline():
    graph = Graph([embed, retrieve, generate])
    runner = SyncRunner()
    result = runner.run(graph, {"text": "hello"})
    assert "answer" in result.outputs
```

For async:

```python
async def test_async_pipeline():
    runner = AsyncRunner()
    result = await runner.run(graph, {"text": "hello"})
    assert "answer" in result.outputs
```

`asyncio_mode = "auto"` means just write `async def test_*` and it works.

### Validation: Assert Build-Time Errors

```python
def test_invalid_route_target():
    @route(targets=["step_a", END])
    def decide(x: int) -> str:
        return "step_c"  # invalid

    with pytest.raises(GraphConfigError, match="not found"):
        Graph([decide, step_a])
```

Always match on a specific substring of the error message.

### Viz: Playwright-Based

Viz tests use Playwright. Shared fixtures live in `tests/viz/conftest.py`:

- `make_workflow()` — graph factories
- `browser`, `page` — Playwright browser/page
- Debug extractors for inspecting rendered output

```bash
# Setup (one-time)
uv run playwright install chromium

# Run viz tests
uv run pytest tests/viz/
```

## Capability Matrix

Located in `tests/capabilities/`. Parametrized pairwise testing across feature combinations.

### What It Is

Each test dimension (node types, runner types, topologies, etc.) is an enum. Tests are generated from pairwise combinations of these dimensions, covering interaction effects without exhaustive enumeration.

### Two Modes

- **Pairwise** (default, fast): `uv run pytest` — ~21 tests, covers all 2-way interactions
- **Full matrix** (CI): `uv run pytest -m full_matrix` — ~8K tests, exhaustive

### When to Update

Update the capability matrix when:
- Adding a new node type
- Adding a new runner feature
- Adding a new control flow pattern

### How to Add a Dimension

1. Add enum values to `tests/capabilities/matrix.py`
2. Update graph factory in `tests/capabilities/builders.py`
3. Add new test parameters

## Test Organization

```
tests/
  capabilities/      # Pairwise capability matrix
  events/            # Event system tests
  viz/               # Visualization tests (Playwright)
  test_*.py          # Unit and integration tests
```

## Conventions

- Test files: `test_<module>.py` or `test_<feature>.py`
- Test classes: `class TestFeatureName:` for grouping related tests
- Descriptive names: `test_route_node_rejects_async_function` not `test_route_1`
- Fixtures: prefer `conftest.py` for shared fixtures, local for one-file fixtures
- Marks: `@pytest.mark.slow` for slow tests, `@pytest.mark.full_matrix` for exhaustive

### Scheduling: Test Node Readiness Directly

For subtle scheduling bugs, test `get_ready_nodes` directly against `GraphState`:

```python
from hypergraph.runners._shared.helpers import compute_execution_scope, get_ready_nodes
from hypergraph.runners._shared.types import GraphState, NodeExecution

scope = compute_execution_scope(graph)
state = GraphState()
state.update_value("x", 42)

ready = get_ready_nodes(
    graph, state,
    active_nodes=scope.active_nodes,
    startup_predecessors=scope.startup_predecessors,
)
assert "my_node" in [n.name for n in ready]
```

Simulate execution by adding `NodeExecution` entries to `state.node_executions` and routing decisions to `state.routing_decisions`. This lets you verify scheduling invariants superstep-by-superstep without running the full engine.

See `tests/test_gate_activation_scheduling.py` and `tests/test_interrupt_shared_cycle_bug.py` for examples.

### Interrupt: Test Pause/Resume with Checkpointer

Multi-turn interrupt tests need a checkpointer — each `.run()` call simulates a separate request:

```python
@pytest.mark.asyncio
async def test_pause_resume(self, tmp_path):
    from hypergraph.checkpointers import SqliteCheckpointer

    cp = SqliteCheckpointer(str(tmp_path / "test.db"))
    try:
        runner = AsyncRunner(checkpointer=cp)

        r1 = await runner.run(graph, workflow_id="w", user_input="hello")
        assert r1.paused

        r2 = await runner.run(graph, workflow_id="w", user_input="more")
        assert r2.paused  # or not, depending on graph logic
    finally:
        await cp.close()
```

Without a checkpointer, the runner has no state between calls — each run starts fresh.

### Async Fixtures: Always Close Long-Lived Resources

When a test creates async resources with background helpers or worker threads, teardown must be explicit.

This matters especially for:

- `SqliteCheckpointer`
- raw `aiosqlite` connections
- browser/process fixtures outside the shared Playwright setup

`aiosqlite` uses a worker thread behind the async connection. If a test fixture yields a live connection and never awaits `close()`, pytest may tear down the event loop first. The worker thread then tries to report back into a closed loop and you get flaky warnings like:

```text
PytestUnhandledThreadExceptionWarning
RuntimeError: Event loop is closed
```

Prefer async fixtures for async resources:

```python
import pytest_asyncio

@pytest_asyncio.fixture
async def checkpointer(tmp_path):
    cp = SqliteCheckpointer(str(tmp_path / "test.db"))
    yield cp
    await cp.close()
```

Avoid this pattern for async-backed resources:

```python
@pytest.fixture
def checkpointer(tmp_path):
    cp = SqliteCheckpointer(str(tmp_path / "test.db"))
    yield cp
    # no await close() -> worker thread may outlive the test loop
```

For sync-only tests using the sync connection path, close the sync handle explicitly in teardown.

## Common Gotchas

### Stop/Timing Tests: Use Events, Not Sleeps

Tests that coordinate `runner.stop()` with a running node must use `asyncio.Event` for synchronization, not fixed `asyncio.sleep()` delays. Under xdist, sleep-based coordination is unreliable because worker load affects scheduling:

```python
# WRONG — flaky under xdist, stop may fire before or after node completes
async def stop_soon():
    await asyncio.sleep(0.1)  # hope the node started by now
    runner.stop("wf")

# RIGHT — deterministic: stop only fires after node confirms it's running
node_started = asyncio.Event()

@node(output_name="result")
async def my_node(ctx: NodeContext) -> str:
    node_started.set()  # signal we're running
    for chunk in generate():
        if ctx.stop_requested:
            break
    return result

async def stop_after_start():
    await node_started.wait()  # guaranteed the node is executing
    await asyncio.sleep(0.02)  # small buffer for node to enter loop
    runner.stop("wf")
```

### Never Use `asyncio.run()` in Tests

Tests run in parallel via pytest-xdist. `asyncio.run()` inside a sync `def test_*` creates a rogue event loop that races with pytest-asyncio's loop lifecycle — causing flaky "Cannot run the event loop while another loop is running" errors in *other* async tests sharing the same worker.

Since `asyncio_mode = "auto"` is configured, just make the test `async def` and use `await`:

```python
# WRONG — creates unmanaged event loop, causes xdist flakes
def test_async_behavior(self):
    result = asyncio.run(runner.run(graph, {"x": 5}))

# RIGHT — pytest-asyncio manages the loop
async def test_async_behavior(self):
    result = await runner.run(graph, {"x": 5})
```

### Treat Thread-Shutdown Warnings As Real Failures

If you see flaky warnings like `PytestUnhandledThreadExceptionWarning`, do not assume they are harmless just because the test body passed.

First isolate the test and promote the warning to an error:

```bash
uv run pytest tests/test_checkpointer/test_resume.py -k override_workflow_auto_forks_existing_id \
  -W error::pytest.PytestUnhandledThreadExceptionWarning
```

This usually turns intermittent teardown noise into a deterministic failure you can debug.

For async checkpointer/resource issues, check:

1. Is the fixture async?
2. Does teardown explicitly `await close()`?
3. Are background tasks/threads being awaited before loop shutdown?
4. Is the warning coming from test cleanup rather than the feature under test?

### Cycle Tests Need Unique Output Names

Each node in a cycle must produce a **unique** output name. Two nodes producing the same output triggers `validate_output_conflicts` unless they are in mutex gate branches or connected by a directed path.

```python
# WRONG — two producers of "state" raises GraphConfigError
@node(output_name="state")
def init(seed): return seed
@node(output_name="state")
def update(processed): return processed

# RIGHT — unique outputs, cycle formed by data dependencies
@node(output_name="a")
def node_a(c: int) -> int: return c + 1
@node(output_name="b")
def node_b(a: int) -> int: return a * 2
@node(output_name="c")
def node_c(b: int) -> int: return b - 1
# Graph([node_a, node_b, node_c]) → valid 3-node cycle A→B→C→A
```

### with_entrypoint in Pure Cycles

`with_entrypoint("B")` in a pure cycle (A→B→C→A) does **not** exclude any cycle member — all are forward-reachable from each other. It only excludes DAG nodes upstream of the cycle. Test narrowing with a DAG-feeding-cycle topology instead.

### Cycle Tests Should Match the SCC Mental Model

The runner now plans execution as a DAG of strongly connected components (SCCs):

- DAG regions advance in topological order
- Cycles execute as a local fixed-point region until quiescence
- Gates are dynamic activation inside that region

When writing tests, prefer assertions that match those observable semantics:

- Downstream DAG nodes should not run before an upstream cyclic SCC settles
- Gate-driven loops should behave like one local execution region, even if the feedback edge is a control edge
- `wait_for` is still a freshness signal, not just a build-time ordering hint

### bind() Rejects Inactive Inputs

`with_entrypoint("downstream")` narrows the valid input set. `bind(x=5)` for an upstream input will raise `ValueError` because `x` is no longer in the graph's valid inputs. Use `runner.run(graph, {"x": 5})` for override-style injection (will trigger an "internal parameters" warning).
