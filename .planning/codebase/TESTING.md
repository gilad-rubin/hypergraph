# Testing Patterns

**Analysis Date:** 2026-01-21

## Test Framework

**Runner:**
- pytest 8.4.2+
- Config: `pyproject.toml` section `[tool.pytest.ini_options]`
- pytest-asyncio 1.3.0+ for async test support
- pytest-xdist 3.5.0+ for parallel test execution

**Assertion Library:**
- Built-in `assert` statements (no external library)
- `pytest.raises()` context manager for exception testing

**Run Commands:**
```bash
uv run pytest                                # Run all tests (default: pairwise matrix)
uv run pytest -m full_matrix                 # Run full capability matrix (~8K tests, CI only)
uv run pytest -v                             # Verbose output with test names
uv run pytest -k test_name                   # Filter tests by name
uv run pytest --cov                          # Coverage report (if pytest-cov installed)
```

**Key Configuration** (from `pyproject.toml`):
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
markers = [
    "slow: marks tests as slow (run with pytest -m slow)",
    "full_matrix: marks tests that run the full capability matrix (CI only)",
]
addopts = "-n auto --dist worksteal -m 'not full_matrix'"
```

- `-n auto --dist worksteal`: Parallel execution with work-stealing distribution
- `-m 'not full_matrix'`: Default excludes full_matrix (use `-m full_matrix` to run it)
- `asyncio_mode = "auto"`: Automatically detects async test fixtures

## Test File Organization

**Location:**
- Tests co-located in `tests/` directory parallel to `src/hypergraph/`
- Subdirectories mirror source structure: `tests/viz/`, `tests/runners/`, etc.
- Special `tests/capabilities/` for capability matrix infrastructure

**Naming:**
- Test modules: `test_*.py` (e.g., `test_nodes_function.py`, `test_graph.py`)
- Test classes: `Test*` (e.g., `TestFunctionNodeConstruction`, `TestInputSpec`)
- Test functions: `test_*` (e.g., `test_basic_sync_function_no_output()`)

**Directory Structure:**
```
tests/
├── __init__.py
├── test_nodes_function.py         # Tests for FunctionNode
├── test_nodes_gate.py             # Tests for routing nodes (RouteNode, IfElseNode)
├── test_nodes_base.py             # Tests for HyperNode base class
├── test_graph.py                  # Graph construction and InputSpec
├── test_graph_validation.py       # Graph validation logic
├── test_graph_topologies.py       # Complex graph structures
├── test_bind_edge_cases.py        # Graph binding edge cases
├── test_typing.py                 # Type annotation handling
├── test_exception_propagation.py  # Error handling
├── test_cache_behavior.py         # Caching logic
├── test_utils.py                  # Utility functions
├── test_nested_cycle_topologies.py # Nested graphs with cycles
├── runners/                       # Runner-specific tests
│   ├── test_sync_runner.py
│   ├── test_async_runner.py
│   ├── test_execution.py
│   ├── test_routing.py
│   └── test_graphnode_map_over.py
├── viz/                           # Visualization tests
│   ├── test_renderer.py
│   ├── test_html_generator.py
│   └── test_edge_routing.py
└── capabilities/                  # Capability matrix infrastructure
    ├── __init__.py
    ├── matrix.py                  # Capability enums and combinations
    ├── builders.py                # Graph builders for capability testing
    └── test_matrix.py             # Tests for matrix itself
```

## Test Structure

**Suite Organization:**
```python
class TestFunctionNodeConstruction:
    """Tests for FunctionNode.__init__."""

    def test_basic_sync_function_no_output(self):
        """Basic sync function without output_name."""

        def foo(x):
            pass

        fn = FunctionNode(foo)
        assert fn.name == "foo"
        assert fn.inputs == ("x",)
        assert fn.outputs == ()

    def test_name_defaults_to_func_name(self):
        """Name defaults to func.__name__."""

        def my_function(x):
            pass

        fn = FunctionNode(my_function)
        assert fn.name == "my_function"
