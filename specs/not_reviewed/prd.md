---
stepsCompleted: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
inputDocuments:
  - '_bmad-output/index.md'
  - '_bmad-output/architecture.md'
  - '_bmad-output/project-overview.md'
  - '_bmad-output/development-guide.md'
  - '_bmad-output/source-tree-analysis.md'
  - 'guides/runner_api_design.md'
  - 'guides/hypernodes_v2_design.md'
  - 'guides/graph_implementation_guide.md'
  - 'guides/graph_edge_cases.md'
  - 'guides/async_execution_design.md'
  - 'guides/nested_graph_results.md'
  - 'guides/node_chaining.md'
  - 'guides/optional_outputs_design.md'
documentCounts:
  briefs: 0
  research: 0
  brainstorming: 0
  projectDocs: 5
  designGuides: 8
workflowType: 'prd'
lastStep: 11
project_name: 'hypernodes'
user_name: 'Giladrubin'
date: '2025-12-22'
---
# Product Requirements Document - hypernodes

**Author:** Giladrubin
**Date:** 2025-12-22

## Executive Summary

HyperNodes is evolving from a DAG-only pipeline framework to a **graph-native execution system** that supports cycles, multi-turn interactions, and complex control flow - all while maintaining the framework's core philosophy of pure, portable functions.

### The Journey: From Hierarchical DAGs to Reactive Graphs

**Where it started (v0.1-0.4):**

HyperNodes began as an answer to existing DAG frameworks like Hamilton and Pipefunc. The key innovation: **hierarchical composition** - pipelines are nodes that can be nested infinitely.

```python
# The original vision: pipelines as composable building blocks
inner = Pipeline(nodes=[clean, tokenize])
outer = Pipeline(nodes=[fetch, inner.as_node(), analyze])
```

This enabled:

- ✅ Reusable pipeline components
- ✅ Modular testing (test small pipelines, compose into large ones)
- ✅ Visual hierarchy (expand/collapse nested pipelines)
- ✅ "Think singular, scale with map" - write for one item, map over collections

**Where it hit the wall:**

The DAG constraint (no cycles) works beautifully for:

- ETL workflows
- Single-pass ML inference
- Batch data processing

But **fundamentally breaks** for modern AI workflows:


| Use Case                 | Why DAGs Fail                                                                                           |
| -------------------------- | --------------------------------------------------------------------------------------------------------- |
| **Multi-turn RAG**       | User asks → retrieve → answer →*user follows up* → retrieve **more** → refine (needs to loop back) |
| **Agentic workflows**    | LLM decides next action, may need to retry/refine until satisfied                                       |
| **Iterative refinement** | Generate → evaluate → if not good enough → generate again                                            |
| **Conversational AI**    | Maintain conversation state, allow user to steer at any point                                           |

**The inciting incident:**

Building a multi-turn RAG system where:

1. User asks a question
2. System retrieves documents and generates answer
3. User says "can you explain X in more detail?"
4. System needs to **retrieve more documents** using conversation context
5. System refines the answer

Step 4 is **impossible** in a DAG - can't loop back to retrieval. The entire architecture assumes single-pass execution.

I looked at LangGraph and Pydantic-Graph as alternatives. Both solve cycles, but both require:

- Explicit state objects that functions must read from and write to (single responsibility principle violated)
- Manual edge wiring
- Framework-coupled functions that are not portable
- Reducer annotations for append semantics
- Field names repeated in state class, reads, writes, and edges (not DRY)

**The frustration:**

I wanted to write this:

```python
@node(output_name="messages")
def add_response(messages: list, response: str) -> list:
    return messages + [response]
```

Not this:

```python
def add_response(state: AgentState) -> dict:
    messages = state["messages"]  # Read from state
    response = state["response"]
    return {"messages": messages + [response]}  # Write to state
```

### The Solution: Dynamic Graphs with Build-Time Validation

HyperNodes 0.5 introduces **fully dynamic graph construction** with validation at build time (when `Graph()` is called), not compile time.

**Key differentiator from LangGraph/Pydantic-Graph:**


| Aspect                 | LangGraph / Pydantic-Graph                   | HyperNodes                                |
| ------------------------ | ---------------------------------------------- | ------------------------------------------- |
| **State definition**   | Static`TypedDict` or Pydantic model required | No state class - just function signatures |
| **Graph construction** | Edges defined at class definition time       | Build graphs dynamically at runtime       |
| **Validation timing**  | Compile time (static types)                  | Build time (`Graph()` construction)       |
| **Type hints**         | Mandatory                                    | Optional (opt-in for extra checks)        |

```python
# LangGraph - static, tied to schema
class AgentState(TypedDict):
    messages: list[str]  # Must know fields at definition time
graph = StateGraph(AgentState)

# HyperNodes - fully dynamic
nodes = [create_tool_node(t) for t in available_tools]  # Built at runtime!
graph = Graph(nodes=nodes)  # Validation happens here
```

**Why implicit edges by string are fine in the AI era:**

**LLMs already work in a write-then-validate loop** - They write code, then get compiler/runtime feedback to fix issues. **Build-time validation = compiler feedback** - `Graph()` construction errors serve the same purpose as type errors for LLMs

The workflow is very similar:

```
Traditional: Write code → Compiler error → Fix → Repeat
HyperNodes:  Write code → Graph() error → Fix → Repeat
```

Both catch errors before runtime. The difference is *when* validation happens (compile time vs build time), not *whether* it happens.

**Core architectural changes from 0.4:**

1. **`Graph` replaces `Pipeline`** - Pure definition, constructed from list of nodes
2. **`Runner` / `AsyncRunner`** - Execution separated from definition; runners own cache and callbacks
3. **Reactive dataflow with versioning** - Values have versions, staleness drives execution
4. **Unified execution algorithm** - Same code handles DAGs, branches, AND cycles

**Example - Multi-turn RAG becomes possible:**

```python
from hypernodes import Graph, node, route, END

@node(output_name="docs")
def retrieve(query: str, messages: list) -> list:
    return vector_db.search(query, context=messages)

@node(output_name="response")
async def generate(docs: list, messages: list, llm) -> str:
    async for chunk in llm.stream(...):
        yield chunk

@node(output_name="messages")  # Accumulator pattern
def add_response(messages: list, response: str) -> list:
    return messages + [{"role": "assistant", "content": response}]

@route(targets=["retrieve", END])  # Explicit targets - validated at build time
def should_continue(messages: list) -> str:
    if len(messages) > 10 or detect_done(messages[-1]):
        return END
    return "retrieve"  # Loops back! Creates cycle

graph = Graph(nodes=[retrieve, generate, add_response, should_continue])
# ↑ Build-time validation: "retrieve" exists, targets are valid

runner = AsyncRunner(cache=DiskCache("./cache"))
result = await runner.run(graph, inputs={
    "query": "What is RAG?",
    "messages": [],
    "llm": my_llm
})
```

**Build-time validation (always happens at `Graph()`):**

- All `@route` targets exist as nodes (or are `END`)
- All edges are valid (parameter names match output names)
- Cycles can terminate (path to `END` or leaf node exists)
- No deadlocks (cycles have valid starting inputs)

**The "edge cancels default" rule:**

- If a parameter has an incoming edge → **default is ignored**, value must come from edge OR input
- If no edge AND has function default → use the default
- If no edge AND no default → required input

**Why this is elegant:**

- The **input itself determines where cycles start**
- Framework just runs whatever is ready
- Explicit initialization = clear documentation of cycle state
- No ambiguity, no special cases

**What the framework handles automatically:**

