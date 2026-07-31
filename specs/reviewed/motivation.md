After 10+ years in data science, I kept asking: what's the right way to structure this work? Notebooks are comfortable but don't scale. Multiple notebooks with dependencies become a burden — no caching, no modularity, no way to run the whole thing.

Kedro had beautiful project structure, but every pipeline change required editing at least three places. After a few months, the wiring overhead was too much.

Hamilton was the breakthrough. It automatically infers edges from function signatures. This isn't just a cool trick — it's genuinely useful for DRY code. You can add, remove, or rewire nodes by just changing function signatures. Everything updates automatically.

PipeFunc added parallelism and .map() — write once, apply to many. But Map Spec was hard to reason about, especially with multiple nesting levels. And critically: no dynamic runtime mapping. If you have an unknown number of chunks per document, you're stuck. That was a no-go.

HyperNodes (v1) was my first attempt — hierarchical pipelines as a first-class citizen, designed around my own preferences.

Then I started building a RAG system and thought ahead: what happens when we need multi-turn? Do I have to switch to LangGraph or Pydantic-Graph? I looked at them and didn't like what I saw.

HyperGraph was born — supporting both DAGs and cycles, dynamic branching, and the developer experience I actually want.

What's Wrong with LangGraph/Pydantic-Graph?
My main issue is how they implement nodes. Every function takes state as input:


# LangGraph style - what I don't want
def add_response(state: AgentState) -> dict:
    messages = state["messages"]  # read from state
    response = state["response"]  # read from state
    return {"messages": messages + [response]}  # write to state
This violates Single Responsibility — you're reading, processing, AND writing in one function. That's a heuristic that something is wrong.

It also creates coupling. The function isn't portable. You can't just assert add_response(messages, response) == expected. You need the whole framework to test it.

What I want:


# HyperGraph style - portable, testable
@node(output_name="messages")
def add_response(messages: list, response: str) -> list:
    return messages + [response]
Clear inputs. Clear output. Test it anywhere.

The Dynamism Problem
LangGraph and Pydantic-Graph validate statically — at compile/definition time. That's a feature, not a bug, for some use cases.

But it makes certain things impossible:

Convert a YAML config into a graph
Have an AI generate a graph structure (not in code)
Create arbitrary graph structures from a few reusable node types
HyperGraph validates at build-time (when Graph() is called), not compile-time. You can construct graphs from data, from config files, from AI-generated specs — then validate them.

Core Principles
Automatic edge inference — Change signatures, graph updates. DRY by default.

Portable functions — No framework coupling. Test with plain assert.

Hierarchical composition — Pipelines as nodes, infinitely nestable.

Dynamic runtime mapping — Unknown number of items? No problem.

Build-time validation — Fail fast, but allow dynamic construction.

Cycles when you need them — Multi-turn, agentic loops, iterative refinement.