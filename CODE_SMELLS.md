# Code Smells Analysis - RouteNode Implementation

Analysis of commits: `3204714` (feat: add RouteNode) and `323cd8d` (fix: remove test skips)

## Summary

The RouteNode implementation is generally well-structured and follows good design principles. However, there are several code smells and design issues worth addressing:

---

## 1. Duplicate Code - Sync/Async Executors (Severity: HIGH)

**Location**:
- `src/hypergraph/runners/sync/executors/route_node.py`
- `src/hypergraph/runners/async_/executors/route_node.py`

**Issue**: The two executors contain **100% identical validation logic** (109 lines duplicated):
- `_validate_routing_decision()` (lines 61-107 in sync, 65-110 in async)
- `_validate_single_target()` (lines 109-122 in sync, 113-125 in async)

This violates the DRY principle and creates a maintenance burden - any bug fix or enhancement must be applied in two places.

**Why it's problematic**:
- Changes to validation logic require synchronizing two files
- Risk of divergence where one file gets updated and the other doesn't
- Doubles the testing surface area
- The async executor doesn't actually need to be async (the routing function is always sync)

**Recommendation**:
Extract shared validation logic to a common module:

```python
# src/hypergraph/runners/_shared/validation.py
def validate_routing_decision(node: RouteNode, decision: Any) -> None:
    """Shared validation for sync and async executors."""
    # ... validation logic ...

def validate_single_target(node: RouteNode, target: Any) -> None:
    """Validate a single target is in the valid targets list."""
    # ... validation logic ...
```

Then both executors import and use these functions. This reduces ~200 lines of duplication to ~10 lines of imports.

**Reference**: Code smell #7 - Duplicate code

---

## 2. Feature Envy - `map_inputs_to_func_params()` (Severity: MEDIUM)

**Location**: `src/hypergraph/runners/_shared/helpers.py:216-267`

**Issue**: The function `map_inputs_to_func_params()` reaches deeply into `node._rename_history` and manipulates it extensively. It knows too much about the internal structure of FunctionNode.

```python
def map_inputs_to_func_params(node: HyperNode, inputs: dict[str, Any]) -> dict[str, Any]:
    # ...
    # Directly accessing and processing node's private _rename_history
    input_entries = [e for e in node._rename_history if e.kind == "inputs"]
    batches: dict[int | None, list] = {}
    for entry in input_entries:
        batches.setdefault(entry.batch_id, []).append(entry)
    # ... 30 more lines manipulating rename history ...
```

**Why it's problematic**:
- The function envies FunctionNode's rename tracking data
- Violates encapsulation - should be FunctionNode's responsibility
- Makes testing harder (need to mock internal state)
- Couples the helper to FunctionNode's implementation details

**Recommendation**:
Move this logic into a method on FunctionNode (or HyperNode base class):

```python
class FunctionNode(HyperNode):
    def map_inputs_to_params(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Map renamed inputs back to original function parameters."""
        # ... existing logic from helpers.py ...
```

Then the helper becomes:
```python
def map_inputs_to_func_params(node: HyperNode, inputs: dict[str, Any]) -> dict[str, Any]:
    if hasattr(node, 'map_inputs_to_params'):
        return node.map_inputs_to_params(inputs)
    return inputs
```

**Reference**: Code smell #15 - Feature envy

---

## 3. `hasattr()` for Feature Detection (Severity: LOW)

**Location**: `src/hypergraph/runners/_shared/helpers.py:233`

**Issue**: Uses `isinstance()` to check if node supports rename mapping:

```python
from hypergraph.nodes.function import FunctionNode

if not isinstance(node, FunctionNode):
    return inputs
```

While not strictly wrong, this creates a tight coupling between the helper and FunctionNode. If RouteNode or other node types need rename support, this requires code changes.

**Why it's problematic**:
- Violates Open/Closed Principle
- Type-based branching instead of capability-based
- Makes extending rename support to other node types harder