- ✅ Cycle execution (retrieve can run multiple times)
- ✅ Staleness detection (knows when to re-run nodes)
- ✅ Sole producer rule (prevents infinite loops in accumulators)
- ✅ Route validation (fails fast if "retrieve" doesn't exist)
- ✅ Version tracking (each message update increments version)

### Technical Architecture: NetworkX-Native Reactive Dataflow

**Core architectural changes from Pipeline (v0.4.8) to Graph (v0.5.0):**

1. **`Graph` class wraps `nx.DiGraph`** with node/edge attributes

   - Nodes: hypernode object, is_gate flag, node metadata
   - Edges: edge_type (data vs control), value names, gate decisions
2. **`GraphState` with versioned values**

   - Every value has a version number (increments on update)
   - Staleness detection: "has input changed since I last ran?"
   - Sole producer rule: accumulators don't re-trigger from own output
3. **Unified execution algorithm**

   - Same code handles DAGs, branches, AND cycles
   - Reactive: nodes execute when inputs are stale
   - Gate-driven: conditional routing via control edges
4. **Gate-based routing with build-time validation**

   - `@route(targets=[...])` for multi-way routing
   - `@branch(when_true=..., when_false=...)` for boolean routing (specialized gate)
   - Targets validated at `Graph()` construction (fail fast)
   - `END` sentinel for explicit termination (or reach leaf node)
5. **Standard graph algorithms** (implementation detail)

   - Cycle detection, reachability, topological sort
   - Built on battle-tested algorithms
6. **Runner pattern (separates definition from execution)**

   - `Graph` is pure structure - no `run()` method, no cache, no callbacks
   - `Runner` / `AsyncRunner` handles execution with runtime config
   - Same graph can be executed with different runners (sync, async, distributed)
   - Runners own: cache, callbacks, execution strategy

```python
from hypernodes import Graph, node, Runner, AsyncRunner, DiskCache

# Graph = pure definition
graph = Graph(nodes=[embed, retrieve, generate])

# Runner = execution runtime
runner = Runner(cache=DiskCache("./cache"))
result = runner.run(graph, inputs={"query": "hello"})

# AsyncRunner for async nodes and streaming
async_runner = AsyncRunner(cache=DiskCache("./cache"))
result = await async_runner.run(graph, inputs={"query": "hello"})

# Same graph, different runners
results = runner.map(graph, inputs={"query": queries}, map_over="query")
```

7. **InterruptNode for human-in-the-loop**
   - Declarative pause points in the graph
   - `input_param`: what to surface to user
   - `response_param`: where to write user's response
   - Framework provides plumbing, user defines prompt/response types

```python
from hypernodes import InterruptNode

approval = InterruptNode(
    name="approval",
    input_param="approval_prompt",     # Read prompt from here
    response_param="user_decision",    # Write response to here
    response_type=ApprovalResponse,    # Optional validation
)

graph = Graph(nodes=[create_prompt, approval, route_decision, finalize])
```

**No Entrypoint Needed - Inputs Determine Where Cycles Start:**

The "edge cancels default" rule eliminates the need for entrypoints:

- If a parameter has an edge → **default is ignored** → requires edge value OR input
- Cyclic parameters have edges (by definition of being in a cycle)
- Therefore, cyclic parameters must be initialized via input
- **The input you provide determines where the cycle starts!**

```python
@node(output_name="a")
def node_a(b: int) -> int: ...  # b has edge from node_b

@node(output_name="b")
def node_b(a: int) -> int: ...  # a has edge from node_a

graph = Graph(nodes=[node_a, node_b])

# YOU choose where to start by which input you provide:
runner.run(graph, inputs={"a": 5})  # Start from node_b (has a=5)
runner.run(graph, inputs={"b": 5})  # Start from node_a (has b=5)
```

**Why this is better than entrypoints:**


| Aspect      | Entrypoint approach           | Input-as-entrypoint                  |
| ------------- | ------------------------------- | -------------------------------------- |
| Declaration | Extra concept to learn        | Just provide inputs                  |
| Flexibility | Fixed at build time           | Choose at runtime                    |
| Clarity     | Implicit state initialization | Explicit: you see the initial values |
| Validation  | Complex ambiguity detection   | Simple: run what's ready             |

**Build-time validation (still happens):**

- ✅ All `@route` targets exist
- ✅ Cycles can terminate (path to `END` or leaf node exists)
- ✅ No conflicting parallel producers
- ✅ Deadlock detection (cycle with no possible starting input)

---

**Validation Strategy: Fail Fast at Every Stage**

**Build-Time Validation (Graph Initialization):**

When you create a `Graph`, these errors are caught immediately:

- ✅ All `@route` targets reference existing nodes
- ✅ Mutually exclusive branches can share output names (validated transitively)
- ✅ Gates that can activate together don't produce conflicting outputs
- ✅ Cycles have valid termination paths (route to `END` or reach leaf node)
- ✅ Deadlock detection (cycle with no possible input to start it)
- ✅ No structural impossibilities (self-loops without gates, etc.)

**Runtime Validation (Before Execution Starts):**

When you call `runner.run(graph, inputs={...})`, these errors are caught BEFORE any node executes:

- ✅ Input-dependent conflicts (user provided inputs that make parallel producers ready)
- ✅ All required inputs are available
- ✅ No dynamic conflicts in initial ready set

**During Execution:**

After each node completes:

- ✅ Check next ready set for conflicts before executing
- ✅ Validate gate decisions reference valid targets

**Error Message Philosophy: Explain Like I'm New to This**

Error messages should be helpful to someone who has never seen the framework before. Key principles:

1. **Use simple terms** - Avoid jargon like "producer", "consumer", "parallel execution"
2. **Explain the crux** - What's actually wrong, in plain English
3. **Show the conflict** - Make it obvious why this is a problem
4. **Give concrete options** - Actionable fixes, not just "fix your code"

**Example - Conflict error (human-friendly):**

```
ConflictError: Two nodes create 'messages' at the same time

  → add_user creates messages (ready because you provided 'user_input')
  → add_assistant creates messages (ready because you provided 'response')

The problem: If add_user sets messages=[A] and add_assistant sets messages=[B],
which one should we use? The framework can't decide for you.

How to fix (pick ONE):

  Option A: Remove 'response' from inputs
            → add_user runs first, then add_assistant follows naturally

  Option B: Remove 'user_input' from inputs  
            → start from add_assistant instead

  Option C: Make add_assistant depend on add_user
            → forces add_user to always run first
```

**Example - Missing input (human-friendly):**

```
MissingInputError: 'messages' needs a starting value

  → add_response wants to read 'messages', but nothing has created it yet
  → This is a cycle - add_response creates 'messages' for the NEXT iteration,
    but what about the FIRST iteration?

How to fix:
  Provide an initial value in your inputs:
  
    runner.run(graph, inputs={..., "messages": []})
```

**Example - Invalid route target:**

```
InvalidRouteError: Route returned 'retreive' but that node doesn't exist

  → should_continue() returned "retreive"
  → Valid targets are: "retrieve", "generate", END
  
Hint: Did you mean "retrieve"? (looks like a typo)
```

**Cache + Versioning Interaction:**

- Versions track execution history for staleness detection
- Cache signatures use actual input VALUES, not version numbers
- Multi-turn loops: Each iteration has different messages → different cache key → correctly recomputes
- Deterministic: Same inputs always produce same signature, regardless of iteration count

**Streaming Support:**

**Generator handling** (internal accumulation):

- ✅ Detect if node returns generator (via `inspect.isgenerator()`)
- ✅ Accumulate chunks automatically
- ✅ Store final value in state
- ✅ Streaming events to user via `.iter()` API (AsyncRunner only)

Why: Modern LLM APIs return generators. Framework handles them with automatic accumulation for `run()` and optional streaming via `iter()`.

**Three-Layer Architecture:**

The framework separates three distinct concerns that can be layered independently:


| Layer             | Purpose                          | Example                       | Protocol               |
| ------------------- | ---------------------------------- | ------------------------------- | ------------------------ |
| **UI Protocol**   | Real-time streaming to frontends | AG-UI compatible streaming    | Events → WebSocket    |
| **Observability** | Logging, tracing, analytics      | Langfuse, Logfire integration | Events → Callback     |
| **Durability**    | Checkpoint persistence           | Redis, PostgreSQL, SQLite     | Checkpointer interface |

**Key principle:** Layers consume a unified event stream. The core produces events; layers subscribe to what they need.

**Event Types:**

- `RunStartEvent`, `RunEndEvent` - Execution lifecycle
- `NodeStartEvent`, `NodeEndEvent` - Node execution
- `StreamingChunkEvent` - Token-by-token streaming
- `InterruptEvent` - Human-in-the-loop pauses
- `CacheHitEvent` - Cache usage

**Identity Model:**


| ID           | Scope             | Who Creates | Purpose                                      |
| -------------- | ------------------- | ------------- | ---------------------------------------------- |
| `session_id` | User conversation | User        | Group related runs (multi-turn conversation) |
| `run_id`     | Single execution  | Framework   | Identify specific graph execution            |

```python
# session_id groups runs; run_id is auto-generated
result = await runner.run(graph, inputs={...}, session_id="conversation-123")
# result.run_id → "run-abc-456" (auto)
```

### What This Enables

**Core capabilities:**

- ✅ Multi-turn conversational RAG
- ✅ Agentic workflows with loops
- ✅ Retry patterns
- ✅ Iterative refinement
- ✅ Message accumulators that don't infinite loop
- ✅ Human-in-the-loop with pause/resume (`InterruptNode`)
- ✅ Token-by-token streaming (`.iter()` API)
- ✅ Event streaming for observability
- ✅ Checkpointing and resume
- ✅ Distributed batch processing (DaftRunner for DAG-only graphs)

## Success Criteria

### Acceptance Criteria


| Criterion                       | Definition of Done                                      | Validation Method                    |
| --------------------------------- | --------------------------------------------------------- | -------------------------------------- |
| **Cyclic execution works**      | Multi-turn RAG runs 3+ loops without infinite loops     | Integration test with mock LLM       |
| **Pure functions preserved**    | All nodes testable with plain`assert node_func(x) == y` | Unit tests without framework imports |
| **Staleness detection correct** | Nodes re-execute only when inputs change                | State inspection tests               |
| **Sole producer rule enforced** | Accumulators don't trigger from own output              | Cycle test with message accumulator  |
| **Build-time validation works** | Invalid graphs fail at`Graph()` construction            | Negative test cases                  |
| **Route targets validated**     | `@route` with invalid target fails fast                 | Build-time error tests               |
| **Generator handling works**    | Async generators accumulate correctly                   | Streaming node tests                 |
| **Cache signatures stable**     | Same inputs → same key across iterations               | Signature determinism tests          |
| **`.iter()` streaming works**   | Token-by-token streaming with event types               | Integration test with SSE/websocket  |
| **`InterruptNode` pauses**      | Execution pauses, state persists, resumes correctly     | Human-in-loop test                   |
| **Checkpointing works**         | Save state, kill process, resume from checkpoint        | Persistence test                     |

### Quality Criteria


| Criterion                      | Target                                         | Measurement         |
| -------------------------------- | ------------------------------------------------ | --------------------- |
| **Test coverage**              | ≥80% line coverage on core modules            | `pytest --cov`      |
| **All tests pass**             | Zero failures                                  | CI/local test run   |
| **No regressions in DAG mode** | Existing patterns still work                   | Compatibility tests |
| **Error messages actionable**  | All errors include "what", "why", "how to fix" | Manual review       |
| **Streaming latency**          | First token < 100ms after LLM starts           | Timing tests        |
| **Checkpoint size**            | < 10MB for typical conversation state          | Size measurement    |

### Personal Validation

- [ ] Multi-turn RAG is cleaner than a LangGraph prototype
- [ ] I can test nodes in Jupyter without any framework boilerplate
- [ ] Debugging feels natural (I understand what's happening)
- [ ] Adding a new feature doesn't require touching 5+ files

### Non-Goals & Anti-Patterns

**Explicitly NOT optimizing for:**


| Non-Goal               | Rationale                                          |
| ------------------------ | ---------------------------------------------------- |
| Maximum performance    | Correctness first, optimize when bottleneck proven |
| Enterprise features    | No RBAC, audit logs, compliance (personal project) |
| Backward compatibility | No users to break, clean slate is faster           |
| Framework lock-in      | Functions must remain portable                     |
| Magic behavior         | Explicit > implicit for debugging                  |

**Anti-patterns to avoid:**


| Anti-Pattern          | Why It's Bad                       | Alternative                 |
| ----------------------- | ------------------------------------ | ----------------------------- |
| State object coupling | Tests require framework setup      | Pure input/output functions |
| Hidden dependencies   | Can't reason about execution order | Explicit in signatures      |
| Implicit reducers     | Append semantics unclear           | Return complete list        |
| Silent failures       | Errors discovered too late         | Fail fast at build time     |
| s                     |                                    |                             |

### Measuring Success Over Time

**Weekly Check-in Questions:**

1. Did I hit any frustrating edges this week?
2. Is the codebase getting simpler or more complex?
3. Can I still explain the execution model in one paragraph?
4. Did I need to look at the code to understand behavior, or were the abstractions clear?

**Monthly Review:**

- Lines of code trend (should be stable or decreasing per feature)
- Test-to-code ratio (should stay ≥0.7)
- Time to implement a new feature (should decrease)
- Time spent debugging vs building (should shift toward building)

**Definition of Done:**

The v0.5.0 release is "done" when:

1. All acceptance criteria pass
2. Multi-turn RAG example runs end-to-end
3. Migration guide written (Pipeline → Graph patterns)
4. Architecture decision records exist for all major choices
5. I would choose this over LangGraph for my next project

## Project Classification

**Technical Type:** Python Library (Graph Execution Framework)
**Domain:** AI/ML Workflows, Agentic Systems
**Complexity:** High
**Project Context:** Personal Infrastructure / Research Project
**Status:** Brownfield → Greenfield (v0.5.0 architectural rewrite)

### Classification Details

**Project Type:** Personal Developer Tool

- Personal infrastructure solving a real problem (multi-turn RAG)
- Zero-dependency core with optional integrations
- Published incrementally as development progresses

**Project Nature:**

- **Infrastructure-as-learning:** Deep dive into graph theory, reactive dataflow, execution semantics
- **Opinionated:** Strong stance on pure functions vs state objects
- **Practical:** Must solve the real multi-turn RAG problem, not just theory
- **Research-flavored:** Reading papers, implementing patterns, documenting findings

**Domain Complexity:** Scientific/Research (high complexity)

- **Key Concerns:**
  - Graph theory correctness (cycle detection, staleness, mutual exclusivity)
  - Execution semantics (versioning, conflict resolution, termination)
  - Portability (pure functions, testability, no framework coupling)
  - Caching correctness (signature computation with cycles)

**Primary User:**

- Me (building multi-turn RAG)
- Future me (debugging, iterating, extending)

**Potential Secondary Users:**

- Developers frustrated with state object frameworks
- Researchers building agentic systems
- ML engineers who value function purity
- Anyone building conversational AI / multi-turn workflows

**Migration Strategy:**

- **Clean break:** v0.5.0 introduces `Graph`, marks `Pipeline` as legacy
- **No backward compatibility needed:** No existing external users
- **Documentation focus:**
  - Migration guide (Pipeline → Graph patterns)
  - Theory documentation (why reactive dataflow works)
  - Architecture decisions (ADRs for future reference)
- **Code preservation:** Archive `Pipeline` code in `src/hypernodes/old/` for reference

### Architectural Decisions

**Why NetworkX?**

- Battle-tested graph algorithms (don't reinvent cycle detection)
- Explicit graph modeling makes visualization natural
- Rich ecosystem (compatibility with other graph tools)
- Clear separation: NetworkX does graph theory, we do execution semantics

**Why Clean Break vs Gradual?**

- No user base to support (can move fast)
- Fundamental paradigm shift (DAG → reactive graphs with cycles)
- Simpler mental model (one execution system, not two)
- Learning opportunity (understand the model deeply, not just bolt-on features)

**Why Document Everything?**

- Future me will forget design decisions
- Writing clarifies thinking
- Publishable artifact for reference

## User Stories & Use Cases

This section captures the concrete scenarios that drive the Graph architecture design. Each use case represents a real problem that Pipeline (DAG) couldn't solve.

### Primary Use Case: Multi-Turn Conversational RAG

**As a** developer building a conversational AI system,
**I want** to define a graph that loops back for follow-up retrieval,
**So that** I can handle multi-turn conversations without restarting the pipeline.

#### Scenario: Research Assistant

```
User: "What are the key findings in the 2024 AI safety papers?"
System: [retrieves papers] → [generates summary]
User: "Can you focus on the alignment section?"
System: [retrieves MORE context using conversation history] → [refines answer]
User: "How does this compare to 2023?"
System: [retrieves 2023 papers too] → [generates comparison]
```

**Why Pipeline fails:** Step 2+ requires looping back to retrieval. DAGs can't cycle.

**Graph solution:**

```python
@node(output_name="messages")
def add_user_message(messages: list, user_input: str) -> list:
    return messages + [{"role": "user", "content": user_input}]

@node(output_name="docs")
def retrieve(query: str, messages: list) -> list:
    return vector_db.search(query, context=messages)

@node(output_name="messages")  # Accumulator
def add_assistant(messages: list, response: str) -> list:
    return messages + [{"role": "assistant", "content": response}]

@route(targets=["retrieve", END])
def should_continue(messages: list) -> str:
    if user_says_done(messages[-1]) or len(messages) > 20:
        return END
    return "retrieve"  # Loops back

graph = Graph(nodes=[add_user_message, retrieve, generate, add_assistant, should_continue])

runner = AsyncRunner()
# The input initializes the cycle - no entrypoint needed!
result = await runner.run(graph, inputs={
    "query": "...",
    "user_input": "What is RAG?",
    "messages": []  # ← Explicit initialization
})
```

#### Acceptance Criteria

- [ ] Conversation runs 5+ turns without manual intervention
- [ ] Each turn correctly uses full conversation history for retrieval
- [ ] State persists between turns (messages accumulate)
- [ ] Clear termination (END when user is done)
- [ ] `messages` explicitly initialized - clear documentation of starting state

---

### Use Case: Agentic Tool Loop

**As a** developer building an AI agent,
**I want** the LLM to decide which tool to call and loop until satisfied,
**So that** I can build autonomous agents that solve complex tasks.

#### Scenario: Code Generation Agent

```
Agent: Analyze task → decide: "need to read file"
Agent: [reads file] → analyze → decide: "need to search codebase"
Agent: [searches] → analyze → decide: "ready to generate code"
Agent: [generates] → self-review → decide: "needs refinement"
Agent: [refines] → self-review → decide: "done"
```

**Why Pipeline fails:** Number of iterations unknown. Can't pre-define DAG depth.

**Graph solution:**

```python
@route(targets=["read_file", "search", "generate", "refine", END])
def decide_action(analysis: str, tools_used: list) -> str:
    return llm.decide(analysis, tools_used)  # LLM picks next action

@node(output_name="tools_used")  # Accumulator
def track_tools(tools_used: list, last_tool: str) -> list:
    return tools_used + [last_tool]
```

#### Acceptance Criteria

- [ ] Agent runs variable number of iterations (not fixed)
- [ ] LLM can choose any available tool at each step
- [ ] Tool usage history tracked correctly (accumulator)
- [ ] Terminates when LLM decides task is complete

---

### Use Case: Iterative Refinement

**As a** developer building a content generation system,
**I want** to generate → evaluate → refine in a loop,
**So that** I can achieve quality thresholds without manual iteration.

#### Scenario: Document Generator with Quality Gate

```
Generate draft → Evaluate (score: 0.6) → Below threshold → Refine
Refine → Evaluate (score: 0.75) → Below threshold → Refine  
Refine → Evaluate (score: 0.92) → Above threshold → Done
```

**Graph solution:**

```python
@node(output_name="draft")
def generate(prompt: str, feedback: str | None) -> str:
    return llm.generate(prompt, previous_feedback=feedback)

@node(output_name=("score", "feedback"))
def evaluate(draft: str) -> tuple[float, str]:
    return critic.evaluate(draft)

@route(targets=["generate", END])
def quality_gate(score: float) -> str:
    return END if score > 0.9 else "generate"
```

#### Acceptance Criteria

- [ ] Loop continues until quality threshold met
- [ ] Feedback from evaluator passed to next generation
- [ ] Maximum iteration limit prevents infinite loops
- [ ] Final output is the high-quality version

---

### Use Case: Human-in-the-Loop Approval

**As a** developer building a workflow requiring human approval,
**I want** execution to pause, wait for human input, then resume,
**So that** I can build supervised AI systems.

**Note:** Requires `AsyncRunner` (see Runner Compatibility Matrix).

#### Scenario: Content Moderation Pipeline

```
Generate content → [PAUSE: await human review]
Human approves → Continue to publish
Human rejects → Loop back to regenerate with feedback
```

**Graph solution:**

```python
# InterruptNode pauses execution, waits for human input
approval = InterruptNode(
    name="human_review",
    input_param="content",           # What to show human
    response_param="decision",       # Where to write response
)

@route(targets=["publish", "regenerate"])
def route_decision(decision: str) -> str:
    return decision  # Routes based on human input

# Usage
runner = AsyncRunner()
result = await runner.run(graph, inputs={...})  # Returns at interrupt
# ... human reviews content, provides decision ...
result = await runner.run(graph, checkpoint=saved, inputs={"decision": "publish"})
```

#### Acceptance Criteria

- [ ] Execution pauses at interrupt node
- [ ] State persists across pause (can be hours/days)
- [ ] Resume from checkpoint with human input
- [ ] Both approve and reject paths work correctly

---

### Use Case: Parallel Branch Merge

**As a** developer with independent processing paths,
**I want** branches to execute independently and merge results,
**So that** I can parallelize where possible.

#### Scenario: Multi-Source Research

```
Query → [Branch A: search academic papers]
     → [Branch B: search news articles]  
     → [Branch C: search internal docs]
     → [Merge: combine all sources] → Generate answer
```

**Graph solution:**

```python
# Three independent retrievers (no data dependencies between them)
@node(output_name="academic_docs")
def search_academic(query: str) -> list: ...

@node(output_name="news_docs")
def search_news(query: str) -> list: ...

@node(output_name="internal_docs")
def search_internal(query: str) -> list: ...

# Merge waits for all three
@node(output_name="combined")
def merge(academic_docs: list, news_docs: list, internal_docs: list) -> list:
    return academic_docs + news_docs + internal_docs
```

#### Acceptance Criteria

- [ ] All three searches execute (order doesn't matter for correctness)
- [ ] Merge node waits for all inputs
- [ ] No artificial sequencing between independent branches
- [ ] Works with both SeqEngine (sequential) and future parallel engine

---

### Use Case: Conditional Skip (Existing Feature)

**As a** developer with optional processing steps,
**I want** to skip nodes based on runtime conditions,
**So that** I can build efficient conditional workflows.

#### Scenario: Cache-Aware Processing

```
Check cache → [HIT] → Return cached result (skip expensive processing)
           → [MISS] → Run expensive processing → Cache result → Return
```

**Graph solution (leveraging existing @branch):**

```python
@branch(when_true=return_cached, when_false=process_fresh)
def check_cache(query: str, cache: dict) -> bool:
    return query in cache

@node(output_name="result")
def return_cached(query: str, cache: dict) -> str:
    return cache[query]

@node(output_name="result")  # Same output name OK - exclusive branches
def process_fresh(query: str) -> str:
    return expensive_computation(query)
```

#### Acceptance Criteria

- [ ] Cache hit skips expensive node entirely
- [ ] Both branches produce same output name
- [ ] Skipped nodes don't execute (verified via callbacks)
- [ ] Gate signals track which path was taken

---

### Anti-Use-Cases (What NOT to Build)

These scenarios are explicitly out of scope:


| Scenario                         | Why Not                                 | Alternative                                     |
| ---------------------------------- | ----------------------------------------- | ------------------------------------------------- |
| **Distributed job queue**        | Not a task queue, it's a graph executor | Use Celery/RQ + call graph.run() in worker      |
| **Multi-tenant isolation**       | No RBAC, single-user focus              | Add at application layer if needed              |
| **Sub-millisecond latency**      | Correctness > performance initially     | Profile and optimize specific bottlenecks later |
| **Distributed cyclic execution** | DaftRunner doesn't support cycles       | Use Runner/AsyncRunner for cycles               |

---

### User Story Priority Matrix


| Story                 | Priority     | Complexity | Dependencies                   |
| ----------------------- | -------------- | ------------ | -------------------------------- |
| Multi-turn RAG        | **Critical** | High       | Core architecture              |
| Agentic tool loop     | High         | Medium     | Route decorator                |
| Iterative refinement  | High         | Medium     | Route decorator                |
| Parallel branch merge | Medium       | Low        | Already works with DAG         |
| Conditional skip      | Medium       | Low        | Existing @branch               |
| Human-in-the-loop     | High         | High       | Checkpoint system, AsyncRunner |

## Functional Requirements

This section specifies the capabilities the Graph system must provide, organized by component.

### FR1: Graph Construction

#### FR1.1: Node Registration


| ID      | Requirement                                                                               | Priority |
| --------- | ------------------------------------------------------------------------------------------- | ---------- |
| FR1.1.1 | `Graph` accepts a list of nodes (functions decorated with `@node`, `@route`, `@branch`)   | Must     |
| FR1.1.2 | Edges are inferred from function signatures (parameter names match output names)          | Must     |
| FR1.1.3 | Duplicate output names are rejected unless from mutually exclusive branches               | Must     |
| FR1.1.4 | Unknown parameter names (not produced by any node or provided as input) raise clear error | Must     |
| FR1.1.5 | Self-referencing nodes (output_name in own parameters) detected and rejected              | Must     |

#### FR1.2: Graph Validation (Build-Time)


| ID      | Requirement                                                                        | Priority |
| --------- | ------------------------------------------------------------------------------------ | ---------- |
| FR1.2.1 | Cycle detection:`Graph` identifies if graph contains cycles                        | Must     |
| FR1.2.2 | All`@route` targets must reference existing node names or `END`                    | Must     |
| FR1.2.3 | Route targets validated via`@route(targets=[...])` - explicit declaration required | Must     |
| FR1.2.4 | Termination path validation: cycles must have path to`END` or leaf node            | Must     |
| FR1.2.5 | Deadlock detection: error if cycle has no possible starting input                  | Must     |
| FR1.2.6 | "Edge cancels default" validation: warn if cyclic param has unused default         | Should   |
| FR1.2.7 | Invalid graphs fail with actionable error messages (what, why, how to fix)         | Must     |

#### FR1.3: NetworkX Integration


| ID      | Requirement                                                                                       | Priority |
| --------- | --------------------------------------------------------------------------------------------------- | ---------- |
| FR1.3.1 | `Graph` wraps `nx.DiGraph` internally                                                             | Must     |
| FR1.3.2 | Node attributes store: hypernode object, is_gate flag, metadata                                   | Must     |
| FR1.3.3 | Edge attributes store: edge_type (data/control), value names, gate decisions                      | Must     |
| FR1.3.4 | Standard NetworkX algorithms used for: cycle detection, reachability, ancestors, topological sort | Must     |
| FR1.3.5 | Graph structure accessible for visualization (`graph.nx_graph` property)                          | Should   |

---

### FR2: Execution Model

#### FR2.1: Reactive Dataflow


| ID      | Requirement                                                                       | Priority |
| --------- | ----------------------------------------------------------------------------------- | ---------- |
| FR2.1.1 | Nodes execute when all required inputs are available                              | Must     |
| FR2.1.2 | Staleness detection: node re-executes if any input version changed since last run | Must     |
| FR2.1.3 | Sole producer rule: accumulator nodes don't re-trigger from own output            | Must     |
| FR2.1.4 | Version tracking: each value has monotonically increasing version number          | Must     |
| FR2.1.5 | Ready set computation: determine which nodes can execute given current state      | Must     |

#### FR2.2: Control Flow


| ID      | Requirement                                                                     | Priority |
| --------- | --------------------------------------------------------------------------------- | ---------- |
| FR2.2.1 | `@route` decorator returns target node name as string                           | Must     |
| FR2.2.2 | Route can return`END` sentinel to explicitly terminate cycles                   | Must     |
| FR2.2.3 | Route decision creates control edge to target node                              | Must     |
| FR2.2.4 | `@branch` is a gate for boolean decisions (specialized `@route` with 2 targets) | Must     |
| FR2.2.5 | Gates block downstream nodes until decision is made                             | Must     |

#### FR2.3: Execution Loop & Termination

**Execution terminates when:**

1. **Leaf node reached** - A node with no outgoing edges completes (natural DAG termination)
2. **`END` returned** - A route/branch explicitly returns `END` sentinel (explicit cycle termination)
3. **All outputs produced** - Requested outputs are available and no nodes are stale


| ID      | Requirement                                                                            | Priority |
| --------- | ---------------------------------------------------------------------------------------- | ---------- |
| FR2.3.1 | Single unified algorithm handles DAGs, branches, AND cycles                            | Must     |
| FR2.3.2 | Execution terminates at leaf nodes (no outgoing edges)                                 | Must     |
| FR2.3.3 | `END` sentinel explicitly terminates cycles from routes                                | Must     |
| FR2.3.4 | Infinite loop detection: configurable max iterations with clear error                  | Must     |
| FR2.3.5 | Execution order within ready set is deterministic (alphabetical or registration order) | Should   |

---

### FR3: State Management

#### FR3.1: GraphState


| ID      | Requirement                                                             | Priority |
| --------- | ------------------------------------------------------------------------- | ---------- |
| FR3.1.1 | `GraphState` tracks all value names, their current values, and versions | Must     |
| FR3.1.2 | Input values initialized with version 0                                 | Must     |
| FR3.1.3 | Each node execution increments version of its outputs                   | Must     |
| FR3.1.4 | State tracks which nodes have executed and their last input versions    | Must     |
| FR3.1.5 | State is serializable for checkpointing                                 | Should   |

#### FR3.2: Conflict Detection


| ID      | Requirement                                                                      | Priority |
| --------- | ---------------------------------------------------------------------------------- | ---------- |
| FR3.2.1 | Parallel producer conflict detected before execution starts                      | Must     |
| FR3.2.2 | Conflict error includes: which nodes, which value, why conflict occurred         | Must     |
| FR3.2.3 | Error suggests resolution options (remove input, add dependency, use checkpoint) | Must     |

---

### FR4: Caching

#### FR4.1: Signature Computation


| ID      | Requirement                                                                    | Priority |
| --------- | -------------------------------------------------------------------------------- | ---------- |
| FR4.1.1 | Cache signature = hash(code_hash + env_hash + input_values_hash)               | Must     |
| FR4.1.2 | Signature uses actual VALUES, not version numbers                              | Must     |
| FR4.1.3 | Same inputs produce same signature regardless of iteration count               | Must     |
| FR4.1.4 | Different conversation turns (different messages) produce different signatures | Must     |

#### FR4.2: Cache Integration


| ID      | Requirement                                                 | Priority |
| --------- | ------------------------------------------------------------- | ---------- |
| FR4.2.1 | Existing`DiskCache` works with `Graph` (same as Pipeline)   | Must     |
| FR4.2.2 | Cache check happens before node execution                   | Must     |
| FR4.2.3 | Cache hit skips execution, uses cached value, updates state | Must     |
| FR4.2.4 | Node-level`cache=False` disables caching for that node      | Must     |

---

### FR5: Decorators

#### FR5.1: @node Decorator


| ID      | Requirement                                                                    | Priority |
| --------- | -------------------------------------------------------------------------------- | ---------- |
| FR5.1.1 | `@node(output_name="x")` wraps function as graph node                          | Must     |
| FR5.1.2 | Multiple outputs:`@node(output_name=("x", "y"))` with tuple return             | Must     |
| FR5.1.3 | `cache` parameter controls cacheability (default True)                         | Must     |
| FR5.1.4 | Function remains callable without framework (`node.func(args)`)                | Must     |
| FR5.1.5 | Async functions supported (`async def`)                                        | Must     |
| FR5.1.6 | Generator functions accumulated automatically                                  | Must     |
| FR5.1.7 | Function defaults apply only when parameter has NO edge (edge cancels default) | Must     |

#### FR5.2: @route Decorator (New)


| ID      | Requirement                                                                    | Priority |
| --------- | -------------------------------------------------------------------------------- | ---------- |
| FR5.2.1 | `@route(targets=[...])` marks function as routing decision node                | Must     |
| FR5.2.2 | `targets` parameter is required - lists valid node names and/or `END`          | Must     |
| FR5.2.3 | Targets validated at`Graph()` construction (fail fast if target doesn't exist) | Must     |
| FR5.2.4 | Return value must be a string matching a target or`END`                        | Must     |
| FR5.2.5 | Invalid return value (not in targets) raises runtime error                     | Must     |
| FR5.2.6 | Route nodes are never cached (decisions must be re-evaluated)                  | Must     |
| FR5.2.7 | Type hints on return are optional (can be`str`, `Literal[...]`, or omitted)    | Should   |

#### FR5.3: @branch Decorator (Gate for Boolean Decisions)

**`@branch` is a specialized gate** - same concept as `@route`, but optimized for binary (true/false) decisions. Both are "gates" that control execution flow.


| ID      | Requirement                                                                              | Priority |
| --------- | ------------------------------------------------------------------------------------------ | ---------- |
| FR5.3.1 | `@branch(when_true="node_a", when_false="node_b")` routes based on bool (string targets) | Must     |
| FR5.3.2 | Branch targets validated at Graph init (fail fast if target doesn't exist)               | Must     |
| FR5.3.3 | Branch targets can produce same output name (mutually exclusive)                         | Must     |
| FR5.3.4 | Gate signals track which path was taken                                                  | Must     |
| FR5.3.5 | `@branch` is conceptually a `@route` with exactly 2 targets (true/false)                 | Info     |

#### FR5.4: InterruptNode (New)


| ID      | Requirement                                                                                     | Priority |
| --------- | ------------------------------------------------------------------------------------------------- | ---------- |
| FR5.4.1 | `InterruptNode(name="x", input_param="prompt", response_param="response")` declares pause point | Must     |
| FR5.4.2 | `input_param` specifies which value to surface to user                                          | Must     |
| FR5.4.3 | `response_param` specifies where to write user's response                                       | Must     |
| FR5.4.4 | Optional`response_type` for validation                                                          | Should   |
| FR5.4.5 | Framework provides plumbing, user defines prompt/response types                                 | Must     |

#### FR5.5: Type Hints (Optional, Opt-In Validation)

**Core principle:** Type hints are optional for user functions. Validation happens at build time regardless.


| ID      | Requirement                                                                                     | Priority |
| --------- | ------------------------------------------------------------------------------------------------- | ---------- |
| FR5.5.1 | Functions work without type hints - edges inferred from parameter names                         | Must     |
| FR5.5.2 | Type hints on parameters and returns are optional                                               | Must     |
| FR5.5.3 | If type hints present, they can enable static analysis (mypy/pyright)                           | Should   |
| FR5.5.4 | Opt-in type congruence check:`Graph(nodes=[...], validate_types=True)`                          | Should   |
| FR5.5.5 | Type congruence validates: output type of producer matches input type of consumer               | Should   |
| FR5.5.6 | Opt-in Pydantic validation:`@node(validate=True)` uses Pydantic for runtime type checking       | Could    |
| FR5.5.7 | `validate=True` validates BOTH inputs AND outputs (matches Pydantic's `validate_call` behavior) | Could    |

**Validation levels:**


| Level                | When                         | What                                                       | Required?               |
| ---------------------- | ------------------------------ | ------------------------------------------------------------ | ------------------------- |
| **Build-time**       | `Graph()` construction       | Edges exist, routes valid, no deadlocks                    | Always                  |
| **Static**           | mypy/pyright                 | Type correctness                                           | Opt-in (use type hints) |
| **Type congruence**  | `Graph(validate_types=True)` | Output types match input types                             | Opt-in                  |
| **Pydantic runtime** | `@node(validate=True)`       | Inputs validated before execution, outputs validated after | Opt-in                  |

```python
# No type hints - works fine, validated at build time
@node(output_name="docs")
def retrieve(query, messages):
    return search(query)

# With type hints - enables static analysis
@node(output_name="docs")
def retrieve(query: str, messages: list[dict]) -> list[Document]:
    return search(query)

# With Pydantic validation - runtime type checking of BOTH inputs and outputs
@node(output_name="docs", validate=True)
def retrieve(query: str, messages: list[dict]) -> list[Document]:
    # Before execution: Pydantic validates query is str, messages is list[dict]
    # After execution: Pydantic validates return is list[Document]
    return search(query)
```

---

### FR6: API Surface

#### FR6.1: Graph Class (Pure Definition)


| ID       | Requirement                                                                   | Priority |
| ---------- | ------------------------------------------------------------------------------- | ---------- |
| FR6.1.1  | `Graph(nodes=[...])` constructor - no entrypoint needed                       | Must     |
| FR6.1.2  | Graph has NO`run()` method - use Runner instead                               | Must     |
| FR6.1.3  | `graph.visualize()` generates visual representation                           | Should   |
| FR6.1.4  | `graph.bind(**kwargs)` sets default input values                              | Should   |
| FR6.1.5  | `graph.as_node()` wraps graph for nesting                                     | Must     |
| FR6.1.6  | `.as_node().rename(inputs={old: new}, outputs={old: new})` renames interfaces | Must     |
| FR6.1.7  | `.as_node().map_over(names, mode="zip")` enables internal batch processing    | Should   |
| FR6.1.8  | `graph.root_args` returns required inputs                                     | Must     |
| FR6.1.9  | `graph.unfulfilled_args` returns inputs not yet bound                         | Should   |
| FR6.1.10 | `graph.bound_inputs` returns dict of bound values                             | Should   |

**The "Edge Cancels Default" Rule:**

Simple and deterministic:

1. **If parameter has an edge → default is IGNORED** (must get value from edge OR input)
2. **If no edge AND has default → use the default**
3. **If no edge AND no default → required input**

**Why "edge cancels default"?**

- Eliminates ambiguity in cycles (no two nodes can both be "ready" with defaults)
- The input you provide determines where cycles start
- No entrypoint concept needed
- Explicit is better than implicit

**For cyclic parameters:**

- Cyclic parameters have edges (by definition)
- Therefore, defaults are ignored
- Must provide initial value via input

```python
@node(output_name="messages")
def add_response(messages: list, response: str) -> list:
    # messages has edge from itself (accumulator) → no default applies
    return messages + [...]

# Must initialize the cycle:
runner.run(graph, inputs={..., "messages": []})

# Or start from existing conversation:
runner.run(graph, inputs={..., "messages": [{"role": "user", "content": "..."}]})
```

**Input resolution order (when multiple sources exist):**

1. Edge value (if available) - always wins
2. Runtime input - for initialization or override
3. Bound value (`graph.bind()`) - only if no edge
4. Function default - only if no edge

#### FR6.2: Runner Classes (Execution)


| ID      | Requirement                                                           | Priority |
| --------- | ----------------------------------------------------------------------- | ---------- |
| FR6.2.1 | `Runner(cache=..., callbacks=...)` for sync execution                 | Must     |
| FR6.2.2 | `AsyncRunner(cache=..., callbacks=...)` for async execution           | Must     |
| FR6.2.3 | `runner.run(graph, inputs={...})` executes once                       | Must     |
| FR6.2.4 | `runner.run(graph, inputs={...}, select=["pattern"])` filters outputs | Should   |
| FR6.2.5 | `runner.map(graph, inputs={...}, map_over="x")` batch execution       | Should   |
| FR6.2.6 | `async_runner.iter(graph, inputs={...})` returns event stream         | Must     |
| FR6.2.7 | Runner owns cache and callbacks (execution-specific config)           | Must     |
| FR6.2.8 | Same graph can be used with different runners                         | Must     |
| FR6.2.9 | `Runner` raises error if graph has async nodes                        | Must     |

#### FR6.3: Specialized Runners


| ID      | Requirement                                                        | Priority |
| --------- | -------------------------------------------------------------------- | ---------- |
| FR6.3.1 | `DaftRunner` for distributed DataFrame execution (DAG-only graphs) | Should   |
| FR6.3.2 | Runners can be nested via`.as_node(runner=...)`                    | Should   |

#### FR6.4: Runner Compatibility Matrix

**Not all runners support all features.** The framework validates compatibility at runtime and fails fast with clear errors when an incompatible combination is attempted.

**Runner Summary:**

- **`Runner`** - Sync execution, full feature support
- **`AsyncRunner`** - Async execution, full feature support + streaming + interrupts
- **`DaftRunner`** - Distributed execution for DAG-only graphs (no cycles/gates/interrupts)

**Feature Support by Runner:**


| Feature               | `Runner` | `AsyncRunner` | `DaftRunner` |
| ----------------------- | ---------- | --------------- | -------------- |
| DAG execution         | ✅       | ✅            | ✅           |
| Cycles                | ✅       | ✅            | ❌           |
| `@branch` gates       | ✅       | ✅            | ❌           |
| `@route` gates        | ✅       | ✅            | ❌           |
| `InterruptNode`       | ❌       | ✅            | ❌           |
| `.iter()` streaming   | ❌       | ✅            | ❌           |
| `.map()` batch        | ✅       | ✅            | ✅           |
| Async nodes           | ❌       | ✅            | ✅           |
| Distributed execution | ❌       | ❌            | ✅           |

**DaftRunner use case:** High-throughput batch processing of pure DAG pipelines (e.g., embedding generation, data transformation). Not for interactive/cyclic workflows.

**Callback Compatibility:**


| Callback             | `Runner` | `AsyncRunner` | `DaftRunner` |
| ---------------------- | ---------- | --------------- | -------------- |
| `ProgressCallback`   | ✅       | ✅            | ⚠️ partial |
| `TelemetryCallback`  | ✅       | ✅            | ⚠️ partial |
| `on_iteration_start` | ✅       | ✅            | ❌           |
| `on_route_decision`  | ✅       | ✅            | ❌           |

**Cache Compatibility:**


| Cache         | `Runner` | `AsyncRunner` | `DaftRunner`    |
| --------------- | ---------- | --------------- | ----------------- |
| `DiskCache`   | ✅       | ✅            | ✅              |
| `MemoryCache` | ✅       | ✅            | ⚠️ per-worker |

**Implementation approach:**


| ID      | Requirement                                                                          | Priority |
| --------- | -------------------------------------------------------------------------------------- | ---------- |
| FR6.4.1 | Each runner declares its capabilities via class attributes or protocol               | Must     |
| FR6.4.2 | `Graph` validates runner capabilities at `runner.run()` call                         | Must     |
| FR6.4.3 | Clear error: "DaftRunner doesn't support cycles. Use Runner or AsyncRunner instead." | Must     |
| FR6.4.4 | Callbacks declare which runners they support via`supported_runners` attribute        | Should   |
| FR6.4.5 | Cache backends declare distributed compatibility                                     | Should   |

**Example error messages:**

```
IncompatibleRunnerError: This graph has cycles, but DaftRunner doesn't support cycles.

The problem: DaftRunner uses Daft DataFrames for distributed execution, which requires
a DAG structure. Your graph has a cycle: add_user → add_response → add_user

How to fix (pick ONE):

  Option A: Use Runner or AsyncRunner instead
            → runner = AsyncRunner(cache=...) 

  Option B: Restructure as a DAG
            → Remove the cycle by breaking the loop externally
```

```
IncompatibleCallbackError: TelemetryCallback's on_iteration_start event isn't supported by DaftRunner.

The problem: DaftRunner executes nodes in a distributed DataFrame, so per-iteration
callbacks don't make sense.

How to fix:

  Option A: Remove TelemetryCallback when using DaftRunner
  Option B: Use Runner or AsyncRunner if you need iteration tracing
```

---

### FR7: Callbacks & Observability

#### FR7.1: Lifecycle Events


| ID      | Requirement                                                       | Priority |
| --------- | ------------------------------------------------------------------- | ---------- |
| FR7.1.1 | `on_graph_start(inputs)` fired when execution begins              | Must     |
| FR7.1.2 | `on_graph_end(outputs)` fired when execution completes            | Must     |
| FR7.1.3 | `on_node_start(node_name, inputs)` fired before each node         | Must     |
| FR7.1.4 | `on_node_end(node_name, outputs, duration)` fired after each node | Must     |
| FR7.1.5 | `on_node_cached(node_name)` fired on cache hit                    | Must     |
| FR7.1.6 | `on_route_decision(node_name, target)` fired when route decides   | Must     |
| FR7.1.7 | `on_iteration_start(iteration_number)` fired each cycle iteration | Should   |

#### FR7.2: Existing Callbacks


| ID      | Requirement                                            | Priority |
| --------- | -------------------------------------------------------- | ---------- |
| FR7.2.1 | `ProgressCallback` works with Graph (shows progress)   | Should   |
| FR7.2.2 | `TelemetryCallback` works with Graph (Logfire tracing) | Should   |

---

### FR8: Error Handling

#### FR8.1: Error Message Format

**Philosophy:** Errors should be helpful to someone who has never seen the framework before.


| ID      | Requirement                                                           | Priority |
| --------- | ----------------------------------------------------------------------- | ---------- |
| FR8.1.1 | Use simple terms - avoid jargon ("producer", "consumer", "parallel")  | Must     |
| FR8.1.2 | Explain the crux - what's actually wrong, in plain English            | Must     |
| FR8.1.3 | Show the conflict - make it obvious WHY this is a problem             | Must     |
| FR8.1.4 | Give concrete options - 2-3 actionable fixes                          | Must     |
| FR8.1.5 | Suggest typo fixes when applicable (e.g., "Did you mean 'retrieve'?") | Should   |

#### FR8.2: Error Types


| ID      | Requirement                                                        | Priority |
| --------- | -------------------------------------------------------------------- | ---------- |
| FR8.2.1 | `GraphConfigError` for build-time validation failures              | Must     |
| FR8.2.2 | `ConflictError` for "two nodes want to create the same output"     | Must     |
| FR8.2.3 | `InvalidRouteError` for "route returned a node that doesn't exist" | Must     |
| FR8.2.4 | `InfiniteLoopError` for "cycle ran too many times without ending"  | Must     |
| FR8.2.5 | `MissingInputError` for "this value needs a starting value"        | Must     |
| FR8.2.6 | `DeadlockError` for "cycle can't start - no node is ready"         | Must     |

---

### FR9: Human-in-the-Loop & Streaming


| ID    | Requirement                                                          | Priority |
| ------- | ---------------------------------------------------------------------- | ---------- |
| FR9.1 | `InterruptNode` for declarative human-in-the-loop pause points       | Must     |
| FR9.2 | `runner.run(graph, checkpoint=saved)` for resume from state          | Must     |
| FR9.3 | `async_runner.iter(graph, inputs={...})` for event streaming         | Must     |
| FR9.4 | Token-by-token streaming via`StreamingChunkEvent`                    | Must     |
| FR9.5 | Checkpoint serialization/deserialization with`Checkpointer` protocol | Must     |
| FR9.6 | Visualization shows cycles, active node, gate state                  | Should   |
| FR9.7 | Three-layer architecture (UI Protocol, Observability, Durability)    | Should   |
| FR9.8 | `session_id` / `run_id` identity model for correlation               | Must     |

---

### FR10: Input Resolution (Edge Cancels Default)

**Core principle:** If a parameter has an incoming edge, the function default is ignored.


| ID     | Requirement                                                                         | Priority |
| -------- | ------------------------------------------------------------------------------------- | ---------- |
| FR10.1 | If parameter has edge → default is IGNORED, must get value from edge or input      | Must     |
| FR10.2 | If no edge AND has function default → use the default                              | Must     |
| FR10.3 | If no edge AND no default → required input                                         | Must     |
| FR10.4 | Input resolution order: edge value > runtime input > bound value > function default | Must     |
| FR10.5 | Cyclic parameters have edges → must be initialized via input (starts the cycle)    | Must     |
| FR10.6 | `MissingInputError` raised when edge has no value AND no input provided             | Must     |
| FR10.7 | `graph.root_args` returns parameters that can be provided as inputs                 | Should   |
| FR10.8 | `graph.bound_inputs` returns dict of values set via `.bind()`                       | Should   |

**No entrypoint needed.** The input you provide determines where cycles start. Framework runs whatever is ready.

---

### FR11: Nested Graph Composition


| ID     | Requirement                                                                          | Priority |
| -------- | -------------------------------------------------------------------------------------- | ---------- |
| FR11.1 | `graph.as_node()` wraps cyclic graphs as opaque execution units                      | Must     |
| FR11.2 | Nested cyclic graphs execute their internal loops independently until they reach END | Must     |
| FR11.3 | Outer graph waits for nested graph to complete before continuing                     | Must     |
| FR11.4 | `.as_node().rename(inputs={...}, outputs={...})` works for cyclic graphs             | Should   |
| FR11.5 | `.as_node().map_over(names, mode="zip")` works for cyclic graphs                     | Could    |

---

### Functional Requirements Traceability


| User Story            | Required FRs                                                             |
| ----------------------- | -------------------------------------------------------------------------- |
| Multi-turn RAG        | FR1.2.1-5, FR2.1.1-5, FR2.2.1-3, FR2.3.1-3, FR4.1.3-4, FR5.1.7, FR10.1-5 |
| Agentic tool loop     | FR2.2.1-3, FR5.2.1-5, FR2.3.3, FR10.5                                    |
| Iterative refinement  | FR2.2.1-3, FR5.2.1-5, FR4.1.3-4, FR10.5                                  |
| Parallel branch merge | FR1.1.2, FR2.1.1, FR6.1.2                                                |
| Conditional skip      | FR5.3.1-4, FR2.2.4-5                                                     |
| Human-in-the-loop     | FR9.1-5                                                                  |
| Nested cyclic graphs  | FR11.1-4                                                                 |

## Technical Constraints & Non-Functional Requirements

This section defines the technical boundaries, dependencies, and quality attributes that constrain the implementation.

### TC1: Language & Runtime


| Constraint                 | Specification                 | Rationale                                                 |
| ---------------------------- | ------------------------------- | ----------------------------------------------------------- |
| **Python version**         | ≥3.10                        | Match patterns,`Literal` types, union syntax (`X          |
| **Type hints (framework)** | Required on all public APIs   | IDE support, documentation, static analysis               |
| **Type hints (user code)** | Optional                      | Opt-in for extra validation, not required for basic usage |
| **Async support**          | Native`async/await`           | Modern LLM APIs are async-first                           |
| **No global state**        | All state in explicit objects | Testability, thread safety                                |

### TC2: Dependencies

#### Core Dependencies (Required)


| Package    | Version | Purpose                          |
| ------------ | --------- | ---------------------------------- |
| `networkx` | ≥3.0   | Graph data structure, algorithms |

#### Optional Dependencies (Extras)


| Package      | Extra Name    | Purpose                            |
| -------------- | --------------- | ------------------------------------ |
| `graphviz`   | `[viz]`       | Static SVG visualization           |
| `ipywidgets` | `[viz]`       | Interactive Jupyter visualization  |
| `logfire`    | `[telemetry]` | Distributed tracing                |
| `daft`       | `[daft]`      | Distributed execution (DaftRunner) |

#### Dependency Philosophy

- **Zero required dependencies beyond NetworkX** for core functionality
- User installs extras only for features they need
- No transitive dependency on ML frameworks (torch, transformers, etc.)
- Pin minimum versions, not exact versions (flexibility for users)

### TC3: Compatibility

#### With Existing HyperNodes Code


| Item                | Compatibility | Notes                               |
| --------------------- | --------------- | ------------------------------------- |
| `@node` decorator   | Full          | Same signature, same behavior       |
| `@branch` decorator | Full          | Works in Graph context              |
| `DiskCache`         | Full          | Same cache backend                  |
| `Pipeline` class    | Deprecated    | Marked legacy, not removed          |
| Existing callbacks  | Partial       | May need Graph-specific events      |
| `SeqEngine`         | N/A           | Graph uses`GraphEngine`             |
| `DaftRunner`        | Partial       | DAG-only (see Runner Compatibility) |

#### With External Tools


| Tool              | Compatibility | Notes                              |
| ------------------- | --------------- | ------------------------------------ |
| Jupyter notebooks | Full          | Primary development environment    |
| pytest            | Full          | Standard test runner               |
| mypy              | Target        | Type hints should pass strict mode |
| VS Code           | Full          | Should work with Python extension  |
| Logfire           | Full          | Via`TelemetryCallback`             |

### TC4: Code Quality Standards


| Standard                | Target                         | Enforcement        |
| ------------------------- | -------------------------------- | -------------------- |
| **Test coverage**       | ≥80% line coverage            | `pytest-cov` in CI |
| **Type coverage**       | ≥90% of public APIs           | `mypy --strict`    |
| **Docstrings**          | All public classes/functions   | Manual review      |
| **No `# type: ignore`** | Minimize, document when needed | Code review        |
| **Linting**             | Zero errors                    | `ruff` in CI       |
| **Formatting**          | Consistent                     | `ruff format`      |

### TC5: Architecture Constraints

#### Must Follow


| Constraint                | Description                                           |
| --------------------------- | ------------------------------------------------------- |
| **Pure functions**        | Node functions must be testable without framework     |
| **Explicit dependencies** | All dependencies visible in function signatures       |
| **Engine owns runtime**   | Cache, callbacks, execution strategy in Engine        |
| **Graph owns structure**  | DAG definition, validation, node registry in Graph    |
| **State is immutable**    | GraphState operations return new state, don't mutate  |
| **Fail fast**             | Validate as early as possible (build-time > run-time) |

#### Must Avoid


| Anti-Pattern                | Why                                     |
| ----------------------------- | ----------------------------------------- |
| Global registries           | Makes testing hard, hidden dependencies |
| Implicit state modification | Debugging nightmare                     |
| Framework-coupled functions | Can't reuse outside HyperNodes          |
| Magic method resolution     | Explicit > implicit                     |
| Inheritance hierarchies     | Composition over inheritance            |

---

### NFR1: Performance

#### Performance Targets


| Metric                 | Target               | Notes                                  |
| ------------------------ | ---------------------- | ---------------------------------------- |
| **Graph construction** | <100ms for 100 nodes | Build-time validation                  |
| **Per-node overhead**  | <1ms                 | Framework overhead, not node execution |
| **Memory per value**   | <1KB metadata        | Version, timestamps, etc.              |
| **Cache lookup**       | <10ms                | Disk cache signature check             |

**Philosophy:** Optimize for correctness first. Performance profiling happens after validation of the model.

#### Future Optimization Opportunities


| Opportunity             | When to Consider                    |
| ------------------------- | ------------------------------------- |
| Parallel node execution | Multiple independent nodes ready    |
| Lazy value resolution   | Large values not needed downstream  |
| Incremental validation  | Re-validate only changed subgraph   |
| Cache warming           | Pre-populate cache for known inputs |

---

### NFR2: Reliability


| Requirement                  | Specification                               |
| ------------------------------ | --------------------------------------------- |
| **Deterministic execution**  | Same inputs + same code = same outputs      |
| **No silent failures**       | All errors raised, not swallowed            |
| **Graceful degradation**     | Missing optional deps don't crash           |
| **Idempotent re-runs**       | Running twice with cache = same result      |
| **Infinite loop protection** | Configurable max iterations (default: 1000) |

---

### NFR3: Testability


| Requirement             | Specification                           |
| ------------------------- | ----------------------------------------- |
| **Unit testable nodes** | `assert node.func(x) == expected` works |
| **Mock-friendly**       | No hidden dependencies to mock          |
| **Deterministic tests** | No flaky tests from race conditions     |
| **Fast tests**          | Unit tests complete in <5s total        |
| **Integration tests**   | Full graph runs in <30s                 |

#### Testing Patterns

```python
# Unit test a node (no framework needed)
def test_retrieve():
    result = retrieve.func(query="test", messages=[])
    assert isinstance(result, list)

# Integration test a graph
def test_multi_turn():
    graph = Graph(nodes=[...])
    runner = Runner()
    result = runner.run(graph, inputs={"query": "test", "messages": []})
    assert "response" in result

# Test with mocked LLM
def test_with_mock():
    mock_llm = Mock(return_value="mocked response")
    result = runner.run(graph, inputs={"llm": mock_llm, "messages": [], ...})
    mock_llm.assert_called_once()
```

---

### NFR4: Maintainability


| Requirement               | Specification                            |
| --------------------------- | ------------------------------------------ |
| **Single responsibility** | Each module has one clear purpose        |
| **Low coupling**          | Modules interact via defined interfaces  |
| **High cohesion**         | Related code lives together              |
| **Self-documenting**      | Code reads clearly with minimal comments |
| **Changelog**             | All changes documented in CHANGELOG.md   |

#### Module Responsibility Map


| Module            | Responsibility                            |
| ------------------- | ------------------------------------------- |
| `graph.py`        | Graph construction, validation, structure |
| `graph_state.py`  | Value storage, versioning, staleness      |
| `graph_engine.py` | Execution loop, reactive scheduling       |
| `route.py`        | `@route` decorator, control flow          |
| `node.py`         | `@node` decorator (existing, unchanged)   |
| `branch.py`       | `@branch` decorator (existing, unchanged) |
| `cache.py`        | Cache backends (existing, unchanged)      |
| `callbacks.py`    | Callback protocol (extended for Graph)    |

---

### NFR5: Observability


| Requirement            | Specification                           |
| ------------------------ | ----------------------------------------- |
| **Execution trace**    | Know which nodes ran in what order      |
| **Timing data**        | Duration per node, total duration       |
| **State snapshots**    | Value versions at each step             |
| **Error context**      | Full stack trace + graph state on error |
| **Structured logging** | JSON-friendly log output                |

#### Observability via Callbacks

```python
class DebugCallback(PipelineCallback):
    def on_node_end(self, node_name, outputs, duration):
        print(f"{node_name}: {duration:.2f}ms")
    
    def on_route_decision(self, node_name, target):
        print(f"Route {node_name} → {target}")
```

---

### NFR6: Documentation


| Requirement           | Specification                  |
| ----------------------- | -------------------------------- |
| **API reference**     | Docstrings on all public APIs  |
| **Getting started**   | 5-minute quickstart guide      |
| **Migration guide**   | Pipeline → Graph patterns     |
| **Architecture docs** | ADRs for major decisions       |
| **Examples**          | Working code for each use case |

#### Documentation Structure

```
docs/
├── quickstart.md           # Get running in 5 minutes
├── concepts/
│   ├── reactive-dataflow.md    # How the execution model works
│   ├── cycles-and-routing.md   # @route, END, cycles
│   └── state-and-versioning.md # GraphState internals
├── migration/
│   └── pipeline-to-graph.md    # Pattern mapping
├── api/
│   └── reference.md            # Auto-generated from docstrings
├── examples/
│   ├── multi-turn-rag.py
│   ├── agentic-loop.py
│   └── iterative-refinement.py
└── adr/
    ├── 001-networkx-foundation.md
    ├── 002-reactive-vs-imperative.md
    └── 003-route-decorator-design.md
```

---

### NFR7: Security


| Requirement                        | Specification                   |
| ------------------------------------ | --------------------------------- |
| **No code execution from strings** | No`eval()`, no dynamic imports  |
| **No network access in core**      | Network only in user nodes      |
| **No filesystem access in core**   | Only cache backends touch disk  |
| **Safe pickling**                  | Cache uses restricted unpickler |
| **No credential storage**          | User manages secrets externally |

**Note:** This is a library, not a service. Security is primarily about not introducing vulnerabilities, not about access control.

---

### NFR8: Async Execution


| Requirement                   | Target                              | Measurement                                   |
| ------------------------------- | ------------------------------------- | ----------------------------------------------- |
| **Async overhead**            | <5ms per await                      | Benchmark async vs sync execution             |
| **Concurrent node execution** | Framework supports (engine decides) | AsyncRunner can overlap I/O-bound nodes       |
| **Generator accumulation**    | Memory-efficient                    | Streaming chunks don't buffer entire response |

---

### NFR9: Error Recovery


| Requirement                | Target                                    | Measurement                            |
| ---------------------------- | ------------------------------------------- | ---------------------------------------- |
| **Node failure isolation** | Single node failure doesn't corrupt state | Integration tests with failing nodes   |
| **Partial results**        | State available even after failure        | Exception handler preserves GraphState |
| **Retry support**          | Node-level retry possible                 | Callback hook for retry decisions      |

---

### NFR10: Compatibility Policy


| Requirement | Specification                                                  |
| ------------- | ---------------------------------------------------------------- |
| **v0.5.x**  | Breaking changes allowed; no backward compatibility guarantees |
| **v0.6+**   | Deprecation warnings before breaking changes                   |
| **v1.0+**   | Semantic versioning; breaking changes only in major versions   |

---

### Technical Debt Tolerance


| Area               | Tolerance | Approach                         |
| -------------------- | ----------- | ---------------------------------- |
| **Performance**    | High      | Correctness first, profile later |
| **Edge cases**     | Low       | Handle all known edge cases      |
| **Test coverage**  | Low       | ≥80% from start                 |
| **Documentation**  | Medium    | Core docs now, polish later      |
| **Error messages** | Low       | Invest heavily upfront           |

## Risks & Mitigations

This section identifies potential risks and mitigation strategies.

### Technical Risks


| Risk                               | Likelihood | Impact | Mitigation                                                                           |
| ------------------------------------ | ------------ | -------- | -------------------------------------------------------------------------------------- |
| **Reactive dataflow complexity**   | Medium     | High   | Start with simplest use case (multi-turn RAG); validate model before adding features |
| **Cycle detection edge cases**     | Medium     | Medium | Use NetworkX's battle-tested algorithms; extensive test coverage for cycle scenarios |
| **Staleness detection bugs**       | Medium     | High   | Comprehensive unit tests for versioning; property-based testing for edge cases       |
| **Sole producer rule failures**    | Low        | High   | Static analysis at build time; clear error messages when rule is violated            |
| **Runner pattern confusion**       | Medium     | Medium | Clear documentation; error messages explain "use Runner to execute"                  |
| **InterruptNode state management** | Medium     | Medium | Prototype with simple cases first                                                    |

### Design Risks


| Risk                             | Likelihood | Impact | Mitigation                                                             |
| ---------------------------------- | ------------ | -------- | ------------------------------------------------------------------------ |
| **Over-engineering**             | Medium     | Medium | Validate each feature against real multi-turn RAG use case             |
| **API churn**                    | Low        | Medium | No users yet; can iterate freely                                       |
| **Wrong abstraction level**      | Medium     | High   | Build the concrete use case first; abstract patterns after they emerge |
| **Runner vs Engine confusion**   | Medium     | Low    | Consistent naming: Runner = user-facing, Engine = internal             |
| **Literal type validation gaps** | Low        | Medium | Test with mypy strict mode; validate at runtime too                    |

### Implementation Risks


| Risk                            | Likelihood | Impact | Mitigation                                                                         |
| --------------------------------- | ------------ | -------- | ------------------------------------------------------------------------------------ |
| **NetworkX performance**        | Low        | Medium | Profile after correctness; only optimize proven bottlenecks                        |
| **Async/sync mixing**           | Medium     | Medium | Clear separation via`Runner` vs `AsyncRunner`; error early if mismatch             |
| **Generator accumulation bugs** | Medium     | Medium | `inspect.isgenerator()` detection; explicit `streaming=True` opt-in for edge cases |
| **Checkpoint serialization**    | Medium     | Medium | Use standard pickling; test with complex state objects                             |
| **Callback timing**             | Low        | Low    | Fire-and-forget callbacks; errors in callbacks don't break execution               |

### Schedule Risks


| Risk                            | Likelihood | Impact | Mitigation                                                   |
| --------------------------------- | ------------ | -------- | -------------------------------------------------------------- |
| **Scope creep**                 | Medium     | Medium | Strict boundaries; defer nice-to-haves                       |
| **Perfectionism**               | High       | Medium | Ship working MVP; iterate based on actual usage              |
| **Research rabbit holes**       | Medium     | Low    | Time-box research; document findings even if not implemented |
| **Testing time underestimated** | Medium     | Medium | Write tests alongside code, not after                        |

### Dependency Risks


| Risk                            | Likelihood | Impact | Mitigation                                                |
| --------------------------------- | ------------ | -------- | ----------------------------------------------------------- |
| **NetworkX breaking changes**   | Low        | Medium | Pin minimum version; minimal surface area usage           |
| **Python version requirements** | Low        | Low    | Target 3.10+ (widely adopted); document reasoning         |
| **Optional dependency issues**  | Low        | Low    | Graceful degradation; clear error if optional dep missing |

---

### Risk Matrix Summary

```
        High Impact
            ↑
  Staleness │ Reactive   
    bugs    │ dataflow
            │     
  ─────────────────────→ High Likelihood
            │
  Cycle     │ Scope
  detection │ creep
            │
        Low Impact
```

**Focus Areas:**

1. **Staleness detection** - Core to correctness; invest in testing
2. **Reactive dataflow complexity** - Validate with real use case early
3. **Scope creep** - Maintain strict boundaries

## Project Scoping

### MVP Strategy & Philosophy

**MVP Approach:** Problem-Solving MVP

- Solve the core problem (multi-turn RAG) with minimal features
- Validate through personal usage
- "Infrastructure-as-learning" - deep understanding over feature breadth

**Resource Requirements:**

- Team: Solo developer
- Skills: Python, graph theory, async programming
- Timeline: Weeks to months (personal project pace)

### Feature Set

**Core User Journeys Supported:**

1. Multi-turn Conversational RAG (primary)
2. Agentic Tool Loop
3. Iterative Refinement
4. Conditional Skip (existing @branch gate)
5. Parallel Branch Merge (DAG subset)
6. Human-in-the-Loop (AsyncRunner)
7. Distributed Batch Processing (DaftRunner for DAG-only)

**Must-Have Capabilities:**


| Capability                        | Rationale                                            |
| ----------------------------------- | ------------------------------------------------------ |
| Cyclic execution                  | Core differentiator, enables multi-turn              |
| Reactive dataflow with versioning | Prevents infinite loops, enables staleness detection |
| `@route` and `@branch` gates      | Control flow with build-time validation              |
| `Runner`/`AsyncRunner` separation | Clean architecture, pure Graph definition            |
| `DaftRunner` for DAG graphs       | Distributed batch processing                         |
| Build-time validation             | Fail-fast philosophy                                 |
| Generator accumulation            | Modern LLM APIs return generators                    |
| `.iter()` streaming API           | Token-by-token streaming (AsyncRunner)               |
| `InterruptNode`                   | Human-in-the-loop pause/resume                       |
| Checkpointing                     | Persist and resume execution state                   |
| Existing`DiskCache` integration   | Don't reinvent working infrastructure                |
| Runner compatibility validation   | Fail fast on incompatible runner/graph combinations  |

### Future Features

**Potential future additions:**


| Feature             | When to Consider                 |
| --------------------- | ---------------------------------- |
| Durable workflows   | Multi-day execution needs emerge |
| Web debugging UI    | Need beyond Jupyter              |
| Multi-backend cache | Redis/S3 adapter needs           |

### Risk Mitigation Strategy

**Technical Risks:**


| Risk                         | Mitigation                                                       |
| ------------------------------ | ------------------------------------------------------------------ |
| Reactive dataflow complexity | Start with simplest use case (multi-turn RAG); extensive testing |
| Staleness detection bugs     | Property-based testing; comprehensive unit tests                 |
| Cycle detection edge cases   | Use NetworkX battle-tested algorithms                            |

**Resource Risks:**


| Risk          | Mitigation                               |
| --------------- | ------------------------------------------ |
| Scope creep   | Strict boundaries; defer nice-to-haves   |
| Perfectionism | Ship working MVP; iterate based on usage |

### Scope Reduction Contingencies

**If time-constrained, cut in this order:**

1. Visualization updates (can use existing Pipeline viz)
2. `ProgressCallback` integration (callbacks optional)
3. `map()` support (focus on `run()` first)
4. Async support (sync `Runner` sufficient for validation)

**Minimum viable scope:**

- `Graph` class with NetworkX
- `@route` decorator
- `Runner.run()` (sync only)
- Build-time validation
- Core staleness detection
