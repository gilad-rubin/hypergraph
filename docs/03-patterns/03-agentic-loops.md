# Agentic Loops

Agentic workflows iterate until a goal is achieved. The graph cycles back to earlier nodes based on runtime conditions.

## When to Use

- **Multi-turn conversations**: User asks, system responds, user follows up
- **Iterative refinement**: Generate, evaluate, improve until quality threshold
- **Tool-using agents**: Call tools, observe results, decide next action
- **Retry patterns**: Attempt, check result, retry if needed

## The Core Pattern

Use `@route` to decide whether to continue or stop:

```python
from hypergraph import Graph, node, route, END, SyncRunner

@node(output_name="draft")
def generate(prompt: str, feedback: str = "") -> str:
    """Generate content, incorporating any feedback."""
    full_prompt = f"{prompt}\n\nFeedback to address: {feedback}" if feedback else prompt
    return llm.generate(full_prompt)

@node(output_name="score")
def evaluate(draft: str) -> float:
    """Score the draft quality (0-1)."""
    return quality_model.score(draft)

@node(output_name="feedback")
def critique(draft: str, score: float) -> str:
    """Generate feedback for improvement."""
    if score >= 0.8:
        return ""  # Good enough
    return critic_model.generate(f"Critique this draft:\n{draft}")

@route(targets=["generate", END])
def should_continue(score: float, attempts: int) -> str:
    """Decide whether to continue refining."""
    if score >= 0.8:
        return END  # Quality achieved
    if attempts >= 5:
        return END  # Max attempts reached
    return "generate"  # Keep refining

@node(output_name="attempts")
def count_attempts(attempts: int = 0) -> int:
    """Track iteration count."""
    return attempts + 1

# Build the loop
refinement_loop = Graph([
    generate,
    evaluate,
    critique,
    count_attempts,
    should_continue,
])

# Run until done
runner = SyncRunner()
result = runner.run(refinement_loop, {"prompt": "Write a haiku about Python"})

print(f"Final draft: {result['draft']}")
print(f"Final score: {result['score']}")
print(f"Attempts: {result['attempts']}")
```

## How It Works

```
┌─────────────────────────────────────────┐
│                                         │
│   generate → evaluate → critique        │
│       ↑                    ↓            │
│       └──── should_continue ────→ END   │
│                                         │
└─────────────────────────────────────────┘
```

1. `generate` creates a draft
2. `evaluate` scores it
3. `critique` provides feedback
4. `should_continue` decides:
   - Return `END` → graph completes
   - Return `"generate"` → loop back

## The END Sentinel

`END` is a special value that terminates execution:

```python
from hypergraph import END

@route(targets=["next_step", END])
def check_done(result: dict) -> str:
    if result["complete"]:
        return END
    return "next_step"
```

**Important**: Always include `END` in your targets when you want the option to stop.

## Multi-Turn Conversation

A conversation loop that continues until the user says goodbye:

```python
@node(output_name="response")
def generate_response(messages: list, context: str) -> str:
    """Generate assistant response."""
    return llm.chat(messages, system=context)

@node(output_name="messages")
def update_history(messages: list, user_input: str, response: str) -> list:
    """Append new messages to history."""
    return messages + [
        {"role": "user", "content": user_input},
        {"role": "assistant", "content": response},
    ]

@route(targets=["generate_response", END])
def should_continue_chat(response: str, messages: list) -> str:
    """Check if conversation should continue."""
    # End if assistant said goodbye or max turns reached
    if "goodbye" in response.lower() or len(messages) > 20:
        return END
    return "generate_response"

chat_loop = Graph([
    generate_response,
    update_history,
    should_continue_chat,
])
```

## Tool-Using Agent

An agent that decides which tool to call:

```python
@node(output_name="action")
def decide_action(observation: str, goal: str) -> dict:
    """Decide next action based on observation."""
    return agent_model.decide(observation, goal)

@node(output_name="observation")
def execute_action(action: dict) -> str:
    """Execute the chosen action."""
    tool_name = action["tool"]
    tool_args = action["args"]
    return tools[tool_name](**tool_args)

@route(targets=["decide_action", END])
def check_goal_achieved(action: dict, observation: str) -> str:
    """Check if the goal is achieved."""
    if action["tool"] == "finish":
        return END
    return "decide_action"

agent_loop = Graph([decide_action, execute_action, check_goal_achieved])
```

## Quality Gate Pattern

Ensure output meets quality standards before proceeding:

```python
@node(output_name="content")
def generate_content(topic: str, previous_attempt: str = "") -> str:
    if previous_attempt:
        return llm.generate(f"Improve this: {previous_attempt}")
    return llm.generate(f"Write about: {topic}")

@node(output_name="validation")
def validate(content: str) -> dict:
    return {
        "has_intro": "introduction" in content.lower(),
        "has_conclusion": "conclusion" in content.lower(),
        "min_length": len(content) > 500,
        "no_errors": grammar_check(content),
    }

@node(output_name="all_valid")
def check_validation(validation: dict) -> bool:
    return all(validation.values())

@route(targets=["generate_content", END])
def quality_gate(all_valid: bool, attempts: int = 0) -> str:
    if all_valid:
        return END
    if attempts >= 3:
        return END  # Give up after 3 attempts
    return "generate_content"

quality_loop = Graph([generate_content, validate, check_validation, quality_gate])
```

## Tracking State Across Iterations

Use a node to accumulate state:

```python
@node(output_name="history")
def accumulate_history(history: list, new_item: str) -> list:
    """Append new item to history."""
    return history + [new_item]

@node(output_name="iteration")
def increment(iteration: int = 0) -> int:
    """Track iteration count."""
    return iteration + 1
```

Provide initial values when running:

```python
result = runner.run(graph, {
    "history": [],      # Start with empty history
    "iteration": 0,     # Start at iteration 0
    "prompt": "...",
})
```

## Preventing Infinite Loops

Hypergraph detects potential infinite loops at runtime:

```python
# This will raise InfiniteLoopError if the loop runs too long
runner = SyncRunner()
result = runner.run(graph, inputs, max_iterations=100)  # Safety limit
```

Best practices:
1. Always have a termination condition (max attempts, quality threshold)
2. Include `END` in your route targets
3. Track iteration count and bail out if needed

## What's Next?

- [Hierarchical Composition](04-hierarchical.md) — Nest loops inside DAGs
- [Multi-Agent](05-multi-agent.md) — Coordinate multiple agents
- [Real-World: Multi-Turn RAG](../04-real-world/multi-turn-rag.md) — Complete example
