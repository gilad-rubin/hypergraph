# Quality Criteria Checklist

Single source of truth for code quality standards. Used by builders (to write clean code) and reviewers (to catch violations).

---

## 1. Code Smells

Each smell signals a design problem. Fix the root cause, not just the symptom.

### General

1. **sys.path hacking** — Runtime import-path tweaks. Fix: proper `pyproject.toml` layout + editable install.
2. **hasattr/isinstance for feature detection** — Broken abstraction. Fix: use protocols or proper polymorphism.
3. **Descriptive docstrings** — Docstrings that describe *what* code does (drifts from reality). Fix: self-documenting code + "why" comments only.
4. **Conditional-heavy dataclasses** — Multiple optional fields for different states. Fix: separate types per state.
5. **Unguarded Optional access** — Optional values used without null checks. Fix: assert or guard before access.
6. **God object** — One class does everything. Fix: split by responsibility.
7. **Duplicate code** — Copy/paste logic that drifts. Fix: extract helpers, consolidate flows.
8. **Stateless classes** — Class with no state, only methods. Fix: use plain functions.
9. **Long method/function** — Does too many things, hard to test. Fix: break into smaller named functions.
10. **Magic numbers/strings** — Unexplained literals. Fix: use constants, enums, or config.
11. **Nested conditionals** — Logic maze. Fix: guard clauses, `all()/any()`, strategies.
12. **Long parameter list** — Too many arguments. Fix: group into dataclass/value object.
13. **Flag arguments** — Boolean/mode flags with `if` piles. Fix: separate functions or Strategy pattern.
14. **Shotgun surgery** — One change requires edits in many files. Fix: centralize knowledge, reduce coupling.
15. **Feature envy** — Method manipulates another class's data. Fix: move behavior closer to the data.
16. **Inappropriate intimacy** — Classes reach into each other's internals. Fix: refactor boundaries.
17. **Primitive obsession** — Raw strings/ints where domain types prevent bugs. Fix: introduce value types.
18. **Data clumps** — Same variable group travels together. Fix: bundle into an object.
19. **Dead code** — Unused functions, old paths. Fix: delete it; version control remembers.
20. **Speculative generality (YAGNI)** — Abstractions "just in case." Fix: build only what's needed now.
21. **What-comments** — Comments explaining *what* instead of *why*. Fix: self-documenting code + why/constraint comments.
22. **Utilities burying the main class** — Helpers above the primary class. Fix: move helpers to `_helpers.py` or below.

### Python-Specific

23. **Bare except** — `except:` hides real failures. Fix: catch specific exceptions (or `Exception` at minimum).
24. **Mutable default arguments** — `def f(x, cache=[])`. Fix: use `None` + initialize inside.
25. **Global state** — Module-level singletons, implicit dependencies. Fix: pass dependencies explicitly.

---

## 2. Design Principles

### Foundational Triad

- **YAGNI** — Don't write code you don't need yet. Add complexity only when forced.
- **DRY** — Reduce repetition. Duplication is the enemy of maintainability.
- **KISS** — Simplicity is robust; complexity is fragile.

### Primary Metrics

- **High Cohesion** — Elements in a module/class/function belong together. Do one thing well.
- **Low Coupling** — Minimize dependencies on internal details. Depend on abstractions.

### Code Structure Hierarchy

Prefer in this order:
1. **Functions** — Short, focused, meaningful names. Default choice.
2. **Dataclasses** — When you need state without behavior. Less boilerplate than classes.
3. **Classes** — Only when you need state + behavior together. Keep small, use DI.

### SOLID Principles

| Principle | Rule | Test |
|-----------|------|------|
| **SRP** — Single Responsibility | One reason to change per class/function | Can you describe it without "and"? |
| **OCP** — Open/Closed | Open for extension, closed for modification | Can you add behavior without changing existing code? |
| **LSP** — Liskov Substitution | Subtypes must be drop-in replacements | Do you need `isinstance()` checks? If yes, abstraction is broken. |
| **ISP** — Interface Segregation | Don't force clients to depend on unused interfaces | Does any implementor have empty/stub methods? |
| **DIP** — Dependency Inversion | High-level modules depend on abstractions, not concretions | Are dependencies injected or hard-coded? |

---

## 3. Flat Code Rules

### Core Rule

**Max 2 levels of nesting.** If you have 2+ levels of conditional logic, extract the inner logic to a well-named helper function.

### Flattening Techniques

| Technique | When to use |
|-----------|-------------|
| **Extract to helper** | Inner logic is self-contained and benefits from a name |
| **Generator/comprehension** | Simple filter/map over nested loops |
| **Guard clause** | Skip items early (`if not x: continue`) |
| **Split concerns** | Separate data collection from processing/validation |

### Comprehensions

**Use when:** simple filter + collect, straightforward nested iteration with helper, single transformation.

**Don't use when:**
- Logic needs a name to understand → helper function
- Multiple output buckets → helper + loop
- Side effects (raising, logging) → explicit loop
- More than 1 line of conditions → helper function

**Readability threshold:** If it spans multiple lines with complex conditions, extract to a helper.
