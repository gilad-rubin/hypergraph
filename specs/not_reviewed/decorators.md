# Decorators API Specification

## @node

### Purpose

Wraps a function as a graph node with named output(s).

### Signature

```python
def node(
    outputs: str | tuple[str, ...],
    *,
    cache: bool = True,
) -> Callable[[F], HyperNode[F]]:
    """
    Decorate a function as a graph node.
    
    Args:
        outputs: Name(s) for the output value(s).
                     Single string for one output.
                     Tuple of strings for multiple outputs (function must return tuple).
        cache: Whether to cache results (default True).
    
    Returns:
        HyperNode wrapping the function.
    
    Example:
        @node(outputs="embedding")
        def embed(text: str) -> list[float]:
            return model.encode(text)
        
        @node(outputs=("score", "explanation"))
        def evaluate(text: str) -> tuple[float, str]:
            return (0.95, "Good quality")
    """
```

### HyperNode Object

The decorated function becomes a `HyperNode` object:

```python
class HyperNode(Generic[F]):
    name: str              # Function name (used as node identifier)
    func: F                # Original function (still callable)
    outputs: str | tuple[str, ...]
    parameters: list[str]  # Parameter names from signature
    cache: bool
    
    def __call__(self, *args, **kwargs) -> Any:
        """Call the underlying function directly."""
        return self.func(*args, **kwargs)
```

### Properties

- **Portability**: `node.func(x)` works without framework
- **Testability**: `assert embed.func("hello") == expected`
- **Introspection**: `node.parameters`, `node.outputs` available

### Async Support

```python
@node(outputs="response")
async def generate(messages: list) -> str:
    return await llm.chat(messages)

# Works with AsyncRunner
# Raises IncompatibleRunnerError with SyncRunner
```

### Generator Support

```python
@node(outputs="response")
async def stream_generate(messages: list):
    async for chunk in llm.stream(messages):
        yield chunk

# Framework automatically accumulates chunks
# Final value stored in state
# Streaming available via AsyncRunner.iter()
```

### Multiple Outputs

```python
@node(outputs=("docs", "scores"))
def retrieve(query: str) -> tuple[list[str], list[float]]:
    results = search(query)
    return [r.text for r in results], [r.score for r in results]

# Creates two values in state: "docs" and "scores"
# Downstream nodes can depend on either
```

---

## @route

### Purpose

Multi-way routing decision node. Returns target node name as string.

### Signature

```python
def route(
    targets: list[str | type[END]],
    *,
    cache: bool = False,
) -> Callable[[F], RouteNode[F]]:
    """
    Decorate a function as a routing decision node.
    
    Args:
        targets: REQUIRED list of valid target node names and/or END.
                 Every possible return value must be declared here.
        cache: Whether to cache decisions (default False).
               Usually False because decisions depend on runtime state.
    
    Returns:
        RouteNode wrapping the function.
    
    Raises:
        GraphConfigError: At Graph() if any target doesn't exist.
        InvalidRouteError: At runtime if return value not in targets.
    
    Example:
        @route(targets=["retrieve", "generate", END])
        def decide_next(messages: list) -> str:
            if is_done(messages):
                return END
            if needs_more_context(messages):
                return "retrieve"
            return "generate"
    """
```

### RouteNode Object

```python
class RouteNode(Generic[F]):
    name: str
    func: F
    targets: list[str]     # Validated target names
    parameters: list[str]
    cache: bool
    
    # Routes produce a routing decision output (internal)
```

### Validation

**Build-time (at `Graph()` construction):**
```python
# All targets must exist
for target in route_node.targets:
    if target is not END and target not in graph.node_names:
        raise GraphConfigError(
            f"@route target '{target}' doesn't exist\n\n"
            f"  → {route_node.name}() declares targets={route_node.targets}\n"
            f"  → No node named '{target}' in this graph\n"
            f"  → Available nodes: {graph.node_names}\n"
        )
```

**Runtime (after route executes):**
```python
result = route_node.func(**inputs)
if result not in route_node.targets and result is not END:
    raise InvalidRouteError(
        f"Route returned '{result}' but that's not a valid target\n\n"
        f"  → {route_node.name}() returned \"{result}\"\n"
        f"  → Valid targets are: {route_node.targets}\n"
        + (f"\nHint: Did you mean '{closest_match}'?" if closest_match else "")
    )
```

### Type Hints (Optional)

```python
# All valid - type hints are optional
@route(targets=["a", "b", END])
def decide(x: int) -> str: ...

@route(targets=["a", "b", END])
def decide(x: int) -> Literal["a", "b"] | type[END]: ...

@route(targets=["a", "b", END])
def decide(x: int): ...  # No return type hint
```

