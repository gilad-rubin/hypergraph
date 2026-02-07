# Gates API Reference

Gate nodes control execution flow. They make routing decisions but produce no data outputs.

- **IfElseNode** - Binary gate for true/false routing decisions
- **@ifelse** - Decorator to create an IfElseNode from a boolean function
- **RouteNode** - Routes execution to target nodes based on a function's return value
- **@route** - Decorator to create a RouteNode from a function
- **END** - Sentinel indicating execution should terminate
- **GateNode** - Abstract base class for all gate types

## @ifelse Decorator

Create an IfElseNode from a boolean function. Simplest way to branch on true/false.

### Signature

```python
def ifelse(
    when_true: str | type[END],
    when_false: str | type[END],
    *,
    cache: bool = False,
    hide: bool = False,
    default_open: bool = True,
    name: str | None = None,
    rename_inputs: dict[str, str] | None = None,
    emit: str | tuple[str, ...] | None = None,
    wait_for: str | tuple[str, ...] | None = None,
) -> Callable[[Callable[..., bool]], IfElseNode]: ...
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `when_true` | `str \| END` | required | Target when function returns `True` |
| `when_false` | `str \| END` | required | Target when function returns `False` |
| `cache` | `bool` | `False` | Whether to cache routing decisions |
| `hide` | `bool` | `False` | Whether to hide from visualization |
| `default_open` | `bool` | `True` | If True, targets may execute before the gate runs. If False, targets are blocked until the gate executes. |
| `name` | `str \| None` | `None` | Node name (default: function name) |
| `rename_inputs` | `dict \| None` | `None` | Mapping to rename inputs `{old: new}` |
| `emit` | `str \| tuple \| None` | `None` | Ordering-only output(s). Auto-produced when the gate runs |
| `wait_for` | `str \| tuple \| None` | `None` | Ordering-only input(s). Gate waits until these values are fresh |

### Return Value

The decorated function must return exactly `True` or `False` (not truthy/falsy values):
- `True` - Routes to `when_true` target
- `False` - Routes to `when_false` target

### Basic Usage

```python
from hypergraph import ifelse, END

@ifelse(when_true="process", when_false="skip")
def is_valid(data: dict) -> bool:
    return data.get("valid", False)
```

### With END

```python
@ifelse(when_true="continue", when_false=END)
def should_continue(count: int) -> bool:
    return count < 10  # False terminates execution
```

### Input Renaming

```python
@ifelse(when_true="yes", when_false="no", rename_inputs={"x": "input_value"})
def is_positive(x: int) -> bool:
    return x > 0
```

---

## IfElseNode Class

Binary gate that routes based on boolean decision. Use `@ifelse` decorator for most cases.

### Constructor

```python
def __init__(
    self,
    func: Callable[..., bool],
    when_true: str | type[END],
    when_false: str | type[END],
    *,
    cache: bool = False,
    hide: bool = False,
    default_open: bool = True,
    name: str | None = None,
    rename_inputs: dict[str, str] | None = None,
) -> None: ...
```

### Properties

#### `name: str`

Public node name.

#### `inputs: tuple[str, ...]`

Input parameter names from function signature.

#### `outputs: tuple[str, ...]`

Always empty tuple. Gates produce no data outputs.

#### `targets: list[str | type[END]]`

Always `[when_true, when_false]` (2 elements).

```python
@ifelse(when_true="process", when_false=END)
def check(x: int) -> bool:
    return x > 0

