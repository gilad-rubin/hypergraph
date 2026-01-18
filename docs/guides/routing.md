# Routing Guide

Control execution flow with conditional routing. Route to different paths based on data, loop for agentic workflows, or terminate early.

- **@route** - Route execution to one of several target nodes based on a function's return value
- **END** - Sentinel indicating execution should terminate along this path
- **multi_target** - Route to multiple nodes in parallel when needed
- **Real patterns** - RAG with diagram detection, multi-turn conversations, quality gates

## When to Use Routing

Routing solves problems that pure DAGs cannot:

| Pattern | Example | Why DAGs Fail |
|---------|---------|---------------|
| **Conditional paths** | Route based on document type | DAGs execute all branches |
| **Early termination** | Stop if cache hit | DAGs run to completion |
| **Agentic loops** | Retry until quality threshold | DAGs have no cycles |
| **Multi-turn conversation** | Continue until user satisfied | DAGs are single-pass |

## Basic Routing

### Route to One of Several Targets

```python
from hypergraph import Graph, node, route, END, SyncRunner

@node(output_name="complexity")
def analyze(document: str) -> str:
    """Classify document complexity."""
    if len(document) < 100:
        return "simple"
    elif "diagram" in document.lower():
        return "visual"
    return "complex"

@route(targets=["simple_path", "visual_path", "complex_path"])
def choose_path(complexity: str) -> str:
    """Route based on complexity analysis."""
    if complexity == "simple":
        return "simple_path"
    elif complexity == "visual":
        return "visual_path"
    return "complex_path"

@node(output_name="response")
def simple_path(document: str) -> str:
    return f"Quick answer: {document[:50]}"

@node(output_name="response")
def visual_path(document: str) -> str:
    return f"Processing visual content: {document}"

@node(output_name="response")
def complex_path(document: str) -> str:
    return f"Deep analysis: {document}"

graph = Graph([analyze, choose_path, simple_path, visual_path, complex_path])
runner = SyncRunner()

result = runner.run(graph, {"document": "This contains a diagram of the architecture"})
print(result["response"])  # "Processing visual content: ..."
```

The routing function examines data and returns the target node name. Only that node executes.

### Terminate Early with END

```python
from hypergraph import route, END

@route(targets=["process", END])
def check_cache(query: str) -> str:
    """Skip processing if answer is cached."""
    if query in cache:
        return END  # Stop here, don't run "process"
    return "process"
```

When a route returns `END`, execution terminates along that path. Other independent paths continue.

## Real-World Example: RAG with Diagram Detection

Documents containing diagrams need special handling - convert pages with diagrams to images and send them alongside text to a multimodal LLM.

```python
from hypergraph import Graph, node, route, END, SyncRunner

@node(output_name="documents")
def retrieve(query: str, embedding: list[float]) -> list[dict]:
    """Retrieve relevant documents from vector store."""
    return vector_db.search(embedding, top_k=5)

@node(output_name="doc_analysis")
def analyze_documents(documents: list[dict]) -> dict:
    """Analyze documents to detect which pages contain diagrams."""
    analysis = {
        "has_diagrams": False,
        "diagram_pages": [],
        "text_content": [],
    }
    for doc in documents:
        for page_num, page in enumerate(doc["pages"]):
            if page.get("has_diagram"):
                analysis["has_diagrams"] = True
                analysis["diagram_pages"].append({
                    "doc_id": doc["id"],
                    "page_num": page_num,
                    "content": page["content"],
                })
            analysis["text_content"].append(page["text"])
    return analysis

@route(targets=["text_only_response", "multimodal_response"])
def route_by_content(doc_analysis: dict) -> str:
    """Route based on whether documents contain diagrams."""
    if doc_analysis["has_diagrams"]:
        return "multimodal_response"
    return "text_only_response"

@node(output_name="response")
def text_only_response(query: str, doc_analysis: dict) -> str:
    """Generate response using text-only LLM."""
    context = "\n".join(doc_analysis["text_content"])
    return llm.generate(
        model="gpt-4",
        messages=[
            {"role": "system", "content": f"Context:\n{context}"},
            {"role": "user", "content": query},
        ],
    )

@node(output_name="response")
def multimodal_response(query: str, doc_analysis: dict) -> str:
    """Generate response using multimodal LLM with diagram images."""
    # Convert diagram pages to images
    images = []
    for page_info in doc_analysis["diagram_pages"]:
        image = pdf_to_image(page_info["doc_id"], page_info["page_num"])
        images.append(image)

    # Combine text and images for multimodal LLM
    context = "\n".join(doc_analysis["text_content"])
    return multimodal_llm.generate(
        model="gpt-4-vision",
        messages=[
            {"role": "system", "content": f"Context:\n{context}"},
            {"role": "user", "content": [
                {"type": "text", "text": query},
                *[{"type": "image", "image": img} for img in images],
            ]},
        ],
    )

# Build the graph
rag_with_diagrams = Graph([
    retrieve,
    analyze_documents,
    route_by_content,
    text_only_response,
    multimodal_response,
])

# Run
runner = SyncRunner()
result = runner.run(rag_with_diagrams, {
    "query": "Explain the system architecture",
    "embedding": [0.1, 0.2, ...],
})
print(result["response"])
```

