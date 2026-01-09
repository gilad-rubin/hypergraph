# Core Types Reference

**Complete reference for all types, classes, and data structures in hypergraph.**

---

## Quick Navigation


| Type                              | Purpose                           | Where Used                         |
| --------------------------------- | --------------------------------- | ---------------------------------- |
| [HyperNode](#hypernode)           | Base class for all nodes          | Returned by `@node`, `.as_node()`  |
| [FunctionNode](#functionnode)     | Concrete node wrapping a function | Result of `@node` decorator        |
| [GateNode](#gatenode)             | Base class for routing gates      | Parent of RouteNode, BranchNode, TypeRouteNode |
| [RouteNode](#routenode)           | Multi-way routing gate            | Result of `@route` decorator       |
| [BranchNode](#branchnode)         | Binary routing gate               | Result of `@branch` decorator      |
| [TypeRouteNode](#typeroutenode)   | Type-based routing gate           | Constructed directly               |
| [InterruptNode](#interruptnode)   | Human-in-the-loop pause point     | Constructed directly               |
| [GraphNode](#graphnode)           | Nested graph as a node            | Result of `Graph.as_node()`        |
| [InputSpec](#inputspec)           | Graph input specification         | Returned by `Graph.inputs`         |
| [Graph](#graph)                   | Graph structure definition        | Created with `Graph(nodes=[...])`  |
| [GraphState](#graphstate)         | Runtime value storage             | Internal to execution              |
| [GraphResult](#graphresult)       | Execution results                 | Returned by `.run()`, nested graphs |
| [RunResult](#runresult)           | Async execution result            | Returned by `AsyncRunner.run()`    |

**See also:** [Runner Compatibility](#runner-compatibility) - How runners validate and execute graphs with different features.

---

## HyperNode

### Purpose

**Abstract base class for all node types.** Defines the minimal interface that all nodes share - just enough to wire them together in a graph. Everything else lives on concrete subclasses.

### Class Definition

```python
from abc import ABC
from dataclasses import dataclass, field
from typing import Literal, Self
import copy

@dataclass(frozen=True)
class RenameEntry:
    """Tracks a single rename operation for error messages."""
    kind: Literal["name", "inputs", "outputs"]
    old: str
    new: str

def _apply_renames(
    values: tuple[str, ...],
    mapping: dict[str, str] | None,
    kind: Literal["inputs", "outputs"],
) -> tuple[tuple[str, ...], list[RenameEntry]]:
    """Apply renames to a tuple, returning (new_values, history)."""
    if not mapping:
        return values, []

    history = [RenameEntry(kind, old, new) for old, new in mapping.items()]
    return tuple(mapping.get(v, v) for v in values), history

class HyperNode(ABC):
    """Base class for all node types with shared rename functionality."""

    # Core attributes (4 total) - defined by subclass __init__
    name: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    _rename_history: list[RenameEntry]  # Initialized to [] by subclass

    # === Public API (all one-liners) ===

    def with_name(self, name: str) -> Self:
        """Return new node with different name."""
        return self._with_renamed("name", {self.name: name})

    def with_inputs(self, mapping: dict[str, str] | None = None, /, **kwargs: str) -> Self:
        """Return new node with renamed inputs."""
        return self._with_renamed("inputs", {**(mapping or {}), **kwargs})

    def with_outputs(self, mapping: dict[str, str] | None = None, /, **kwargs: str) -> Self:
        """Return new node with renamed outputs."""
        return self._with_renamed("outputs", {**(mapping or {}), **kwargs})

    # === Internal helpers ===

    def _with_renamed(self, attr: str, mapping: dict[str, str]) -> Self:
        """Rename entries in an attribute (name, inputs, or outputs)."""
        clone = self._copy()
        current = getattr(clone, attr)

        if isinstance(current, str):
            # Single value (name)
            old, new = current, mapping.get(current, current)
            if old != new:
                clone._rename_history.append(RenameEntry(attr, old, new))
                setattr(clone, attr, new)
        else:
            # Tuple (inputs/outputs)
            for old, new in mapping.items():
                if old not in current:
                    raise clone._make_rename_error(old, attr)
                clone._rename_history.append(RenameEntry(attr, old, new))
            setattr(clone, attr, tuple(mapping.get(v, v) for v in current))

        return clone

    def _copy(self) -> Self:
        """Create shallow copy with independent history list."""
        clone = copy.copy(self)
        clone._rename_history = list(self._rename_history)
        return clone

    def _make_rename_error(self, name: str, attr: str) -> "RenameError":
        """Build helpful error message using history."""
        current = getattr(self, attr)
        for entry in self._rename_history:
            if entry.kind == attr and entry.old == name:
                return RenameError(
                    f"'{name}' was renamed to '{entry.new}'. "
                    f"Current {attr}: {current}"
                )
        return RenameError(f"'{name}' not found. Current {attr}: {current}")
```

### Design Philosophy

**Minimal attributes, maximum clarity:**

| Attribute | Type | Purpose |
|-----------|------|---------|
| `name` | `str` | Current public node name |
| `inputs` | `tuple[str, ...]` | Current public input names |
| `outputs` | `tuple[str, ...]` | Current public output names |
| `_rename_history` | `list[RenameEntry]` | For helpful error messages |

- **No `original_*` attributes**: The history tracks what was renamed, not the originals
- **No properties needed**: Direct attribute access is simpler and faster
- **Immutable pattern**: All `with_*` methods return new instances

### What's NOT in the Base Class

| Property | Why Not Universal | Lives On |
|----------|-------------------|----------|
| `cache` | Interrupts never cache, GraphNode delegates to inner nodes | `FunctionNode`, `GateNode` (not GraphNode/InterruptNode) |
| `definition_hash` | Only needed for cacheable nodes | `FunctionNode` only |
| `is_async` / `is_generator` | Only relevant for function wrappers | `FunctionNode` only (runner discovers for GraphNode) |
| `func` | Only function wrappers have underlying function | `FunctionNode`, `RouteNode`, `BranchNode` |

**Benefits:**
- `isinstance(node, HyperNode)` works everywhere
- Rename methods implemented once, inherited by all
- Clear "is-a" relationship in the type hierarchy
- Simple to understand: "a node has a name, inputs, and outputs"

### Type Hints

```python
from hypergraph import HyperNode

def process_node(node: HyperNode) -> None:
    """Works with any node type."""
    print(f"{node.name}: {node.inputs} → {node.outputs}")

# Or be specific when you need type-specific features
from hypergraph import FunctionNode

def process_function_node(node: FunctionNode) -> None:
    """Only FunctionNode has is_async/is_generator properties."""
    if node.is_async:
        ...
```

### Additional Configuration Methods

Rename methods are in the base class (see above). Additional methods are subclass-specific:

| Method | Supported By | Not Supported By |
|--------|--------------|------------------|
| `map_over()` | `GraphNode` | All others (single execution only) |

### with_* Usage Examples

> **Note:** The `/` makes `mapping` positional-only, so kwargs are always renames. If your node has an input named `mapping`, use dict style: `node.with_inputs({"mapping": "data"})`

```python
# Keyword args (preferred - cleaner)
node.with_inputs(text="raw_document", config="settings")
node.with_outputs(result="processed")
node.with_name("preprocessor")

# Dict style (for dynamic mappings or Python keywords)
node.with_inputs({"text": "raw", "class": "category"})

# Chaining (each call returns new node)
adapted = (
    clean_text
    .with_name("preprocessor")
    .with_inputs(text="raw_document")
    .with_outputs(cleaned="processed")
)

# Reusing same node with different configurations
node_a = process
node_b = process.with_inputs(x="count")
node_c = process.with_inputs(x="value")
# All three are independent nodes
```

#### Error Messages for Chained with_* Calls

When users chain calls incorrectly, provide **helpful error messages** that track the rename history:

```python
# User tries to rename using the original name after already renaming
node = clean_text.with_inputs(text="raw")
node.with_inputs(text="document")  # ERROR!

# Error message:
# RenameError: Input 'text' not found.
#
# Current inputs: ('raw', 'config')
#
# Rename history for this node:
#   • 'text' was renamed to 'raw'
#
# Did you mean: node.with_inputs(raw="document")
```

```python
# User tries to rename a name that was already renamed
node = clean_text.with_inputs(text="raw").with_inputs(raw="document")
node.with_inputs(text="final")  # ERROR!

# Error message:
# RenameError: Input 'text' not found.
#
# Current inputs: ('document', 'config')
#
# Rename history for this node:
#   • 'text' → 'raw' → 'document'
#
# Did you mean: node.with_inputs(document="final")
```

#### map_over() Error Messages

Same principle applies to `map_over()` - use current public names:

```python
# After renaming, map_over must use the new name
node = rag.as_node().with_inputs(query="user_question")
node.map_over("query")  # ERROR!

# Error message:
# MapOverError: Input 'query' not found.
#
# Current inputs: ('user_question', 'top_k')
#
# Rename history for this node:
#   • 'query' was renamed to 'user_question'
#
# Did you mean: node.map_over("user_question")
```

#### Implementation Note

Each node tracks its rename history as a list of `RenameEntry` dataclasses. The `_apply_renames` helper makes this consistent across all function-wrapping nodes:

```python
# Clean two-liner in every constructor
inputs = tuple(inspect.signature(func).parameters.keys())
self.inputs, self._rename_history = _apply_renames(inputs, rename_inputs, "inputs")
```

This tracks all rename operations in a single list, enabling helpful error messages when users try to use old names.

See [Node Configuration](node-configuration.md) for full details.

---

## FunctionNode

### Purpose

**Wraps a regular Python function as a graph node.** Created by the `@node` decorator or `FunctionNode()` constructor.

### Class Definition

```python
class FunctionNode(HyperNode):
    """Wraps a Python function as a graph node."""

    def __init__(
        self,
        source: Callable | "FunctionNode",
        output_name: str | tuple[str, ...] | None = None,
        *,
        name: str | None = None,
        rename_inputs: dict[str, str] | None = None,
        cache: bool = False,
    ):
        """
        Wrap a function as a node.

        Args:
            source: Function to wrap, or existing FunctionNode (extracts .func)
            output_name: Name(s) for output value(s). Default: function name.
            name: Public node name (default: func.__name__)
            rename_inputs: Mapping to rename inputs {old: new}
            cache: Whether to cache results (default: False)

        Note: is_async and is_generator are auto-detected from func.

        When source is a FunctionNode:
            Only source.func is extracted. All other configuration (name,
            outputs, renames, cache) from the source node is ignored.
            The new node is built fresh from the underlying function.
        """
        # Extract func if source is FunctionNode (ignores all other config)
        func = source.func if isinstance(source, FunctionNode) else source

        self.func = func
        self.cache = cache
        self._definition_hash = hash_definition(func)

        # Core HyperNode attributes
        self.name = name or func.__name__
        self.outputs = ensure_tuple(output_name) if output_name else (func.__name__,)
        inputs = tuple(inspect.signature(func).parameters.keys())
        self.inputs, self._rename_history = _apply_renames(inputs, rename_inputs, "inputs")

        # Auto-detect execution mode (never user-specified)
        self._is_async = (
            inspect.iscoroutinefunction(func) or
            inspect.isasyncgenfunction(func)
        )
        self._is_generator = (
            inspect.isgeneratorfunction(func) or
            inspect.isasyncgenfunction(func)
        )
```

### FunctionNode-Specific Properties

Inherits `name`, `inputs`, `outputs` from HyperNode.

```python
@property
def definition_hash(self) -> str:
    """SHA256 hash of function source (cached at creation)."""

@property
def is_async(self) -> bool:
    """True if requires await (async def or async generator)."""

@property
def is_generator(self) -> bool:
    """True if yields multiple values (sync or async generator)."""
```

### Special Methods

```python
def __call__(self, *args, **kwargs) -> Any:
    """Call the wrapped function directly."""
    return self.func(*args, **kwargs)

def __repr__(self) -> str:
    # Find original name from history (if renamed) or use func name
    original = self.func.__name__
    for entry in self._rename_history:
        if entry.kind == "name" and entry.new == self.name:
            original = entry.old
            break

    if self.name == original:
        return f"FunctionNode({self.name}, outputs={self.outputs})"
    else:
        return f"FunctionNode({original} as '{self.name}', outputs={self.outputs})"

# Examples:
# >>> process
# FunctionNode(process, outputs=('result',))
#
# >>> process.with_name("preprocessor")
# FunctionNode(process as 'preprocessor', outputs=('result',))
```

### Example

```python
from hypergraph import node, FunctionNode

# Option 1: Decorator (convenient)
@node(output_name="result")
def process(x: int) -> int:
    return x * 2

# Option 2: Decorator without parens (output defaults to function name)
@node
def multiply(x: int) -> int:
    return x * 2
# multiply.outputs == ("multiply",)

# Option 3: Constructor with custom name
def transform(x: int) -> int:
    return x * 2

# Same function, different node names
node_a = FunctionNode(transform, "result_a", name="transform_a")
node_b = FunctionNode(transform, "result_b", name="transform_b")

assert node_a.name == "transform_a"            # Public name (customized)
assert node_a.func.__name__ == "transform"     # Original function name (via .func)
assert node_b.name == "transform_b"            # Different public name

# Option 4: Creating from existing FunctionNode (extracts .func only)
existing = FunctionNode(transform, "old_output", name="old_name")
fresh = FunctionNode(existing, "new_output", name="new_name")

# fresh is built from existing.func, ignoring existing's configuration
assert fresh.func is existing.func            # Same underlying function
assert fresh.name == "new_name"               # New configuration applied
assert fresh.outputs == ("new_output",)       # Not inherited from existing
assert existing.name == "old_name"            # Original unchanged

# Access underlying function
assert process.func(5) == 10

# Node properties (inherited from HyperNode)
assert process.name == "process"               # Current public name
assert process.inputs == ("x",)                # Current public inputs
assert process.outputs == ("result",)          # Current public outputs
assert process.is_async == False
assert process.is_generator == False
```

### All Four Execution Modes

```python
from hypergraph import node
from typing import Iterator, AsyncIterator

# 1. Sync function (most common)
@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2
# is_async=False, is_generator=False

# 2. Async function (I/O-bound operations)
@node(output_name="data")
async def fetch(url: str) -> dict:
    async with httpx.AsyncClient() as client:
        return (await client.get(url)).json()
# is_async=True, is_generator=False

# 3. Sync generator (memory-efficient iteration)
@node(output_name="chunks")
def chunk_text(text: str) -> Iterator[str]:
    for paragraph in text.split("\n\n"):
        yield paragraph
# is_async=False, is_generator=True

# 4. Async generator (streaming, e.g., LLM responses)
@node(output_name="tokens")
async def stream_llm(prompt: str) -> AsyncIterator[str]:
    async for chunk in openai.chat.completions.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        stream=True,
    ):
        yield chunk.choices[0].delta.content or ""
# is_async=True, is_generator=True
```

### The @node Decorator

```python
def node(
    source: Callable | None = None,
    output_name: str | tuple[str, ...] | None = None,
    *,
    name: str | None = None,
    rename_inputs: dict[str, str] | None = None,
    cache: bool = False,
) -> FunctionNode | Callable[[Callable], FunctionNode]:
    """
    Decorator to wrap a function as a FunctionNode.

    Can be used with or without parentheses:
        @node
        def foo(): ...

        @node(output_name="result")
        def bar(): ...

    Args:
        source: The function to wrap (when used without parens)
        output_name: Name(s) for output value(s). Default: function name.
        name: Public node name (default: func.__name__)
        rename_inputs: Mapping to rename inputs {old: new}
        cache: Whether to cache results (default: False)

    Returns:
        FunctionNode if source provided, else decorator function.
    """
    def decorator(func: Callable) -> FunctionNode:
        return FunctionNode(
            source=func,
            output_name=output_name,
            name=name,
            rename_inputs=rename_inputs,
            cache=cache,
        )

    if source is not None:
        # Used without parentheses: @node
        return decorator(source)
    # Used with parentheses: @node(...)
    return decorator
```

### Output Validation Behavior

**Single output name** (string or 1-tuple): The return value is stored as-is, no validation.

```python
@node(output_name="data")
def fetch() -> dict:
    return {"key": "value"}  # ✓ Stored as outputs["data"]

@node(output_name="items")
def get_list() -> list:
    return [1, 2, 3]  # ✓ Stored as outputs["items"] (the list itself)

@node  # No output_name → defaults to ("function_name",)
def compute() -> tuple:
    return (1, 2, 3)  # ✓ Stored as outputs["compute"] (the whole tuple)
```

**Multiple output names** (2+ element tuple): Return value is unpacked and validated.

```python
@node(output_name=("mean", "std"))
def stats(data: list) -> tuple[float, float]:
    return calculate_mean(data), calculate_std(data)  # ✓ Unpacked correctly

@node(output_name=("a", "b", "c"))
def split() -> tuple:
    return (1, 2)  # ✗ Error: expected 3 values, got 2
```

This design means:
- **No surprise unpacking**: Returning a tuple with single `output_name` keeps the tuple intact
- **Explicit unpacking**: Only when you declare multiple outputs does unpacking occur
- **Fail-fast validation**: Mismatched output count errors immediately at execution time

---

## GateNode

### Purpose

**Abstract base class for all routing gates.** Provides shared behavior for nodes that control execution flow but don't produce data outputs.

### Class Definition

```python
class GateNode(HyperNode):
    """Base class for routing gates (RouteNode, BranchNode, TypeRouteNode)."""

    # Shared gate attributes
    targets: list[str | type[END]]
    outputs: tuple[str, ...] = ()  # Gates never produce data - they route
    cache: bool = False
```

### Type Hierarchy

```
HyperNode (ABC)
├── FunctionNode
├── GateNode (ABC)
│   ├── RouteNode      - func returns target name (str)
│   ├── BranchNode     - func returns bool
│   └── TypeRouteNode  - no func, routes by isinstance()
├── InterruptNode
└── GraphNode
```

### Shared Gate Properties

All gates share:
- `targets: list[str | type[END]]` - Valid routing destinations
- `outputs = ()` - Gates don't produce data
- `cache = False` - Default (routing decisions rarely cached)

Use `isinstance(node, GateNode)` for type checking.

---

## RouteNode

### Purpose

**Multi-way routing gate that returns target node name.** Created by the `@route` decorator.

### Class Definition

```python
class RouteNode(GateNode):
    """Gate that routes to one of multiple targets based on function return."""

    def __init__(
        self,
        func: Callable[..., str | None],
        targets: list[str | type[END]],
        *,
        name: str | None = None,
        rename_inputs: dict[str, str] | None = None,
        fallback: str | type[END] | None = None,
        cache: bool = False,
    ):
        """
        Create a routing gate.

        Args:
            func: Function that returns target name (or None to use fallback)
            targets: REQUIRED list of valid target node names and/or END
            name: Public node name (default: func.__name__)
            rename_inputs: Mapping to rename inputs {old: new}
            fallback: Target when func returns None (optional)
            cache: Whether to cache decisions (default: False)
        """
        self.func = func
        self.targets = targets + ([fallback] if fallback else [])
        self.fallback = fallback
        self.cache = cache

        # Core HyperNode attributes
        self.name = name or func.__name__
        inputs = tuple(inspect.signature(func).parameters.keys())
        self.inputs, self._rename_history = _apply_renames(inputs, rename_inputs, "inputs")
```

### RouteNode-Specific Properties

Inherits `name`, `inputs`, `outputs` from HyperNode, `targets`, `cache` from GateNode.

```python
@property
def func(self) -> Callable[..., str | None]:
    """Function that returns target node name (or None for fallback)."""

@property
def fallback(self) -> str | type[END] | None:
    """Target when func returns None."""
```

### Example

```python
from hypergraph import route, END

@route(targets=["option_a", "option_b", "option_c", END])
def decide(state: dict) -> str:
    if state["ready"]:
        return END
    if state["needs_more"]:
        return "option_a"
    return "option_b"

# Properties
assert decide.name == "decide"
assert decide.inputs == ("state",)
assert decide.targets == ["option_a", "option_b", "option_c", END]
assert decide.outputs == ()  # Gates don't produce data outputs
assert isinstance(decide, GateNode)  # Type check
```

### With Fallback

```python
@route(targets=["fast_path", "slow_path"], fallback="default_path")
def select_path(score: float) -> str | None:
    if score > 0.9:
        return "fast_path"
    if score < 0.1:
        return "slow_path"
    return None  # Uses fallback

assert select_path.fallback == "default_path"
assert select_path.targets == ["fast_path", "slow_path", "default_path"]
```

---

## BranchNode

### Purpose

**Binary routing gate (true/false).** Created by the `@branch` decorator. Specialized form of `RouteNode` with exactly 2 targets.

### Class Definition

```python
class BranchNode(GateNode):
    """Gate that routes based on boolean decision."""

    def __init__(
        self,
        func: Callable[..., bool],
        when_true: str,
        when_false: str,
        *,
        name: str | None = None,
        rename_inputs: dict[str, str] | None = None,
        cache: bool = False,
    ):
        """
        Create a binary routing gate.

        Args:
            func: Function that returns bool
            when_true: Target node name when True
            when_false: Target node name when False
            name: Public node name (default: func.__name__)
            rename_inputs: Mapping to rename inputs {old: new}
            cache: Whether to cache decisions (default: False)
        """
        self.func = func
        self.when_true = when_true
        self.when_false = when_false
        self.targets = [when_true, when_false]  # For validation
        self.cache = cache

        # Core HyperNode attributes
        self.name = name or func.__name__
        inputs = tuple(inspect.signature(func).parameters.keys())
        self.inputs, self._rename_history = _apply_renames(inputs, rename_inputs, "inputs")
```

### BranchNode-Specific Properties

Inherits `name`, `inputs`, `outputs` from HyperNode, `targets`, `cache` from GateNode.

```python
@property
def when_true(self) -> str:
    """Target node when function returns True."""

@property
def when_false(self) -> str:
    """Target node when function returns False."""
```

### Example

```python
from hypergraph import branch

@branch(when_true="valid_path", when_false="error_path")
def check_valid(data: dict) -> bool:
    return data.get("valid", False)

# Properties
assert check_valid.name == "check_valid"
assert check_valid.when_true == "valid_path"
assert check_valid.when_false == "error_path"
assert check_valid.targets == ["valid_path", "error_path"]
assert isinstance(check_valid, GateNode)  # Type check
```

---

## TypeRouteNode

### Purpose

**Declarative type-based router.** Routes based on the runtime type of an input value using `isinstance()` checks. No function needed - purely declarative.

### Class Definition

```python
class TypeRouteNode(GateNode):
    """Declarative gate that routes based on input value's type."""

    def __init__(
        self,
        name: str,
        input_param: str,
        routes: dict[type, str | type[END]],
        *,
        fallback: str | type[END] | None = None,
    ):
        """
        Create a type-based router.

        Args:
            name: Node name
            input_param: Name of the input to check type of
            routes: Mapping of {type: target_node_name}
            fallback: Target if no type matches (optional)

        Raises:
            ValueError: If no routes provided
            ValueError: If fallback is None and routes don't cover all cases
        """
        self.input_param = input_param
        self.routes = routes
        self.fallback = fallback

        # GateNode attributes
        self.targets = list(routes.values()) + ([fallback] if fallback else [])
        self.cache = False
        self._rename_history: list[RenameEntry] = []

        # HyperNode attributes
        self.name = name
        self.inputs = (input_param,)
```

### TypeRouteNode-Specific Properties

Inherits `name`, `inputs`, `outputs` from HyperNode, `targets`, `cache` from GateNode.

```python
@property
def routes(self) -> dict[type, str | type[END]]:
    """Type-to-target mapping."""

@property
def fallback(self) -> str | type[END] | None:
    """Target if no type matches."""

@property
def input_param(self) -> str:
    """Name of the input whose type is checked."""
```

### Example

```python
from hypergraph import node, TypeRouteNode, Graph
from dataclasses import dataclass

@dataclass
class ChatResponse:
    message: str

@dataclass
class InvalidRequest:
    reason: str

# Function that returns different types
@node(output_name="response")
def call_llm(prompt: str) -> ChatResponse | InvalidRequest:
    result = llm.call(prompt)
    if result.error:
        return InvalidRequest(reason=result.error)
    return ChatResponse(message=result.text)

# Declarative type-based routing (no function needed!)
router = TypeRouteNode(
    name="route_response",
    input_param="response",
    routes={
        ChatResponse: "handle_chat",
        InvalidRequest: "handle_error",
    }
)

@node(output_name="reply")
def handle_chat(response: ChatResponse) -> str:
    return f"Bot: {response.message}"

@node(output_name="error_msg")
def handle_error(response: InvalidRequest) -> str:
    return f"Error: {response.reason}"

# Build graph
graph = Graph(nodes=[call_llm, router, handle_chat, handle_error])

# Execution flow:
# call_llm → response = ChatResponse(message="Hi!")
#     ↓
# router: isinstance(response, ChatResponse) → True
#     ↓
# → handle_chat
```

### With Fallback

```python
router = TypeRouteNode(
    name="route_response",
    input_param="response",
    routes={
        ChatResponse: "handle_chat",
        InvalidRequest: "handle_error",
    },
    fallback="handle_unknown",  # For unexpected types
)
```

### Key Differences from RouteNode

| | RouteNode | TypeRouteNode |
|--|-----------|---------------|
| Requires function | ✅ `func -> str \| None` | ❌ Declarative only |
| Routing logic | Custom function | `isinstance()` checks |
| Fallback | When func returns `None` | When no type matches |
| Created via | `@route` decorator | Constructor |
| Best for | Complex routing logic | Union type dispatch |

---

## InterruptNode

### Purpose

**Declarative pause point for human-in-the-loop workflows.** Unlike other nodes, this is constructed directly (not via decorator).

### Class Definition

```python
class InterruptNode(HyperNode):
    """Pause point that surfaces a value and waits for response."""

    def __init__(
        self,
        name: str,
        input_param: str,
        response_param: str,
        response_type: type | None = None,
    ):
        """
        Create an interrupt point.

        Args:
            name: Unique identifier for this interrupt
            input_param: Parameter name containing value to show user
            response_param: Parameter name where user's response will be written
            response_type: Optional type for validating user's response
        """
        self.input_param = input_param
        self.response_param = response_param
        self.response_type = response_type
        self._rename_history: list[RenameEntry] = []

        # Core HyperNode attributes
        self.name = name
        self.inputs = (input_param,)
        self.outputs = (response_param,)
```

### InterruptNode-Specific Properties

Inherits `name`, `inputs`, `outputs` from HyperNode.

```python
@property
def cache(self) -> bool:
    """Interrupts are never cached."""
    return False
```

**Note:** Use `isinstance(node, InterruptNode)` for type checking, not a property.

### Example

```python
from hypergraph import InterruptNode
from dataclasses import dataclass

@dataclass
class ApprovalPrompt:
    message: str
    draft: str

@dataclass
class ApprovalResponse:
    choice: str  # "approve", "edit", "reject"
    feedback: str | None = None

# Create interrupt
approval = InterruptNode(
    name="approval",
    input_param="approval_prompt",
    response_param="user_decision",
    response_type=ApprovalResponse,
)

# Properties (inherited from HyperNode)
assert approval.name == "approval"
assert approval.inputs == ("approval_prompt",)
assert approval.outputs == ("user_decision",)
assert approval.cache == False

# with_* methods work (inherited from HyperNode)
adapted = approval.with_name("human_review").with_inputs(approval_prompt="draft")
assert adapted.name == "human_review"
assert adapted.inputs == ("draft",)
# Original tracked in _rename_history, not as separate attributes
```

**See also:** [Interrupt Handling with AsyncRunner](#interrupt-handling-with-asyncrunner) for how to handle interrupts at runtime.

---

## GraphNode

### Purpose

**Wrapper that allows a Graph to be used as a node in another graph.** Created by `Graph.as_node()`. This is what enables graph composition and nesting.

### Immutability Guarantees

GraphNode follows strict immutability rules:

1. **`as_node()` creates a new wrapper** - the original Graph is never modified
2. **`with_*` methods return new GraphNodes** - the original GraphNode is unchanged
3. **Same Graph, multiple wrappers** - you can create different configurations from one Graph

This enables safe reuse patterns where the same Graph can participate in multiple outer graphs with different input/output mappings.

### Class Definition

```python
class GraphNode(HyperNode):
    """Wrapper created by Graph.as_node(). Does NOT modify original Graph."""

    def __init__(
        self,
        graph: Graph,
        name: str | None = None,
        runner: BaseRunner | None = None,
    ):
        """
        Wrap a graph as a node.

        Args:
            graph: The graph to wrap (reference, not copy)
            name: Node name (default: use graph.name if set)
            runner: Runner for nested execution (default: inherit from parent)

        Raises:
            ValueError: If name not provided and graph has no name.

        Note:
            Use .with_outputs() to override output names (default: graph's leaf outputs).
            Use .map_over() to configure iteration.
        """
        resolved_name = name or graph.name
        if resolved_name is None:
            raise ValueError(
                "GraphNode requires a name. Either set name on Graph(..., name='x') "
                "or pass name to GraphNode(graph, name='x')"
            )

        self._graph = graph
        self._runner = runner
        self._map_over: list[str] | None = None
        self._map_mode: Literal["zip", "product"] = "zip"
        self._rename_history: list[RenameEntry] = []

        # Core HyperNode attributes
        self.name = resolved_name
        self.inputs = graph.inputs  # Already a tuple
        self.outputs = graph.leaf_outputs  # Use .with_outputs() to override
```

### GraphNode-Specific Properties

Inherits `name`, `inputs`, `outputs` from HyperNode.

```python
@property
def graph(self) -> Graph:
    """The wrapped graph (read-only reference)."""
```

**Note:** GraphNode intentionally has no `cache`, `definition_hash`, `is_async`, or `is_generator` properties:
- **No `cache`**: Caching happens at individual node level inside the graph. Graph-level caching would override inner nodes' cache decisions and skip side effects.
- **No `definition_hash`**: No caching means no need for cache invalidation hashing.
- **No `is_async`/`is_generator`**: The runner discovers these properties recursively when executing inner nodes.

GraphNode is a pure structural wrapper with no execution semantics of its own.

### Methods

```python
def map_over(
    self,
    *params: str,
    mode: Literal["zip", "product"] = "zip",
) -> Self:
    """
    Configure this GraphNode for iteration (config setter, not execution).

    The params must be current public input names at the time of calling.
    If you later call with_inputs() to rename a mapped param, the _map_over
    list is updated automatically to use the new name.

    This is a CONFIGURATION method for nested graphs. It stores which
    parameters should be iterated over when this GraphNode is executed
    as part of a parent graph.

    For direct batch execution, use runner.map() instead - that's the
    primary API. This method uses the same validation logic.

    Args:
        *params: Input parameter name(s) to iterate over. REQUIRED (at least one).
                 Must be CURRENT names at time of call.
        mode: How to combine multiple mapped parameters:
              - "zip": Iterate in parallel (requires same-length iterables)
              - "product": Cartesian product of all combinations

    Returns:
        New GraphNode with map_over configured (immutable pattern).

    Raises:
        ValueError: If no params provided.
        MapOverError: If any param not found in current inputs.

    Note:
        Validation logic is shared with runner.map().
        See runners.md for the primary batch execution API.

    Example:
        # Both orderings work - with_inputs propagates to _map_over:

        # Option 1: map_over first, then rename
        rag_node = (
            graph.as_node()
            .map_over("query")                    # Uses current name
            .with_inputs(query="user_question")   # _map_over updated to ["user_question"]
        )

        # Option 2: rename first, then map_over
        rag_node = (
            graph.as_node()
            .with_inputs(query="user_question")   # Rename first
            .map_over("user_question")            # Use new name
        )

        # Both result in _map_over = ["user_question"]

        # For direct execution - use runner.map() instead:
        results = runner.map(graph, inputs={...}, map_over=["query"])
    """
    _validate_map_over(params, self.inputs, self._rename_history)
    clone = self._copy()
    clone._map_over = list(params)
    clone._map_mode = mode
    return clone
```

### Rename Propagation to map_over

When `with_inputs()` renames an input that is in `_map_over`, the `_map_over` list is updated:

```python
def with_inputs(self, mapping: dict[str, str] | None = None, /, **kwargs: str) -> Self:
    # ... standard rename logic from HyperNode ...

    # Additionally for GraphNode: update _map_over if affected
    if clone._map_over:
        all_renames = {**(mapping or {}), **kwargs}
        clone._map_over = [all_renames.get(p, p) for p in clone._map_over]

    return clone
```

This ensures consistent behavior regardless of call order.

### Shared Validation

The `_validate_map_over` helper is used by:
- `GraphNode.map_over()`
- `runner.map()`

```python
def _validate_map_over(
    params: tuple[str, ...] | list[str],
    inputs: tuple[str, ...],
    rename_history: list[RenameEntry] | None = None,
) -> None:
    """
    Validate map_over parameters. Shared across entry points.

    Raises:
        ValueError: If params is empty.
        MapOverError: If any param not in inputs (with helpful rename hints).
    """
    if not params:
        raise ValueError("map_over requires at least one parameter to iterate over")

    for param in params:
        if param not in inputs:
            # Build helpful error message using rename history if available
            raise MapOverError(_build_rename_hint(param, inputs, rename_history))
```

### Example

```python
# Inner graph
@node(output_name="embedding")
def embed(query: str) -> list[float]:
    return model.embed(query)

@node(output_name="docs")
def retrieve(embedding: list[float]) -> list[str]:
    return db.search(embedding)

inner = Graph(nodes=[embed, retrieve], name="rag")

# Wrap as node
rag_node = inner.as_node()  # Returns GraphNode, uses graph.name

# Properties (inherited from HyperNode)
assert rag_node.name == "rag"
assert rag_node.inputs == ("query",)
assert rag_node.outputs == ("docs",)  # Default: graph's leaf_outputs

# Use in outer graph
outer = Graph(nodes=[preprocess, rag_node, postprocess])
```

### Chaining Examples

All `with_*` and `map_over()` methods return new instances, enabling fluent chaining:

```python
# === Basic chaining ===

# Rename inputs and outputs
adapted = (
    inner.as_node()
    .with_inputs(query="user_question")
    .with_outputs(docs="documents")
)
assert adapted.inputs == ("user_question",)
assert adapted.outputs == ("documents",)

# === Full chain with all methods ===

# Complete configuration in one chain
full_config = (
    inner.as_node(name="rag_pipeline")      # Start with custom name
    .with_inputs(query="search_query")       # Rename input
    .with_outputs(docs="results")            # Rename output
    .map_over("search_query")                # Configure for batch iteration
)
assert full_config.name == "rag_pipeline"
assert full_config.inputs == ("search_query",)
assert full_config.outputs == ("results",)
assert full_config._map_over == ["search_query"]

# === Order flexibility with map_over ===

# Order 1: map_over BEFORE renames - _map_over gets updated automatically
order1 = (
    inner.as_node()
    .map_over("query")                       # Map over original name
    .with_inputs(query="q")                  # Rename propagates to _map_over
    .with_outputs(docs="d")
)
assert order1._map_over == ["q"]             # Updated!
assert order1.inputs == ("q",)

# Order 2: map_over AFTER renames - use current names
order2 = (
    inner.as_node()
    .with_inputs(query="q")                  # Rename first
    .with_outputs(docs="d")
    .map_over("q")                           # Use new name
)
assert order2._map_over == ["q"]
assert order2.inputs == ("q",)

# Order 3: map_over in the middle
order3 = (
    inner.as_node()
    .with_inputs(query="q")                  # Rename input
    .map_over("q")                           # Map over renamed input
    .with_outputs(docs="d")                  # Rename output (doesn't affect map_over)
)
assert order3._map_over == ["q"]
assert order3.outputs == ("d",)

# All three produce equivalent results!

# === Reuse pattern ===
# Same graph, different configurations - all independent

rag_for_search = inner.as_node().with_inputs(query="search_query")
rag_for_chat = inner.as_node().with_inputs(query="chat_message")
rag_batch = inner.as_node().map_over("query")

assert rag_for_search.inputs == ("search_query",)
assert rag_for_chat.inputs == ("chat_message",)
assert rag_batch._map_over == ["query"]

# Original graph unchanged
assert inner.inputs.all == frozenset({"query"})

# === Multiple mapped parameters ===

@node(output_name="score")
def compare(query: str, document: str) -> float:
    return similarity(query, document)

compare_graph = Graph(nodes=[compare], name="comparator")

# Zip mode (default): iterate in parallel
zipped = (
    compare_graph.as_node()
    .with_inputs(query="queries", document="docs")
    .map_over("queries", "docs", mode="zip")
)
# Requires same-length lists: zip(queries, docs)

# Product mode: all combinations
product = (
    compare_graph.as_node()
    .with_inputs(query="queries", document="docs")
    .map_over("queries", "docs", mode="product")
)
# Produces len(queries) * len(docs) results
```

---

## InputSpec

### Purpose

**Structured specification of graph inputs.** Returned by `Graph.inputs`, provides all information about what inputs a graph accepts.

### Class Definition

```python
@dataclass(frozen=True)
class InputSpec:
    """Specification of graph input parameters."""

    required: frozenset[str]
    """Must provide: no incoming edge, not bound, no default value."""

    optional: frozenset[str]
    """Has fallback: bound (highest priority) OR has default value."""

    seeds: frozenset[str]
    """Cycle initialization: params with self/cycle edge that need initial values."""

    bound: dict[str, Any]
    """Currently bound values from .bind(). Takes priority over defaults."""

    @property
    def all(self) -> frozenset[str]:
        """All input names (required + optional)."""
        return self.required | self.optional
```

### Priority Rules

When determining if an input is required or optional:

1. **Bound values** (highest priority): If `name in bound`, it's optional
2. **Default values**: If function parameter has a default, it's optional
3. **Otherwise**: It's required

### Example

```python
@node(output_name="result")
def process(x: int, config: dict = None) -> int:
    return x * 2

graph = Graph(nodes=[process])

# Before binding
assert graph.inputs.required == frozenset({"x"})
assert graph.inputs.optional == frozenset({"config"})  # Has default
assert graph.inputs.all == frozenset({"x", "config"})
assert graph.inputs.bound == {}

# After binding
bound_graph = graph.bind(x=5)
assert bound_graph.inputs.required == frozenset()      # x is now bound
assert bound_graph.inputs.optional == frozenset({"x", "config"})
assert bound_graph.inputs.bound == {"x": 5}

# With cycles
@node(output_name="count")
def counter(count: int) -> int:
    return count + 1

cyclic = Graph(nodes=[counter])  # count feeds back to itself
assert cyclic.inputs.seeds == frozenset({"count"})  # Needs initial value
```

---

## Graph

### Purpose

**Pure structure definition of a computation graph.** No execution logic, just nodes and their relationships.

### Class Definition

```python
class Graph:
    """Graph structure definition."""
  
    def __init__(
        self,
        nodes: list[HyperNode],
        *,
        name: str | None = None,
        strict_types: bool = False,
    ):
        """
        Create a graph from nodes.

        Args:
            nodes: List of HyperNode objects
            name: Optional graph name. Used as default for as_node() when
                  nesting this graph. If not set here, must be provided
                  when calling as_node(name='...')
            strict_types: Validate type annotations between connected nodes (default: False)
        """
        self._nodes = {n.name: n for n in nodes}
        self.name = name
        self._nx_graph = self._build_graph(nodes)
        self._bound = {}
        self._validate()  # Build-time validation
```

### Properties

**Ordering:** All tuple/frozenset properties maintain deterministic order based on node list order (the order nodes were passed to the constructor). This ensures consistent behavior across runs.

```python
@property
def nodes(self) -> dict[str, HyperNode]:
    """Map of node name → node object. Use .keys() for node names."""

@property
def nx_graph(self) -> nx.DiGraph:
    """Underlying NetworkX graph for visualization/analysis."""

# --- Efficient Boolean Checks (for runner compatibility) ---
# These use short-circuit evaluation - no list building needed.

@property
def has_cycles(self) -> bool:
    """True if this graph level contains cycles.

    Note: Only checks current level, not nested GraphNodes.
    Implementation: not nx.is_directed_acyclic_graph(self._nx_graph)
    """

@property
def has_gates(self) -> bool:
    """True if graph contains routing gates (RouteNode, BranchNode, TypeRouteNode).

    Implementation: any(isinstance(n, GateNode) for n in self._nodes.values())
    """

@property
def has_interrupts(self) -> bool:
    """True if graph contains InterruptNodes.

    Implementation: any(isinstance(n, InterruptNode) for n in self._nodes.values())
    """

@property
def has_async_nodes(self) -> bool:
    """True if any FunctionNode is async.

    Implementation: any(isinstance(n, FunctionNode) and n.is_async for n in self._nodes.values())
    """

# --- Detailed Node Lists (when you need the actual nodes) ---

@property
def interrupt_nodes(self) -> list[InterruptNode]:
    """All interrupt nodes."""

# --- Input Specification ---

@property
def inputs(self) -> InputSpec:
    """Input parameter specification. See InputSpec for details."""

@property
def outputs(self) -> tuple[str, ...]:
    """All output names produced by nodes, in node order."""

@property
def leaf_outputs(self) -> tuple[str, ...]:
    """Outputs from leaf nodes (nodes with no downstream consumers)."""
```

### Methods

```python
def bind(self, **values: Any) -> Graph:
    """Bind default values for graph inputs (like functools.partial)."""

def unbind(self, *keys: str) -> Graph:
    """Remove specific bindings."""

def as_node(
    self,
    *,
    name: str | None = None,
    runner: BaseRunner | None = None,
) -> GraphNode:
    """
    Wrap graph as a node for composition.

    Returns a NEW GraphNode instance. Does NOT modify this Graph.

    Name Resolution (in GraphNode):
        1. Use `name` parameter if provided
        2. Otherwise use `graph.name` (from Graph constructor)
        3. If neither exists, raise ValueError

    Args:
        name: Override node name (default: use graph.name)
        runner: Runner for nested execution (default: inherit from parent)

    Returns:
        GraphNode with graph's leaf_outputs as default outputs.
        Use .with_outputs() to override output names.
        Use .map_over() to configure iteration.

    Raises:
        ValueError: If name not provided and graph has no name.

    Example:
        # Simple wrapping (uses graph.name if set)
        node = graph.as_node()

        # Override name
        node = graph.as_node(name="custom")

        # Configure outputs and iteration via chaining
        node = (
            graph.as_node()
            .with_outputs(docs="retrieved_docs")
            .map_over("query")
        )
    """
    return GraphNode(self, name=name, runner=runner)

def visualize(self, **kwargs):
    """Generate visual representation of graph."""
```

### Example

```python
from hypergraph import Graph, node

@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2

@node(output_name="result")
def add_ten(doubled: int) -> int:
    return doubled + 10

# Create graph
graph = Graph(nodes=[double, add_ten])

# Properties
assert tuple(graph.nodes.keys()) == ("double", "add_ten")  # Node names in order
assert graph.has_cycles == False

# Input specification (via InputSpec)
assert graph.inputs.required == frozenset({"x"})
assert graph.inputs.optional == frozenset()
assert graph.inputs.all == frozenset({"x"})
assert graph.inputs.seeds == frozenset()  # No cycles
assert graph.inputs.bound == {}

# Bind values
bound = graph.bind(x=5)
assert bound.inputs.bound == {"x": 5}
assert bound.inputs.required == frozenset()  # x is now bound
assert bound.inputs.optional == frozenset({"x"})  # x has a fallback (bound value)

# === as_node() name resolution ===

# Option 1: Name from Graph constructor
named_graph = Graph(nodes=[double, add_ten], name="math_ops")
node1 = named_graph.as_node()  # Uses graph.name
assert node1.name == "math_ops"

# Option 2: Override with as_node(name=...)
node2 = named_graph.as_node(name="custom_name")  # Overrides graph.name
assert node2.name == "custom_name"

# Option 3: Provide name on as_node when Graph has no name
anonymous_graph = Graph(nodes=[double, add_ten])  # No name
node3 = anonymous_graph.as_node(name="required_name")  # Must provide
assert node3.name == "required_name"

# Error case: Neither Graph nor as_node has a name
try:
    anonymous_graph.as_node()  # No name anywhere → raises
except ValueError as e:
    assert "GraphNode requires a name" in str(e)
```

---

## GraphState

### Purpose

**Runtime storage for value versions and execution history.** Used internally by runners to track what's been computed and when to re-execute nodes.

### Class Definition

```python
@dataclass
class GraphState:
    """Runtime value storage with versioning."""
  
    values: dict[str, Any]
    """Current values by name."""
  
    versions: dict[str, int]
    """Version number for each value (increments on update)."""
  
    node_executions: dict[str, NodeExecution]
    """Last execution record per node."""
  
    history: list[NodeExecution]
    """Chronological execution history."""
```

### Properties

```python
@property
def value_names(self) -> set[str]:
    """All value names currently in state."""
    return set(self.values.keys())

def get(self, name: str, default=None) -> Any:
    """Get value by name."""
    return self.values.get(name, default)

def has(self, name: str) -> bool:
    """Check if value exists."""
    return name in self.values

def version(self, name: str) -> int:
    """Get version number for a value."""
    return self.versions.get(name, 0)
```

### Methods

```python
def set(self, name: str, value: Any) -> GraphState:
    """Set value and increment version (returns new state)."""

def is_stale(self, node: HyperNode) -> bool:
    """Check if node needs to re-execute based on input versions."""

def checkpoint(self) -> bytes:
    """Serialize state for persistence."""

@staticmethod
def from_checkpoint(data: bytes) -> GraphState:
    """Restore state from checkpoint."""
```

### Example (Internal Use)

```python
# Created and managed by runners, not user-facing
state = GraphState(
    values={"x": 5, "doubled": 10},
    versions={"x": 0, "doubled": 1},
    node_executions={...},
    history=[...]
)

# Check staleness
if state.is_stale(add_ten_node):
    # Re-execute node
    ...
```

---

## GraphResult

### Purpose

**Results from graph execution, including nested graph outputs.** Provides dict-like access to outputs.

### Class Definition

```python
@dataclass
class GraphResult:
    """Result from graph execution."""
  
    outputs: dict[str, Any | "GraphResult"]
    """Output values and nested GraphResults."""
  
    status: Literal["complete", "interrupted", "error"]
    """Execution status."""
  
    history: list[NodeExecution] | None = None
    """Execution history (if tracking enabled)."""
```

### Methods

```python
def __getitem__(self, key: str) -> Any | GraphResult:
    """Dict-like access to outputs."""
    return self.outputs[key]

def keys(self):
    """Get output names."""
    return self.outputs.keys()

def items(self):
    """Get output name-value pairs."""
    return self.outputs.items()

def __contains__(self, key: str):
    """Check if output exists."""
    return key in self.outputs
```

### Example

```python
# Nested graph execution
inner = Graph(nodes=[embed, retrieve], name="rag")
outer = Graph(nodes=[preprocess, inner.as_node(), postprocess])

result = runner.run(outer, inputs={"query": "hello"})

# Access results
result["final"]                    # Top-level output
result["rag"]                      # Nested GraphResult
result["rag"]["embedding"]         # Nested graph output
result.status                      # "complete"

# Dict-like access
assert "rag" in result
for name, value in result.items():
    print(f"{name}: {value}")
```

---

## RunResult

### Purpose

**Extended result from AsyncRunner with interrupt/checkpoint support.** Returned by `AsyncRunner.run()`.

### Class Definition

```python
@dataclass
class RunResult:
    """Result from async graph execution."""
  
    outputs: dict[str, Any]
    """Output values."""
  
    interrupted: bool
    """True if stopped at InterruptNode."""
  
    checkpoint: bytes | None
    """Serialized state for resume (if interrupted)."""
  
    run_id: str
    """Unique identifier for this execution."""
  
    interrupt_name: str | None = None
    """Name of interrupt point (if interrupted)."""
  
    interrupt_value: Any | None = None
    """Value to show user (if interrupted)."""
```

### Example

```python
from hypergraph import AsyncRunner

runner = AsyncRunner()
result = await runner.run(graph, inputs={...})

if result.interrupted:
    # Show prompt to user
    prompt = result.interrupt_value
    print(f"Paused at: {result.interrupt_name}")
  
    # Get user's response
    response = await get_user_input(prompt)
  
    # Resume
    result = await runner.run(
        graph,
        inputs={result.interrupt_name: response},
        checkpoint=result.checkpoint,
    )

# Access outputs
print(result.outputs["answer"])
```

---

## Event Types (AsyncRunner.iter())

### NodeStartEvent

```python
@dataclass
class NodeStartEvent:
    run_id: str
    node_name: str
    inputs: dict[str, Any]
    timestamp: float
```

### NodeEndEvent

```python
@dataclass
class NodeEndEvent:
    run_id: str
    node_name: str
    outputs: Any
    duration_ms: float
    cached: bool
    timestamp: float
```

### StreamingChunkEvent

```python
@dataclass
class StreamingChunkEvent:
    run_id: str
    node_name: str
    chunk: str | Any
    chunk_index: int
    timestamp: float
```

### InterruptEvent

```python
@dataclass
class InterruptEvent:
    run_id: str
    interrupt_name: str
    value: Any              # Value to show user
    response_param: str     # Where to write response
    checkpoint: bytes       # State for resume
    timestamp: float
```

### RouteDecisionEvent

```python
@dataclass
class RouteDecisionEvent:
    run_id: str
    gate_name: str
    decision: str  # Target node name or "END"
    timestamp: float
```

### Example

```python
async with runner.iter(graph, inputs={...}) as run:
    async for event in run:
        match event:
            case NodeStartEvent(node_name=name):
                print(f"Starting: {name}")
        
            case NodeEndEvent(node_name=name, duration_ms=ms):
                print(f"Finished: {name} in {ms}ms")
        
            case StreamingChunkEvent(chunk=chunk):
                print(chunk, end="")
        
            case InterruptEvent(interrupt_name=name, value=prompt):
                print(f"Paused at: {name}")
                # Handle interrupt
```

---

## Type Hierarchy Summary

```
HyperNode (ABC)
├── FunctionNode (regular function wrapper, @node decorator)
├── GateNode (ABC) - base for routing gates
│   ├── RouteNode (multi-way gate, func returns str)
│   ├── BranchNode (binary gate, func returns bool)
│   └── TypeRouteNode (declarative type-based routing)
├── InterruptNode (pause point)
└── GraphNode (nested graph as node)
    └── Created by Graph.as_node()

Graph (structure definition)
├── InputSpec (input parameter specification, returned by .inputs)
└── GraphState (runtime values)
    └── GraphResult (execution results)
        └── RunResult (async execution with interrupts)
```

---

## Runner Compatibility

### Graph Feature Support

Runners validate graph compatibility before execution using the boolean properties:

| Feature | `SyncRunner` | `AsyncRunner` | `DaftRunner` |
|---------|--------------|---------------|--------------|
| DAG execution | ✅ | ✅ | ✅ |
| Cycles (`has_cycles`) | ✅ | ✅ | ❌ |
| Gates (`has_gates`) | ✅ | ✅ | ❌ |
| Interrupts (`has_interrupts`) | ❌ | ✅ | ❌ |
| Async nodes (`has_async_nodes`) | ❌ | ✅ | ✅ |
| `.iter()` streaming | ❌ | ✅ | ❌ |
| `.map()` batch | ✅ | ✅ | ✅ |
| `.map()` with interrupts | ❌ | ❌ | ❌ |

### Validation

Runners check compatibility at the start of execution:

```python
class SyncRunner:
    def _validate_graph(self, graph: Graph) -> None:
        if graph.has_interrupts:
            raise IncompatibleRunnerError(
                "Graph has interrupts but SyncRunner doesn't support them. "
                "Use AsyncRunner instead."
            )
        if graph.has_async_nodes:
            raise IncompatibleRunnerError(
                "Graph has async nodes but SyncRunner doesn't support them. "
                "Use AsyncRunner instead."
            )

class DaftRunner:
    def _validate_graph(self, graph: Graph) -> None:
        if graph.has_cycles:
            raise IncompatibleRunnerError(
                "Graph has cycles but DaftRunner requires a DAG. "
                "Use SyncRunner or AsyncRunner instead."
            )
        if graph.has_gates:
            raise IncompatibleRunnerError(
                "Graph has gates but DaftRunner doesn't support dynamic routing. "
                "Use SyncRunner or AsyncRunner instead."
            )
        if graph.has_interrupts:
            raise IncompatibleRunnerError(
                "Graph has interrupts but DaftRunner doesn't support them. "
                "Use AsyncRunner instead."
            )
```

### Interrupt Handling with AsyncRunner

**AsyncRunner supports interrupts in `.run()` and `.iter()`, but NOT in `.map()`.**

#### Using `.run()` - Pause and Resume

```python
result = await runner.run(graph, inputs={"query": "hello"})

if result.interrupted:
    # Execution paused at InterruptNode
    prompt = result.interrupt_value

    # Get user response (your application logic)
    response = await get_user_response(prompt)

    # Resume with checkpoint
    result = await runner.run(
        graph,
        inputs={result.interrupt_name: response},
        checkpoint=result.checkpoint,
    )

# Now complete
assert not result.interrupted
print(result.outputs["answer"])
```

#### Using `.iter()` - Handle Inline

```python
async with runner.iter(graph, inputs={"query": "hello"}) as run:
    async for event in run:
        match event:
            case StreamingChunkEvent(chunk=chunk):
                print(chunk, end="")

            case InterruptEvent(value=prompt, response_param=target):
                # Handle interrupt inline
                response = await get_user_response(prompt)
                run.respond(target, response)
                # Iteration continues automatically

            case NodeEndEvent(node_name=name):
                print(f"Completed: {name}")

    # After iteration, result is available
    print(run.result.outputs)
```

#### Using `.run()` with Handlers

Pass handlers per-call for automatic interrupt resolution:

```python
result = await runner.run(
    graph,
    inputs={"query": "hello"},
    interrupt_handlers={
        "approval": handle_approval,
        "topic_selection": handle_topic,
    },
)

# If all interrupts have handlers → runs to completion
# If some handlers missing → returns interrupted at first unhandled
```

Handler signature:

```python
async def handle_approval(prompt: ApprovalPrompt) -> ApprovalResponse:
    """
    Receives: The value from InterruptNode's input_param
    Returns: The value to write to InterruptNode's response_param
    """
    choice = await show_dialog(prompt.message, prompt.options)
    return ApprovalResponse(choice=choice)
```

#### `.map()` Does Not Support Interrupts

Batch processing with `.map()` cannot handle interrupts:

```python
# This will raise an error at validation time
if graph.has_interrupts:
    raise IncompatibleRunnerError(
        "Graph has interrupts but .map() doesn't support them.\n"
        "Use .run() or .iter() for graphs with interrupts."
    )
```

Rationale: Each batch item would potentially pause at different points, making the execution model complex and confusing. Use `.run()` in a loop if you need batch processing with interrupts.

---

## Common Patterns

### Type Checking

```python
from hypergraph import FunctionNode, RouteNode, BranchNode, InterruptNode

def process_node(node: HyperNode):
    """Handle different node types."""
    if isinstance(node, InterruptNode):
        # Handle interrupt
        ...
    elif isinstance(node, (RouteNode, BranchNode)):
        # Handle gate
        ...
    elif isinstance(node, FunctionNode):
        # Handle regular node
        ...
```

### Creating Nodes Programmatically

```python
def create_nodes(config: dict) -> list[HyperNode]:
    """Create nodes from configuration."""
    nodes = []

    for item in config["nodes"]:
        if item["type"] == "transform":
            fn_node = FunctionNode(
                source=item["func"],
                output_name=item["output"],
            )
            nodes.append(fn_node)
        elif item["type"] == "gate":
            gate_node = RouteNode(
                func=item["func"],
                targets=item["targets"],
            )
            nodes.append(gate_node)

    return nodes
```

### Working with Results

```python
def extract_all_values(result: GraphResult, prefix="") -> dict[str, Any]:
    """Recursively extract all values from nested results."""
    flat = {}
  
    for key, value in result.items():
        full_key = f"{prefix}{key}" if prefix else key
    
        if isinstance(value, GraphResult):
            # Recurse into nested result
            nested = extract_all_values(value, f"{full_key}/")
            flat.update(nested)
        else:
            flat[full_key] = value
  
    return flat
```
