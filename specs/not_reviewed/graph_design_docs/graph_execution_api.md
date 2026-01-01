# Hypernodes Graph Execution API

This document describes the execution API for Hypernodes, a reactive dataflow graph framework. It covers how to run graphs, handle human-in-the-loop interactions, and manage long-running workflows.

---

## Part 1: Human-in-the-Loop Interactions

Hypernodes supports **interrupts** - pause points where the graph surfaces a value to the user and waits for a response before continuing. This enables human-in-the-loop workflows like approvals, edits, topic selection, and multi-turn conversations.

### Design Philosophy

The framework provides the **plumbing**, the user provides the **semantics**:

- **Framework's job**: Pause execution, surface a value, know where to put the response
- **User's job**: Define what the prompt looks like, what the response looks like, how to render UI

The framework never dictates the structure of prompts or responses - that's entirely up to the application.

### Core Types

```python
from dataclasses import dataclass
from typing import Any, TypeVar, Generic

T = TypeVar('T')
R = TypeVar('R')

@dataclass
class Interrupt(Generic[T, R]):
    """
    Represents a paused graph waiting for user input.
    
    Attributes:
        value: The prompt object (user-defined type). Could be a string,
               a Pydantic model, a dataclass - whatever the app needs.
        response_param: The parameter name where the response will be written.
                        This also serves as the interrupt's name/identifier.
        response_type: Optional type for validating the response.
    """
    value: T
    response_param: str              # Both the name AND where to write
    response_type: type[R] | None = None
```

### The InterruptNode

An `InterruptNode` declares a pause point in the graph. It reads a value (the prompt) and writes the response to a specified output.

```python
@dataclass
class InterruptNode:
    """
    Declares a pause point in the graph.
    
    - Reads from `input_param` (the parameter containing the value to surface)
    - Writes to `response_param` (the parameter for the user's response)
    - The `response_param` field also serves as the interrupt's name/identifier
    
    This explicit naming makes graph wiring clear for visualization and validation.
    """
    input_param: str                 # Input: parameter name containing the prompt value
    response_param: str              # Output: parameter name for response (also the interrupt name)
    response_type: type | None = None  # Optional: validate response type
    
    @property
    def name(self) -> str:
        """The interrupt name is the response parameter."""
        return self.response_param
```
### Defining Prompts and Responses

The application defines its own prompt and response types. The framework doesn't care about their structure:

```python
from dataclasses import dataclass
from pydantic import BaseModel

# ============ Application-Defined Types ============

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

### Creating Interrupt Points in a Graph

```python
from hypernodes import node, Graph, InterruptNode

# Step 1: Create a node that produces the prompt
@node(output_name="approval_prompt")
def create_approval_prompt(draft: str) -> ApprovalPrompt:
    """Regular node that creates a prompt object."""
    return ApprovalPrompt(
        message="Please review this draft. How would you like to proceed?",
        draft=draft,
    )

# Step 2: Declare the interrupt point
approval_interrupt = InterruptNode(
    input_param="approval_prompt",     # Read the prompt from this parameter
    response_param="user_decision",    # Write response to this parameter (also the interrupt name)
    response_type=ApprovalResponse,    # Optional: validate response
)

# Step 3: Use a gate to route based on the response
# Gates are preferred over inline conditionals for control flow (see graph_implementation_guide.md)

from typing import Literal
from hypernodes import gate, END

# String-based targets with optional descriptions (tuple format)
ApprovalRoute = Literal[
    ("finalize", "Approve and publish"),
    ("apply_edit", "Apply user's changes"),
    END,
]

@gate
def route_decision(user_decision: ApprovalResponse) -> ApprovalRoute:
    """Route based on user's choice."""
    if user_decision.choice == "approve":
        return "finalize"
    elif user_decision.choice == "edit":
        return "apply_edit"
    else:
        return END

@node(output_name="final_content")
def finalize(draft: str) -> str:
    """Finalize the approved draft."""
    return f"✅ APPROVED\n\n{draft}"

@node(output_name="final_content")
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
```

### Handling Interrupts: The Handler Pattern

Instead of using `match` statements to dispatch on interrupt types, register handlers by name. The `response` field serves as both the interrupt's identity and the response target.

#### Option 1: Decorator-Based Registration (Recommended)

Handlers are validated at registration time - if you register a handler for an interrupt that doesn't exist in the graph, you get an immediate error.

```python
from hypernodes import GraphRunner

runner = GraphRunner(graph)

# ✅ Valid: "user_decision" exists in the graph
@runner.on_interrupt("user_decision")
async def handle_approval(prompt: ApprovalPrompt) -> ApprovalResponse:
    """
    Handle approval interrupts.
    
    Args:
        prompt: The ApprovalPrompt produced by the graph
        
    Returns:
        ApprovalResponse to resume the graph with
    """
    print(f"Message: {prompt.message}")
    print(f"Draft: {prompt.draft}")
    
    choice = await get_user_choice(prompt.options)
    feedback = await get_optional_feedback()
    
    return ApprovalResponse(choice=choice, feedback=feedback)


# ❌ Error at registration time: "unknown_interrupt" doesn't exist
# @runner.on_interrupt("unknown_interrupt")  
# ValueError: Unknown interrupt 'unknown_interrupt'. Available: user_decision, selected_topic


@runner.on_interrupt("selected_topic")
async def handle_topic_selection(prompt: TopicPrompt) -> TopicResponse:
    """Handle topic selection interrupts."""
    print(f"Message: {prompt.message}")
    selected = await get_user_choice(prompt.options)
    return TopicResponse(selected=selected)


# Run - handlers are invoked automatically
result = await runner.run(inputs={"topic": "AI safety", "llm": my_llm})
```

**Implementation of validation:**

```python
class GraphRunner:
    def __init__(self, graph: Graph):
        self.graph = graph
        self._handlers: dict[str, Callable] = {}
        
        # Extract all interrupt names from the graph at init time
        self._interrupt_names: frozenset[str] = frozenset(
            node.response_param for node in graph.nodes 
            if isinstance(node, InterruptNode)
        )
    
    def on_interrupt(
        self, 
        name: str,
    ) -> Callable[[Callable[[T], R]], Callable[[T], R]]:
        """
        Register a handler for an interrupt.
        
        Raises:
            ValueError: If `name` doesn't match any InterruptNode in the graph.
        """
        if name not in self._interrupt_names:
            available = ", ".join(sorted(self._interrupt_names))
            raise ValueError(
                f"Unknown interrupt '{name}'. "
                f"Available interrupts: {available}"
            )
        
        def decorator(fn: Callable[[T], R]) -> Callable[[T], R]:
            self._handlers[name] = fn
            return fn
        
        return decorator
    
    async def run(self, inputs: dict[str, Any], **kwargs) -> GraphResult:
        """Run with automatic interrupt handling."""
        result = await self.graph.run(inputs=inputs, **kwargs)
        
        while result.is_interrupted:
            handler = self._handlers.get(result.interrupt.response_param)
            if handler is None:
                # No handler registered - return interrupted result
                return result
            
            response = await handler(result.interrupt.value)
            result = await self.graph.run(
                inputs={result.interrupt.response_param: response},
                checkpoint=result.checkpoint,
            )
        
        return result
```

#### Option 2: Handler Dictionary

For simpler cases or when you want more explicit control:

```python
from hypernodes import Graph, GraphResult

# Define handlers as a dictionary
handlers: dict[str, Callable] = {
    "user_decision": handle_approval_interaction,
    "selected_topic": handle_topic_selection,
}

