# Integration Test: Multi-Turn Conversational RAG

## Overview

This is the PRIMARY use case that drove the Graph architecture. The test must prove that hypergraph can handle multi-turn conversations with cycles.

---

## Scenario

```
User: "What is RAG?"
System: [retrieves docs] → [generates answer]
User: "Can you explain the retrieval part more?"
System: [retrieves MORE docs using conversation context] → [refines answer]
User: "Thanks, that's clear now."
System: [detects completion] → END
```

---

## Graph Definition

```python
from hypergraph import Graph, node, route, END, AsyncRunner

@node(outputs="docs")
def retrieve(query: str, messages: list) -> list[str]:
    """Retrieve documents using query and conversation context."""
    # Mock implementation for testing
    context = " ".join(m["content"] for m in messages)
    return [f"Doc about {query} with context: {context[:50]}"]

@node(outputs="response")
async def generate(docs: list[str], messages: list) -> str:
    """Generate response from docs and conversation."""
    # Mock LLM
    return f"Based on {len(docs)} docs: Here's information about your query."

@node(outputs="messages")
def add_response(messages: list, response: str) -> list:
    """Accumulator: add assistant response to messages."""
    return messages + [{"role": "assistant", "content": response}]

@route(targets=["retrieve", END])
def should_continue(messages: list) -> str:
    """Decide whether to continue or end conversation."""
    if len(messages) >= 6:  # 3 turns (user + assistant each)
        return END
    last_msg = messages[-1]["content"] if messages else ""
    if "thanks" in last_msg.lower() or "clear" in last_msg.lower():
        return END
    return "retrieve"

# Build graph
rag_graph = Graph(nodes=[retrieve, generate, add_response, should_continue])
```

---

## Test Cases

### test_single_turn_completes

```python
@pytest.mark.asyncio
async def test_single_turn_completes():
    """Single turn with completion signal ends correctly."""
    runner = AsyncRunner()
    
    result = await runner.run(
        rag_graph,
        inputs={
            "query": "What is RAG?",
            "messages": [
                {"role": "user", "content": "Thanks, that's clear!"}
            ],
        },
    )
    
    # Should have ended (thanks detected)
    assert "response" in result
    assert len(result["messages"]) >= 1
```

### test_multi_turn_cycles_correctly

```python
@pytest.mark.asyncio
async def test_multi_turn_cycles_correctly():
    """Multiple turns cycle through retrieve → generate → accumulate."""
    runner = AsyncRunner()
    execution_log = []
    
    class LogCallback:
        def on_node_start(self, node_name, inputs):
            execution_log.append(f"start:{node_name}")
        def on_node_end(self, node_name, outputs, duration):
            execution_log.append(f"end:{node_name}")
    
    result = await runner.run(
        rag_graph,
        inputs={
            "query": "What is RAG?",
            "messages": [
                {"role": "user", "content": "What is RAG?"},
            ],
        },
        callbacks=[LogCallback()],
        max_iterations=100,
    )
    
    # Verify cycling occurred
    retrieve_count = execution_log.count("start:retrieve")
    generate_count = execution_log.count("start:generate")
    
    # Should have multiple iterations before hitting max_turns or completion
    assert retrieve_count >= 1
    assert generate_count >= 1
    
    # Messages should have accumulated
    assert len(result["messages"]) > 1
```

### test_messages_accumulate_correctly

```python
@pytest.mark.asyncio
async def test_messages_accumulate_correctly():
    """Messages list grows with each turn."""
    runner = AsyncRunner()
    
    initial_messages = [
        {"role": "user", "content": "What is RAG?"},
    ]
    
    result = await runner.run(
        rag_graph,
        inputs={
            "query": "What is RAG?",
            "messages": initial_messages.copy(),
        },
        max_iterations=10,
    )
    
    final_messages = result["messages"]
    
    # Should have grown
    assert len(final_messages) > len(initial_messages)
    
    # Original messages should be preserved at start
    assert final_messages[0] == initial_messages[0]
    
    # New messages should be assistant responses
    for msg in final_messages[1:]:
        assert msg["role"] == "assistant"
```

