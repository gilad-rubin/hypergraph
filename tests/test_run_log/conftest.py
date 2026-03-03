"""Mock nodes for RunLog tests.

These simulate real-world scenarios (slow LLMs, failing APIs, routing
decisions, caching) so we can exercise the full flow end-to-end without
external dependencies.
"""

import time

from hypergraph import node, route


@node
def mock_embed(text: str) -> list[float]:
    """Simulates an embedding call (fast, ~1ms)."""
    time.sleep(0.001)
    return [0.1, 0.2, 0.3] * 512  # 1536 floats


@node(output_name="response")
def mock_llm(prompt: str) -> str:
    """Simulates an LLM call (slow, ~10ms)."""
    time.sleep(0.01)
    return f"Response to: {prompt[:50]}"


@node(output_name="response")
def mock_failing_llm(prompt: str) -> str:
    """Simulates a failing LLM call."""
    raise ConnectionError("504 Gateway Timeout")


@node(output_name="response")
def mock_slow_llm(prompt: str) -> str:
    """Simulates a very slow LLM call (~50ms)."""
    time.sleep(0.05)
    return "slow response"


@route(targets=["account_support", "general_support"])
def mock_classifier(query: str) -> str:
    """Routes to different handlers based on query content."""
    if "password" in query.lower():
        return "account_support"
    return "general_support"


@node(output_name="answer")
def account_support(query: str) -> str:
    return f"Account help for: {query}"


@node(output_name="answer")
def general_support(query: str) -> str:
    return f"General help for: {query}"


@node(output_name="formatted")
def mock_format(response: str) -> str:
    """Fast formatting node."""
    return response.upper()