async def run_with_interactions(
    graph: Graph,
    inputs: dict[str, Any],
    handlers: dict[str, Callable],
) -> GraphResult:
    """Run a graph, handling any interrupts via registered handlers."""
    
    result = await graph.run(inputs=inputs)
    
    while result.is_interrupted:
        interrupt = result.interrupt
        handler = handlers.get(interrupt.response_param)
        
        if handler is None:
            raise ValueError(f"No handler for interrupt: {interrupt.response_param}")
        
        # Call the handler with the prompt value
        response = await handler(interrupt.value)
        
        # Resume with the response
        result = await graph.run(
            inputs={interrupt.response_param: response},
            checkpoint=result.checkpoint,
        )
    
    return result
```

#### Option 3: Streaming with iter() (Like pydantic-graph)

For maximum flexibility and streaming support, use `iter()` to handle each event:

```python
async def run_with_streaming(inputs: dict[str, Any]) -> GraphResult:
    """Run with streaming events and inline interrupt handling."""
    
    async with graph.iter(inputs=inputs) as run:
        async for event in run:
            match event:
                case NodeStartEvent(node=name, iteration=i):
                    print(f"[{i}] Starting: {name}")
                    
                case NodeCompleteEvent(node=name, outputs=out):
                    print(f"✅ {name}")
                    # Stream partial outputs to UI as they complete
                    await stream_to_ui(out)
                    
                case InterruptEvent() as interrupt:
                    # Handle interrupt inline based on the response_param name
                    print(f"🛑 Interrupt: {interrupt.response_param}")
                    
                    if interrupt.response_param == "user_decision":
                        prompt: ApprovalPrompt = interrupt.value
                        response = await show_approval_dialog(prompt)
                        
                    elif interrupt.response_param == "selected_topic":
                        prompt: TopicPrompt = interrupt.value
                        response = await show_topic_picker(prompt)
                        
                    else:
                        raise ValueError(f"Unknown interrupt: {interrupt.response_param}")
                    
                    # Resume execution
                    run.respond(interrupt.response_param, response)
                    
                case GateDecisionEvent(gate=g, decision=d):
                    print(f"🚦 {g} -> {d}")
        
        return run.result
```

**Key differences between options:**

| Aspect | Option 1 (Decorator) | Option 2 (Dict) | Option 3 (iter) |
|--------|---------------------|-----------------|-----------------|
| Validation | At registration | At runtime | At runtime |
| Streaming | No | No | Yes |
| Control | Automatic | Semi-manual | Full manual |
| Best for | Most cases | Simple scripts | Streaming UIs |

### Multiple Interrupts in a Graph

A graph can have multiple interrupt points. Each has a unique `response` name:

```python
# First interrupt: choose a topic
topic_interrupt = InterruptNode(
    input_param="topic_prompt",
    response_param="selected_topic",
    response_type=TopicResponse,
)

# Second interrupt: approve the draft
approval_interrupt = InterruptNode(
    input_param="approval_prompt", 
    response_param="user_decision",
    response_type=ApprovalResponse,
)

# Third interrupt: confirm publish
publish_interrupt = InterruptNode(
    input_param="publish_prompt",
    response_param="publish_confirmed",
    response_type=bool,
)
```

The graph pauses at each interrupt in sequence (or based on dataflow), and the appropriate handler is invoked each time.

### Extracting the Interrupt for External Handling

Sometimes the handler lives outside the process (e.g., a web UI). Extract the interrupt data, serialize it, and resume later:

```python
# In your API endpoint
result = await graph.run(inputs=request.inputs)

if result.is_interrupted:
    # Save state for later
    await save_checkpoint(
        conversation_id=request.conversation_id,
        checkpoint=result.checkpoint,
    )
    
    # Return interrupt info to client
    return InterruptResponse(
        interrupt_name=result.interrupt.response_param,
        prompt=result.interrupt.value,  # Serialized to JSON
        conversation_id=request.conversation_id,
    )

# ... later, when user responds ...

# In your resume endpoint
checkpoint = await load_checkpoint(request.conversation_id)

result = await graph.run(
    inputs={request.interrupt_name: request.response},
    checkpoint=checkpoint,
)
```

### Why This Design?

1. **No Framework-Imposed Structure**: Apps define their own prompt/response types
2. **Name-Based Dispatch**: The `response_param` field is both identifier and target - no extra discriminator needed
3. **Explicit Wiring**: Using `input_param` and `response_param` makes graph dependencies clear for visualization and validation
4. **Type Safety**: Handlers can be typed with specific prompt/response types
5. **Handler Registration**: Declare handlers once, framework invokes them - no manual dispatch code
6. **Flexibility**: Works with decorators, dicts, or manual handling
7. **Serializable**: Prompts/responses are user-defined, so apps control serialization
8. **Gate-Based Routing**: Control flow decisions use gates (see graph_implementation_guide.md), keeping nodes pure

---

## Part 2: Execution API

### The GraphResult Type

Every graph execution returns a `GraphResult`. There's a single type with an explicit status - no unions to match on:

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4
import time

class RunStatus(Enum):
    """Status of a graph run."""
    PENDING = "pending"         # Initialized but not started
    RUNNING = "running"         # Currently executing
    INTERRUPTED = "interrupted" # Paused, waiting for user input
    COMPLETE = "complete"       # Finished successfully
    ERROR = "error"             # Failed with an exception

@dataclass
class GraphResult:
    """
    Result of a graph execution.
    
    Always this type - check .status or use .is_complete/.is_interrupted
    to determine the state.
    """
    status: RunStatus
    outputs: dict[str, Any]
    
    # Identifiers
    run_id: str = field(default_factory=lambda: uuid4().hex[:12])
    thread_id: str | None = None  # User-provided, for conversation tracking
    
    # Present when status == INTERRUPTED
    interrupt: Interrupt | None = None
    
    # Present when status == ERROR
    error: GraphError | None = None
    
    # For resuming (always available, enables resume even after errors)
    checkpoint: Checkpoint | None = None
    
    @property
    def is_complete(self) -> bool:
        """Graph finished - all outputs available."""
        return self.status == RunStatus.COMPLETE
    
    @property
    def is_interrupted(self) -> bool:
        """Graph paused - interrupt data available."""
        return self.status == RunStatus.INTERRUPTED
    
    @property
    def is_error(self) -> bool:
        """Graph failed - error data available."""
        return self.status == RunStatus.ERROR


@dataclass
class GraphError:
    """Details about a graph execution error."""
    message: str
    node: str | None = None           # Which node failed (if applicable)
    exception_type: str | None = None # e.g., "ValueError"
    traceback: str | None = None      # Full traceback string
```

### Run ID vs Thread ID

Two different identifiers serve different purposes:

| Identifier | Generated By | Purpose | Scope |
|------------|--------------|---------|-------|
| `run_id` | Framework (auto) | Unique execution identifier | Single run() call |
| `thread_id` | User (optional) | Conversation/session tracking | Multiple runs |

```python
# First run in a conversation
result1 = await graph.run(
    inputs={"query": "Hello"},
    thread_id="conv_abc123",  # Your conversation ID
)
print(result1.run_id)      # "7f3a2b1c" (auto-generated)
print(result1.thread_id)   # "conv_abc123"

# Second run in same conversation (after interrupt)
result2 = await graph.run(
    inputs={"user_decision": response},
    thread_id="conv_abc123",
    checkpoint=result1.checkpoint,
)
print(result2.run_id)      # "9d4e5f6a" (new run, new ID)
print(result2.thread_id)   # "conv_abc123" (same conversation)
```

