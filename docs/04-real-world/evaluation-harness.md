# Evaluation Harness

Test your multi-turn conversation system at scale. The cyclic conversation graph becomes a node inside an evaluation DAG.

## Why This Example?

This showcases the flip side of the natural hierarchy: **a cycle (conversation) nested inside a DAG (evaluation)**.

Same graph. Different context. Build once, reuse everywhere.

## The Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    EVALUATION PIPELINE (DAG)                    │
│                                                                 │
│  load_test_cases → conversation → score → aggregate → report   │
│                         │                                       │
│                         ▼                                       │
│              ┌─────────────────────┐                           │
│              │  CONVERSATION LOOP  │                           │
│              │     (cyclic)        │                           │
│              │                     │                           │
│              │  rag → accumulate   │                           │
│              │   ↑         ↓       │                           │
│              │   └── continue? ────┘                           │
│              └─────────────────────┘                           │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Complete Implementation

```python
from hypergraph import Graph, node, route, END, AsyncRunner
import json
from statistics import mean

# ═══════════════════════════════════════════════════════════════
# REUSE: Import the conversation graph from multi-turn-rag
# ═══════════════════════════════════════════════════════════════

# Assuming conversation graph is defined elsewhere:
# from my_app.graphs import conversation
#
# Or define it here (abbreviated):

@node(output_name="response")
async def generate(retrieved_docs: list, user_input: str, history: list) -> str:
    # ... RAG generation logic ...
    pass

@node(output_name="history")
def accumulate(history: list, user_input: str, response: str) -> list:
    return history + [
        {"role": "user", "content": user_input},
        {"role": "assistant", "content": response},
    ]

@route(targets=["rag", END])
def should_continue(history: list) -> str:
    if len(history) >= 10:  # Max 5 turns for eval
        return END
    return "rag"

rag_pipeline = Graph([embed_query, retrieve, generate], name="rag")
conversation = Graph([rag_pipeline.as_node(), accumulate, should_continue], name="conversation")


# ═══════════════════════════════════════════════════════════════
# EVALUATION PIPELINE (DAG)
# ═══════════════════════════════════════════════════════════════

@node(output_name="test_cases")
def load_test_cases(dataset_path: str) -> list[dict]:
    """
    Load test conversations.

    Each test case has:
    - initial_query: The first user message
    - follow_ups: List of follow-up questions
    - expected_topics: Topics that should be covered
    - expected_facts: Facts that should be mentioned
    """
    with open(dataset_path) as f:
        return json.load(f)


@node(output_name="conversation_result")
async def run_conversation(
    test_case: dict,
    system_prompt: str = "You are a helpful assistant.",
) -> dict:
    """
    Run the conversation graph with a test case.

    This demonstrates running a cyclic graph as part of a larger workflow.
    """
    runner = AsyncRunner()

    # Start with the initial query
    state = {
        "user_input": test_case["initial_query"],
        "history": [],
        "system_prompt": system_prompt,
    }

    # Run the first turn
    result = await runner.run(conversation, state)
    history = result["history"]

    # Run follow-up turns
    for follow_up in test_case.get("follow_ups", []):
        state = {
            "user_input": follow_up,
            "history": history,
            "system_prompt": system_prompt,
        }
        result = await runner.run(conversation, state)
        history = result["history"]

    return {
        "test_case_id": test_case.get("id", "unknown"),
        "history": history,
        "final_response": history[-1]["content"] if history else "",
        "turn_count": len(history) // 2,
    }


@node(output_name="scores")
def score_conversation(conversation_result: dict, test_case: dict) -> dict:
    """
    Score a single conversation against expected outcomes.
    """
    history = conversation_result["history"]
    all_responses = " ".join(
        msg["content"] for msg in history if msg["role"] == "assistant"
    )

    # Topic coverage
    expected_topics = test_case.get("expected_topics", [])
    topics_covered = sum(
        1 for topic in expected_topics if topic.lower() in all_responses.lower()
    )
    topic_coverage = topics_covered / len(expected_topics) if expected_topics else 1.0

    # Fact accuracy
    expected_facts = test_case.get("expected_facts", [])
    facts_mentioned = sum(
        1 for fact in expected_facts if fact.lower() in all_responses.lower()
    )
    fact_accuracy = facts_mentioned / len(expected_facts) if expected_facts else 1.0

    # Response quality (placeholder - use your own metrics)
    avg_response_length = mean(
        len(msg["content"]) for msg in history if msg["role"] == "assistant"
    ) if history else 0

    return {
        "test_case_id": conversation_result["test_case_id"],
        "topic_coverage": topic_coverage,
        "fact_accuracy": fact_accuracy,
        "turn_count": conversation_result["turn_count"],
        "avg_response_length": avg_response_length,
        "passed": topic_coverage >= 0.8 and fact_accuracy >= 0.8,
    }


@node(output_name="report")
def aggregate_results(all_scores: list[dict]) -> dict:
    """
    Aggregate scores across all test cases.
    """
    if not all_scores:
        return {"error": "No test cases evaluated"}

    passed_count = sum(1 for s in all_scores if s["passed"])

    return {
        "total_tests": len(all_scores),
        "passed": passed_count,
        "failed": len(all_scores) - passed_count,
        "pass_rate": passed_count / len(all_scores),
        "avg_topic_coverage": mean(s["topic_coverage"] for s in all_scores),
        "avg_fact_accuracy": mean(s["fact_accuracy"] for s in all_scores),
        "avg_turns": mean(s["turn_count"] for s in all_scores),
        "detailed_results": all_scores,
    }


@node(output_name="formatted_report")
def format_report(report: dict) -> str:
    """Generate human-readable report."""
    lines = [
        "=" * 50,
        "EVALUATION REPORT",
        "=" * 50,
        f"Total tests: {report['total_tests']}",
        f"Passed: {report['passed']} ({report['pass_rate']:.1%})",
        f"Failed: {report['failed']}",
        "",
        "Metrics:",
        f"  Topic coverage: {report['avg_topic_coverage']:.1%}",
        f"  Fact accuracy:  {report['avg_fact_accuracy']:.1%}",
        f"  Avg turns:      {report['avg_turns']:.1f}",
        "=" * 50,
    ]

    # Add failed test details
    failed = [r for r in report["detailed_results"] if not r["passed"]]
    if failed:
        lines.append("\nFailed tests:")
        for f in failed:
            lines.append(f"  - {f['test_case_id']}: "
                        f"topics={f['topic_coverage']:.0%}, "
                        f"facts={f['fact_accuracy']:.0%}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# COMPOSE THE EVALUATION PIPELINE
# ═══════════════════════════════════════════════════════════════

# For a single test case
single_eval = Graph([
    run_conversation,
    score_conversation,
], name="single_eval")

# For the full evaluation suite
evaluation_pipeline = Graph([
    load_test_cases,
    # Note: We'll use runner.map() to fan out over test cases
    aggregate_results,
    format_report,
], name="evaluation")


# ═══════════════════════════════════════════════════════════════
# RUNNING THE EVALUATION
# ═══════════════════════════════════════════════════════════════

async def run_evaluation(dataset_path: str) -> str:
    """
    Run the full evaluation pipeline.
    """
    runner = AsyncRunner()

    # Load test cases
    with open(dataset_path) as f:
        test_cases = json.load(f)

    # Run conversations in parallel using map
    conversation_results = await runner.map(
        single_eval,
        {"test_case": test_cases},
        map_over="test_case",
        max_concurrency=5,  # Limit parallel conversations
    )

    # Extract scores
    all_scores = [r["scores"] for r in conversation_results]

    # Generate report
    report = aggregate_results.func(all_scores)
    formatted = format_report.func(report)

    return formatted


# Example usage:
# report = asyncio.run(run_evaluation("test_conversations.json"))
# print(report)
```