The routing logic is clean and explicit:
1. Retrieve documents
2. Analyze for diagrams
3. Route to appropriate LLM based on content type
4. Generate response with the right model

## Agentic Loops

Routing enables cycles - essential for agentic workflows where the system iterates until a condition is met.

### Multi-Turn RAG with Refinement

```python
from hypergraph import Graph, node, route, END, SyncRunner

@node(output_name="docs")
def retrieve(query: str, conversation: list) -> list[str]:
    """Retrieve documents based on query and conversation context."""
    search_query = query
    if conversation:
        # Refine search based on conversation history
        last_exchange = conversation[-1]
        search_query = f"{query} {last_exchange.get('followup', '')}"
    return vector_db.search(search_query)

@node(output_name="response")
def generate(docs: list[str], query: str, conversation: list) -> str:
    """Generate response from documents and conversation history."""
    return llm.chat(
        context=docs,
        query=query,
        history=conversation,
    )

@node(output_name="conversation")
def update_conversation(conversation: list, response: str, user_satisfied: bool) -> list:
    """Append the latest exchange to conversation history."""
    return conversation + [{
        "response": response,
        "satisfied": user_satisfied,
    }]

@route(targets=["retrieve", END])
def should_continue(conversation: list, max_turns: int = 5) -> str:
    """Continue if user not satisfied and under turn limit."""
    if len(conversation) >= max_turns:
        return END
    if conversation and conversation[-1].get("satisfied"):
        return END
    return "retrieve"  # Loop back for another round

graph = Graph([retrieve, generate, update_conversation, should_continue])

# Seed inputs break the cycle - initial values before first iteration
runner = SyncRunner()
result = runner.run(graph, {
    "query": "Explain microservices",
    "conversation": [],  # Seed: start with empty history
    "max_turns": 3,
    "user_satisfied": False,  # Initial state
})
```

The graph loops: retrieve → generate → update → should_continue → retrieve, until `should_continue` returns `END`.

### Quality Gate with Retry

```python
from hypergraph import Graph, node, route, END, SyncRunner

@node(output_name="draft")
def generate_draft(prompt: str, feedback: str = "") -> str:
    """Generate content, incorporating feedback if provided."""
    full_prompt = prompt
    if feedback:
        full_prompt = f"{prompt}\n\nPrevious feedback: {feedback}"
    return llm.generate(full_prompt)

@node(output_name="score")
def evaluate(draft: str) -> float:
    """Score the draft quality (0-1)."""
    return quality_model.score(draft)

@node(output_name="feedback")
def generate_feedback(draft: str, score: float) -> str:
    """Generate improvement suggestions."""
    if score >= 0.8:
        return ""  # No feedback needed
    return critic_llm.suggest_improvements(draft)

@route(targets=["generate_draft", "finalize"])
def quality_gate(score: float) -> str:
    """Route based on quality threshold."""
    if score >= 0.8:
        return "finalize"
    return "generate_draft"  # Retry

@node(output_name="final")
def finalize(draft: str) -> str:
    """Final processing of approved draft."""
    return draft.strip()

graph = Graph([generate_draft, evaluate, generate_feedback, quality_gate, finalize])

runner = SyncRunner()
result = runner.run(graph, {
    "prompt": "Write a technical blog post about Python generators",
    "feedback": "",  # Seed: start with no feedback
})
print(result["final"])
```

