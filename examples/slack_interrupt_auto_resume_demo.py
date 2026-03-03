"""Slack interrupt cycle demo: ask user -> LLM -> ask user (repeat).

Terminal A:
    uv run python examples/mock_slack_server.py --port 8765

Terminal B:
    uv run python examples/slack_interrupt_auto_resume_demo.py \
      --slack-url http://127.0.0.1:8765 \
      --question "Should we ship this?" \
      --max-turns 3
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from slack_mock import SlackClient

from hypergraph import END, AsyncRunner, Graph, PauseInfo, interrupt, node, route


def with_receiver(interrupt_node: Any, receiver: Any) -> Any:
    """Attach a receiver coroutine to an InterruptNode."""
    interrupt_node.receiver = receiver
    return interrupt_node


def fake_llm(messages: list[str]) -> str:
    """Simple placeholder for LLM behavior."""
    latest_user = next((m for m in reversed(messages) if m.startswith("user: ")), "user: <none>")
    return f"assistant draft based on [{latest_user}]"


async def run_auto_resume_cycle(
    runner: AsyncRunner,
    graph: Graph,
    values: dict[str, Any],
    *,
    max_pauses: int = 30,
) -> Any:
    """Run a cyclic graph, auto-resuming interrupts via `node.receiver`."""

    def _resolve_node_by_path(root: Graph, node_path: str) -> Any | None:
        """Resolve nested node paths like 'ask_user/ask_slack'."""
        parts = node_path.split("/")
        current_graph = root
        current_node: Any | None = None
        for idx, part in enumerate(parts):
            current_node = current_graph.nodes.get(part)
            if current_node is None:
                return None
            if idx < len(parts) - 1:
                nested = getattr(current_node, "graph", None)
                if nested is None:
                    return None
                current_graph = nested
        return current_node

    merged = dict(values)

    for _ in range(max_pauses + 1):
        result = await runner.run(
            graph,
            merged,
            on_internal_override="ignore",
        )
        if not result.paused:
            return result

        pause = result.pause
        if pause is None:
            raise RuntimeError("Paused run returned no PauseInfo.")

        paused_node = _resolve_node_by_path(graph, pause.node_name)
        if paused_node is None:
            raise RuntimeError(f"Paused node '{pause.node_name}' not found in graph.")

        receiver = getattr(paused_node, "receiver", None)
        if receiver is None:
            raise RuntimeError(f"Paused node '{pause.node_name}' has no receiver attribute.")

        print(f"[demo] paused at '{pause.node_name}', waiting for human reply...")
        response = receiver(pause)
        if asyncio.iscoroutine(response):
            response = await response

        # Carry cycle seed forward from pause context.
        if pause.value is not None:
            merged["messages"] = pause.value
        merged[pause.response_key] = response
        print(f"[demo] received reply -> {pause.response_key}={response!r}")

    raise RuntimeError(f"Exceeded max_pauses={max_pauses}.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cyclic ask->llm->ask demo with mock Slack.")
    parser.add_argument("--slack-url", default="http://127.0.0.1:8765", help="Mock Slack base URL")
    parser.add_argument("--question", default="Should we ship this policy update?", help="Initial question")
    parser.add_argument("--max-turns", type=int, default=3, help="Number of assistant turns before END")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    slack = SlackClient(args.slack_url)

    @interrupt(output_name="user_input")
    def ask_slack(messages: list[str]) -> None:
        turn_number = sum(1 for m in messages if m.startswith("assistant: ")) + 1
        prompt = f"Turn {turn_number}. {args.question}"
        if messages:
            prompt = f"Turn {turn_number}. Prior context:\n" + "\n".join(messages[-4:])
        slack.post_message(prompt)
        return None

    async def receive_slack(_: PauseInfo) -> str:
        return await slack.receive_response()

    with_receiver(ask_slack, receive_slack)

    @node(output_name="assistant_text")
    def llm_step(messages: list[str], user_input: str) -> str:
        return fake_llm([*messages, f"user: {user_input}"])

    @node(output_name="messages")
    def add_user_message(messages: list[str], user_input: str) -> list[str]:
        return [*messages, f"user: {user_input}"]

    @node(output_name="messages")
    def add_assistant_message(messages: list[str], assistant_text: str) -> list[str]:
        return [*messages, f"assistant: {assistant_text}"]

    @route(targets=["ask_user", END])
    def should_continue(messages: list[str], max_turns: int) -> str:
        turns = sum(1 for m in messages if m.startswith("assistant: "))
        return END if turns >= max_turns else "ask_user"

    # Build subgraphs with explicit inner edges to avoid accidental auto-wiring
    # around the shared "messages" value.
    user_graph = Graph(
        [ask_slack, add_user_message],
        edges=[
            (ask_slack, add_user_message),
        ],
        name="ask_user",
    )
    llm_graph = Graph(
        [llm_step, add_assistant_message],
        edges=[
            (llm_step, add_assistant_message),
        ],
        name="llm",
    )
    ask_user_node = user_graph.as_node()
    llm_node = llm_graph.as_node()

    # Outer cycle topology.
    graph = Graph(
        [ask_user_node, llm_node, should_continue],
        edges=[
            (ask_user_node, llm_node),
            (llm_node, should_continue),
            (llm_node, ask_user_node),
        ],
        name="slack_cycle",
        entrypoint="ask_user",
    )
    runner = AsyncRunner()

    print("Demo started. Queue one Slack response per turn in the mock server.")
    result = await run_auto_resume_cycle(
        runner,
        graph,
        {"messages": [], "max_turns": args.max_turns},
    )

    print("\n[final messages]")
    for line in result["messages"]:
        print(f"- {line}")


if __name__ == "__main__":
    asyncio.run(main())