**Recommendation**:
Use duck typing with a protocol or abstract method:

```python
# In base.py
class HyperNode(ABC):
    def map_inputs_to_params(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """Map renamed inputs to original parameter names. Override if needed."""
        return inputs  # Default: no mapping

# Then in helpers.py
def map_inputs_to_func_params(node: HyperNode, inputs: dict[str, Any]) -> dict[str, Any]:
    return node.map_inputs_to_params(inputs)  # Polymorphic call
```

**Reference**: Code smell #2 - hasattr/isinstance for feature detection

---

## 4. Long Method - `RouteNode.__init__()` (Severity: MEDIUM)

**Location**: `src/hypergraph/nodes/gate.py:124-216`

**Issue**: The `__init__` method is 92 lines long and does ~8 distinct things:
1. Validate function type (async/generator check)
2. Normalize targets (list vs dict)
3. Validate targets non-empty
4. Deduplicate targets
5. Validate fallback compatibility
6. Add fallback to targets
7. Set instance attributes
8. Apply input renames

**Why it's problematic**:
- Hard to test individual validation rules
- Hard to understand what the constructor does at a glance
- Multiple responsibilities (violates SRP)

**Recommendation**:
Extract validation and normalization into helper functions:

```python
class RouteNode(GateNode):
    def __init__(self, func, targets, *, fallback=None, ...):
        _validate_routing_function(func)
        target_list, descriptions = _normalize_targets(targets)
        _validate_targets(target_list, func.__name__)
        target_list = _add_fallback_to_targets(target_list, fallback, multi_target, func.__name__)

        # Now just set attributes
        self.func = func
        self.targets = target_list
        # ... etc
```

Each extracted function would be 5-15 lines and testable in isolation.

**Reference**: Code smell #9 - Long method/function

---

## 5. Magic Strings - "END" Sentinel (Severity: LOW)

**Location**:
- `src/hypergraph/runners/sync/executors/route_node.py:76`
- `src/hypergraph/runners/async_/executors/route_node.py:80`

**Issue**: String literal `"END"` used for detection:

```python
if decision == "END" and decision is not END:
    warnings.warn(
        f"Gate '{node.name}' returned string 'END' instead of END sentinel.\n"
        # ...
    )
```

**Why it's problematic**:
- Magic string comparison
- If someone has a legitimate target named "END" (allowed by validation), this warning fires
- Fragile detection logic