## Multi-Target Routing

Sometimes you need to run multiple paths in parallel. Use `multi_target=True`.

```python
@route(targets=["notify_slack", "notify_email", "log_event"], multi_target=True)
def choose_notifications(event_type: str, severity: str) -> list[str]:
    """Route to multiple notification channels based on event."""
    targets = ["log_event"]  # Always log

    if severity == "critical":
        targets.extend(["notify_slack", "notify_email"])
    elif severity == "warning":
        targets.append("notify_slack")

    return targets

@node(output_name="slack_sent")
def notify_slack(message: str) -> bool:
    return slack.send(message)

@node(output_name="email_sent")
def notify_email(message: str) -> bool:
    return email.send(message)

@node(output_name="logged")
def log_event(message: str) -> bool:
    return logger.info(message)
```

With `multi_target=True`, the function returns a list of targets to execute. All returned targets run (potentially in parallel with AsyncRunner).

**Important**: When using `multi_target=True`, target nodes must have unique output names. If multiple nodes produce the same output name, you'll get a `GraphConfigError` at build time.

## Fallback Targets

When a routing function might return `None`, use `fallback` to specify a default:

```python
@route(targets=["premium_path", "standard_path"], fallback="standard_path")
def route_by_tier(user_tier: str | None) -> str | None:
    """Route premium users to premium path."""
    if user_tier == "premium":
        return "premium_path"
    return None  # Falls back to standard_path
```

## Validation and Error Handling

### Build-Time Validation

Hypergraph validates routing at graph construction:

```python
@route(targets=["step_a", "step_b", END])
def decide(x: int) -> str:
    return "step_c"  # Typo - not in targets

graph = Graph([decide, step_a, step_b])
# GraphConfigError: Route target 'step_c' not found.
# Valid targets: ['step_a', 'step_b', 'END']
# Did you mean 'step_a'?
```

### Invalid Return Values at Runtime

If a routing function returns a value not in its targets:

```python
@route(targets=["a", "b"])
def decide(x: int) -> str:
    return "nonexistent"  # Not in targets!

graph = Graph([decide, a, b])
result = runner.run(graph, {"x": 5})

# result.status == RunStatus.FAILED
# result.error: ValueError: invalid target 'nonexistent'
```

### Type Safety

Routing functions must be synchronous and non-generator:

```python
# This raises TypeError at decoration time:
@route(targets=["a", "b"])
async def async_decide(x: int) -> str:  # Can't be async
    return "a"

# Error: Routing function 'async_decide' cannot be async.
# Routing decisions should be fast and based on already-computed values.
```

## Patterns and Best Practices

### Keep Routing Functions Simple

Routing functions should be fast and deterministic. Move complex logic to regular nodes:

```python
# Good: Routing based on pre-computed value
@node(output_name="doc_type")
def classify(document: str) -> str:
    """Heavy classification logic here."""
    return classifier.predict(document)

@route(targets=["pdf_processor", "image_processor", "text_processor"])
def route_by_type(doc_type: str) -> str:
    """Simple routing based on classification result."""
    return f"{doc_type}_processor"
```

### Use Descriptive Targets

You can provide descriptions for visualization and documentation:

```python
@route(targets={
    "text_only_response": "Use text-only LLM for documents without visual content",
    "multimodal_response": "Use vision LLM for documents containing diagrams",
    END: "Terminate if no relevant documents found",
})
def route_by_content(doc_analysis: dict) -> str:
    ...
```

### Chained Gates

Gates can route to other gates for multi-stage decisions:

```python
@route(targets=["check_quality", END])
def check_length(text: str) -> str:
    """First gate: check minimum length."""
    return END if len(text) < 10 else "check_quality"

@route(targets=["process", END])
def check_quality(text: str) -> str:
    """Second gate: check content quality."""
    return "process" if is_quality_content(text) else END

@node(output_name="result")
def process(text: str) -> str:
    return text.upper()

graph = Graph([check_length, check_quality, process])
```

## Next Steps

- [API Reference: Gates](../api/gates.md) - Complete RouteNode and @route documentation
- [Philosophy](../philosophy.md) - Why hypergraph supports cycles
- [Getting Started](../getting-started.md) - Core concepts and basics