---

## @branch

### Purpose

Binary routing for boolean decisions. Syntactic sugar over `@route`.

### Signature

```python
def branch(
    when_true: str | HyperNode,
    when_false: str | HyperNode,
    *,
    cache: bool = False,
) -> Callable[[F], BranchNode[F]]:
    """
    Decorate a function as a binary routing decision.
    
    Args:
        when_true: Target when function returns True.
                   Can be node name (str) or node object.
        when_false: Target when function returns False.
        cache: Whether to cache (default False).
    
    Returns:
        BranchNode wrapping the function.
    
    Example:
        @branch(when_true="use_cache", when_false="compute")
        def check_cache(key: str, cache: dict) -> bool:
            return key in cache
    """
```

### BranchNode Object

```python
class BranchNode(Generic[F]):
    name: str
    func: F
    when_true: str         # Target node name
    when_false: str        # Target node name
    parameters: list[str]
    cache: bool
```

### Target Resolution

Targets can be strings or node objects:

```python
@node(outputs="result")
def path_a(x: int) -> int:
    return x + 1

@node(outputs="result")
def path_b(x: int) -> int:
    return x - 1

# Both valid:
@branch(when_true="path_a", when_false="path_b")
def gate1(x: int) -> bool: ...

@branch(when_true=path_a, when_false=path_b)  # Node objects
def gate2(x: int) -> bool: ...
```

### Mutual Exclusivity

Branch targets can produce the same output name:

```python
@branch(when_true="positive", when_false="negative")
def check_sign(x: int) -> bool:
    return x > 0

@node(outputs="label")  # Same output name
def positive(x: int) -> str:
    return "positive"

@node(outputs="label")  # Same output name - OK!
def negative(x: int) -> str:
    return "negative"

# Valid: positive and negative are mutually exclusive
```

---

## InterruptNode

### Purpose

**Declarative pause point for human-in-the-loop workflows.**

An `InterruptNode` declares where the graph should pause, what value to surface to the user, and where to write the user's response.

**Key principle:** The framework provides **plumbing**, the user provides **semantics**. The framework never dictates what prompts or responses look like - that's entirely up to your application.

### Constructor

```python
class InterruptNode:
    def __init__(
        self,
        name: str,
        input_param: str,
        response_param: str,
        *,
        response_type: type | None = None,
    ):
        """
        Create an interrupt node.
        
        Args:
            name: Unique identifier for this interrupt (stable across refactors).
            input_param: Parameter name containing value to show user (the "prompt").
            response_param: Parameter name where user's response will be written.
            response_type: Optional type for validating response.
        
        Example:
            approval = InterruptNode(
                name="human_review",
                input_param="draft_content",
                response_param="approval_decision",
                response_type=ApprovalDecision,
            )
        """
        self.name = name
        self.input_param = input_param
        self.response_param = response_param
        self.response_type = response_type
```

### Why Separate name, input_param, and response_param?

| Field | Purpose | Example |
|-------|---------|---------|
| `name` | **Identifies the interrupt point** (for handlers, events) | `"approval"`, `"human_review"` |
| `input_param` | **Which state value to show user** | `"approval_prompt"` (the prompt object) |
| `response_param` | **Where to write user's response** | `"user_decision"` (feeds downstream nodes) |

The interrupt `name` is stable across refactors - you can rename parameters without breaking handler registration or checkpoint compatibility.

```python
# Handler registered by name, not parameter
async def handle_approval(prompt: ApprovalPrompt) -> ApprovalResponse:
    return await external_service.check(prompt)

# Even if you rename input_param="approval_prompt" → "prompt_data"
# The name="approval" stays the same, handlers still work
```

### Defining Prompt and Response Types

**The framework doesn't care about the structure of prompts or responses.** You define them however makes sense for your application.

```python
from dataclasses import dataclass
from pydantic import BaseModel

# Option 1: Simple dataclasses
@dataclass
class ApprovalPrompt:
    """Prompt for approve/edit/reject workflow"""
    message: str
    draft: str
    options: list[str] = None
    
    def __post_init__(self):
        if self.options is None:
            self.options = ["approve", "edit", "reject"]

@dataclass 
class ApprovalResponse:
    """User's response to an approval prompt"""
    choice: str  # "approve", "edit", or "reject"
    feedback: str | None = None
    edited_content: str | None = None


# Option 2: Pydantic models (for validation)
class TopicPrompt(BaseModel):
    """Prompt for selecting a topic"""
    message: str
    options: list[str]
    allow_custom: bool = False

class TopicResponse(BaseModel):
    """User's topic selection"""
    selected: str
    is_custom: bool = False


# Option 3: Simple strings (for basic cases)
# prompt: str = "What would you like to do next?"
# response: str = "Continue with option A"
```