### Checkpoint vs History

**They serve different purposes:**

| Aspect | Checkpoint | History |
|--------|------------|---------|
| **Purpose** | Resume execution | Audit/debugging |
| **Contains** | Current state + metadata | What ran, when, with what |
| **Required for** | Resuming interrupted graphs | Understanding execution flow |
| **Includes streaming** | Yes - partial outputs preserved | No - only completed executions |

#### Checkpoint Structure

```python
@dataclass
class Checkpoint:
    """
    Serializable snapshot for resuming a graph.
    
    The structure is exposed (not opaque) so users can:
    - Inspect state for debugging
    - Store in any backend (Redis, Postgres, file, etc.)
    - Manipulate if needed (advanced use cases)
    """
    # Current values (including partial/streaming outputs)
    values: dict[str, Any]
    
    # Version tracking for staleness detection
    versions: dict[str, int]
    
    # What has executed (minimal - just enough to not re-run)
    executed: dict[str, dict[str, int]]  # node -> {param: version_used}
    
    # Active routing state
    active_gates: set[str] = field(default_factory=set)
    
    # Streaming state (partial outputs still accumulating)
    streaming: dict[str, StreamingState] | None = None
    
    # Metadata
    run_id: str | None = None
    thread_id: str | None = None
    created_at: float = field(default_factory=time.time)
    
    def to_json(self) -> str:
        """Serialize to JSON string."""
        ...
    
    def to_bytes(self) -> bytes:
        """Serialize to bytes (e.g., for Redis)."""
        ...
    
    @classmethod
    def from_json(cls, data: str) -> "Checkpoint":
        """Deserialize from JSON string."""
        ...


@dataclass
class StreamingState:
    """State of a streaming output (for partial results)."""
    partial_value: Any        # What we have so far
    is_complete: bool = False # Whether streaming finished
```

#### Execution History (Optional)

History is an optional audit log - useful for debugging but not required for resumption:

```python
@dataclass
class NodeExecution:
    """Record of a single node execution."""
    node: str                           # Node name
    inputs_used: dict[str, int]         # param -> version consumed
    outputs_produced: tuple[str, ...]   # Which outputs were written
    iteration: int = 1                  # Which iteration (for cycles)
    duration_ms: float | None = None    # How long it took
    timestamp: float = field(default_factory=time.time)

# Request history explicitly
result = await graph.run(
    inputs={...},
    include_history=True,  # Default: False (for performance)
)

if result.history:
    for execution in result.history:
        print(f"{execution.node} ran at iteration {execution.iteration}")
```

### The Graph.run() Method

The primary execution method. Async-first design (sync wrapper available):

```python
class Graph:
    async def run(
        self,
        inputs: dict[str, Any],
        *,
        checkpoint: Checkpoint | None = None,
        thread_id: str | None = None,
        callbacks: list[Callback] | None = None,
        include_history: bool = False,
    ) -> GraphResult:
        """
        Run the graph to completion or interruption.
        
        Args:
            inputs: Values to inject into the graph. Keys are output names.
            checkpoint: Optional checkpoint to resume from.
            thread_id: Optional identifier for conversation/session tracking.
            callbacks: Optional callbacks for observability.
            include_history: Whether to include execution history in result.
            
        Returns:
            GraphResult with status COMPLETE, INTERRUPTED, or ERROR.
        """
        ...
    
    def run_sync(
        self,
        inputs: dict[str, Any],
        **kwargs,
    ) -> GraphResult:
        """Synchronous wrapper around run()."""
        return asyncio.run(self.run(inputs, **kwargs))
```

### Basic Usage

```python
from hypernodes import Graph

# Simple run - no interrupts expected
result = await graph.run(
    inputs={"topic": "machine learning", "llm": my_llm_client},
    thread_id="session_123",
)

if result.is_complete:
    print(f"Run {result.run_id} complete!")
    print(result.outputs["final_article"])
elif result.is_error:
    print(f"Run {result.run_id} failed: {result.error.message}")
else:
    # Unexpected interrupt
    raise RuntimeError(f"Unexpected interrupt: {result.interrupt.response_param}")
```

### Resuming from Interrupts

```python
# Initial run
result = await graph.run(
    inputs={"topic": "AI safety", "llm": llm},
    thread_id="conv_abc",
)

if result.is_interrupted:
    # Graph paused - get user input
    prompt = result.interrupt.value
    response = await get_user_response(prompt)
    
    # Resume with the response (same thread_id, new run_id)
    result = await graph.run(
        inputs={result.interrupt.response_param: response},
        checkpoint=result.checkpoint,
        thread_id="conv_abc",
    )

# Now complete
assert result.is_complete
print(result.outputs["final_content"])
```

### The Graph.iter() Method

For full control over execution - streaming, custom event handling, in-process interaction:

```python
class Graph:
    def iter(
        self,
        inputs: dict[str, Any],
        *,
        checkpoint: Checkpoint | None = None,
        thread_id: str | None = None,
    ) -> AsyncContextManager[GraphRun]:
        """
        Iterate over graph execution events.
        
        Use this when you need:
        - Streaming output as nodes complete
        - Custom handling of each event
        - In-process interaction (no serialization needed)
        - Fine-grained control over execution
        
        Returns:
            Context manager yielding a GraphRun for iteration.
        """
        ...


class GraphRun:
    """Handle for an in-progress graph execution."""
    
    # Identifiers
    run_id: str
    thread_id: str | None
    
    def __aiter__(self) -> AsyncIterator[GraphEvent]:
        """Iterate over execution events."""
        ...
    
    def respond(self, name: str, value: Any) -> None:
        """
        Provide a value to resume from an interrupt.
        
        Args:
            name: The interrupt name (same as interrupt.response_param)
            value: The response value
            
        Call this after receiving an InterruptEvent.
        """
        ...
    
    def checkpoint(self) -> Checkpoint:
        """Get a checkpoint of current state."""
        ...
    
    @property
    def current_outputs(self) -> dict[str, Any]:
        """Current output values (may be partial if streaming)."""
        ...
    
    @property
    def result(self) -> GraphResult | None:
        """Final result, or None if still running."""
        ...
```

### Event Types

```python
from dataclasses import dataclass
from typing import Any

@dataclass
class NodeStartEvent:
    """A node is about to execute."""
    node: str
    inputs: dict[str, Any]
    iteration: int  # Which iteration (for cycles)

@dataclass
class NodeCompleteEvent:
    """A node finished executing."""
    node: str
    outputs: dict[str, Any]
    iteration: int

@dataclass
class InterruptEvent:
    """Graph is pausing for user input."""
    value: Any                # The prompt to surface
    response_param: str       # Where to put the response (also the interrupt name)
    response_type: type | None
    node_name: str            # Which InterruptNode triggered this

@dataclass
class GateDecisionEvent:
    """A gate made a routing decision."""
    gate: str
    decision: str | list[str]  # Which branch(es) activated

@dataclass
class StreamingEvent:
    """Partial output from a streaming node."""
    node: str
    output: str               # Which output is streaming
    chunk: Any                # The chunk of data

@dataclass
class ErrorEvent:
    """An error occurred during execution."""
    node: str | None
    error: GraphError

# Union of all event types
GraphEvent = (
    NodeStartEvent | NodeCompleteEvent | InterruptEvent | 
    GateDecisionEvent | StreamingEvent | ErrorEvent
)
```

