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