print(check.targets)  # ["process", <class 'END'>]
```

#### `when_true: str | type[END]`

Target when function returns `True`.

#### `when_false: str | type[END]`

Target when function returns `False`.

#### `descriptions: dict[bool, str]`

Fixed `{True: "True", False: "False"}`.

#### `cache: bool`

Whether routing decisions are cached.

#### `func: Callable`

The wrapped boolean function.

#### `is_async: bool`

Always `False`. Routing functions must be synchronous.

#### `is_generator: bool`

Always `False`. Routing functions cannot be generators.

#### `definition_hash: str`

SHA256 hash of function source code.

### Methods

#### `has_default_for(param: str) -> bool`

Check if parameter has a default value.

#### `get_default_for(param: str) -> Any`

Get default value for a parameter. Raises `KeyError` if no default.

#### `with_name(name: str) -> IfElseNode`

Return a new node with a different name.

#### `with_inputs(mapping=None, /, **kwargs) -> IfElseNode`

Return a new node with renamed inputs.

#### `__call__(*args, **kwargs) -> bool`

Call the boolean function directly.

#### `__repr__() -> str`

Informative string representation.

```python
print(repr(check))
# IfElseNode(check, true=process, false=END)
```

---

## @route Decorator

Create a RouteNode from a routing function.

### Signature

```python
def route(
    targets: list[str | type[END]] | dict[str | type[END], str],
    *,
    fallback: str | type[END] | None = None,
    multi_target: bool = False,
    cache: bool = False,
    hide: bool = False,
    default_open: bool = True,
    name: str | None = None,
    rename_inputs: dict[str, str] | None = None,
    emit: str | tuple[str, ...] | None = None,
    wait_for: str | tuple[str, ...] | None = None,
) -> Callable[[Callable], RouteNode]: ...
```

### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `targets` | `list` or `dict` | required | Valid target node names. Dict values are descriptions for documentation. |
| `fallback` | `str \| END \| None` | `None` | Default target if function returns `None` |
| `multi_target` | `bool` | `False` | If `True`, function returns list of targets |
| `cache` | `bool` | `False` | Whether to cache routing decisions |
| `hide` | `bool` | `False` | Whether to hide from visualization |
| `default_open` | `bool` | `True` | If True, targets may execute before the gate runs. If False, targets are blocked until the gate executes. |
| `name` | `str \| None` | `None` | Node name (default: function name) |
| `rename_inputs` | `dict \| None` | `None` | Mapping to rename inputs `{old: new}` |
| `emit` | `str \| tuple \| None` | `None` | Ordering-only output(s). Auto-produced when the gate runs |
| `wait_for` | `str \| tuple \| None` | `None` | Ordering-only input(s). Gate waits until these values are fresh |

### Return Value

The decorated function should return:
- `str` - Target node name to activate
- `END` - Terminate execution along this path
- `None` - Use fallback (if set) or activate no targets
- `list[str | END]` - Multiple targets (only with `multi_target=True`)

### Basic Usage

```python
from hypergraph import route, END

@route(targets=["process", "skip", END])
def decide(x: int) -> str:
    if x == 0:
        return END
    return "process" if x > 0 else "skip"
```

### With Descriptions

```python
@route(targets={
    "fast_path": "Use cached response for simple queries",
    "full_rag": "Full retrieval for complex queries",
    END: "Terminate if query is invalid",
})
def route_query(query_type: str) -> str:
    ...
```

### With Fallback

```python
@route(targets=["premium", "standard"], fallback="standard")
def route_by_tier(user_tier: str | None) -> str | None:
    if user_tier == "premium":
        return "premium"
    return None  # Falls back to "standard"
```

### Multi-Target Mode

```python
@route(targets=["notify", "log", "alert"], multi_target=True)
def choose_actions(severity: str) -> list[str]:
    actions = ["log"]  # Always log
    if severity == "critical":
        actions.extend(["notify", "alert"])
    return actions
```

**Note**: When `multi_target=True`, target nodes must have unique output names.

---

## RouteNode Class

Concrete gate that routes to target nodes based on a routing function.

### Constructor

```python
def __init__(
    self,
    func: Callable[..., str | type[END] | list[str | type[END]] | None],
    targets: list[str | type[END]] | dict[str | type[END], str],
    *,
    fallback: str | type[END] | None = None,
    multi_target: bool = False,
    cache: bool = False,
    hide: bool = False,
    default_open: bool = True,
    name: str | None = None,
    rename_inputs: dict[str, str] | None = None,
) -> None: ...
```

Parameters are the same as `@route`. Use the decorator for most cases.

### Properties

#### `name: str`

Public node name.

```python
@route(targets=["a", "b"])
def decide(x: int) -> str:
    return "a"

print(decide.name)  # "decide"
```

#### `inputs: tuple[str, ...]`

Input parameter names from function signature.

```python
@route(targets=["a", "b"])
def decide(x: int, threshold: float = 0.5) -> str:
    return "a" if x > threshold else "b"

print(decide.inputs)  # ("x", "threshold")
```

#### `outputs: tuple[str, ...]`

Always empty tuple. Gates produce no data outputs.

```python
print(decide.outputs)  # ()
```

#### `targets: list[str | type[END]]`

List of valid target names.

```python
@route(targets=["process", END])
def decide(x: int) -> str:
    return "process"