### Streaming Execution with iter()

```python
async with graph.iter(inputs={"topic": "quantum computing", "llm": llm}) as run:
    async for event in run:
        match event:
            case NodeStartEvent(node=name, iteration=i):
                if i > 1:
                    print(f"🔄 Iteration {i}: {name}")
                else:
                    print(f"▶️  {name}")
                
            case NodeCompleteEvent(node=name, outputs=out):
                print(f"✅ {name}")
                
            case StreamingEvent(node=name, output=out, chunk=chunk):
                # Stream partial results to UI
                await ui.append_chunk(out, chunk)
                
            case InterruptEvent(value=prompt, response_param=target):
                print(f"🛑 Interrupt: {target}")
                response = await get_user_input(prompt)
                run.respond(target, response)
                print(f"▶️  Resuming...")
                
            case GateDecisionEvent(gate=g, decision=d):
                print(f"🚦 {g} -> {d}")
                
            case ErrorEvent(node=n, error=e):
                print(f"❌ Error in {n}: {e.message}")
    
    # Iteration complete
    if run.result.is_complete:
        print(f"✨ Done: {list(run.result.outputs.keys())}")
```

### Callbacks for Observability

Callbacks are fire-and-forget hooks for logging, metrics, and tracing. They don't affect execution:

```python
from typing import Protocol

class Callback(Protocol):
    """Protocol for execution callbacks."""
    
    def on_event(self, event: GraphEvent) -> None:
        """Called for each execution event."""
        ...


class LoggingCallback:
    """Example: Log all events."""
    
    def on_event(self, event: GraphEvent) -> None:
        match event:
            case NodeStartEvent(node=n):
                logger.info(f"Node starting: {n}")
            case NodeCompleteEvent(node=n):
                logger.info(f"Node complete: {n}")
            case InterruptEvent(response_param=r):
                logger.info(f"Interrupt: {r}")


class MetricsCallback:
    """Example: Track execution metrics."""
    
    def __init__(self):
        self.node_count = 0
        self.start_time = None
    
    def on_event(self, event: GraphEvent) -> None:
        if isinstance(event, NodeStartEvent):
            if self.start_time is None:
                self.start_time = time.time()
        elif isinstance(event, NodeCompleteEvent):
            self.node_count += 1


# Use callbacks with run()
result = await graph.run(
    inputs={...},
    callbacks=[LoggingCallback(), MetricsCallback()],
)
```

### run() vs iter(): When to Use Each

| Use Case | Method | Why |
|----------|--------|-----|
| Simple execution | `run()` | Clean, returns final result |
| Background job | `run()` | No streaming needed |
| Interrupt → serialize → resume later | `run()` | Checkpoint serialization |
| Streaming UI | `iter()` | Real-time updates |
| In-process interaction | `iter()` | No serialization overhead |
| Debugging execution | `iter()` | See every event |
| Custom event handling | `iter()` | Full control |

Note: `run()` is essentially `iter()` with a default event handler that stops on interrupts.

---

## Part 3: Complete End-to-End Example

Here's a full example: a content generation workflow with topic selection and approval steps.

### Define the Graph

```python
from dataclasses import dataclass
from hypernodes import node, branch, Graph, InterruptNode

# ============ Types ============

@dataclass
class TopicPrompt:
    message: str
    suggestions: list[str]

@dataclass
class TopicSelection:
    topic: str
    
@dataclass
class ApprovalPrompt:
    message: str
    draft: str
    
@dataclass
class ApprovalDecision:
    approved: bool
    feedback: str | None = None

# ============ Nodes ============

@node(output_name="topic_prompt")
def create_topic_prompt(domain: str) -> TopicPrompt:
    """Create a prompt for topic selection."""
    return TopicPrompt(
        message=f"What aspect of {domain} would you like to explore?",
        suggestions=[
            f"Latest advances in {domain}",
            f"Beginner's guide to {domain}",
            f"Common misconceptions about {domain}",
        ],
    )

# First interrupt: topic selection
topic_interrupt = InterruptNode(
    input_param="topic_prompt",
    response_param="topic_selection",
    response_type=TopicSelection,
)

@node(output_name="draft")
async def generate_draft(topic_selection: TopicSelection, llm: Any) -> str:
    """Generate a draft article using the LLM."""
    response = await llm.generate(
        prompt=f"Write a short article about: {topic_selection.topic}"
    )
    return response.text

@node(output_name="approval_prompt")
def create_approval_prompt(draft: str) -> ApprovalPrompt:
    """Create a prompt for draft approval."""
    return ApprovalPrompt(
        message="Please review this draft. Approve or provide feedback.",
        draft=draft,
    )

# Second interrupt: approval
approval_interrupt = InterruptNode(
    input_param="approval_prompt",
    response_param="approval_decision",
    response_type=ApprovalDecision,
)

@branch(when_true="finalize", when_false="revise")
def route_approval(approval_decision: ApprovalDecision) -> bool:
    return approval_decision.approved

@node(output_name="final_article")
def finalize(draft: str) -> str:
    """Finalize the approved draft."""
    return f"✨ PUBLISHED ✨\n\n{draft}"

@node(output_name="draft")  # Overwrites draft, creating a cycle
async def revise(
    draft: str, 
    approval_decision: ApprovalDecision, 
    llm: Any,
) -> str:
    """Revise the draft based on feedback."""
    response = await llm.generate(
        prompt=f"Revise this draft based on feedback.\n\n"
               f"Draft: {draft}\n\n"
               f"Feedback: {approval_decision.feedback}"
    )
    return response.text

# ============ Build Graph ============

content_graph = Graph(
    nodes=[
        create_topic_prompt,
        topic_interrupt,
        generate_draft,
        create_approval_prompt,
        approval_interrupt,
        route_approval,
        finalize,
        revise,
    ],
)
```

### Run with Handler Registration

```python
from hypernodes import GraphRunner

runner = GraphRunner(content_graph)

@runner.on_interrupt("topic_selection")
async def handle_topic(prompt: TopicPrompt) -> TopicSelection:
    """Handle topic selection interrupt."""
    print(f"\n{prompt.message}\n")
    for i, suggestion in enumerate(prompt.suggestions, 1):
        print(f"  {i}. {suggestion}")
    print(f"  {len(prompt.suggestions) + 1}. Custom topic")
    
    choice = int(input("\nYour choice: "))
    
    if choice <= len(prompt.suggestions):
        topic = prompt.suggestions[choice - 1]
    else:
        topic = input("Enter your topic: ")
    
    return TopicSelection(topic=topic)


@runner.on_interrupt("approval_decision")
async def handle_approval(prompt: ApprovalPrompt) -> ApprovalDecision:
    """Handle approval interrupt."""
    print(f"\n{prompt.message}\n")
    print("-" * 40)
    print(prompt.draft)
    print("-" * 40)
    
    approved = input("\nApprove? (y/n): ").lower() == "y"
    
    if not approved:
        feedback = input("Feedback for revision: ")
        return ApprovalDecision(approved=False, feedback=feedback)
    
    return ApprovalDecision(approved=True)


async def main():
    # Create a mock LLM client
    llm = MockLLM()
    
    # Run the workflow
    result = await runner.run(inputs={
        "domain": "artificial intelligence",
        "llm": llm,
    })
    
    print("\n" + "=" * 50)
    print("WORKFLOW COMPLETE")
    print("=" * 50)
    print(result.outputs["final_article"])


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

### Run with Web API (Serialize/Resume)

```python
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import json

