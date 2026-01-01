# Integration Test: Agentic Tool Loop

## Overview

An AI agent that decides which tools to call and loops until the task is complete. Number of iterations is unknown at start.

---

## Scenario

```
Agent: Analyze task → decide: "need to read file"
Agent: [reads file] → analyze → decide: "need to search codebase"  
Agent: [searches] → analyze → decide: "ready to generate code"
Agent: [generates] → self-review → decide: "needs refinement"
Agent: [refines] → self-review → decide: "done"
```

---

## Graph Definition

```python
from hypergraph import Graph, node, route, END

# Available tools
TOOLS = ["read_file", "search", "generate", "refine"]

@node(outputs="analysis")
def analyze(task: str, tool_results: list, messages: list) -> str:
    """Analyze current state and tool results."""
    # Mock: return analysis based on history
    return f"Analysis after {len(tool_results)} tool calls"

@route(targets=["read_file", "search", "generate", "refine", END])
def decide_action(analysis: str, tool_results: list) -> str:
    """LLM decides next action based on analysis."""
    # Mock decision logic
    if len(tool_results) == 0:
        return "read_file"
    elif len(tool_results) == 1:
        return "search"
    elif len(tool_results) == 2:
        return "generate"
    elif len(tool_results) == 3:
        return "refine"
    else:
        return END

@node(outputs="tool_result")
def read_file(task: str) -> dict:
    """Read file tool."""
    return {"tool": "read_file", "result": "file contents"}

@node(outputs="tool_result")
def search(task: str, analysis: str) -> dict:
    """Search codebase tool."""
    return {"tool": "search", "result": "search results"}

@node(outputs="tool_result")
def generate(task: str, analysis: str, tool_results: list) -> dict:
    """Generate code tool."""
    return {"tool": "generate", "result": "generated code"}

@node(outputs="tool_result")
def refine(task: str, tool_result: dict) -> dict:
    """Refine output tool."""
    return {"tool": "refine", "result": "refined code"}

@node(outputs="tool_results")
def track_tools(tool_results: list, tool_result: dict) -> list:
    """Accumulator: track tool usage history."""
    return tool_results + [tool_result]

@node(outputs="messages")
def update_messages(messages: list, analysis: str, tool_result: dict) -> list:
    """Accumulator: update conversation with tool results."""
    return messages + [
        {"role": "assistant", "content": analysis},
        {"role": "tool", "content": str(tool_result)},
    ]

agent_graph = Graph(nodes=[
    analyze, decide_action,
    read_file, search, generate, refine,
    track_tools, update_messages,
])
```

---

## Test Cases

### test_agent_runs_variable_iterations

```python
def test_agent_runs_variable_iterations():
    """Agent runs correct number of iterations based on task."""
    execution_log = []

    class LogProcessor(TypedEventProcessor):
        def on_route_decision(self, event: RouteDecisionEvent):
            execution_log.append(event.decision)

    runner = SyncRunner(event_processors=[LogProcessor()])
    result = runner.run(
        agent_graph,
        inputs={
            "task": "Implement feature X",
            "tool_results": [],
            "messages": [],
        },
    )

    # Should have made multiple decisions
    assert len(execution_log) >= 4

    # Last decision should be END
    assert execution_log[-1] == "END" or execution_log[-1] == END
```

### test_tool_history_tracked

```python
def test_tool_history_tracked():
    """Tool usage is correctly tracked in accumulator."""
    runner = SyncRunner()
    
    result = runner.run(
        agent_graph,
        inputs={
            "task": "Test task",
            "tool_results": [],
            "messages": [],
        },
    )
    
    # Should have accumulated tool results
    assert len(result["tool_results"]) > 0
    
    # Each entry should have tool name and result
    for tr in result["tool_results"]:
        assert "tool" in tr
        assert "result" in tr
```

### test_mutually_exclusive_tools

```python
def test_mutually_exclusive_tools():
    """Only one tool runs per iteration (they're exclusive branches)."""
    tool_calls = []
    
    @node(outputs="tool_result")
    def tracked_read_file(task: str) -> dict:
        tool_calls.append("read_file")
        return {"tool": "read_file", "result": "contents"}
    
    @node(outputs="tool_result")
    def tracked_search(task: str, analysis: str) -> dict:
        tool_calls.append("search")
        return {"tool": "search", "result": "results"}
    
    # ... rebuild graph with tracked versions ...
    
    runner = SyncRunner()
    result = runner.run(
        test_graph,
        inputs={"task": "test", "tool_results": [], "messages": []},
    )
    
    # Tools should be called sequentially, not in parallel
    # (route picks one at a time)
```

### test_llm_decides_termination

```python
def test_llm_decides_termination():
    """Agent stops when LLM/route decides task is complete."""
    
    @route(targets=["generate", END])
    def quick_decide(analysis: str, tool_results: list) -> str:
        # Immediately decide done
        return END
    
    quick_graph = Graph(nodes=[analyze, quick_decide, generate, track_tools])
    runner = SyncRunner()
    
    result = runner.run(
        quick_graph,
        inputs={"task": "simple", "tool_results": [], "messages": []},
    )
    
    # Should complete quickly
    assert result is not None
```

---

## Acceptance Criteria

- [ ] Agent runs variable number of iterations (not fixed)
- [ ] LLM can choose any available tool at each step
- [ ] Tool usage history tracked correctly (accumulator)
- [ ] Terminates when LLM decides task is complete
- [ ] Each tool is mutually exclusive per iteration

---

## Key Insight: Tools as Mutually Exclusive Branches

The `@route` decorator selects ONE tool per iteration. This is different from parallel execution. The pattern is:

```
analyze → decide → [ONE tool] → track → analyze → decide → ...
```

Not:

```
analyze → decide → [ALL tools in parallel] → ...
```

This matches how real AI agents work: one action at a time, evaluate, decide next.