print(decide.targets)  # ["process", <class 'END'>]
```

#### `descriptions: dict[str | type[END], str]`

Target descriptions (empty if not provided).

```python
@route(targets={"a": "First option", "b": "Second option"})
def decide(x: int) -> str:
    return "a"

print(decide.descriptions)  # {"a": "First option", "b": "Second option"}
```

#### `fallback: str | type[END] | None`

Default target when function returns `None`.

```python
@route(targets=["a", "b"], fallback="b")
def decide(x: int) -> str | None:
    return None

print(decide.fallback)  # "b"
```

#### `multi_target: bool`

Whether function returns list of targets.

```python
@route(targets=["a", "b"], multi_target=True)
def decide(x: int) -> list[str]:
    return ["a", "b"]

print(decide.multi_target)  # True
```

#### `cache: bool`

Whether routing decisions are cached.

```python
@route(targets=["a", "b"], cache=True)
def decide(x: int) -> str:
    return "a"

print(decide.cache)  # True
```

#### `func: Callable`

The wrapped routing function.

```python
@route(targets=["a", "b"])
def decide(x: int) -> str:
    return "a"

print(decide.func.__name__)  # "decide"
```

#### `is_async: bool`

Always `False`. Routing functions must be synchronous.

#### `is_generator: bool`

Always `False`. Routing functions cannot be generators.

#### `definition_hash: str`

SHA256 hash of function source code.

```python
print(len(decide.definition_hash))  # 64
```

### Methods

#### `has_default_for(param: str) -> bool`

Check if parameter has a default value.

```python
@route(targets=["a", "b"])
def decide(x: int, threshold: float = 0.5) -> str:
    return "a"

print(decide.has_default_for("x"))          # False
print(decide.has_default_for("threshold"))  # True
```

#### `get_default_for(param: str) -> Any`

Get default value for a parameter.

```python
print(decide.get_default_for("threshold"))  # 0.5
decide.get_default_for("x")  # Raises KeyError
```

#### `with_name(name: str) -> RouteNode`

Return a new node with a different name.

```python
renamed = decide.with_name("router")
print(renamed.name)  # "router"
print(decide.name)   # "decide" (unchanged)
```

#### `with_inputs(mapping=None, /, **kwargs) -> RouteNode`

Return a new node with renamed inputs.

```python
adapted = decide.with_inputs(x="value", threshold="cutoff")
print(adapted.inputs)  # ("value", "cutoff")
```

#### `__call__(*args, **kwargs)`

Call the routing function directly.

```python
result = decide(5)
print(result)  # "a"
```

#### `__repr__() -> str`

Informative string representation.

```python
print(repr(decide))
# RouteNode(decide, targets=['a', 'b'])
```

---

## END Sentinel

Class indicating execution should terminate along this path.

### Usage

```python
from hypergraph import END

@route(targets=["process", END])
def decide(x: int) -> str:
    if x == 0:
        return END  # Terminate
    return "process"
```

### Properties

- `END` is a class, not an instance. Use it directly.
- Cannot be instantiated: `END()` raises `TypeError`
- String representation: `"END"`

```python
print(END)              # END
print(repr(END))        # END
END()                   # TypeError: END cannot be instantiated
```

### Checking for END

```python
if target is END:
    print("Terminating")
```

---

## GateNode Base Class

Abstract base class for all gate types. You typically won't use this directly.

### Attributes

All GateNode subclasses have:

```python
node.name: str                           # Public node name
node.inputs: tuple[str, ...]             # Input parameter names
node.outputs: tuple[str, ...]            # Always empty for gates
node.targets: list[str | type[END]]      # Valid target names
node.descriptions: dict[str | type[END], str]  # Target descriptions
node.cache: bool                         # Whether to cache decisions
```

---

## Validation Errors

### At Decoration Time

**Async functions not allowed:**

```python
@route(targets=["a"])
async def decide(x: int) -> str:  # ERROR
    return "a"

# TypeError: Routing function 'decide' cannot be async.
# Routing decisions should be fast and based on already-computed values.
```

**Generator functions not allowed:**

```python
@route(targets=["a"])
def decide(x: int):  # ERROR
    yield "a"

# TypeError: Routing function 'decide' cannot be a generator.
# Routing functions must return a single decision, not yield multiple.
```

**Empty targets:**

```python
@route(targets=[])  # ERROR
def decide(x: int) -> str:
    return "a"