app = FastAPI()

# In-memory store (use Redis/Postgres in production)
checkpoints: dict[str, str] = {}
pending_interrupts: dict[str, dict] = {}

class StartRequest(BaseModel):
    conversation_id: str
    domain: str

class ResumeRequest(BaseModel):
    conversation_id: str
    interrupt_name: str
    response: dict

class WorkflowResponse(BaseModel):
    conversation_id: str
    status: str  # "interrupted" or "complete"
    interrupt: dict | None = None
    result: dict | None = None


@app.post("/workflow/start")
async def start_workflow(req: StartRequest) -> WorkflowResponse:
    """Start a new content generation workflow."""
    
    result = await content_graph.run(
        inputs={"domain": req.domain, "llm": get_llm_client()},
        thread_id=req.conversation_id,
    )
    
    if result.is_interrupted:
        # Save checkpoint
        checkpoints[req.conversation_id] = result.checkpoint.to_json()
        
        # Return interrupt info
        return WorkflowResponse(
            conversation_id=req.conversation_id,
            status="interrupted",
            interrupt={
                "name": result.interrupt.response_param,
                "prompt": serialize_prompt(result.interrupt.value),
            },
        )
    
    if result.is_error:
        return WorkflowResponse(
            conversation_id=req.conversation_id,
            status="error",
            result={"error": result.error.message},
        )
    
    return WorkflowResponse(
        conversation_id=req.conversation_id,
        status="complete",
        result=result.outputs,
    )


@app.post("/workflow/resume")
async def resume_workflow(req: ResumeRequest) -> WorkflowResponse:
    """Resume a workflow with user's response."""
    
    # Load checkpoint
    checkpoint_json = checkpoints.get(req.conversation_id)
    if not checkpoint_json:
        raise HTTPException(404, "Conversation not found")
    
    checkpoint = Checkpoint.from_json(checkpoint_json)
    
    # Parse response based on interrupt type
    response = parse_response(req.interrupt_name, req.response)
    
    # Resume the graph
    result = await content_graph.run(
        inputs={req.interrupt_name: response},
        checkpoint=checkpoint,
        thread_id=req.conversation_id,
    )
    
    if result.is_interrupted:
        # Save updated checkpoint
        checkpoints[req.conversation_id] = result.checkpoint.to_json()
        
        return WorkflowResponse(
            conversation_id=req.conversation_id,
            status="interrupted",
            interrupt={
                "name": result.interrupt.response_param,
                "prompt": serialize_prompt(result.interrupt.value),
            },
        )
    
    # Workflow complete - clean up
    del checkpoints[req.conversation_id]
    
    return WorkflowResponse(
        conversation_id=req.conversation_id,
        status="complete",
        result=result.outputs,
    )


def serialize_prompt(prompt: Any) -> dict:
    """Serialize prompt to JSON-compatible dict."""
    if hasattr(prompt, "__dict__"):
        return {"type": type(prompt).__name__, **prompt.__dict__}
    return {"value": prompt}


def parse_response(interrupt_name: str, data: dict) -> Any:
    """Parse response based on interrupt type."""
    if interrupt_name == "topic_selection":
        return TopicSelection(**data)
    elif interrupt_name == "approval_decision":
        return ApprovalDecision(**data)
    else:
        raise ValueError(f"Unknown interrupt: {interrupt_name}")
```

### Run with Streaming (iter)

```python
async def run_with_streaming():
    """Run workflow with real-time streaming output."""
    
    llm = get_llm_client()
    
    async with content_graph.iter(inputs={"domain": "robotics", "llm": llm}) as run:
        async for event in run:
            match event:
                case NodeStartEvent(node=name, iteration=i):
                    if i > 1:
                        print(f"🔄 Iteration {i}: {name}")
                    else:
                        print(f"▶️  {name}")
                
                case NodeCompleteEvent(node=name, outputs=out):
                    print(f"✅ {name}")
                
                case StreamingEvent(output=out, chunk=chunk):
                    # Stream partial results to UI
                    print(chunk, end="", flush=True)
                
                case InterruptEvent(value=prompt, response_param=target):
                    print(f"\n🛑 INTERRUPT: {target}")
                    
                    # Handle inline
                    if target == "topic_selection":
                        response = await handle_topic_cli(prompt)
                    elif target == "approval_decision":
                        response = await handle_approval_cli(prompt)
                    else:
                        raise ValueError(f"Unknown interrupt: {target}")
                    
                    # Resume
                    run.respond(target, response)
                    print(f"▶️  Resuming...\n")
                
                case GateDecisionEvent(gate=g, decision=d):
                    print(f"🚦 {g} -> {d}")
                
                case ErrorEvent(error=e):
                    print(f"❌ Error: {e.message}")
        
        # Done
        if run.result.is_complete:
            print("\n" + "=" * 50)
            print("WORKFLOW COMPLETE")
            print("=" * 50)
            print(run.result.outputs.get("final_article", "No output"))


async def handle_topic_cli(prompt: TopicPrompt) -> TopicSelection:
    print(f"\n{prompt.message}")
    for i, s in enumerate(prompt.suggestions, 1):
        print(f"  {i}. {s}")
    choice = int(input("Choice: "))
    return TopicSelection(topic=prompt.suggestions[choice - 1])


async def handle_approval_cli(prompt: ApprovalPrompt) -> ApprovalDecision:
    print(f"\n{prompt.message}")
    print(prompt.draft[:200] + "..." if len(prompt.draft) > 200 else prompt.draft)
    approved = input("Approve? (y/n): ").lower() == "y"
    feedback = None if approved else input("Feedback: ")
    return ApprovalDecision(approved=approved, feedback=feedback)
```

---

## Summary

### Key Design Decisions

1. **Single Return Type**: `GraphResult` with explicit `RunStatus` - no unions to match
2. **User-Defined Types**: Framework doesn't dictate prompt/response structure
3. **Name-Based Dispatch**: `response_param` field is both identifier and response target
4. **Explicit Wiring**: `input_param` and `response_param` make graph dependencies clear for visualization/validation
5. **Handler Registration**: Declare handlers with types, validate at registration time
6. **Structured Checkpoints**: Inspectable, includes streaming state, user controls serialization
7. **Run ID + Thread ID**: Auto-generated run ID, optional user-provided thread ID
8. **Async-First**: `run()` is async, `run_sync()` is a convenience wrapper
9. **Three Execution Modes**: `run()` for simple, `GraphRunner` for handlers, `iter()` for streaming
10. **Callbacks AND Events**: Callbacks for observability, events for control
11. **Error Handling**: Explicit `ERROR` status with `GraphError` details
12. **Gate-Based Routing**: Control flow uses gates/branches, keeping nodes pure

### The Interrupt Flow
```
┌─────────────────────────────────────────────────────────────────┐
│                        Graph Execution                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   1. Node produces prompt value                                 │
│          ↓                                                      │
│   2. InterruptNode reads prompt, pauses graph                   │
│          ↓                                                      │
│   3. GraphResult returned with:                                 │
│      - status: INTERRUPTED                                      │
│      - interrupt.value: the prompt                              │
│      - interrupt.response_param: where to put response (+ name) │
│      - checkpoint: serializable state (includes streaming)      │
│      - run_id / thread_id                                       │
│          ↓                                                      │
│   4. Handler invoked (by name) OR manual dispatch               │
│          ↓                                                      │
│   5. Response provided via:                                     │
│      - run(inputs={response_param: response}, checkpoint=...)   │
│      - OR run.respond(response_param, response) in iter()       │
│          ↓                                                      │
│   6. Graph resumes, response injected at `response_param`       │
│          ↓                                                      │
│   7. Continue until next interrupt, error, or completion        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## Part 6: Three-Layer Architecture (UI, Observability, Durability)

