"""Minimal durable chat app with FastAPI + Hypergraph.

Run:
    uv run uvicorn examples.chat_app:app --reload

Test:
    # Start a chat with first message
    curl -X POST localhost:8000/chats/my-chat/reply -H 'Content-Type: application/json' -d '{"text":"hello"}'

    # Continue the conversation
    curl -X POST localhost:8000/chats/my-chat/reply -H 'Content-Type: application/json' -d '{"text":"tell me more"}'

    # Inspect checkpoint history (every node execution, with values)
    curl localhost:8000/chats/my-chat/history
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from hypergraph import END, AsyncRunner, Graph, interrupt, node, route
from hypergraph.checkpointers import SqliteCheckpointer
from hypergraph.exceptions import WorkflowAlreadyCompletedError

# --- Fake LLM (replace with real client) ---


class FakeLLM:
    async def chat(self, messages: list) -> str:
        last_user = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"),
            "nothing",
        )
        return f"You said: {last_user}. What else?"


# --- Graph ---


@interrupt(output_name="user_input")
def wait_for_user() -> None:
    return None


@node(output_name="messages")
def add_user_message(messages: list, user_input: str) -> list:
    return [*messages, {"role": "user", "content": user_input}]


@node(output_name="assistant_text")
async def llm_reply(messages: list, llm_client) -> str:
    return await llm_client.chat(messages)


@node(output_name="messages")
def add_assistant_message(messages: list, assistant_text: str) -> list:
    return [*messages, {"role": "assistant", "content": assistant_text}]


@route(targets=["wait_for_user", END])
def should_continue(messages: list, max_turns: int) -> str:
    turns = sum(1 for m in messages if m["role"] == "assistant")
    return END if turns >= max_turns else "wait_for_user"


chat_graph = Graph(
    [wait_for_user, add_user_message, llm_reply, add_assistant_message, should_continue],
    edges=[
        (add_user_message, llm_reply),
        (llm_reply, add_assistant_message),
        (add_assistant_message, should_continue),
    ],
    name="chat",
    shared=["messages"],
    entrypoint="add_user_message",
)

chat = chat_graph.bind(messages=[], llm_client=FakeLLM(), max_turns=5)


# --- FastAPI ---


checkpointer = SqliteCheckpointer("chat.db")
runner = AsyncRunner(checkpointer=checkpointer)
app = FastAPI()


class UserMessage(BaseModel):
    text: str


@app.post("/chats/{chat_id}/reply")
async def user_reply(chat_id: str, body: UserMessage):
    try:
        result = await runner.run(chat, workflow_id=chat_id, user_input=body.text)
    except WorkflowAlreadyCompletedError:
        return JSONResponse(
            status_code=410,
            content={"error": f"Chat '{chat_id}' has ended. Start a new chat with a different ID."},
        )
    messages = result["messages"]
    return {
        "assistant": messages[-1]["content"],
        "turn": sum(1 for m in messages if m["role"] == "assistant"),
        "done": not result.paused,
    }


@app.get("/chats/{chat_id}/history")
async def chat_history(chat_id: str):
    run = checkpointer.get_run(chat_id)
    if not run:
        return JSONResponse(status_code=404, content={"error": f"Chat '{chat_id}' not found."})
    steps = checkpointer.steps(chat_id)

    # Extract the conversation from the last messages-producing step
    messages = []
    for s in reversed(steps):
        if s.values and "messages" in s.values:
            messages = s.values["messages"]
            break

    # Compact step log: skip redundant message values
    step_log = []
    for s in steps:
        entry = {"superstep": s.superstep, "node": s.node_name, "status": s.status}
        if s.decision:
            entry["decision"] = s.decision
        if s.values and "messages" not in s.values:
            entry["values"] = s.values
        step_log.append(entry)

    return {
        "chat_id": run.id,
        "status": run.status,
        "messages": messages,
        "steps": step_log,
    }