## Test Data Format

```json
[
  {
    "id": "test_001",
    "initial_query": "What is hypergraph?",
    "follow_ups": [
      "How do I install it?",
      "Show me a simple example"
    ],
    "expected_topics": ["workflow", "graph", "nodes"],
    "expected_facts": ["pip install", "@node decorator"]
  },
  {
    "id": "test_002",
    "initial_query": "How do I create a multi-turn conversation?",
    "follow_ups": [
      "What about handling history?",
      "How do I know when to stop?"
    ],
    "expected_topics": ["loop", "history", "END"],
    "expected_facts": ["@route", "accumulate"]
  }
]
```

## Key Patterns Demonstrated

### 1. Cycle Inside DAG

The `conversation` graph (cyclic) runs inside the `evaluation_pipeline` (DAG):

```python
conversation_results = await runner.map(
    single_eval,  # Contains the cyclic conversation
    {"test_case": test_cases},
    map_over="test_case",
)
```

### 2. Same Graph, Different Context

The conversation graph is the same one used in production. We're just running it in an evaluation context:

```python
# In production:
result = await runner.run(conversation, {"user_input": user_query, "history": []})

# In evaluation:
result = await runner.run(conversation, {"user_input": test_case["initial_query"], "history": []})
```

### 3. Parallel Test Execution

Run multiple test conversations concurrently:

```python
conversation_results = await runner.map(
    single_eval,
    {"test_case": test_cases},
    map_over="test_case",
    max_concurrency=5,
)
```

### 4. Pure Scoring Functions

Scoring is a pure function — easy to test:

```python
def test_scoring():
    result = score_conversation.func(
        conversation_result={"history": [...], "turn_count": 3},
        test_case={"expected_topics": ["graph"], "expected_facts": ["@node"]}
    )
    assert "topic_coverage" in result
    assert 0 <= result["topic_coverage"] <= 1
```

## Extending the Pattern

### A/B Testing

Compare two conversation implementations:

```python
@node(output_name="comparison")
async def compare_implementations(
    test_case: dict,
    impl_a: Graph,
    impl_b: Graph,
) -> dict:
    runner = AsyncRunner()

    result_a = await runner.run(impl_a, {...})
    result_b = await runner.run(impl_b, {...})

    return {
        "test_case_id": test_case["id"],
        "impl_a_score": score(result_a),
        "impl_b_score": score(result_b),
        "winner": "a" if score(result_a) > score(result_b) else "b",
    }
```

### Regression Testing

Compare against baseline responses:

```python
@node(output_name="regression_result")
def check_regression(
    conversation_result: dict,
    baseline: dict,
) -> dict:
    current = conversation_result["final_response"]
    expected = baseline["expected_response"]

    similarity = compute_similarity(current, expected)

    return {
        "regression": similarity < 0.8,
        "similarity": similarity,
        "current": current,
        "expected": expected,
    }
```

### CI Integration

```python
async def test_conversation_quality():
    """Run in CI to catch regressions."""
    report = await run_evaluation("tests/fixtures/eval_dataset.json")

    # Parse report
    lines = report.split("\n")
    pass_rate_line = [l for l in lines if "Passed:" in l][0]
    pass_rate = float(pass_rate_line.split("(")[1].split("%")[0]) / 100

    assert pass_rate >= 0.9, f"Pass rate {pass_rate:.1%} below threshold"
```

## What's Next?

- [Multi-Turn RAG](multi-turn-rag.md) — The conversation system being evaluated
- [Batch Processing](../05-how-to/batch-processing.md) — More on runner.map()