Hypernodes is designed to support three complementary but distinct layers without coupling to specific implementations:

| Layer | Purpose | Key Identifiers | Example Systems |
|-------|---------|-----------------|-----------------|
| **UI Protocol** | Stream events to frontend | `thread_id`, `run_id` | AG-UI, custom WebSocket |
| **Observability** | Debugging, analytics, traces | `trace_id`, `span_id`, `session_id` | Langfuse, Logfire, OpenTelemetry |
| **Durability** | Resume after crash, exactly-once | `workflow_id`, `run_id`, `step_id` | Temporal, DBOS, LangGraph checkpointers |

### Unified Identity Model

All three layers need stable correlation identifiers. Hypernodes provides a unified model:

```python
@dataclass(frozen=True)
class ExecutionIdentity:
    """Stable identifiers that flow through all layers."""
    
    thread_id: str          # Conversation/workflow identity (user-provided)
                            # → AG-UI threadId
                            # → Langfuse sessionId  
                            # → Temporal workflowId
    
    run_id: str             # Single execution/turn (auto-generated or user-provided)
                            # → AG-UI runId
                            # → Langfuse traceId
                            # → execution attempt ID
    
    parent_run_id: str | None = None  # For nested executions
                                       # → AG-UI parentRunId
                                       # → Langfuse parent trace


@dataclass
class NodeIdentity:
    """Identity for a specific node execution within a run."""
    
    node_id: str            # Node name in graph
    step_index: int         # Execution order (0, 1, 2, ...)
                            # → Langfuse span
                            # → DBOS step number
                            # → Temporal event index
    
    execution_id: str       # Unique ID for this specific execution
                            # Computed as: hash(run_id + node_id + step_index)
```

### Event System

Hypernodes emits a unified event stream that all three layers can consume:

```python
from typing import Literal, Any
from dataclasses import dataclass, field
from datetime import datetime

# ============ Base Event ============

@dataclass
class GraphEvent:
    """Base class for all graph events."""
    
    event_type: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    # Identity context (always present)
    thread_id: str = ""
    run_id: str = ""
    parent_run_id: str | None = None


# ============ Lifecycle Events ============

@dataclass
class RunStartEvent(GraphEvent):
    """Emitted when a graph run begins."""
    event_type: Literal["run_start"] = "run_start"
    
    graph_id: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    
    # Metadata for observability
    graph_version: str | None = None  # Code hash or version tag


@dataclass  
class RunEndEvent(GraphEvent):
    """Emitted when a graph run completes (success or error)."""
    event_type: Literal["run_end"] = "run_end"
    
    status: Literal["complete", "error", "interrupted"] = "complete"
    outputs: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    error: str | None = None


@dataclass
class NodeStartEvent(GraphEvent):
    """Emitted when a node begins execution."""
    event_type: Literal["node_start"] = "node_start"
    
    node_id: str = ""
    step_index: int = 0
    inputs: dict[str, Any] = field(default_factory=dict)
    
    # For tracing
    node_type: Literal["function", "gate", "branch", "interrupt", "nested"] = "function"


@dataclass
class NodeEndEvent(GraphEvent):
    """Emitted when a node completes."""
    event_type: Literal["node_end"] = "node_end"
    
    node_id: str = ""
    step_index: int = 0
    outputs: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    cached: bool = False


@dataclass
class NodeSkippedEvent(GraphEvent):
    """Emitted when a node is skipped (branch routing, cache hit)."""
    event_type: Literal["node_skipped"] = "node_skipped"
    
    node_id: str = ""
    reason: Literal["branch_routing", "cached", "dependency_failed"] = "branch_routing"


# ============ Control Flow Events ============

@dataclass
class GateDecisionEvent(GraphEvent):
    """Emitted when a gate/branch makes a routing decision."""
    event_type: Literal["gate_decision"] = "gate_decision"
    
    gate_id: str = ""
    decision: str | bool = ""        # The route chosen (string for gate, bool for branch)
    activated_targets: list[str] = field(default_factory=list)


@dataclass
class InterruptEvent(GraphEvent):
    """Emitted when execution pauses for human input."""
    event_type: Literal["interrupt"] = "interrupt"
    
    interrupt_name: str = ""         # The response_param
    prompt_value: Any = None         # The value surfaced to user
    checkpoint_id: str = ""          # For resumption


@dataclass  
class ResumeEvent(GraphEvent):
    """Emitted when execution resumes after interrupt."""
    event_type: Literal["resume"] = "resume"
    
    interrupt_name: str = ""
    response_value: Any = None
    resumed_from_checkpoint: str = ""


# ============ State Events (for UI sync) ============

@dataclass
class StateSnapshotEvent(GraphEvent):
    """Full state snapshot for UI synchronization."""
    event_type: Literal["state_snapshot"] = "state_snapshot"
    
    state: dict[str, Any] = field(default_factory=dict)  # Full current state


@dataclass
class StateDeltaEvent(GraphEvent):
    """Incremental state update (JSON Patch-like)."""
    event_type: Literal["state_delta"] = "state_delta"
    
    # JSON Patch operations
    operations: list[dict] = field(default_factory=list)
    # e.g., [{"op": "add", "path": "/messages/-", "value": {...}}]


# ============ Streaming Events ============

@dataclass
class StreamingStartEvent(GraphEvent):
    """Emitted when a node begins streaming output."""
    event_type: Literal["streaming_start"] = "streaming_start"
    
    node_id: str = ""
    output_name: str = ""  # Which output is streaming


@dataclass
class StreamingChunkEvent(GraphEvent):
    """Emitted for each chunk of streaming output."""
    event_type: Literal["streaming_chunk"] = "streaming_chunk"
    
    node_id: str = ""
    output_name: str = ""
    chunk: str = ""        # The token/chunk
    chunk_index: int = 0


@dataclass
class StreamingEndEvent(GraphEvent):
    """Emitted when streaming completes."""
    event_type: Literal["streaming_end"] = "streaming_end"
    
    node_id: str = ""
    output_name: str = ""
    final_value: str = ""  # Complete accumulated value
```

### Callback Protocol

Callbacks translate events into system-specific formats:

