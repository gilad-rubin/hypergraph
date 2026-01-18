# Prompt Optimization

Iteratively improve prompts through testing, evaluation, and feedback. Multiple levels of nesting: run → evaluate → human feedback → improve.

## The Pattern

```
Outer loop (human feedback):
└── Inner loop (variant testing):
    └── Pipeline under test (the prompt being optimized)
```

This demonstrates hypergraph's natural hierarchy — cycles inside DAGs, DAGs inside cycles, at multiple levels.

## Complete Implementation

```python
from hypergraph import Graph, node, route, END, AsyncRunner
from anthropic import Anthropic
import json

client = Anthropic()

# ═══════════════════════════════════════════════════════════════
# THE PIPELINE BEING OPTIMIZED
# ═══════════════════════════════════════════════════════════════

@node(output_name="response")
def generate(query: str, system_prompt: str) -> str:
    """The pipeline under test - uses the system prompt we're optimizing."""

    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": query}],
    )

    return message.content[0].text


pipeline = Graph([generate], name="pipeline")


# ═══════════════════════════════════════════════════════════════
# VARIANT GENERATION
# ═══════════════════════════════════════════════════════════════

@node(output_name="variants")
def generate_variants(
    base_prompt: str,
    feedback: str = "",
    num_variants: int = 3,
) -> list[str]:
    """
    Generate prompt variants based on feedback.
    Uses Claude Opus 4.5 for high-quality prompt engineering.
    """

    instruction = f"""Generate {num_variants} variations of this system prompt.
Each variation should be meaningfully different while preserving the core intent.

Base prompt:
{base_prompt}
"""

    if feedback:
        instruction += f"""
Previous feedback to incorporate:
{feedback}
"""

    instruction += """
Return a JSON array of strings, each being a complete system prompt.
No explanation, just the JSON array."""

    message = client.messages.create(
        model="claude-opus-4-5-20251101",
        max_tokens=2048,
        messages=[{"role": "user", "content": instruction}],
    )

    variants = json.loads(message.content[0].text)
    return [base_prompt] + variants  # Include original for comparison


# ═══════════════════════════════════════════════════════════════
# EVALUATION
# ═══════════════════════════════════════════════════════════════

@node(output_name="test_results")
async def test_variants(
    variants: list[str],
    test_cases: list[dict],
) -> list[dict]:
    """
    Test each variant against the test cases.
    Returns scores for each variant.
    """
    runner = AsyncRunner()
    results = []

    for i, variant in enumerate(variants):
        scores = []

        for test in test_cases:
            # Run the pipeline with this variant
            result = await runner.run(pipeline, {
                "query": test["query"],
                "system_prompt": variant,
            })

            # Score the response
            score = evaluate_response(
                response=result["response"],
                expected=test.get("expected_keywords", []),
                criteria=test.get("criteria", {}),
            )
            scores.append(score)

        results.append({
            "variant_index": i,
            "prompt": variant[:100] + "..." if len(variant) > 100 else variant,
            "full_prompt": variant,
            "avg_score": sum(scores) / len(scores),
            "scores": scores,
        })

    return sorted(results, key=lambda x: x["avg_score"], reverse=True)


def evaluate_response(response: str, expected_keywords: list, criteria: dict) -> float:
    """Score a response (0-1)."""
    score = 0.0

    # Keyword coverage
    if expected_keywords:
        found = sum(1 for kw in expected_keywords if kw.lower() in response.lower())
        score += 0.5 * (found / len(expected_keywords))

    # Length criteria
    if "min_length" in criteria:
        if len(response) >= criteria["min_length"]:
            score += 0.25

    # Format criteria
    if "must_include" in criteria:
        if all(s in response for s in criteria["must_include"]):
            score += 0.25

    return min(score, 1.0)


@node(output_name="best_variant")
def select_best(test_results: list[dict]) -> dict:
    """Select the best performing variant."""
    return test_results[0]  # Already sorted by score


# ═══════════════════════════════════════════════════════════════
# OPTIMIZATION LOOP (INNER)
# ═══════════════════════════════════════════════════════════════

@node(output_name="iteration")
def track_iteration(iteration: int = 0) -> int:
    return iteration + 1

@route(targets=["generate_variants", END])
def optimization_gate(
    best_variant: dict,
    iteration: int,
    target_score: float = 0.9,
    max_iterations: int = 5,
) -> str:
    """Decide if optimization should continue."""

    if best_variant["avg_score"] >= target_score:
        print(f"✓ Target score reached: {best_variant['avg_score']:.2f}")
        return END

    if iteration >= max_iterations:
        print(f"✓ Max iterations reached. Best score: {best_variant['avg_score']:.2f}")
        return END

    print(f"→ Iteration {iteration}: score={best_variant['avg_score']:.2f}, continuing...")
    return "generate_variants"


optimization_loop = Graph([
    generate_variants,
    test_variants,
    select_best,
    track_iteration,
    optimization_gate,
], name="optimization")


# ═══════════════════════════════════════════════════════════════
# HUMAN-IN-THE-LOOP (OUTER)
# ═══════════════════════════════════════════════════════════════

@node(output_name="feedback")
def get_human_feedback(best_variant: dict, test_results: list[dict]) -> str:
    """
    Display results and get human feedback.
    In production, this might be a web UI or API call.
    """
    print("\n" + "=" * 60)
    print("OPTIMIZATION RESULTS")
    print("=" * 60)

    for i, result in enumerate(test_results[:3]):  # Top 3
        print(f"\n#{i+1} (score: {result['avg_score']:.2f})")
        print(f"   {result['prompt']}")

    print("\n" + "-" * 60)
    print(f"Best prompt (score: {best_variant['avg_score']:.2f}):")
    print(best_variant["full_prompt"])
    print("-" * 60)

    feedback = input("\nFeedback (or 'done' to finish): ").strip()
    return feedback


@route(targets=["optimization", END])
def human_gate(feedback: str) -> str:
    """Check if human wants to continue."""
    if feedback.lower() in ("done", "quit", "exit", ""):
        return END
    return "optimization"


human_loop = Graph([
    optimization_loop.as_node(),  # Inner loop as a node
    get_human_feedback,
    human_gate,
], name="human_optimization")


# ═══════════════════════════════════════════════════════════════
# RUN THE FULL SYSTEM
# ═══════════════════════════════════════════════════════════════

async def main():
    runner = AsyncRunner()

    # Test cases for evaluation
    test_cases = [
        {
            "query": "Explain quantum computing to a beginner",
            "expected_keywords": ["qubit", "superposition", "classical"],
            "criteria": {"min_length": 200},
        },
        {
            "query": "What is machine learning?",
            "expected_keywords": ["data", "algorithm", "pattern"],
            "criteria": {"min_length": 150},
        },
        {
            "query": "How does encryption work?",
            "expected_keywords": ["key", "secure", "decrypt"],
            "criteria": {"min_length": 150},
        },
    ]

    result = await runner.run(human_loop, {
        "base_prompt": "You are a helpful assistant that explains technical concepts.",
        "test_cases": test_cases,
        "target_score": 0.85,
        "max_iterations": 3,
    })

    print("\n" + "=" * 60)
    print("FINAL OPTIMIZED PROMPT:")
    print("=" * 60)
    print(result["best_variant"]["full_prompt"])
    print(f"\nFinal score: {result['best_variant']['avg_score']:.2f}")


# asyncio.run(main())
```