**Recommendation**:
Either:
1. **Prohibit** string targets named "END" during validation, OR
2. Remove this check entirely (it's trying to be helpful but may cause false positives)

Given that `"END"` is a valid identifier and could be a reasonable node name in some domains, option #2 is cleaner. Users who accidentally use string `"END"` will get a validation error anyway (target not found).

**Reference**: Code smell #10 - Magic numbers/strings

---

## 6. Conditional-Heavy Validation (Severity: LOW)

**Location**: `src/hypergraph/graph/validation.py:_validate_routing_decision()`

**Issue**: The validation function has nested conditionals:

```python
if node.multi_target:
    if decision is not None and not isinstance(decision, list):
        raise TypeError(...)
    if decision is not None:
        for target in decision:
            _validate_single_target(node, target)
else:
    if isinstance(decision, list):
        raise TypeError(...)
    if decision is not None:
        _validate_single_target(node, decision)
```

**Why it's problematic**:
- Nested conditionals reduce readability
- Could be flattened with guard clauses

**Recommendation**:
Use early returns and guard clauses:

```python
def _validate_routing_decision(node, decision):
    _check_for_string_end_confusion(node, decision)

    if decision is None:
        return  # None is always valid (fallback or no-op)

    if node.multi_target:
        _validate_multi_target_decision(node, decision)
    else:
        _validate_single_target_decision(node, decision)

def _validate_multi_target_decision(node, decision):
    if not isinstance(decision, list):
        raise TypeError(...)
    for target in decision:
        _validate_single_target(node, target)

def _validate_single_target_decision(node, decision):
    if isinstance(decision, list):
        raise TypeError(...)
    _validate_single_target(node, decision)
```

**Reference**: Code smell #11 - Nested conditionals

---

## 7. Utilities Burying Main Class (Severity: LOW)

**Location**: `src/hypergraph/nodes/gate.py`

**Issue**: The file structure is:
1. END sentinel and metaclass (52 lines)
2. Type alias (2 lines)
3. GateNode base (11 lines)
4. RouteNode (177 lines)
5. `@route` decorator (52 lines)

When opening the file, the user sees metaclass implementation details before the main abstractions.

**Why it's problematic**:
- Main classes (GateNode, RouteNode) are buried below implementation details
- Hard to get an overview of what the module provides
- The metaclass for END is clever but not central to understanding gates

**Recommendation**:
Reorder the file:

```python
# 1. Public API first
class GateNode(HyperNode): ...
class RouteNode(GateNode): ...
def route(...): ...

# 2. END sentinel (public but special)
class _ENDMeta(type): ...
class END(metaclass=_ENDMeta): ...

# Or move END to a separate file if it's used elsewhere
```

Alternatively, move `_ENDMeta` to a `_sentinels.py` file if END is used in multiple places.

**Reference**: Code smell #22 - Utilities burying the main class

---

## 8. Long Parameter List - `RouteNode.__init__()` (Severity: LOW)

**Location**: `src/hypergraph/nodes/gate.py:124-134`

**Issue**: The constructor takes 6 parameters (1 positional + 5 keyword-only):

```python
def __init__(
    self,
    func: Callable,
    targets: TargetsSpec,
    *,
    fallback: str | type[END] | None = None,
    multi_target: bool = False,
    cache: bool = False,
    name: str | None = None,
    rename_inputs: dict[str, str] | None = None,
) -> None:
```

**Why it's potentially problematic**:
- Many parameters can be hard to remember
- Easy to mix up boolean flags

**Why it's actually okay here**:
- All parameters are keyword-only (enforced by `*`)
- Each parameter represents a distinct, independent configuration option
- Creating a config object would be overengineering for this use case
- The `@route` decorator provides a nicer interface anyway

**Verdict**: This is a borderline case. Given that:
1. The decorator hides the complexity for most users
2. All params are keyword-only
3. Each param has a clear, distinct purpose

This is **acceptable** and not a smell requiring action. Including it here for completeness.

**Reference**: Code smell #12 - Long parameter list

---

## Positive Design Patterns Observed

The implementation demonstrates several good practices worth highlighting:

1. **Single Responsibility** - Each executor class has one job (execute a node type)
2. **Descriptive error messages** - All validation errors include "How to fix" guidance
3. **Immutability** - Graph.bind() returns a new graph instead of mutating
4. **Type safety** - Comprehensive type hints throughout
5. **Separation of concerns** - Validation logic separated from execution logic
6. **Guard clauses** - Used in many places (e.g., `_is_node_ready()`)
7. **Proper use of ABC** - GateNode as abstract base with required attributes
8. **Dependency Injection** - Executors receive state as parameters

---

## Recommendations Priority

### High Priority
1. **Extract duplicate validation logic** from sync/async executors (DRY violation)

### Medium Priority
2. **Move rename mapping logic** to FunctionNode method (feature envy)
3. **Extract helpers from RouteNode.__init__()** (long method)

### Low Priority
4. Use duck typing instead of `isinstance()` for rename support
5. Reconsider string "END" detection logic
6. Flatten validation conditionals with guard clauses
7. Reorder gate.py file structure

---

## Conclusion

The RouteNode implementation is solid and production-ready. The main issue is **code duplication** between sync/async executors, which should be addressed to prevent future maintenance issues. The other smells are minor and can be addressed opportunistically during future refactoring.

The code demonstrates good architectural thinking, especially around validation, error messages, and separation of concerns. The graph validation logic is particularly well-designed with comprehensive edge case handling.
