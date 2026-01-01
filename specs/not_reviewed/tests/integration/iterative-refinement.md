# Integration Test: Iterative Refinement

## Overview

A pattern where output is generated, evaluated, and refined until quality threshold is met. Classic "generate → evaluate → refine" loop.

---

## Scenario

```
Generate draft → Evaluate quality (0.6) → Below threshold → Refine
Refine draft → Evaluate quality (0.75) → Below threshold → Refine  
Refine draft → Evaluate quality (0.92) → Above threshold → END
```

---

## Graph Definition

```python
from hypergraph import Graph, node, route, END

@node(outputs="draft")
def generate(prompt: str, feedback: str | None = None) -> str:
    """Generate or refine draft based on feedback."""
    if feedback:
        return f"Refined draft incorporating: {feedback}"
    return f"Initial draft for: {prompt}"

@node(outputs=("score", "feedback"))
def evaluate(draft: str) -> tuple[float, str]:
    """Evaluate draft quality, return score and feedback."""
    # Mock: score increases with each refinement
    if "Refined" in draft:
        depth = draft.count("Refined")
        score = min(0.5 + depth * 0.2, 0.95)
    else:
        score = 0.5
    
    feedback = "Add more detail" if score < 0.9 else "Looks good"
    return (score, feedback)

@route(targets=["generate", END])
def quality_gate(score: float, threshold: float = 0.9) -> str:
    """Route based on quality score."""
    return END if score >= threshold else "generate"

refinement_graph = Graph(nodes=[generate, evaluate, quality_gate])
```

---

## Test Cases

### test_reaches_quality_threshold

```python
def test_reaches_quality_threshold():
    """Refinement continues until threshold met."""
    runner = SyncRunner()
    
    result = runner.run(
        refinement_graph,
        inputs={"prompt": "Write a poem"},
    )
    
    assert result["score"] >= 0.9
    assert "Refined" in result["draft"]
```

### test_iteration_count_varies

```python
def test_iteration_count_varies():
    """Number of iterations depends on quality progression."""
    iteration_counts = []
    
    for threshold in [0.6, 0.8, 0.95]:
        count = 0
        
        class CountCallback:
            def on_node_start(self, name, inputs):
                nonlocal count
                if name == "generate":
                    count += 1
        
        runner = SyncRunner(callbacks=[CountCallback()])
        runner.run(
            refinement_graph,
            inputs={"prompt": "test", "threshold": threshold},
        )
        iteration_counts.append(count)
    
    # Higher threshold → more iterations
    assert iteration_counts[0] <= iteration_counts[1] <= iteration_counts[2]
```

### test_feedback_passed_to_generator

```python
def test_feedback_passed_to_generator():
    """Feedback from evaluator reaches generator on refinement."""
    received_feedback = []
    
    @node(outputs="draft")
    def tracked_generate(prompt: str, feedback: str | None = None) -> str:
        received_feedback.append(feedback)
        if feedback:
            return f"Refined: {feedback}"
        return "Initial"
    
    test_graph = Graph(nodes=[tracked_generate, evaluate, quality_gate])
    runner = SyncRunner()
    
    runner.run(test_graph, inputs={"prompt": "test"})
    
    # First call: no feedback, subsequent: has feedback
    assert received_feedback[0] is None
    assert all(f is not None for f in received_feedback[1:])
```

### test_draft_improves_each_iteration

```python
def test_draft_improves_each_iteration():
    """Each refinement should build on previous."""
    drafts = []
    
    @node(outputs="draft")
    def tracked_generate(prompt: str, feedback: str | None = None) -> str:
        if feedback:
            draft = f"[Refined] Previous + {feedback}"
        else:
            draft = f"[Initial] {prompt}"
        drafts.append(draft)
        return draft
    
    test_graph = Graph(nodes=[tracked_generate, evaluate, quality_gate])
    runner = SyncRunner()
    
    runner.run(test_graph, inputs={"prompt": "test"})
    
    # Verify progression
    assert "[Initial]" in drafts[0]
    for draft in drafts[1:]:
        assert "[Refined]" in draft
```

### test_configurable_threshold

```python
def test_configurable_threshold():
    """Threshold can be provided as input."""
    runner = SyncRunner()
    
    # Low threshold - should finish quickly
    result_low = runner.run(
        refinement_graph,
        inputs={"prompt": "test", "threshold": 0.5},
    )
    
    # High threshold - needs more refinement
    result_high = runner.run(
        refinement_graph,
        inputs={"prompt": "test", "threshold": 0.95},
    )
    
    # Both should complete
    assert result_low["score"] >= 0.5
    assert result_high["score"] >= 0.95
```

### test_initial_feedback_none

```python
def test_initial_feedback_none():
    """First generation has no feedback (uses default)."""
    runner = SyncRunner()
    
    # Should work without providing feedback
    result = runner.run(
        refinement_graph,
        inputs={"prompt": "test"},  # No feedback provided
    )
    
    assert result is not None
```

---

## Acceptance Criteria

- [ ] Loop continues until quality threshold met
- [ ] Evaluator feedback reaches generator each iteration
- [ ] Score improves (or at least changes) each iteration
- [ ] Threshold is configurable via inputs
- [ ] Initial generation works without feedback
- [ ] Terminates successfully when threshold reached

---

## Edge Cases

### test_already_good_enough

```python
def test_already_good_enough():
    """If first draft meets threshold, no refinement needed."""
    @node(outputs="draft")
    def perfect_generate(prompt: str, feedback: str | None = None) -> str:
        return "Perfect draft"
    
    @node(outputs=("score", "feedback"))
    def generous_evaluate(draft: str) -> tuple[float, str]:
        return (0.99, "Perfect!")
    
    test_graph = Graph(nodes=[perfect_generate, generous_evaluate, quality_gate])
    runner = SyncRunner()
    
    iterations = 0
    class CountCallback:
        def on_node_start(self, name, inputs):
            nonlocal iterations
            if name == "perfect_generate":
                iterations += 1
    
    runner = SyncRunner(callbacks=[CountCallback()])
    result = runner.run(test_graph, inputs={"prompt": "test"})
    
    assert iterations == 1  # Only one generation
    assert result["score"] >= 0.9
```

### test_never_good_enough

```python
def test_never_good_enough():
    """If quality never meets threshold, hits max_iterations."""
    @node(outputs=("score", "feedback"))
    def harsh_evaluate(draft: str) -> tuple[float, str]:
        return (0.1, "Terrible!")  # Always fails
    
    test_graph = Graph(nodes=[generate, harsh_evaluate, quality_gate])
    runner = SyncRunner()
    
    with pytest.raises(InfiniteLoopError):
        runner.run(
            test_graph,
            inputs={"prompt": "test", "threshold": 0.9},
            max_iterations=10,
        )
```