## Key Patterns

### 1. Multiple Nesting Levels

```
human_loop (cyclic)
└── optimization_loop.as_node() (cyclic)
    └── pipeline (DAG)
```

### 2. Automated Testing

Each prompt variant is tested against a suite of test cases with scoring criteria.

### 3. Human-in-the-Loop

The outer loop pauses for human feedback, allowing guided optimization.

### 4. Early Termination

Both loops can terminate early when goals are reached.

## Variations

### A/B Testing

Compare two prompts directly:

```python
@node(output_name="winner")
async def ab_test(prompt_a: str, prompt_b: str, test_cases: list) -> dict:
    """A/B test two prompts."""
    runner = AsyncRunner()

    results_a = await test_variants.func([prompt_a], test_cases)
    results_b = await test_variants.func([prompt_b], test_cases)

    return {
        "winner": "A" if results_a[0]["avg_score"] > results_b[0]["avg_score"] else "B",
        "score_a": results_a[0]["avg_score"],
        "score_b": results_b[0]["avg_score"],
    }
```

### LLM-as-Judge

Use an LLM to evaluate responses:

```python
@node(output_name="score")
def llm_evaluate(response: str, query: str, criteria: str) -> float:
    """Use Claude to evaluate response quality."""

    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": f"""Rate this response from 0 to 1.

Query: {query}
Response: {response}
Criteria: {criteria}

Return only a number between 0 and 1.""",
        }],
    )

    return float(message.content[0].text.strip())
```

## What's Next?

- [Hierarchical Composition](../03-patterns/04-hierarchical.md) — More on nesting patterns
- [Evaluation Harness](evaluation-harness.md) — Testing conversation systems
