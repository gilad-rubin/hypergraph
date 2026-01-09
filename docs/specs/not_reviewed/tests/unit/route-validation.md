# Unit Tests: Route Validation

## Overview

These tests verify that `@route` and `@branch` decorators are validated correctly at both build-time and runtime.

---

## Test Category: Build-Time Target Validation

### test_route_requires_targets

```python
def test_route_requires_targets():
    """@route must have targets parameter."""
    with pytest.raises(TypeError):
        @route()  # Missing targets
        def decide(x: int) -> str:
            return "something"
```

### test_route_targets_not_empty

```python
def test_route_targets_not_empty():
    """@route targets cannot be empty list."""
    with pytest.raises(ValueError) as exc:
        @route(targets=[])
        def decide(x: int) -> str:
            return "something"
    
    assert "at least one target" in str(exc.value).lower()
```

### test_route_targets_validated_at_graph_construction

```python
def test_route_targets_validated_at_graph_construction():
    """Invalid targets caught when Graph() is called."""
    @node(outputs="result")
    def process(x: int) -> int:
        return x
    
    @route(targets=["process", "nonexistent", END])
    def decide(x: int) -> str:
        return "process"
    
    # Decorator succeeds (no validation yet)
    assert decide is not None
    
    # Graph construction fails
    with pytest.raises(GraphConfigError) as exc:
        Graph(nodes=[process, decide])
    
    assert "nonexistent" in str(exc.value)
```

### test_all_targets_validated

```python
def test_all_targets_validated():
    """All targets in list are validated, not just first."""
    @node(outputs="a")
    def node_a(x: int) -> int:
        return x
    
    @route(targets=["a", "b", "c", END])  # b and c don't exist
    def decide(x: int) -> str:
        return "a"
    
    with pytest.raises(GraphConfigError) as exc:
        Graph(nodes=[node_a, decide])
    
    # Should mention at least one missing target
    error_msg = str(exc.value)
    assert "b" in error_msg or "c" in error_msg
```

---

## Test Category: END Sentinel

### test_end_must_be_declared

```python
def test_end_must_be_declared():
    """Can only return END if it's in targets."""
    @node(outputs="result")
    def process(x: int) -> int:
        return x
    
    @route(targets=["process"])  # END not declared
    def decide(x: int) -> str:
        return END  # Will fail at runtime
    
    graph = Graph(nodes=[process, decide])
    runner = SyncRunner()
    
    with pytest.raises(InvalidRouteError) as exc:
        runner.run(graph, inputs={"x": 1})
    
    assert "END" in str(exc.value)
    assert "not a valid target" in str(exc.value)
```

### test_end_in_targets_allows_termination

```python
def test_end_in_targets_allows_termination():
    """END in targets allows route to terminate."""
    @node(outputs="result")
    def process(x: int) -> int:
        return x
    
    @route(targets=["process", END])
    def decide(x: int) -> str:
        return END
    
    graph = Graph(nodes=[process, decide])
    runner = SyncRunner()
    
    # Should complete without error
    result = runner.run(graph, inputs={"x": 1})
    assert result is not None
```

### test_end_is_sentinel_not_string

```python
def test_end_is_sentinel_not_string():
    """END is a sentinel object, not the string "END"."""
    @node(outputs="result")
    def process(x: int) -> int:
        return x
    
    @route(targets=["process", END])
    def decide(x: int) -> str:
        return "END"  # String, not sentinel!
    
    graph = Graph(nodes=[process, decide])
    runner = SyncRunner()
    
    with pytest.raises(InvalidRouteError) as exc:
        runner.run(graph, inputs={"x": 1})
    
    # Should suggest using END sentinel
    assert "END" in str(exc.value)
```

---

## Test Category: Runtime Return Validation

### test_invalid_return_raises

```python
def test_invalid_return_raises():
    """Route returning invalid target raises InvalidRouteError."""
    @node(outputs="a")
    def node_a(x: int) -> int:
        return x
    
    @node(outputs="b")
    def node_b(x: int) -> int:
        return x
    
    @route(targets=["a", "b", END])
    def decide(x: int) -> str:
        return "c"  # Not in targets!
    
    graph = Graph(nodes=[node_a, node_b, decide])
    runner = SyncRunner()
    
    with pytest.raises(InvalidRouteError) as exc:
        runner.run(graph, inputs={"x": 1})
    
    assert "c" in str(exc.value)
    assert "not a valid target" in str(exc.value)
```

### test_typo_suggestion_at_runtime