# ValueError: RouteNode 'decide' must have at least one target.
```

**Fallback with multi_target:**

```python
@route(targets=["a"], fallback="a", multi_target=True)  # ERROR
def decide(x: int) -> list[str]:
    return ["a"]

# ValueError: RouteNode 'decide' cannot have both fallback and multi_target=True.
```

**IfElseNode with same targets:**

```python
@ifelse(when_true="same", when_false="same")  # ERROR
def check(x: int) -> bool:
    return x > 0

# ValueError: IfElseNode 'check' has the same target for both branches.
# when_true='same' == when_false='same'
```

**String 'END' as target:**

```python
@ifelse(when_true="END", when_false="process")  # ERROR
def check(x: int) -> bool:
    return x > 0

# ValueError: Gate 'check' has 'END' as a string target.
# Use 'from hypergraph import END' and use END directly.
```

### At Graph Build Time

**Invalid target (not a node in graph):**

```python
@route(targets=["nonexistent"])
def decide(x: int) -> str:
    return "nonexistent"

Graph([decide])
# GraphConfigError: Route target 'nonexistent' references unknown node.
```

**Self-targeting:**

```python
@route(targets=["decide"])
def decide(x: int) -> str:
    return "decide"

Graph([decide])
# GraphConfigError: RouteNode 'decide' cannot target itself.
```

**Multi-target with shared outputs:**

```python
@node(output_name="result")
def path_a(x: int) -> int:
    return x

@node(output_name="result")  # Same output name!
def path_b(x: int) -> int:
    return x

@route(targets=["path_a", "path_b"], multi_target=True)
def decide(x: int) -> list[str]:
    return ["path_a", "path_b"]

Graph([decide, path_a, path_b])
# GraphConfigError: Multiple nodes produce 'result': path_a, path_b
# With multi_target=True, target nodes must have unique output names.
```

### At Runtime

**Invalid return value:**

```python
@route(targets=["a", "b"])
def decide(x: int) -> str:
    return "c"  # Not in targets!

graph = Graph([decide, a, b])
result = runner.run(graph, {"x": 5})

# result.status == RunStatus.FAILED
# result.error: ValueError: invalid target 'c'. Valid targets: ['a', 'b']
```

**Wrong return type for multi_target:**

```python
@route(targets=["a", "b"], multi_target=True)
def decide(x: int) -> str:  # Should return list!
    return "a"

result = runner.run(graph, {"x": 5})
# result.error: TypeError: multi_target=True but returned str, expected list
```

**IfElseNode returns non-bool:**

```python
@ifelse(when_true="yes", when_false="no")
def check(x: int) -> bool:
    return 1  # Truthy, but not True!

result = runner.run(graph, {"x": 5})
# result.error: TypeError: IfElseNode 'check' must return exactly True or False, got int
```

IfElseNode strictly validates boolean returns. `1`, `"yes"`, or any truthy value that isn't `True` will fail.

---

## Complete Example

```python
from hypergraph import Graph, node, route, END, SyncRunner

# Processing nodes
@node(output_name="analysis")
def analyze(document: str) -> dict:
    return {
        "length": len(document),
        "has_code": "```" in document,
        "language": detect_language(document),
    }

# Routing node
@route(targets={
    "code_processor": "Handle documents with code blocks",
    "text_processor": "Handle plain text documents",
    END: "Skip empty documents",
})
def route_document(analysis: dict) -> str:
    if analysis["length"] == 0:
        return END
    if analysis["has_code"]:
        return "code_processor"
    return "text_processor"

# Target nodes
@node(output_name="result")
def code_processor(document: str, analysis: dict) -> str:
    return f"Processed code document ({analysis['language']})"

@node(output_name="result")
def text_processor(document: str, analysis: dict) -> str:
    return f"Processed text document ({analysis['length']} chars)"

# Build and run
graph = Graph([analyze, route_document, code_processor, text_processor])
runner = SyncRunner()

# Route to code_processor
result = runner.run(graph, {"document": "```python\nprint('hello')\n```"})
print(result["result"])  # "Processed code document (python)"

# Route to text_processor
result = runner.run(graph, {"document": "Hello, world!"})
print(result["result"])  # "Processed text document (13 chars)"

# Route to END (no result)
result = runner.run(graph, {"document": ""})
print("result" in result.values)  # False
```
