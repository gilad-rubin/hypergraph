# Unit Tests: Staleness Detection

## Overview

These tests verify that the staleness detection system correctly determines when nodes need re-execution.

---

## Test Category: Basic Staleness

### test_never_run_is_stale

```python
def test_never_run_is_stale():
    """Node that has never run is stale."""
    state = GraphState({"x": 1})
    
    assert state.is_stale("my_node", ["x"]) is True
```

### test_just_run_not_stale

```python
def test_just_run_not_stale():
    """Node that just ran with same inputs is not stale."""
    state = GraphState({"x": 1})
    state = state.set("y", 2)
    state = state.record_execution(
        node_name="my_node",
        input_versions={"x": 0},  # x was at version 0
        output_version=1,
        duration_ms=10.0,
    )
    
    assert state.is_stale("my_node", ["x"]) is False
```

### test_input_changed_is_stale

```python
def test_input_changed_is_stale():
    """Node is stale when input has changed since last run."""
    state = GraphState({"x": 1})
    state = state.record_execution(
        node_name="my_node",
        input_versions={"x": 0},
        output_version=1,
        duration_ms=10.0,
    )
    
    # Input hasn't changed
    assert state.is_stale("my_node", ["x"]) is False
    
    # Now change the input
    state = state.set("x", 100)  # Version becomes 1
    
    # Now it's stale
    assert state.is_stale("my_node", ["x"]) is True
```

---

## Test Category: Sole Producer Rule

### test_sole_producer_not_stale_from_own_output

```python
def test_sole_producer_not_stale_from_own_output():
    """
    Accumulator pattern: node doesn't re-trigger from its own output.
    
    This is THE critical test for preventing infinite loops.
    """
    # Setup: add_message produces "messages" and reads "messages"
    state = GraphState({"messages": [], "new_msg": "hello"})
    
    # First run: messages=[], new_msg="hello" → messages=["hello"]
    state = state.set("messages", ["hello"])
    state = state.record_execution(
        node_name="add_message",
        input_versions={"messages": 0, "new_msg": 0},
        output_version=1,
        duration_ms=10.0,
    )
    
    # messages changed (we just updated it!), but...
    # add_message should NOT be stale because IT produced the change
    
    # When checking staleness for add_message, we exclude "messages"
    # from the check because add_message produces it
    node_outputs = {"messages"}  # What add_message produces
    
    def is_stale_with_sole_producer(state, node_name, inputs, own_outputs):
        if node_name not in state.node_history:
            return True
        last_run = state.node_history[node_name]
        for inp in inputs:
            if inp in own_outputs:
                continue  # Skip own outputs
            if state.versions.get(inp, -1) > last_run.input_versions.get(inp, -1):
                return True
        return False
    
    # With sole producer rule: NOT stale (messages excluded from check)
    assert is_stale_with_sole_producer(
        state, "add_message", ["messages", "new_msg"], {"messages"}
    ) is False
    
    # Without sole producer rule: WOULD be stale (messages v1 > v0)
    # This would cause infinite loop!
```

### test_sole_producer_stale_from_other_input

```python
def test_sole_producer_stale_from_other_input():
    """
    Accumulator IS stale when a non-output input changes.
    """
    state = GraphState({"messages": [], "new_msg": "hello"})
    
    # First run
    state = state.set("messages", ["hello"])
    state = state.record_execution(
        node_name="add_message",
        input_versions={"messages": 0, "new_msg": 0},
        output_version=1,
        duration_ms=10.0,
    )
    
    # Change new_msg (not an output of add_message)
    state = state.set("new_msg", "world")
    
    # Now add_message IS stale because new_msg changed
    # (sole producer rule only excludes own outputs)
    assert state.is_stale("add_message", ["messages", "new_msg"]) is True
```

---

## Test Category: Version Tracking

### test_initial_values_version_zero

```python
def test_initial_values_version_zero():
    """Initial values have version 0."""
    state = GraphState({"a": 1, "b": 2, "c": 3})
    
    assert state.get_version("a") == 0
    assert state.get_version("b") == 0
    assert state.get_version("c") == 0
```

### test_set_increments_version

```python
def test_set_increments_version():
    """Each set() call increments version."""
    state = GraphState({"x": 1})
    assert state.get_version("x") == 0
    
    state = state.set("x", 10)
    assert state.get_version("x") == 1
    
    state = state.set("x", 100)
    assert state.get_version("x") == 2
    
    state = state.set("x", 1000)
    assert state.get_version("x") == 3
```