```

**Patterns:**
- Each test class focuses on one component or feature
- Test docstrings describe what is being tested in plain English
- Arrange-Act-Assert (AAA) pattern:
  1. **Arrange**: Set up test data (define functions, create nodes)
  2. **Act**: Perform action being tested (create FunctionNode, run graph)
  3. **Assert**: Verify results (`assert` statements)

**Fixtures:**
- Module-level fixtures (functions decorated with `@node`) defined at top of test file for reuse:
  ```python
  @node(output_name="doubled")
  def double(x: int) -> int:
      return x * 2

  @node(output_name="incremented")
  def increment(x: int) -> int:
      return x + 1
  ```
- Inline fixtures for one-off test cases
- No pytest `@pytest.fixture` decorator observed (fixtures are simple functions/nodes)

## Test Structure

**Async Test Patterns:**
```python
@pytest.mark.asyncio
async def test_async_runner_with_async_nodes(self):
    """Async runner can execute async nodes."""

    @node(output_name="result")
    async def async_double(x: int) -> int:
        return x * 2

    graph = Graph([async_double])
    runner = AsyncRunner()
    result = await runner.run(graph, {"x": 5})

    assert result.value == {"result": 10}
```

- Tests marked with `@pytest.mark.asyncio`
- Test function declared `async def`
- Await runner results: `result = await runner.run(...)`

**Generator Test Patterns:**
```python
def test_generator_node_yields_multiple_values(self):
    """Sync generator node yields multiple values."""

    @node(output_name="items")
    def gen_items(n: int):
        for i in range(n):
            yield i

    graph = Graph([gen_items])
    runner = SyncRunner()
    result = runner.run(graph, {"n": 3})

    assert result.value == {"items": [0, 1, 2]}
```

- Generators yield multiple values collected into list

## Mocking

**Framework:** unittest.mock (not observed in current tests but standard library available)

**Patterns in Codebase:**
- Minimal mocking observed - tests create real nodes and graphs
- Tests use helper functions to create nodes of specific types rather than mocking:
  ```python
  def _make_sync_func(name: str, input_name: str, output_name: str) -> FunctionNode:
      """Create a sync function node."""

      @node(output_name=output_name)
      def sync_node(**kwargs: Any) -> int:
          val = kwargs.get(input_name, 0)
          return val * 2 if isinstance(val, int) else 0

      return sync_node.with_name(name).with_inputs(**{list(sync_node.inputs)[0]: input_name})
  ```

**What to Mock:**
- External I/O (if testing runners that call external functions)
- Time-dependent behavior (if testing timeouts)
- Generally avoided in current codebase - prefer real node creation

**What NOT to Mock:**
- Node creation/execution logic (test with real nodes)
- Graph structure validation (test real graphs)
- Pure functions (test directly)

## Fixtures and Factories

**Test Data:**
- Node factories in `tests/capabilities/builders.py`:
  ```python
  def _make_sync_func(name: str, input_name: str, output_name: str) -> FunctionNode:
      """Create a sync function node."""
      @node(output_name=output_name)
      def sync_node(**kwargs: Any) -> int:
          val = kwargs.get(input_name, 0)
          return val * 2 if isinstance(val, int) else 0
      return sync_node.with_name(name).with_inputs(**{list(sync_node.inputs)[0]: input_name})

  def _make_async_func(name: str, input_name: str, output_name: str) -> FunctionNode:
      """Create an async function node."""
      @node(output_name=output_name)
      async def async_node(**kwargs: Any) -> int:
          val = kwargs.get(input_name, 0)
          return val * 2 if isinstance(val, int) else 0
      return async_node.with_name(name).with_inputs(**{list(async_node.inputs)[0]: input_name})
  ```

- Graph builders that create specific topologies from Capability specs:
  ```python
  def build_graph_for_capability(cap: Capability) -> Graph:
      """Build a graph matching the capability specification."""
      nodes = _build_nodes_for_topology(cap)
      # Apply nesting, renaming, binding as per capability
      return Graph(nodes, ...)
  ```

**Location:**
- `tests/capabilities/builders.py` - Factory functions for creating test graphs
- Module-level node definitions in individual test files

## Coverage

**Requirements:** No explicit coverage target enforced in configuration

**View Coverage:**
```bash
uv run pytest --cov=src/hypergraph --cov-report=html
# Opens htmlcov/index.html
```

## Test Types

**Unit Tests:**
- Scope: Individual functions, nodes, graph components
- Approach: Direct function/class calls with isolated dependencies
- Examples:
  - `TestFunctionNodeConstruction` - constructor behavior
  - `TestInputSpec` - dataclass properties
  - `TestFunctionNodeProperties` - parameter annotations, defaults
  - Located in `test_nodes_*.py`, `test_graph.py`, etc.

**Integration Tests:**
- Scope: Multiple components working together (e.g., graph + runner)
- Approach: Create real graphs, run with runners, verify end-to-end behavior
- Examples:
  - `test_linear_chain()` in `test_graph.py` - graph construction with dependencies
  - Runner execution tests in `tests/runners/test_sync_runner.py` - graph execution
  - `test_nested_graphs_execute_in_order()` - nested graph execution
  - Located in `tests/runners/`, `test_graph_topologies.py`

**Capability Matrix Tests:**
- Scope: Comprehensive coverage across feature combinations
- Approach: Parametrized testing using pairwise combinations (default) or full matrix (CI)
- Location: `tests/capabilities/test_matrix.py`
- Infrastructure:
  - `matrix.py` defines dimensions (Runner, NodeType, Topology, etc.)
  - `builders.py` creates graphs for capability specs
  - Test uses `@pytest.mark.parametrize` or loops over combinations
  - Pairwise: ~100 combinations for speed (local development)
  - Full matrix: ~8,000 combinations for comprehensive CI coverage

**Example Capability Test:**
```python
@pytest.mark.parametrize("cap", pairwise_combinations())
def test_graph_executes_with_capability(cap: Capability):
    """Test execution with specific capability combination."""
    graph = build_graph_for_capability(cap)
    runner = AsyncRunner() if cap.runner == Runner.ASYNC else SyncRunner()
    result = runner.run(graph, inputs_for_capability(cap))
    assert result.status == RunStatus.COMPLETED