### Complete Example

```python
from hypernodes import Graph, node, route, InterruptNode, END, AsyncRunner

# Step 1: Create node that produces the prompt
@node(outputs="approval_prompt")
def create_approval_prompt(draft: str) -> ApprovalPrompt:
    """Regular node that creates a prompt object."""
    return ApprovalPrompt(
        message="Please review this draft. How would you like to proceed?",
        draft=draft,
    )

# Step 2: Declare the interrupt point
approval_interrupt = InterruptNode(
    name="approval",
    input_param="approval_prompt",     # Read the prompt from this parameter
    response_param="user_decision",    # Write response to this parameter
    response_type=ApprovalResponse,    # Optional: validate response
)

# Step 3: Use a routing node to handle the response
@route(targets=["finalize", "apply_edit", END])
def route_decision(user_decision: ApprovalResponse) -> str:
    """Route based on user's choice."""
    if user_decision.choice == "approve":
        return "finalize"
    elif user_decision.choice == "edit":
        return "apply_edit"
    else:
        return END

@node(outputs="final_content")
def finalize(draft: str) -> str:
    """Finalize the approved draft."""
    return f"✅ APPROVED\n\n{draft}"

@node(outputs="final_content")
def apply_edit(user_decision: ApprovalResponse) -> str:
    """Apply user's edited content."""
    return f"✏️ EDITED\n\n{user_decision.edited_content}"

# Build the graph
graph = Graph(
    nodes=[
        create_approval_prompt, 
        approval_interrupt, 
        route_decision,
        finalize,
        apply_edit,
    ],
)

# Execute
runner = AsyncRunner()
result = await runner.run(graph, inputs={"draft": "Initial content..."})

# If interrupted, result contains checkpoint
if result.interrupted:
    # Show the prompt to user
    prompt = result.interrupt_value  # The ApprovalPrompt object
    print(prompt.message)
    print(prompt.draft)
    
    # Get user's decision (via UI, CLI, etc.)
    user_response = ApprovalResponse(
        choice="approve",
        feedback="Looks good!"
    )
    
    # Resume execution
    result = await runner.run(
        graph,
        inputs={"user_decision": user_response},
        checkpoint=result.checkpoint,
    )

print(result.outputs["final_content"])
```

### Event Streaming with Interrupts

```python
async with runner.iter(graph, inputs={...}) as run:
    async for event in run:
        match event:
            case NodeEndEvent(node_name=name, outputs=outputs):
                print(f"{name} → {outputs}")
            
            case StreamingChunkEvent(chunk=chunk):
                print(chunk, end="")
            
            case InterruptEvent(interrupt_name=name, value=prompt):
                print(f"Paused at: {name}")
                # Show prompt to user, get response
                response = await get_user_input(prompt)
                # Resume by providing response
                # (implementation depends on your application architecture)
                break

# Access final result
print(run.result.outputs)
```

### Requirements

- **AsyncRunner only** - InterruptNode requires async execution
- **Checkpoint persistence** - State must be serializable
- **Clear prompt/response contract** - Framework provides plumbing, user defines types
- **RunResult vs dict** - AsyncRunner returns `RunResult` object (with `interrupted`, `checkpoint` fields)

---

## Common Patterns

### Accumulator

```python
@node(outputs="messages")
def add_message(messages: list, new_message: dict) -> list:
    return messages + [new_message]

# Note: returns NEW list, doesn't mutate
# Sole producer rule prevents infinite loop
```

### Conditional Processing

```python
@branch(when_true="expensive_path", when_false="cheap_path")
def should_use_expensive(data: dict) -> bool:
    return data.get("quality_required", False)
```

### Multi-Step Pipeline

```python
@node(outputs="cleaned")
def clean(raw: str) -> str: ...

@node(outputs="embedded")
def embed(cleaned: str) -> list[float]: ...

@node(outputs="result")
def classify(embedded: list[float]) -> str: ...

# Edges inferred: raw → clean → embed → classify
```

### Cycle with Termination

```python
@node(outputs="draft")
def generate(prompt: str, feedback: str | None = None) -> str: ...

@node(outputs=("score", "feedback"))
def evaluate(draft: str) -> tuple[float, str]: ...

@route(targets=["generate", END])
def quality_gate(score: float, threshold: float = 0.9) -> str:
    return END if score >= threshold else "generate"
```