```python
def test_typo_suggestion_at_runtime():
    """Runtime error suggests typo fixes."""
    @node(outputs="retrieve")
    def retrieve(x: int) -> int:
        return x
    
    @route(targets=["retrieve", END])
    def decide(x: int) -> str:
        return "retreive"  # Typo!
    
    graph = Graph(nodes=[retrieve, decide])
    runner = SyncRunner()
    
    with pytest.raises(InvalidRouteError) as exc:
        runner.run(graph, inputs={"x": 1})
    
    assert "Did you mean 'retrieve'?" in str(exc.value)
```

### test_none_return_invalid

```python
def test_none_return_invalid():
    """Route returning None raises clear error."""
    @node(outputs="result")
    def process(x: int) -> int:
        return x
    
    @route(targets=["process", END])
    def decide(x: int) -> str:
        return None  # Forgot to return!
    
    graph = Graph(nodes=[process, decide])
    runner = SyncRunner()
    
    with pytest.raises(InvalidRouteError) as exc:
        runner.run(graph, inputs={"x": 1})
    
    assert "None" in str(exc.value) or "null" in str(exc.value).lower()
```

---

## Test Category: @branch Validation

### test_branch_requires_both_targets

```python
def test_branch_requires_both_targets():
    """@branch must have both when_true and when_false."""
    with pytest.raises(TypeError):
        @branch(when_true="a")  # Missing when_false
        def decide(x: int) -> bool:
            return x > 0
```

### test_branch_targets_validated

```python
def test_branch_targets_validated():
    """@branch targets validated at Graph() construction."""
    @node(outputs="a")
    def node_a(x: int) -> int:
        return x
    
    @branch(when_true="a", when_false="nonexistent")
    def decide(x: int) -> bool:
        return x > 0
    
    with pytest.raises(GraphConfigError) as exc:
        Graph(nodes=[node_a, decide])
    
    assert "nonexistent" in str(exc.value)
```

### test_branch_accepts_node_objects

```python
def test_branch_accepts_node_objects():
    """@branch can use node objects instead of strings."""
    @node(outputs="positive")
    def handle_positive(x: int) -> str:
        return "positive"
    
    @node(outputs="negative")
    def handle_negative(x: int) -> str:
        return "negative"
    
    @branch(when_true=handle_positive, when_false=handle_negative)
    def decide(x: int) -> bool:
        return x > 0
    
    # Should work
    graph = Graph(nodes=[handle_positive, handle_negative, decide])
    assert graph is not None
```

### test_branch_must_return_bool

```python
def test_branch_must_return_bool():
    """@branch function must return boolean."""
    @node(outputs="a")
    def node_a(x: int) -> int:
        return x
    
    @node(outputs="b")
    def node_b(x: int) -> int:
        return x
    
    @branch(when_true="a", when_false="b")
    def decide(x: int) -> bool:
        return "true"  # String, not bool!
    
    graph = Graph(nodes=[node_a, node_b, decide])
    runner = SyncRunner()
    
    with pytest.raises(InvalidRouteError) as exc:
        runner.run(graph, inputs={"x": 1})
    
    assert "bool" in str(exc.value).lower()
```

---

## Test Category: Control Edge Creation

### test_route_creates_control_edges

```python
def test_route_creates_control_edges():
    """@route creates control edges to all targets."""
    @node(outputs="a")
    def node_a(x: int) -> int:
        return x
    
    @node(outputs="b")
    def node_b(x: int) -> int:
        return x
    
    @route(targets=["a", "b", END])
    def decide(x: int) -> str:
        return "a"
    
    graph = Graph(nodes=[node_a, node_b, decide])
    
    # Check control edges exist
    assert graph.nx_graph.has_edge("decide", "node_a")
    assert graph.nx_graph.has_edge("decide", "node_b")
    
    # Check edge type
    edge_data = graph.nx_graph.get_edge_data("decide", "node_a")
    assert edge_data["edge_type"] == "control"
```

### test_branch_creates_two_control_edges

```python
def test_branch_creates_two_control_edges():
    """@branch creates exactly two control edges."""
    @node(outputs="pos")
    def positive(x: int) -> str:
        return "pos"
    
    @node(outputs="neg")
    def negative(x: int) -> str:
        return "neg"
    
    @branch(when_true="positive", when_false="negative")
    def decide(x: int) -> bool:
        return x > 0
    
    graph = Graph(nodes=[positive, negative, decide])
    
    # Check both edges exist
    assert graph.nx_graph.has_edge("decide", "positive")
    assert graph.nx_graph.has_edge("decide", "negative")
    
    # Check conditions
    true_edge = graph.nx_graph.get_edge_data("decide", "positive")
    false_edge = graph.nx_graph.get_edge_data("decide", "negative")
    assert true_edge["condition"] == True
    assert false_edge["condition"] == False
```
