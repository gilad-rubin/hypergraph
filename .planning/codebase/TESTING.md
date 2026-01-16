# Testing Patterns

**Analysis Date:** 2026-01-16

## Test Framework

**Runner:**
- pytest >= 8.4.2
- Config: `pyproject.toml` section `[tool.pytest.ini_options]`

**Assertion Library:**
- pytest native assertions (no additional assertion library)

**Async Support:**
- pytest-asyncio >= 1.3.0
- `asyncio_mode = "auto"` (automatic async test detection)

**Run Commands:**
```bash
uv run pytest                    # Run all tests
uv run pytest tests/             # Run tests directory
uv run pytest -v                 # Verbose output
uv run pytest -x                 # Stop on first failure
uv run pytest tests/test_graph.py::TestGraphConstruction  # Run specific class
uv run pytest -k "test_bind"     # Run tests matching pattern
```

## Test File Organization

**Location:**
- Tests in `tests/` directory (separate from source)
- Config: `testpaths = ["tests"]`

**Naming:**
- Files: `test_<module_name>.py`
- Classes: `Test<FeatureOrClass>`
- Methods: `test_<behavior_description>`

**Structure:**
```
tests/
├── __init__.py
├── test_graph.py              # Tests for Graph class
├── test_graph_validation.py   # Graph validation tests
├── test_integration.py        # Integration tests
├── test_nodes_base.py         # Tests for HyperNode base class
├── test_nodes_function.py     # Tests for FunctionNode class
├── test_typing.py             # Tests for type compatibility
├── test_utils.py              # Tests for utility functions
└── unit/                      # Unit test subdirectory (currently empty)
```

**Exclusions:**
- Config: `norecursedirs = ["old", ".*", "build", "dist", "*.egg"]`

## Test Structure

**Class-Based Organization:**
Group related tests into classes with descriptive names:
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
```

**Docstrings:**
- Each test method has a brief docstring explaining what it tests
- Class docstring describes the scope of tests in that class

**Patterns:**
- Arrange-Act-Assert pattern (implicit, not commented)
- Define test fixtures inline within test methods
- Use `@node` decorator for quick node creation in tests

**Typical Test Structure:**
```python
class TestFeatureName:
    """Tests for feature description."""

    def test_expected_behavior(self):
        """Test that feature works in normal case."""
        # Arrange - create test data
        @node(output_name="result")
        def process(x: int) -> int:
            return x * 2

        # Act - perform operation
        g = Graph([process])

        # Assert - verify results
        assert g.inputs.required == ("x",)
        assert g.outputs == ("result",)

    def test_edge_case(self):
        """Test edge case handling."""
        ...

    def test_error_case_raises(self):
        """Test that error is raised for invalid input."""
        with pytest.raises(ErrorType, match="expected message"):
            invalid_operation()
```

## Mocking

**Framework:** No external mocking framework - minimal mocking used

**Patterns:**
- Use `warnings.catch_warnings()` for testing warning behavior:
```python
def test_return_annotation_no_output_name_warns(self):
    """Warning when function has return annotation but no output_name."""
    def foo(x) -> int:
        return x

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        FunctionNode(foo)
        assert len(w) == 1
        assert "has return type" in str(w[0].message)
```

**What to Mock:**
- Warnings (using `warnings.catch_warnings`)
- Nothing else currently - tests use real objects

**What NOT to Mock:**
- Core node/graph functionality
- Type checking utilities
- NetworkX graph operations

## Fixtures and Factories

**Test Data:**
Define functions inline using `@node` decorator:
```python
@node(output_name="x")
def node_a(a: int) -> int:
    return a + 1

@node(output_name="y")
def node_b(x: int) -> int:
    return x * 2

g = Graph([node_a, node_b])
```

**Location:**
- No separate fixtures file - fixtures defined inline in test methods
- No `conftest.py` with shared fixtures

**Pattern for Common Setups:**
```python
class TestGraphInputs:
    """Test Graph.inputs property and InputSpec computation."""

    def test_all_required(self):
        """Test graph with all required parameters."""
        @node(output_name="result")
        def foo(a, b, c):
            return a + b + c

        g = Graph([foo])
        assert g.inputs.required == ("a", "b", "c")
```

## Coverage

**Requirements:** No enforced coverage target

**View Coverage:**
```bash
uv run pytest --cov=hypergraph        # With coverage plugin (if installed)
uv run pytest --cov-report=html       # HTML coverage report
```

## Test Types

**Unit Tests:**
- Location: `tests/test_*.py`
- Scope: Individual classes and methods
- Examples: `test_nodes_function.py`, `test_nodes_base.py`

**Integration Tests:**
- Location: `tests/test_integration.py`
- Scope: Multiple components working together
- Focus: Graph construction with real nodes

**E2E Tests:**
- Framework: None currently implemented
- Note: Playwright listed in dev dependencies for future use

## Common Patterns

**Testing Immutability:**
```python
def test_original_unchanged_after_bind(self):
    """Test original graph is not mutated by bind."""
    @node(output_name="result")
    def foo(x, y):
        return x + y

    g = Graph([foo])
    original_bound = dict(g.inputs.bound)

    g.bind(x=10)  # Create new graph, ignore result

    # Original graph unchanged
    assert g.inputs.bound == original_bound
```

**Testing Errors:**
```python
def test_bind_edge_produced_raises(self):
    """Test binding an edge-produced value raises ValueError."""
    @node(output_name="x")
    def source(a):
        return a * 2

    @node(output_name="result")
    def destination(x):
        return x + 1

    g = Graph([source, destination])

    with pytest.raises(ValueError, match="Cannot bind 'x': output of node 'source'"):
        g.bind(x=10)
```

**Testing Property Returns Copy:**
```python
def test_nodes_property_returns_dict_copy(self):
    """Test that nodes property returns a copy (prevents mutation)."""
    @node(output_name="result")
    def single(x: int) -> int:
        return x

    g = Graph([single])
    nodes_copy = g.nodes

    # Mutating the copy shouldn't affect the graph
    nodes_copy["fake"] = None

    assert "fake" not in g.nodes
```

**Testing Type Compatibility:**
```python
def test_identical_types_are_compatible(self) -> None:
    """Same type should be compatible."""
    assert is_type_compatible(int, int) is True
    assert is_type_compatible(str, str) is True

def test_different_types_are_not_compatible(self) -> None:
    """Different types should not be compatible."""
    assert is_type_compatible(str, int) is False
```

**Async Testing:**
```python
def test_has_async_nodes_true(self):
    """Test has_async_nodes is True when at least one node is async."""
    @node(output_name="x")
    def sync_node(a):
        return a + 1

    @node(output_name="y")
    async def async_node(x):
        return x * 2

    g = Graph([sync_node, async_node])

    assert g.has_async_nodes is True
```

## Warning Filters

**Configuration:**
```toml
filterwarnings = [
    "ignore:The numpy.linalg.linalg has been made private:DeprecationWarning:daft.pickle.cloudpickle",
]
```

---

*Testing analysis: 2026-01-16*
