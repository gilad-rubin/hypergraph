# Testing Guide

How to write and run tests in hypergraph.

## Running Tests

```bash
# Default: parallel, excludes full_matrix and slow
uv run pytest

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

**Note**: Tests run in parallel via `pytest-xdist` by default (`-n auto --dist worksteal`). This is configured in `pyproject.toml`.

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