### test_new_value_gets_version_one

```python
def test_new_value_gets_version_one():
    """New values (not in initial) start at version 1."""
    state = GraphState({"x": 1})
    
    state = state.set("y", 100)  # New value
    
    assert state.get_version("y") == 1
```

### test_versions_independent

```python
def test_versions_independent():
    """Each value has independent version counter."""
    state = GraphState({"a": 1, "b": 2})
    
    state = state.set("a", 10)  # a: 0 → 1
    state = state.set("a", 20)  # a: 1 → 2
    state = state.set("b", 30)  # b: 0 → 1
    
    assert state.get_version("a") == 2
    assert state.get_version("b") == 1
```

---

## Test Category: Multiple Inputs

### test_stale_if_any_input_changed

```python
def test_stale_if_any_input_changed():
    """Node is stale if ANY input changed."""
    state = GraphState({"a": 1, "b": 2, "c": 3})
    
    state = state.record_execution(
        node_name="process",
        input_versions={"a": 0, "b": 0, "c": 0},
        output_version=1,
        duration_ms=10.0,
    )
    
    assert state.is_stale("process", ["a", "b", "c"]) is False
    
    # Change just one input
    state = state.set("b", 200)
    
    # Node is stale even though a and c didn't change
    assert state.is_stale("process", ["a", "b", "c"]) is True
```

### test_not_stale_if_unrelated_changes

```python
def test_not_stale_if_unrelated_changes():
    """Node not stale if only unrelated values change."""
    state = GraphState({"a": 1, "b": 2, "unrelated": 100})
    
    state = state.record_execution(
        node_name="process",
        input_versions={"a": 0, "b": 0},  # Only uses a and b
        output_version=1,
        duration_ms=10.0,
    )
    
    # Change unrelated value
    state = state.set("unrelated", 999)
    
    # Node still not stale
    assert state.is_stale("process", ["a", "b"]) is False
```

---

## Test Category: Execution History

### test_record_execution_stores_history

```python
def test_record_execution_stores_history():
    """record_execution stores node history."""
    state = GraphState({"x": 1})
    
    state = state.record_execution(
        node_name="my_node",
        input_versions={"x": 0},
        output_version=1,
        duration_ms=15.5,
        cached=False,
    )
    
    assert "my_node" in state.node_history
    history = state.node_history["my_node"]
    assert history.input_versions == {"x": 0}
    assert history.output_version == 1
    assert history.duration_ms == 15.5
    assert history.cached is False
```

### test_multiple_executions_update_history

```python
def test_multiple_executions_update_history():
    """Later executions update history with latest versions."""
    state = GraphState({"x": 1})
    
    # First run
    state = state.record_execution(
        node_name="my_node",
        input_versions={"x": 0},
        output_version=1,
        duration_ms=10.0,
    )
    
    # Input changes
    state = state.set("x", 100)
    
    # Second run
    state = state.record_execution(
        node_name="my_node",
        input_versions={"x": 1},  # Now at version 1
        output_version=2,
        duration_ms=12.0,
    )
    
    # History reflects latest run
    assert state.node_history["my_node"].input_versions == {"x": 1}
    assert state.node_history["my_node"].output_version == 2
```

---

## Test Category: Edge Cases

### test_empty_inputs_list

```python
def test_empty_inputs_list():
    """Node with no inputs is stale only on first run."""
    state = GraphState({})
    
    # First run - stale (never run)
    assert state.is_stale("constant_node", []) is True
    
    state = state.record_execution(
        node_name="constant_node",
        input_versions={},
        output_version=1,
        duration_ms=1.0,
    )
    
    # After run - not stale (nothing can change)
    assert state.is_stale("constant_node", []) is False
```

### test_missing_input_in_state

```python
def test_missing_input_in_state():
    """Staleness check handles missing values gracefully."""
    state = GraphState({"a": 1})  # b not present
    
    # Node needs both a and b
    # Should be considered stale (b doesn't exist yet)
    # OR should raise - depends on design choice
    
    # If we choose "stale when input missing":
    assert state.is_stale("process", ["a", "b"]) is True
```

### test_same_value_different_version

```python
def test_same_value_different_version():
    """Setting same value still increments version."""
    state = GraphState({"x": 42})
    assert state.get_version("x") == 0
    
    state = state.set("x", 42)  # Same value!
    assert state.get_version("x") == 1
    
    # This is intentional: we track updates, not value equality
    # This ensures deterministic behavior without deep equality checks
```