```python
from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

@runtime_checkable
class EventEmitter(Protocol):
    """Protocol for anything that can receive graph events."""
    
    def emit(self, event: GraphEvent) -> None:
        """Emit an event to this handler."""
        ...


class GraphCallback(ABC):
    """Base class for graph execution callbacks.
    
    Callbacks receive events and can:
    - Log to observability systems (Langfuse, Logfire)
    - Stream to UI protocols (AG-UI, WebSocket)
    - Persist for durability (checkpoint stores)
    - Collect metrics
    """
    
    # ---- Required: Identity ----
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Unique name for this callback (for debugging)."""
        ...
    
    # ---- Lifecycle hooks (all optional) ----
    
    def on_run_start(self, event: RunStartEvent) -> None:
        """Called when graph execution begins."""
        pass
    
    def on_run_end(self, event: RunEndEvent) -> None:
        """Called when graph execution completes."""
        pass
    
    def on_node_start(self, event: NodeStartEvent) -> None:
        """Called before a node executes."""
        pass
    
    def on_node_end(self, event: NodeEndEvent) -> None:
        """Called after a node executes."""
        pass
    
    def on_node_skipped(self, event: NodeSkippedEvent) -> None:
        """Called when a node is skipped."""
        pass
    
    def on_gate_decision(self, event: GateDecisionEvent) -> None:
        """Called when a gate/branch makes a routing decision."""
        pass
    
    def on_interrupt(self, event: InterruptEvent) -> None:
        """Called when execution pauses for human input."""
        pass
    
    def on_resume(self, event: ResumeEvent) -> None:
        """Called when execution resumes after interrupt."""
        pass
    
    # ---- State sync (for UI protocols) ----
    
    def on_state_snapshot(self, event: StateSnapshotEvent) -> None:
        """Called to emit full state snapshot."""
        pass
    
    def on_state_delta(self, event: StateDeltaEvent) -> None:
        """Called to emit incremental state update."""
        pass
    
    # ---- Streaming (for token-level updates) ----
    
    def on_streaming_start(self, event: StreamingStartEvent) -> None:
        """Called when a node begins streaming."""
        pass
    
    def on_streaming_chunk(self, event: StreamingChunkEvent) -> None:
        """Called for each streaming chunk."""
        pass
    
    def on_streaming_end(self, event: StreamingEndEvent) -> None:
        """Called when streaming completes."""
        pass
    
    # ---- Error handling ----
    
    def on_error(self, node_id: str, error: Exception, event: GraphEvent) -> None:
        """Called when an error occurs."""
        pass
```

### Layer-Specific Adapters

Each layer has an adapter that translates Hypernodes events:

#### 1. AG-UI Adapter (UI Protocol Layer)

```python
from typing import Callable, Any
import json

class AGUIAdapter(GraphCallback):
    """Translates Hypernodes events to AG-UI protocol events.
    
    AG-UI is a protocol for streaming agent interactions to frontends.
    See: https://docs.ag-ui.com/
    """
    
    def __init__(self, send_fn: Callable[[dict], None]):
        """
        Args:
            send_fn: Function to send AG-UI events (e.g., WebSocket send)
        """
        self._send = send_fn
        self._state: dict[str, Any] = {}
    
    @property
    def name(self) -> str:
        return "ag-ui"
    
    def on_run_start(self, event: RunStartEvent) -> None:
        self._send({
            "type": "RUN_STARTED",
            "threadId": event.thread_id,
            "runId": event.run_id,
            "parentRunId": event.parent_run_id,
            "timestamp": event.timestamp.isoformat(),
        })
    
    def on_run_end(self, event: RunEndEvent) -> None:
        self._send({
            "type": "RUN_FINISHED" if event.status == "complete" else "RUN_ERROR",
            "threadId": event.thread_id,
            "runId": event.run_id,
            "timestamp": event.timestamp.isoformat(),
        })
    
    def on_state_snapshot(self, event: StateSnapshotEvent) -> None:
        self._state = event.state
        self._send({
            "type": "STATE_SNAPSHOT",
            "threadId": event.thread_id,
            "runId": event.run_id,
            "state": event.state,
        })
    
    def on_state_delta(self, event: StateDeltaEvent) -> None:
        # Apply delta to local state
        self._apply_patches(event.operations)
        self._send({
            "type": "STATE_DELTA",
            "threadId": event.thread_id,
            "runId": event.run_id,
            "delta": event.operations,
        })
    
    def on_streaming_chunk(self, event: StreamingChunkEvent) -> None:
        self._send({
            "type": "TEXT_MESSAGE_CONTENT",
            "threadId": event.thread_id,
            "runId": event.run_id,
            "delta": event.chunk,
            "messageId": f"{event.node_id}:{event.output_name}",
        })
    
    def on_interrupt(self, event: InterruptEvent) -> None:
        # AG-UI might surface this as a "tool" or custom event
        self._send({
            "type": "CUSTOM",
            "threadId": event.thread_id,
            "runId": event.run_id,
            "name": "interrupt",
            "value": {
                "interruptName": event.interrupt_name,
                "promptValue": event.prompt_value,
                "checkpointId": event.checkpoint_id,
            },
        })
    
    def _apply_patches(self, operations: list[dict]) -> None:
        """Apply JSON Patch operations to local state."""
        # Implementation uses jsonpatch library
        import jsonpatch
        self._state = jsonpatch.apply_patch(self._state, operations)
```

#### 2. Langfuse Adapter (Observability Layer)

```python
class LangfuseAdapter(GraphCallback):
    """Translates Hypernodes events to Langfuse traces/spans.
    
    Langfuse provides observability for LLM applications.
    See: https://langfuse.com/docs
    """
    
    def __init__(self, client: "Langfuse"):
        self._client = client
        self._traces: dict[str, "Trace"] = {}
        self._spans: dict[str, "Span"] = {}
    
    @property
    def name(self) -> str:
        return "langfuse"
    
    def on_run_start(self, event: RunStartEvent) -> None:
        # Create trace, map thread_id → sessionId
        trace = self._client.trace(
            id=event.run_id,
            session_id=event.thread_id,  # Groups traces by conversation
            name=event.graph_id,
            input=event.inputs,
            metadata={"graph_version": event.graph_version},
        )
        self._traces[event.run_id] = trace
    
    def on_run_end(self, event: RunEndEvent) -> None:
        if trace := self._traces.get(event.run_id):
            trace.update(
                output=event.outputs if event.status == "complete" else None,
                status_message=event.error,
            )
    
    def on_node_start(self, event: NodeStartEvent) -> None:
        trace = self._traces.get(event.run_id)
        if not trace:
            return
        
        # Create span for this node
        span = trace.span(
            name=event.node_id,
            input=event.inputs,
            metadata={
                "node_type": event.node_type,
                "step_index": event.step_index,
            },
        )
        self._spans[f"{event.run_id}:{event.node_id}:{event.step_index}"] = span
    
    def on_node_end(self, event: NodeEndEvent) -> None:
        key = f"{event.run_id}:{event.node_id}:{event.step_index}"
        if span := self._spans.get(key):
            span.end(
                output=event.outputs,
                metadata={"cached": event.cached, "duration_ms": event.duration_ms},
            )
    
    def on_gate_decision(self, event: GateDecisionEvent) -> None:
        trace = self._traces.get(event.run_id)
        if trace:
            trace.event(
                name="gate_decision",
                metadata={
                    "gate_id": event.gate_id,
                    "decision": event.decision,
                    "activated_targets": event.activated_targets,
                },
            )
    
    def on_interrupt(self, event: InterruptEvent) -> None:
        trace = self._traces.get(event.run_id)
        if trace:
            trace.event(
                name="interrupt",
                metadata={
                    "interrupt_name": event.interrupt_name,
                    "checkpoint_id": event.checkpoint_id,
                },
                input={"prompt_value": event.prompt_value},
            )
```

#### 3. Checkpoint Adapter (Durability Layer)

