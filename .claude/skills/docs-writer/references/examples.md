# Annotated Documentation Examples

Real before/after rewrites and annotated good examples. Use these as calibration for writing quality.

## Table of Contents

1. [Landing Page: Good Example (Annotated)](#landing-page-good-example)
2. [Before/After: Opening Hook](#beforeafter-opening-hook)
3. [Before/After: Introducing a Code Example](#beforeafter-introducing-a-code-example)
4. [Before/After: Feature Explanation](#beforeafter-feature-explanation)
5. [Before/After: Decision Tables](#beforeafter-decision-tables)
6. [Before/After: Error Examples](#beforeafter-error-examples)
7. [Before/After: Design Philosophy Section](#beforeafter-design-philosophy-section)
8. [Before/After: Pattern Page Opening](#beforeafter-pattern-page-opening)
9. [The Spectrum Pattern](#the-spectrum-pattern)
10. [The Problem-First Pattern](#the-problem-first-pattern)
11. [Common Mistakes Gallery](#common-mistakes-gallery)

---

## Landing Page: Good Example

This annotated example shows a well-structured landing page. Each annotation explains the writing technique.

```markdown
# MyLib
                                          ← [TECHNIQUE: Bold tagline as subtitle]
**One framework for the full spectrum of Python workflows** — from batch data
pipelines to multi-turn AI agents.
                                          ← [TECHNIQUE: "The Idea" bridges from
                                              tagline to mental model]
## The Idea

Data pipelines and agentic AI share more than you'd expect. Both are graphs
of functions — the difference is whether the graph has cycles. MyLib
gives you one framework that handles the full spectrum:
                                          ← [TECHNIQUE: ASCII diagram as visual
                                              anchor. Reader remembers this.]
┌────────────────────────────────────────────────────────────────┐
│  Batch Pipelines  →  Branching  →  Agentic Loops              │
│  (DAG)               (@ifelse)     (@route, END)              │
│  ───────────── the library handles all of it ────────────     │
└────────────────────────────────────────────────────────────────┘
                                          ← [TECHNIQUE: "How It Works" shows
                                              code within 30 seconds of opening]
## How It Works

Define functions. Name their outputs. The library connects them automatically.
                                          ← [TECHNIQUE: 1-sentence setup before
                                              code. Says what AND why.]
```python
from mylib import Graph, node, SyncRunner

@node(output_name="embedding")
def embed(text: str) -> list[float]:     ← [TECHNIQUE: Comments say "your X here"
    # Your embedding model here               to show where real logic goes]
    return [0.1, 0.2, 0.3]
...
```
                                          ← [TECHNIQUE: 1-line explanation AFTER
                                              code. The "aha" moment.]
`embed` produces `embedding`. `retrieve` takes `embedding`. Connected automatically.
```

**Why it works**:
- Hook → Visual → Code → Explanation in under 60 seconds
- Reader knows what the library does before they see a single API
- The spectrum diagram gives them a mental model they can carry forward
- Code is realistic (RAG pipeline) not a toy example

---

## Before/After: Opening Hook

### Before (weak)

```markdown
# Introduction

MyLib is a Python library for workflow orchestration. It supports
various types of workflows including DAGs, conditional branching, and
agentic patterns with cycles. This document will explain the key features
and design principles of the framework.
```

**Problems**: Generic ("Python library"), passive ("this document will explain"), lists features without context, doesn't answer "why should I care?"

### After (strong)

```markdown
# What is MyLib?

**One framework for the full spectrum of Python workflows** — from batch
data pipelines to multi-turn AI agents.

Data pipelines and agentic AI share more than you'd expect. Both are
graphs of functions — the difference is whether the graph has cycles.
```

**Why it works**: Starts with a bold claim (not a description), connects two familiar concepts (pipelines and agents), reveals an insight (both are graphs), and tells you what this library uniquely does about it.

---

## Before/After: Introducing a Code Example

### Before (weak)

```markdown
The following code example demonstrates the basic usage of the node decorator
and Graph class. First, we define three functions using the @node decorator,
each with a specified output_name parameter. Then we create a Graph instance
and run it with a SyncRunner:
```

**Problems**: Narrates what the reader can see. "The following" is filler. Describes syntax instead of purpose.

### After (strong)

```markdown
Define functions. Name their outputs. The framework connects them automatically.
```

**Why it works**: States the mental model (3 short imperatives), then the payoff (automatic connection). Reader knows *why* this code is structured this way before reading it. After the code, a single line explains the key mechanism:

```markdown
`embed` produces `embedding`. `retrieve` takes `embedding`. Connected automatically.
```

---

## Before/After: Feature Explanation

### Before (weak)

```markdown
## Testing

The library's nodes can be tested using standard testing frameworks. Because
nodes are decorated functions, you can access the underlying function using
the .func attribute of the node. This allows you to call the function
directly in your tests without needing to instantiate a graph or runner.
The advantage of this approach is that your tests remain fast and isolated.
```

**Problems**: 4 sentences saying the same thing. Buries the insight (`.func`). Tells you it's advantageous instead of showing you.

### After (strong)

```markdown
## Pure, Testable Functions

Your functions are just functions. Test them directly:

```python
def test_embed():
    result = embed.func("hello world")
    assert len(result) == 768
```

No graph, no runner, no framework. Standard test tools.
```

**Why it works**: Claim → Code → Payoff. The code IS the explanation. ".func" is self-documenting in context. "No graph, no runner, no framework" drives the point home in a way a paragraph never could.

---

## Before/After: Decision Tables

### Before (weak)

```markdown
There are several cases where you should consider using this library.
If you need to build workflows that combine different types of patterns,
like both DAGs and cycles, it is a good choice. It's also useful
when you want to compose smaller workflows into larger ones. On the other
hand, if you only need a simple DAG runner or you need a hosted orchestrator,
other tools might be more appropriate.
```

**Problems**: Vague ("several cases"), no concrete examples, buries the "don't use" signal, hard to scan.

### After (strong)

```markdown
## When to Use Routing

| Pattern | Example | Why DAGs Fail |
|---------|---------|---------------|
| **Conditional paths** | Route based on document type | DAGs execute all branches |
| **Early termination** | Stop if cache hit | DAGs run to completion |
| **Agentic loops** | Retry until quality threshold | DAGs have no cycles |
| **Multi-turn conversation** | Continue until user satisfied | DAGs are single-pass |
```

**Why it works**: Scannable in 3 seconds. Each row is a concrete scenario. The "Why DAGs Fail" column makes the value proposition self-evident — the reader doesn't need to be told the library is better, they can see it.

---

## Before/After: Error Examples

### Before (weak)

```markdown
The framework validates your graph at build time and will raise an error if
there are invalid route targets.
```

**Problems**: States a fact without showing the benefit. The reader has to trust you.

### After (strong)

```markdown
Catch errors when you build the graph, not at 2am in production:

```python
@route(targets=["step_a", "step_b", END])
def decide(x: int) -> str:
    return "step_c"  # Typo

graph = Graph([decide, step_a, step_b])
# GraphConfigError: Route target 'step_c' not found.
# Valid targets: ['step_a', 'step_b', 'END']
# Did you mean 'step_a'?
```

**Why it works**: Shows the actual error message. The reader sees the typo, sees the helpful error, and immediately understands the value. "Not at 2am in production" is memorable and concrete — better than "build-time validation."

---

## Before/After: Design Philosophy Section

### Before (weak)

```markdown
## Design Philosophy

The library was designed with several principles in mind. We believe in
keeping functions pure and composable. The framework uses automatic edge
inference to reduce boilerplate. We also prioritize build-time validation
to catch errors early.
```

**Problems**: Tells principles without showing why they matter. "We believe" is unsubstantiated. No narrative tension.

### After (strong)

```markdown
## Where DAGs Hit the Wall

The DAG constraint works beautifully for ETL, single-pass ML inference,
and batch processing. But it fundamentally breaks for modern AI workflows:

| Use Case | Why DAGs Fail |
|----------|---------------|
| **Multi-turn RAG** | User follows up, system needs to retrieve **more**. Needs to loop back. |
| **Agentic workflows** | LLM decides next action, may need to retry/refine |
| **Iterative refinement** | Generate, evaluate, if not good enough, generate again |

### The Inciting Incident

The breaking point was building a multi-turn RAG system where:
1. User asks a question
2. System retrieves and generates
3. User says "can you explain X in more detail?"
4. System needs to **retrieve more documents** using conversation context

Step 4 is **impossible** in a DAG.
```

**Why it works**: Tells a story with tension (problem → inciting incident → solution). The reader experiences the limitation before hearing the solution. "Step 4 is impossible in a DAG" is unforgettable.

---

## Before/After: Pattern Page Opening

### Before (weak)

```markdown
# Routing

This page describes the routing capabilities of the framework. Routing allows
you to control execution flow based on conditions. There are two main
decorators: @ifelse and @route.
```

**Problems**: Describes the page instead of the problem. Lists features before context.

### After (strong)

```markdown
# Routing Guide

Control execution flow with conditional routing. Route to different paths
based on data, loop for agentic workflows, or terminate early.

- **@ifelse** — Simple boolean routing: True goes one way, False goes another
- **@route** — Route to one of several targets based on a function's return value
- **END** — Sentinel indicating execution should terminate along this path
```

**Why it works**: First sentence is action-oriented (what you'll DO). Bullet list gives you the API surface in 5 seconds. Each bullet is a one-line definition. Reader can decide which section to jump to immediately.

---

## The Spectrum Pattern

When a library covers a range of use cases, show the range visually.

```markdown
┌────────────────────────────────────────────────────────────────┐
│  Simple Task  →  Medium Complexity  →  Advanced Pattern        │
│  (basic API)     (composition)         (full framework)        │
│  ──────────── your library handles all of it ────────────     │
└────────────────────────────────────────────────────────────────┘
```

This ASCII pattern works because it gives the reader a spatial mental model. They can locate themselves on the spectrum and know where they're headed.

**When to use**: Landing pages and "What is X?" pages where the library spans multiple complexity levels.

---

## The Problem-First Pattern

For design/philosophy pages, state the problem BEFORE the solution. Create narrative tension.

**Structure**:
1. "X works great for A, B, C..."
2. "But it fundamentally breaks for D, E, F..."
3. Table showing concrete failures
4. "The inciting incident was when we tried to..."
5. "Step N is impossible because..."
6. "So we built [solution]."

This mirrors how the reader thinks: "I have a problem, does this library solve it?" Starting with the problem lets them self-qualify.

---

## Common Mistakes Gallery

Patterns to actively avoid in documentation. Each shows the mistake and the fix.

### Mistake 1: The Feature Dump

```markdown
BAD: The library supports DAGs, cycles, routing, branching, streaming,
caching, HITL, async, sync, generators, composition, nesting, batch
processing, map operations, events, validation, and more.
```

```markdown
GOOD: Define functions. Name their outputs. The framework connects them
automatically. Add routing when you need branches. Add cycles when
you need agents.
```

### Mistake 2: The "We" Voice

```markdown
BAD: We designed the framework to be composable. We believe that
functions should be pure and testable.
```

```markdown
GOOD: Your functions are just functions. Test them directly — no graph,
no runner, no framework needed.
```

### Mistake 3: The Apology

```markdown
BAD: This is a somewhat advanced topic that may take some time to fully
understand. Don't worry if it doesn't click right away.
```

```markdown
GOOD: Hierarchical composition means a graph can become a node in another
graph. Here's what that looks like:
```

### Mistake 4: The Fake Question

```markdown
BAD: Have you ever wondered how to handle conditional routing in your
workflow graphs? Well, this library has the answer!
```

```markdown
GOOD: Control execution flow with conditional routing. Route to different
paths based on data, loop for agentic workflows, or terminate early.
```

### Mistake 5: The Synonym Paragraph

```markdown
BAD: Nodes are the basic building blocks of your graph. They are the
fundamental units of computation. Each node represents a single
step in your workflow. Nodes process inputs and produce outputs.
```

```markdown
GOOD: A *node* is a pure function decorated with `@node`. It takes typed
inputs and produces a named output:

@node(output_name="embedding")
def embed(text: str) -> list[float]: ...
```
