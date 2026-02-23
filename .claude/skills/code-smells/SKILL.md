---
description: Surface code smells and issues in design.
user_invocable: true
model: sonnet
---

Your goal is to go over a subset (or all) of the codebase and find code smells and issues in the design of the library.
Come up with a markdown file that surfaces these issues with a short explanation about why they're not recommended and what is.

<code_smells>
Recognize and avoid these anti-patterns. Each smell indicates a design problem worth addressing.
</code_smells>

## General Code Smells

1. **sys.path.append hacking** - Runtime import-path tweaks usually mean the project isn't packaged correctly (wrong working dir, missing install, ad-hoc scripts). Use proper pyproject.toml layout and install the project (editable) instead of mutating `sys.path`.

2. **hasattr/isinstance for feature detection** - Using `hasattr()` or `isinstance()` to decide if a feature can be used indicates a broken abstraction.

3. **Descriptive docstrings** - Docstrings shouldn't describe the code (e.g., "checks for 1, 2, 3... then does 4"). This creates WET documentation that drifts from reality.

4. **Conditional-heavy dataclasses** - A class/dataclass with multiple conditional fields (e.g., RunResult with paused_reason, pause_node, pause_value when pausing hasn't necessarily happened) signals a need for separate types.

5. **Unguarded Optional access** - Optional values without existence/type assertions cause linting errors and runtime bugs (e.g., `if "ok" in response.cited_answer` when `cited_answer: str | None`).

6. **God object** - One class/module does everything (business logic, I/O, validation, persistence). Split by responsibility.

7. **Duplicate code** - Copy/paste logic that drifts over time. Extract helpers, consolidate flows.

8. **Stateless classes** - A class with only one or two functions and no state should probably be function(s).

9. **Long method/function** - "Does 10 steps" and is hard to test. Break into smaller functions with clear names.

10. **Magic numbers/strings** - Unexplained literals (`1.25`, `"PENDING_V2"`) sprinkled around. Use constants/enums/config.

11. **Nested conditionals** - Logic becomes a maze. Prefer guard clauses, `all()/any()`, polymorphism/strategies.

12. **Long parameter list** - Many arguments often signals missing abstraction. Group into dataclass/value object.

13. **Flag arguments** - `do_thing(x, fast=True, verbose=False, mode="B")` followed by `if` piles. Split into separate functions or use Strategy.

14. **Shotgun surgery** - One change requires edits in many files. Centralize the knowledge; reduce coupling.

15. **Feature envy** - Method lives on class A but mostly manipulates class B's data. Move behavior closer to the data.

16. **Inappropriate intimacy** - Classes/functions reach into each other's internals. Refactor boundaries.

17. **Primitive obsession** - Using raw strings/ints/dicts where a domain type would prevent bugs (e.g., `user_id: str` everywhere instead of `UserId`).

18. **Data clumps** - The same group of variables travels together (`x, y, z` or `start, end, tz`) across many functions. Bundle into an object.

19. **Dead code** - Old paths, unused functions, "maybe later" scaffolding. Delete it; rely on version control.

20. **Speculative generality (YAGNI)** - Building frameworks/abstractions "just in case". Keep it simple until requirements force complexity.

21. **What-comments instead of why-comments** - Comments that explain what code does (or drift from reality). Aim for self-documenting code; keep only "why"/constraints.

22. **Utilities burying the main class** - When opening a file, the primary class/function should be visible first, not helper utilities. Move internal helpers to a private module (e.g., `_helpers.py`).

## Python-Specific Smells

1. **Bare except** - `except:` hides real failures and catches `KeyboardInterrupt`/`SystemExit`. Catch specific exceptions (or at least `Exception`) and keep `try` blocks tight.

2. **Mutable default arguments** - `def f(x, cache=[])` causes state to leak across calls. Use `None` + initialize inside.

3. **Global state** - Functions relying on globals or module-level singletons make behavior implicit and tests painful. Pass dependencies explicitly.

<design_principles>
These principles guide code structure decisions. Follow them to produce maintainable, testable Python code.
</design_principles>

# Planning

- Separate what's important to design now (even futuristic things that affect the present)
- What can be added later without breaking things

## Core Philosophy: The Software Designer Mindset

**Pragmatism over dogma**. Principles and patterns are tools, not rules.

### The Foundational Triad

1. **YAGNI (You Ain't Gonna Need It)**: Don't write unnecessary code. Only implement functionality required now. Add complexity only when you have no other choice.
2. **DRY (Don't Repeat Yourself)**: Reduce code and logic repetition. Duplication is the enemy of maintainability.
3. **KISS (Keep It Simple)**: Simplicity is robust, complexity is fragile. Simplicity is the ultimate sophistication.

### The Primary Metrics

**High Cohesion**: Elements inside a module, class, or function belong together. Do one thing well.

**Low Coupling**: Minimize dependencies on internal implementation details. Depend on abstractions, not concretions.

All design patterns and SOLID principles ultimately serve these two goals.

## Code Structure Hierarchy

Follow this preference order:

### 1. Default to Functions

- ✓ Keep short and focused (high cohesion)
- ✓ Use meaningful, specific names
- ✓ Document thought process, not just code behavior
- ✗ Too many parameters (use dataclass)
- ✗ Boolean flags (make two separate functions)
- ✗ Error handling as logic (use exceptions for exceptional cases)

### 2. Use dataclass for State

```
from dataclasses import dataclass

@dataclass
class UserData:
    name: str
    email: str
    age: int

```

Less boilerplate, more clarity than plain classes, tuples, or dicts.

### 3. Use class for State + Behavior

Only when you need to co-locate data and the methods that operate on it.

**Rules for classes**:

- ✓ Keep classes small
- ✓ Use encapsulation (hide internals)
- ✓ Use dependency injection (inject dependencies in `__init__`)
- ✗ Don't use `self` if method doesn't access instance state (make it a function or `@staticmethod`)

## SOLID Principles (Python-Adapted)

### S - Single Responsibility Principle

**A class/function should have one reason to change.**

Apply at ALL levels: functions, classes, modules.

**Bad**: Order class handles both order items AND payment logic **Good**: Separate `Order` and `PaymentHandler` classes

### O - Open/Closed Principle

**Open for extension, closed for modification.**

Primary tool for eliminating long if-elif chains.

**Pattern**: Use abstract base classes and subclasses

```
from abc import ABC, abstractmethod

class PaymentHandler(ABC):
    @abstractmethod
    def pay(self, order: Order) -> None:
        pass

class DebitPaymentHandler(PaymentHandler):
    def pay(self, order: Order) -> None:
        # Implementation
        pass

class PayPalPaymentHandler(PaymentHandler):
    def pay(self, order: Order) -> None:
        # Implementation
        pass

```

Add new payment types by creating new classes, not modifying existing ones.

### L - Liskov Substitution Principle

**Objects of a superclass should be replaceable with subclasses without breaking the program.**

**Test**: If you need `isinstance()` checks, your abstraction is broken.

**Fix**: Move varying data (like security codes, emails) to constructor, keep method signatures identical.

### I - Interface Segregation Principle

**Clients shouldn't depend on interfaces they don't use.**

Powerful argument for **composition over inheritance**.

**Bad**: Adding `auth_2fa_sms()` to base `PaymentHandler` (forces all subclasses to implement it) **Good**: Create separate `Authorizer` interface, inject only where needed

### D - Dependency Inversion Principle

**High-level modules shouldn't depend on low-level modules. Both depend on abstractions.**

**Most important architectural principle.** Key to testability and the "Business Seam" pattern.

```
# Bad: Depends on concrete implementation
class PaymentProcessor:
    def __init__(self):
        self.sms_sender = SMSService()  # Hard-coded dependency

# Good: Depends on abstraction
class PaymentProcessor:
    def __init__(self, authorizer: Authorizer):
        self.authorizer = authorizer  # Injected dependency

```

**Pythonic alternatives**: Often a dictionary of functions or first-class functions suffice instead of full class hierarchies.