```python
from abc import ABC, abstractmethod

class CheckpointStore(ABC):
    """Abstract interface for checkpoint persistence."""
    
    @abstractmethod
    def save(self, checkpoint: "Checkpoint") -> str:
        """Save checkpoint, return checkpoint_id."""
        ...
    
    @abstractmethod
    def load(self, checkpoint_id: str) -> "Checkpoint":
        """Load checkpoint by ID."""
        ...
    
    @abstractmethod
    def list_by_thread(self, thread_id: str) -> list["Checkpoint"]:
        """List all checkpoints for a thread."""
        ...


class CheckpointAdapter(GraphCallback):
    """Persists execution state for durability and resumption.
    
    This adapter doesn't couple to any specific system (Temporal, DBOS, etc.)
    but provides the hooks they need.
    """
    
    def __init__(
        self, 
        store: CheckpointStore,
        checkpoint_on: Literal["every_node", "interrupt_only", "gate_only"] = "interrupt_only",
    ):
        self._store = store
        self._checkpoint_on = checkpoint_on
        self._current_state: dict[str, Any] = {}
        self._step_index: int = 0
    
    @property
    def name(self) -> str:
        return "checkpoint"
    
    def on_run_start(self, event: RunStartEvent) -> None:
        self._current_state = dict(event.inputs)
        self._step_index = 0
    
    def on_node_end(self, event: NodeEndEvent) -> None:
        # Update state with outputs
        self._current_state.update(event.outputs)
        self._step_index = event.step_index + 1
        
        if self._checkpoint_on == "every_node":
            self._save_checkpoint(event)
    
    def on_gate_decision(self, event: GateDecisionEvent) -> None:
        if self._checkpoint_on in ("every_node", "gate_only"):
            self._save_checkpoint(event)
    
    def on_interrupt(self, event: InterruptEvent) -> None:
        # Always checkpoint on interrupt (required for resumption)
        checkpoint = Checkpoint(
            checkpoint_id=event.checkpoint_id,
            thread_id=event.thread_id,
            run_id=event.run_id,
            step_index=self._step_index,
            state=self._current_state.copy(),
            interrupt_name=event.interrupt_name,
            prompt_value=event.prompt_value,
        )
        self._store.save(checkpoint)
    
    def _save_checkpoint(self, event: GraphEvent) -> None:
        checkpoint = Checkpoint(
            checkpoint_id=f"{event.run_id}:{self._step_index}",
            thread_id=event.thread_id,
            run_id=event.run_id,
            step_index=self._step_index,
            state=self._current_state.copy(),
        )
        self._store.save(checkpoint)


@dataclass
class Checkpoint:
    """Serializable execution state for resumption."""
    
    checkpoint_id: str
    thread_id: str
    run_id: str
    step_index: int
    state: dict[str, Any]
    
    # If interrupted
    interrupt_name: str | None = None
    prompt_value: Any = None
    
    # Metadata
    created_at: datetime = field(default_factory=datetime.utcnow)
    graph_version: str | None = None
```

### Wiring It All Together

```python
from hypernodes import Graph, node

# Define your graph
@node(output_name="response")
def generate(prompt: str, model: Any) -> str:
    return model.invoke(prompt)

graph = Graph(nodes=[generate])

# Wire up all three layers via callbacks
runner = GraphRunner(
    graph,
    callbacks=[
        # UI layer: stream to frontend
        AGUIAdapter(send_fn=websocket.send),
        
        # Observability layer: trace to Langfuse
        LangfuseAdapter(client=langfuse_client),
        
        # Durability layer: checkpoint to Postgres
        CheckpointAdapter(
            store=PostgresCheckpointStore(db_url),
            checkpoint_on="interrupt_only",
        ),
    ],
)

# Run with identity
result = await runner.run(
    inputs={"prompt": "Hello"},
    thread_id="conversation-123",  # User-provided, semantic
    # run_id auto-generated, or provide for idempotency
)
```

### ID Mapping Reference

| Hypernodes | AG-UI | Langfuse | Temporal | DBOS |
|------------|-------|----------|----------|------|
| `thread_id` | `threadId` | `sessionId` | `workflowId` | — |
| `run_id` | `runId` | `traceId` | `runId` | `workflowId` |
| `parent_run_id` | `parentRunId` | parent trace | — | — |
| `step_index` | — | span | event index | step number |
| `node_id` | — | span name | activity | step function |
| `checkpoint_id` | — | — | event ID | step checkpoint |

### Integration Patterns

#### Pattern 1: Multi-Turn Chat (AG-UI + Observability)

```python
# Each user message is a new run within the same thread
async def handle_message(user_message: str, thread_id: str):
    result = await runner.run(
        inputs={"message": user_message, "history": load_history(thread_id)},
        thread_id=thread_id,      # Same across turns
        # run_id auto-generated   # New per turn
    )
    save_history(thread_id, result.outputs["history"])
    return result
```

#### Pattern 2: Long-Running Workflow (Durability + Observability)

```python
# Workflow that may take hours and survive restarts
async def process_order(order_id: str):
    result = await runner.run(
        inputs={"order_id": order_id},
        thread_id=f"order-{order_id}",
        run_id=f"order-{order_id}-v1",  # Idempotency key
    )
    
    if result.status == RunStatus.INTERRUPTED:
        # Store checkpoint, notify human
        notify_approver(result.interrupt)
        # Later: runner.run(checkpoint=result.checkpoint, inputs={response_param: ...})
```

#### Pattern 3: Temporal Integration (Durability as External System)

```python
from temporalio import workflow, activity

# Hypernodes graph runs as a Temporal activity
@activity.defn
async def run_graph_activity(inputs: dict, thread_id: str) -> dict:
    result = await runner.run(
        inputs=inputs,
        thread_id=thread_id,
        run_id=workflow.info().run_id,  # Use Temporal's run_id
        callbacks=[
            # Only observability here; Temporal handles durability
            LangfuseAdapter(client=langfuse_client),
        ],
    )
    return result.outputs

@workflow.defn
class OrderWorkflow:
    @workflow.run
    async def run(self, order_id: str) -> dict:
        # Temporal handles durability/replay
        # Hypernodes handles graph execution
        return await workflow.execute_activity(
            run_graph_activity,
            args=[{"order_id": order_id}, f"order-{order_id}"],
            start_to_close_timeout=timedelta(minutes=30),
        )
```

### Key Design Decisions

1. **Events are data, not behavior**: Events are plain dataclasses with no logic. Callbacks interpret them.

2. **Identity flows through context**: `thread_id`, `run_id` are passed at execution time, not stored in graph definition.

3. **Callbacks are composable**: Mix and match adapters for your deployment (cloud vs. local, dev vs. prod).

4. **Checkpoint format is serializable**: Any JSON-compatible store works (Postgres, Redis, S3, local files).

5. **No global state**: All state is explicit in events and callbacks. Multiple runners can coexist.

6. **Streaming is first-class**: Token-level events enable real-time UI without buffering.

7. **Layers are independent**: You can use AG-UI without Langfuse, or Langfuse without checkpointing.

---

### Type Reference

```python
# Core types
class RunStatus(Enum):
    PENDING | RUNNING | INTERRUPTED | COMPLETE | ERROR

@dataclass
class GraphResult:
    status: RunStatus
    outputs: dict[str, Any]
    run_id: str
    thread_id: str | None
    interrupt: Interrupt | None      # When INTERRUPTED
    error: GraphError | None         # When ERROR
    checkpoint: Checkpoint | None
    history: list[NodeExecution] | None  # If include_history=True

@dataclass
class Interrupt:
    value: Any                       # The prompt (user-defined type)
    response_param: str              # Interrupt name + response target
    response_type: type | None

@dataclass
class InterruptNode:
    input_param: str                 # Parameter to read prompt from
    response_param: str              # Parameter to write response to (+ name)
    response_type: type | None

# Events for iter()
GraphEvent = (
    NodeStartEvent | NodeCompleteEvent | InterruptEvent |
    GateDecisionEvent | StreamingEvent | ErrorEvent
)
```