```

## Common Patterns

**Async Testing:**
```python
@pytest.mark.asyncio
async def test_async_runner_concurrency(self):
    """AsyncRunner respects max_concurrency."""
    nodes = [async_double, async_double.with_name("double2")]
    graph = Graph(nodes)
    runner = AsyncRunner(max_concurrency=1)
    result = await runner.run(graph, {"x": 5})
```

**Error Testing:**
```python
def test_missing_required_input_raises(self):
    """Runner raises MissingInputError without required inputs."""
    graph = Graph([double])  # double requires x
    runner = SyncRunner()

    with pytest.raises(MissingInputError) as exc_info:
        runner.run(graph, {})  # Missing x

    assert "x" in exc_info.value.missing
```

**Exception Propagation:**
```python
def test_node_exception_propagates_to_result(self):
    """Exceptions in nodes are captured in RunResult."""

    @node(output_name="result")
    def failing_node(x: int) -> int:
        raise ValueError("Something went wrong")

    graph = Graph([failing_node])
    runner = SyncRunner()
    result = runner.run(graph, {"x": 5})

    assert result.status == RunStatus.ERROR
    assert isinstance(result.error, ValueError)
    assert "Something went wrong" in str(result.error)
```

**Graph Validation Testing:**
```python
def test_duplicate_node_names_raises(self):
    """Graph rejects duplicate node names."""

    @node(output_name="result")
    def process(x: int) -> int:
        return x * 2

    with pytest.raises(GraphConfigError) as exc_info:
        graph = Graph([process, process])  # Same node twice with same name

    assert "Duplicate node name" in str(exc_info.value)
```

## Pytest Markers

**Custom Markers:**
- `@pytest.mark.slow` - Slow tests (rarely used in current codebase)
- `@pytest.mark.full_matrix` - Full capability matrix tests (CI only)

**Usage:**
```bash
pytest -m slow                    # Run only slow tests
pytest -m "not full_matrix"       # Run everything except full_matrix (default)
pytest -m full_matrix             # Run full matrix (CI)
```

## Performance Characteristics

- Pairwise matrix: ~21 combinations, runs locally in seconds
- Full matrix: ~8,000 combinations, runs on CI only
- Parallel execution: `-n auto` uses all CPU cores for work-stealing distribution
- Most individual tests complete in <100ms
- Async tests may take slightly longer due to event loop overhead

## Ignoring and Filtering

**Pytest Configuration** (from `pyproject.toml`):
```toml
norecursedirs = [
    "old",
    ".*",
    "build",
    "dist",
    "*.egg",
]
filterwarnings = [
    "ignore:The numpy.linalg.linalg has been made private:DeprecationWarning:daft.pickle.cloudpickle",
]
```

- Old code in `src/hypergraph/old/` excluded
- Hidden directories (`.git`, `.venv`) excluded
- Build artifacts excluded
- Third-party warnings suppressed where not actionable

---

*Testing analysis: 2026-01-21*
