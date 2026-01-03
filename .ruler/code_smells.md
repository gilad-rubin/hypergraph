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

## Python-Specific Smells

1. **Bare except** - `except:` hides real failures and catches `KeyboardInterrupt`/`SystemExit`. Catch specific exceptions (or at least `Exception`) and keep `try` blocks tight.

2. **Mutable default arguments** - `def f(x, cache=[])` causes state to leak across calls. Use `None` + initialize inside.

3. **Global state** - Functions relying on globals or module-level singletons make behavior implicit and tests painful. Pass dependencies explicitly.
