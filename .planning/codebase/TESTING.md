# Testing Patterns

**Analysis Date:** 2026-01-16

## Test Framework

**Runner:**
- pytest >= 8.4.2
- Config: `pyproject.toml` under `[tool.pytest.ini_options]`

**Assertion Library:**
- Built-in pytest assertions (no external library)

**Async Testing:**
- pytest-asyncio >= 1.3.0
- Mode: `asyncio_mode = "auto"` (automatic async test detection)

**Run Commands:**
```bash
uv run pytest                    # Run all tests
uv run pytest -v                 # Verbose output
uv run pytest tests/test_graph.py  # Single file
uv run pytest -k "test_basic"    # Pattern match
```

## Test File Organization

**Location:**
- Separate `tests/` directory at project root (not co-located)

**Naming:**
- `test_<module_name>.py` pattern
- Maps to source: `src/hypergraph/graph.py` -> `tests/test_graph.py`

**Structure:**
```
tests/
├── __init__.py
├── test_graph.py           # Tests for graph.py
├── test_graph_validation.py # Graph validation tests
├── test_integration.py     # End-to-end and cross-module tests
├── test_nodes_base.py      # Tests for nodes/base.py
├── test_nodes_function.py  # Tests for nodes/function.py
└── test_utils.py           # Tests for _utils.py
```

## Test Structure

**Suite Organization:**
```python
class TestClassName:
    """Docstring describing test group purpose."""

    def test_specific_behavior(self):
        """Docstring describing what this test verifies."""
        # Arrange
        @node(output_name="result")
        def foo(x):
            return x * 2

        # Act
        g = Graph([foo])

        # Assert
        assert g.inputs.required == ("x",)
```

**Patterns:**
- Group related tests in classes (e.g., `TestFunctionNodeConstruction`)
- One assertion focus per test method
- Descriptive docstrings explaining test purpose
- Test classes inherit nothing (plain classes, not unittest.TestCase)

**Class naming:**
- `TestClassName` for subject under test (e.g., `TestFunctionNodeConstruction`)
- `TestFeatureName` for behavior groups (e.g., `TestGraphBind`)

## Mocking

**Framework:** No external mocking library used

**Patterns:**
- Define test-local functions instead of mocking:
```python
def test_basic_sync_function(self):
    def foo(x):
        pass

    fn = FunctionNode(foo)
    assert fn.name == "foo"
```

**Warnings testing:**
```python
def test_return_annotation_no_output_name_warns(self):
    def foo(x) -> int:
        return x

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        FunctionNode(foo)
        assert len(w) == 1
        assert "has return type" in str(w[0].message)
```

**What to Mock:**
- Not applicable - this codebase tests pure functions/classes without external dependencies

**What NOT to Mock:**
- Core logic under test
- Simple helper functions
- Data structures

## Fixtures and Factories

**Test Data:**
- Define functions inline within test methods:
```python
def test_linear_chain(self):
    @node(output_name="a_out")
    def node_a(x: int) -> int:
        return x + 1

    @node(output_name="b_out")
    def node_b(a_out: int) -> int:
        return a_out * 2

    g = Graph([node_a, node_b])
    # assertions...
```

**Location:**
- No shared fixtures directory
- Test data defined inline per test
- No pytest fixtures observed

## Coverage

**Requirements:** Not enforced (no coverage config)

**View Coverage:**
```bash
uv run pytest --cov=src/hypergraph   # Requires pytest-cov
```

## Test Types

**Unit Tests:**
- Located in `tests/test_*.py`
- Test individual functions and classes in isolation
- Examples: `test_utils.py`, `test_nodes_function.py`

**Integration Tests:**
- Located in `tests/test_integration.py`
- Test cross-module interactions and workflows
- Examples: `TestEndToEndWorkflow`, `TestPublicAPIImports`

**Validation Tests:**
- Located in `tests/test_graph_validation.py`
- Test build-time validation rules
- Examples: `TestGraphNameValidation`, `TestConsistentDefaultsValidation`

**E2E Tests:**
- Not used - no external systems to test against

## Common Patterns

**Async Testing:**
```python
@pytest.mark.asyncio
async def test_async_function_call(self):
    @node(output_name="result")
    async def fetch_data(url: str) -> str:
        return f"fetched: {url}"

    result = await fetch_data("http://example.com")
    assert result == "fetched: http://example.com"
```

**Error Testing:**
```python
def test_bind_edge_produced_raises(self):
    @node(output_name="x")
    def producer(a):
        return a * 2

    g = Graph([producer])

    with pytest.raises(ValueError, match="Cannot bind 'x': output of node"):
        g.bind(x=10)
```

**Immutability Testing:**
```python
def test_with_name_does_not_mutate(self):
    @node(output_name="result")
    def foo():
        return 1

    original_name = foo.name
    renamed = foo.with_name("bar")

    assert foo.name == original_name  # Original unchanged
    assert renamed.name == "bar"      # New instance modified
```

**Warning Testing:**
```python
def test_no_annotation_no_warning(self):
    def foo(x):
        pass

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        FunctionNode(foo)
        assert len(w) == 0
```

**Property/Attribute Testing:**
```python
def test_definition_hash_is_sha256(self):
    @node(output_name="result")
    def foo(x):
        return x

    g = Graph([foo])

    assert len(g.definition_hash) == 64
    assert all(c in "0123456789abcdef" for c in g.definition_hash)
```

## Test Organization by Concern

**Construction tests:** `TestFunctionNodeConstruction`, `TestGraphConstruction`
- Verify object creation with various parameters

**Property tests:** `TestFunctionNodeProperties`, `TestGraphFeatureProperties`
- Verify computed properties return correct values

**Method tests:** `TestFunctionNodeCall`, `TestGraphBind`
- Verify method behavior

**Validation tests:** `TestGraphNameValidation`, `TestConsistentDefaultsValidation`
- Verify error handling and validation rules

**Integration tests:** `TestEndToEndWorkflow`, `TestPublicAPIImports`
- Verify cross-module behavior

## Pytest Configuration

From `pyproject.toml`:
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
norecursedirs = ["old", ".*", "build", "dist", "*.egg"]
filterwarnings = [
    "ignore:The numpy.linalg.linalg has been made private:DeprecationWarning:daft.pickle.cloudpickle",
]
```

---

*Testing analysis: 2026-01-16*
