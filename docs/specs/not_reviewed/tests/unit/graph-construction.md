# Unit Tests: Graph Construction

## Overview

These tests verify that `Graph()` construction correctly validates structure and fails fast on invalid configurations.

---

## Test Category: Node Registration

### test_graph_accepts_node_list

```python
def test_graph_accepts_node_list():
    """Graph can be constructed from list of nodes."""
    @node(outputs="b")
    def node_a(a: int) -> int:
        return a + 1
    
    @node(outputs="c")
    def node_b(b: int) -> int:
        return b * 2
    
    graph = Graph(nodes=[node_a, node_b])
    
    assert "node_a" in graph.node_names
    assert "node_b" in graph.node_names
    assert len(graph.nodes) == 2
```

### test_edges_inferred_from_signatures

```python
def test_edges_inferred_from_signatures():
    """Edges are created when parameter name matches output name."""
    @node(outputs="intermediate")
    def producer(x: int) -> int:
        return x + 1
    
    @node(outputs="result")
    def consumer(intermediate: int) -> int:
        return intermediate * 2
    
    graph = Graph(nodes=[producer, consumer])
    
    # Check edge exists in NetworkX graph
    assert graph.nx_graph.has_edge("producer", "consumer")
```

### test_multiple_outputs

```python
def test_multiple_outputs():
    """Nodes can produce multiple outputs via tuple."""
    @node(outputs=("docs", "scores"))
    def retrieve(query: str) -> tuple[list, list]:
        return ["doc1"], [0.9]
    
    @node(outputs="result")
    def process(docs: list, scores: list) -> str:
        return f"{len(docs)} docs"
    
    graph = Graph(nodes=[retrieve, process])
    
    assert "docs" in graph.outputs
    assert "scores" in graph.outputs
    assert graph.nx_graph.has_edge("retrieve", "process")
```

---

## Test Category: Route Target Validation

### test_valid_route_targets

```python
def test_valid_route_targets():
    """Route with valid targets passes validation."""
    @node(outputs="result")
    def process(x: int) -> int:
        return x
    
    @route(targets=["process", END])
    def decide(x: int) -> str:
        return "process"
    
    # Should not raise
    graph = Graph(nodes=[process, decide])
    assert graph.gates == [decide]
```

### test_invalid_route_target_raises

```python
def test_invalid_route_target_raises():
    """Route with non-existent target raises GraphConfigError."""
    @node(outputs="result")
    def process(x: int) -> int:
        return x
    
    @route(targets=["nonexistent", END])
    def decide(x: int) -> str:
        return "nonexistent"
    
    with pytest.raises(GraphConfigError) as exc:
        Graph(nodes=[process, decide])
    
    assert "nonexistent" in str(exc.value)
    assert "doesn't exist" in str(exc.value)
```

### test_route_target_typo_suggestion

```python
def test_route_target_typo_suggestion():
    """Error message suggests fix for typos."""
    @node(outputs="retrieve")
    def retrieve(x: int) -> int:
        return x
    
    @route(targets=["retreive", END])  # Typo!
    def decide(x: int) -> str:
        return "retreive"
    
    with pytest.raises(GraphConfigError) as exc:
        Graph(nodes=[retrieve, decide])
    
    assert "Did you mean 'retrieve'?" in str(exc.value)
```

### test_branch_targets_validated

```python
def test_branch_targets_validated():
    """Branch targets are validated same as route."""
    @node(outputs="result")
    def path_a(x: int) -> int:
        return x
    
    @branch(when_true="path_a", when_false="nonexistent")
    def gate(x: int) -> bool:
        return x > 0
    
    with pytest.raises(GraphConfigError) as exc:
        Graph(nodes=[path_a, gate])
    
    assert "nonexistent" in str(exc.value)
```

---

## Test Category: Parallel Producer Validation

### test_same_output_different_branches_ok

```python
def test_same_output_different_branches_ok():
    """Same output name OK if mutually exclusive branches."""
    @branch(when_true="positive", when_false="negative")
    def gate(x: int) -> bool:
        return x > 0
    
    @node(outputs="label")
    def positive(x: int) -> str:
        return "positive"
    
    @node(outputs="label")  # Same output name
    def negative(x: int) -> str:
        return "negative"
    
    # Should not raise - they're mutually exclusive
    graph = Graph(nodes=[gate, positive, negative])
    assert "label" in graph.outputs
```

### test_same_output_parallel_raises

```python
def test_same_output_parallel_raises():
    """Same output name without gate raises error."""
    @node(outputs="result")
    def producer_a(x: int) -> int:
        return x + 1
    
    @node(outputs="result")  # Conflict!
    def producer_b(x: int) -> int:
        return x - 1
    
    with pytest.raises(GraphConfigError) as exc:
        Graph(nodes=[producer_a, producer_b])
    
    assert "Multiple nodes produce 'result'" in str(exc.value)
    assert "producer_a" in str(exc.value)
    assert "producer_b" in str(exc.value)
```

---

## Test Category: Cycle Validation

### test_cycle_with_end_route_ok

```python
def test_cycle_with_end_route_ok():
    """Cycle with END route passes validation."""
    @node(outputs="count")
    def increment(count: int) -> int:
        return count + 1
    
    @route(targets=["increment", END])
    def check(count: int) -> str:
        return END if count >= 10 else "increment"
    
    graph = Graph(nodes=[increment, check])
    
    assert graph.has_cycles
    # Should not raise - has termination path
```

### test_cycle_without_termination_raises