### test_retrieval_uses_conversation_context

```python
@pytest.mark.asyncio
async def test_retrieval_uses_conversation_context():
    """Each retrieval has access to full conversation history."""
    retrieved_contexts = []
    
    @node(outputs="docs")
    def retrieve_with_tracking(query: str, messages: list) -> list[str]:
        retrieved_contexts.append(len(messages))
        return [f"Doc for turn {len(messages)}"]
    
    # Rebuild graph with tracking version
    test_graph = Graph(nodes=[
        retrieve_with_tracking, generate, add_response, should_continue
    ])
    
    runner = AsyncRunner()
    await runner.run(
        test_graph,
        inputs={
            "query": "test",
            "messages": [{"role": "user", "content": "test"}],
        },
        max_iterations=10,
    )
    
    # Each retrieval should see growing message history
    for i in range(1, len(retrieved_contexts)):
        assert retrieved_contexts[i] >= retrieved_contexts[i-1]
```

### test_terminates_on_end_signal

```python
@pytest.mark.asyncio
async def test_terminates_on_end_signal():
    """Conversation ends when route returns END."""
    runner = AsyncRunner()
    
    # Start with "thanks" to trigger immediate END
    result = await runner.run(
        rag_graph,
        inputs={
            "query": "anything",
            "messages": [
                {"role": "user", "content": "Thanks!"},
            ],
        },
    )
    
    # Should complete without error
    assert result is not None
```

### test_max_iterations_prevents_infinite_loop

```python
@pytest.mark.asyncio
async def test_max_iterations_prevents_infinite_loop():
    """Max iterations prevents runaway cycles."""
    
    @route(targets=["retrieve"])  # Never returns END!
    def never_ends(messages: list) -> str:
        return "retrieve"
    
    infinite_graph = Graph(nodes=[retrieve, generate, add_response, never_ends])
    runner = AsyncRunner()
    
    with pytest.raises(InfiniteLoopError) as exc:
        await runner.run(
            infinite_graph,
            inputs={"query": "test", "messages": []},
            max_iterations=5,
        )
    
    assert "5 iterations" in str(exc.value)
```

---

## Acceptance Criteria Checklist

- [ ] Conversation runs 3+ turns without manual intervention
- [ ] Each turn correctly uses full conversation history for retrieval  
- [ ] State persists between turns (messages accumulate)
- [ ] Clear termination (END when user is done)
- [ ] `messages` explicitly initialized - clear documentation of starting state
- [ ] No infinite loops (sole producer rule works)
- [ ] Cache works correctly across iterations (different messages = different cache key)

---

## Performance Expectations

| Metric | Target |
|--------|--------|
| Framework overhead per iteration | <10ms |
| State serialization | <50ms for typical conversation |
| Ready set computation | <1ms |

---

## Comparison: What This Looks Like in LangGraph

```python
# LangGraph equivalent - note the boilerplate
from langgraph.graph import StateGraph

class RAGState(TypedDict):
    query: str
    messages: Annotated[list, operator.add]  # Reducer annotation
    docs: list
    response: str

def retrieve(state: RAGState) -> dict:
    # Must read from state dict
    query = state["query"]
    messages = state["messages"]
    docs = retriever.search(query, context=messages)
    return {"docs": docs}  # Must return dict

def generate(state: RAGState) -> dict:
    docs = state["docs"]
    messages = state["messages"]
    response = llm.chat(docs, messages)
    return {"response": response, "messages": [{"role": "assistant", "content": response}]}

# hypergraph is cleaner because:
# 1. No TypedDict state class required
# 2. No Annotated reducers
# 3. Functions take direct parameters, not state dict
# 4. Return values are direct, not wrapped in dict
```