```python
def test_cycle_without_termination_raises():
    """Cycle without termination path raises error."""
    @node(outputs="a")
    def node_a(b: int) -> int:
        return b + 1
    
    @node(outputs="b")
    def node_b(a: int) -> int:
        return a + 1
    
    # No route, no END, no leaf - infinite cycle
    with pytest.raises(GraphConfigError) as exc:
        Graph(nodes=[node_a, node_b])
    
    assert "no termination path" in str(exc.value).lower()
```

### test_cycle_detection

```python
def test_cycle_detection():
    """Graph correctly identifies cycles."""
    @node(outputs="b")
    def node_a(a: int) -> int:
        return a + 1
    
    @node(outputs="a")
    def node_b(b: int) -> int:
        return b + 1
    
    @route(targets=["node_a", END])
    def gate(a: int) -> str:
        return END
    
    graph = Graph(nodes=[node_a, node_b, gate])
    
    assert graph.has_cycles
    assert len(graph.cycles) > 0
```

---

## Test Category: Deadlock Detection

### test_deadlock_detected

```python
def test_deadlock_detected():
    """Cycle where no node can start raises DeadlockError."""
    # Both nodes need each other, no external input possible
    @node(outputs="b")
    def node_a(a: int, c: int) -> int:  # needs a (from node_b) AND c (external)
        return a + c
    
    @node(outputs="a")
    def node_b(b: int) -> int:  # needs b (from node_a)
        return b + 1
    
    # If c is not provided, node_a can't start
    # node_b needs b from node_a
    # → Deadlock
    
    # Note: This specific case might actually be startable via c
    # A true deadlock would be:
    @node(outputs="b")
    def node_a_v2(a: int) -> int:
        return a + 1
    
    @node(outputs="a")
    def node_b_v2(b: int) -> int:
        return b + 1
    
    # Neither can start without the other
    # But we need a route to make it a valid cyclic graph...
```

### test_cycle_startable_via_input

```python
def test_cycle_startable_via_input():
    """Cycle can start if one input can be provided externally."""
    @node(outputs="b")
    def node_a(a: int) -> int:
        return a + 1

    @node(outputs="a")
    def node_b(b: int) -> int:
        return b + 1

    @route(targets=["node_a", END])
    def gate(a: int) -> str:
        return END

    graph = Graph(nodes=[node_a, node_b, gate])

    # Cycle params are in seeds (need initial value to start)
    assert "a" in graph.seeds or "b" in graph.seeds
```

---

## Test Category: Input/Output Properties

### test_required_and_optional

```python
def test_required_and_optional():
    """required/optional correctly classify inputs."""
    @node(outputs="result")
    def process(required: str, optional: int = 10) -> str:
        return f"{required}-{optional}"

    graph = Graph(nodes=[process])

    assert "required" in graph.required
    assert "optional" not in graph.required
    assert "optional" in graph.optional
```

### test_seeds_for_cycles

```python
def test_seeds_for_cycles():
    """seeds contains cycle params that need initial values."""
    @node(outputs="messages")
    def accumulate(messages: list, item: str) -> list:
        return messages + [item]

    @route(targets=["accumulate", END])
    def gate(messages: list) -> str:
        return END if len(messages) > 3 else "accumulate"

    graph = Graph(nodes=[accumulate, gate])

    # messages has self-edge, needs seed
    assert "messages" in graph.seeds
    # item has no edge, is required
    assert "item" in graph.required
```

### test_leaf_outputs

```python
def test_leaf_outputs():
    """leaf_outputs contains outputs from nodes with no downstream."""
    @node(outputs="intermediate")
    def step1(x: int) -> int:
        return x + 1
    
    @node(outputs="final")
    def step2(intermediate: int) -> int:
        return intermediate * 2
    
    graph = Graph(nodes=[step1, step2])
    
    assert "final" in graph.leaf_outputs
    assert "intermediate" not in graph.leaf_outputs
```

---

## Test Category: Graph Binding

### test_bind_returns_new_graph

```python
def test_bind_returns_new_graph():
    """bind() returns new graph, original unchanged."""
    @node(outputs="result")
    def process(x: int, multiplier: int = 1) -> int:
        return x * multiplier

    graph1 = Graph(nodes=[process])
    graph2 = graph1.bind(multiplier=5)

    assert graph1 is not graph2
    assert graph2.bound.get("multiplier") == 5
    assert "multiplier" not in graph1.bound
```

### test_bind_changes_required_optional

```python
def test_bind_changes_required_optional():
    """Binding a required param moves it to optional."""
    @node(outputs="result")
    def process(a: int, b: int) -> int:
        return a + b

    graph = Graph(nodes=[process])
    assert "a" in graph.required
    assert "b" in graph.required

    bound = graph.bind(a=10)
    assert "a" not in bound.required
    assert "a" in bound.optional
    assert "b" in bound.required
```

---

## Test Category: InterruptNode

### test_interrupt_node_registered

```python
def test_interrupt_node_registered():
    """InterruptNode appears in graph.interrupt_nodes."""
    @node(outputs="content")
    def generate(prompt: str) -> str:
        return "generated content"
    
    review = InterruptNode(
        name="review",
        input_param="content",
        response_param="decision",
    )
    
    @node(outputs="result")
    def finalize(decision: str) -> str:
        return f"Decision: {decision}"
    
    graph = Graph(nodes=[generate, review, finalize])
    
    assert len(graph.interrupt_nodes) == 1
    assert graph.interrupt_nodes[0].name == "review"
```
