"""The graph-derived answer contract: ``answer_type`` rendered as JSON Schema.

PRD 0010 / ADR 0008 (A8): a durable pause slot persists the answer contract as
**data**, never as a callable. The interrupt handler's return annotation
already declares ``answer_type`` (enforced at graph build time in
``graph/validation.py``); this module renders that declared type into a JSON
Schema the slot can store, and checks one settled value against a stored
schema.

**What a settled answer actually is.** The answer is durable resume input, so
it must survive the journal: every accepted value is JSON-safe. The stored
schema therefore describes the JSON *form* of the declared type, not the live
Python object — ``answer_type=Verdict`` (a dataclass) renders to
``{"type": "object"}`` and is answered with ``dataclasses.asdict(verdict)``,
never with the instance. That is the whole contract; nothing here coerces a
value, because a coercion would hand the graph's answer port a different type
than the one it declared.

Three rules keep the stored contract honest:

- **Only expressible types get a constraint.** Primitives, ``Enum``,
  ``Literal``, unions, containers, dataclasses, ``TypedDict``, and
  ``NamedTuple`` all have an honest JSON form and render to one. Nothing
  renders a keyword this module cannot also check — that is why a dataclass
  stops at ``{"type": "object"}`` and does not claim ``properties``.
- **A type the renderer cannot express is recorded as such.** The stored
  schema carries :data:`UNRENDERABLE_KEY` naming the declared type, so an
  operator reading a slot can tell "the renderer gave up on ``Score``" from
  "the handler declared ``Any``" (which stores ``{}``). Both constrain
  nothing beyond JSON-safety — an unknown JSON Schema keyword is ignored by
  every checker, including this one — but the degradation is now visible
  instead of silent. An un-renderable type never raises at pause time: an
  existing graph must keep pausing.
- **Occurrence options narrow the contract only when the declared type
  accepts them.** ``question.options`` is ``tuple[str, ...]`` — human-facing
  choices. They become an ``enum`` only when every option already validates
  against the rendered base schema (``answer_type=str`` does; a
  ``answer_type=bool`` question whose options are ``("yes", "no")`` does
  not). Options that the declared type cannot accept — and every option of an
  unconstrained contract, where "accepts" is unknowable — stay display data on
  ``PauseSlot.options`` and constrain nothing.

Both the pause write path (runners) and the settlement path (checkpointers)
import from here, so the contract is rendered and checked by one definition.
"""

from __future__ import annotations

import dataclasses
import json
import types
from collections.abc import Sequence
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin, is_typeddict

#: The schema for a declared ``Any``/absent type: constrains nothing, and
#: nothing was lost rendering it.
UNCONSTRAINED: dict[str, Any] = {}

#: Keyword recorded in a stored contract the renderer could NOT express. Its
#: value is the display name of the declared type. JSON Schema ignores unknown
#: keywords, so a marked schema still constrains nothing — the point is that a
#: reader can see which type was given up on rather than reading ``{}`` and
#: assuming nothing was declared.
UNRENDERABLE_KEY = "x-hypergraph-unrenderable"

_PRIMITIVE_SCHEMAS: dict[Any, dict[str, Any]] = {
    bool: {"type": "boolean"},
    int: {"type": "integer"},
    float: {"type": "number"},
    str: {"type": "string"},
    type(None): {"type": "null"},
    list: {"type": "array"},
    tuple: {"type": "array"},
    set: {"type": "array"},
    frozenset: {"type": "array"},
    dict: {"type": "object"},
}

_CONTAINER_ORIGINS: dict[Any, dict[str, Any]] = {
    list: {"type": "array"},
    tuple: {"type": "array"},
    set: {"type": "array"},
    frozenset: {"type": "array"},
    dict: {"type": "object"},
}

_UNION_ORIGINS = (Union, types.UnionType)


def render_answer_schema(answer_type: Any, options: Sequence[str] | None = None) -> dict[str, Any]:
    """Render one declared ``answer_type`` (plus occurrence options) as JSON Schema.

    Returns a schema carrying :data:`UNRENDERABLE_KEY` for a type this
    renderer cannot express, and :data:`UNCONSTRAINED` when nothing was
    declared — see the module docstring. Neither is a failure, and neither
    ever raises: a graph that paused before must keep pausing.
    """
    schema = _render(answer_type)
    if not options or is_unconstrained(schema):
        # An unconstrained contract cannot tell whether the declared type
        # accepts these labels, so it never narrows on them.
        return schema
    candidates = list(options)
    if not _all_options_accepted(schema, candidates):
        # Display labels the declared type cannot accept (a non-JSON-safe
        # option lands here too). They stay on ``PauseSlot.options``.
        return schema
    return {**schema, "enum": candidates}


def is_unconstrained(schema: dict[str, Any]) -> bool:
    """Whether this stored contract constrains nothing beyond JSON-safety."""
    return not schema or UNRENDERABLE_KEY in schema


def validate_answer(schema: dict[str, Any], value: Any) -> tuple[str, ...]:
    """Check one value against a stored answer schema; ``()`` means valid.

    A value that cannot be JSON-serialized always fails, whatever the schema
    says: the settled answer is durable resume input, so it must survive the
    journal. See the module docstring — the accepted value is the JSON form
    of the declared type, never the live object.
    """
    json_issue = _json_issue(value)
    if json_issue is not None:
        return (json_issue + _json_form_hint(schema),)
    return tuple(_schema_issues(schema, value))


def type_display_name(value: Any) -> str | None:
    """Display-stable name for a declared type (mirrors the HyperTable seam)."""
    if value is None:
        return None
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    return repr(value)


def _all_options_accepted(schema: dict[str, Any], options: Sequence[Any]) -> bool:
    """Whether EVERY option already validates against the base schema.

    ``validate_answer`` returns the issues it found, so an ACCEPTED option is
    an empty tuple — hence the explicit ``== ()`` rather than a truthiness
    test that would read as its own opposite.
    """
    return all(validate_answer(schema, option) == () for option in options)


def _json_issue(value: Any) -> str | None:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as error:
        return f"value is not JSON-serializable ({type(value).__name__}): {error}"
    return None


def _json_form_hint(schema: dict[str, Any]) -> str:
    """Tell the caller what a rejected non-JSON-safe value should have been."""
    declared = schema.get(UNRENDERABLE_KEY)
    if declared is not None:
        return f"; the declared answer_type {declared} could not be rendered as JSON Schema, so send its JSON-safe form rather than the live object"
    return "; a settled answer is durable resume input, so send the JSON-safe form of the declared answer_type"


def _schema_issues(schema: dict[str, Any], value: Any) -> list[str]:
    if is_unconstrained(schema):
        return []
    issues: list[str] = []
    if "anyOf" in schema:
        branches = schema["anyOf"]
        if all(_schema_issues(branch, value) for branch in branches):
            expected = ", ".join(sorted({str(branch.get("type", "any")) for branch in branches}))
            issues.append(f"expected one of [{expected}], got {_type_name(value)}")
        return issues
    expected_type = schema.get("type")
    if expected_type is not None and not _matches_type(expected_type, value):
        issues.append(f"expected type {expected_type!r}, got {_type_name(value)}")
    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(option) for option in schema["enum"])
        issues.append(f"expected one of [{allowed}], got {value!r}")
    return issues


def _matches_type(expected: str, value: Any) -> bool:
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        # bool is a Python int subclass; JSON Schema keeps them distinct.
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "null":
        return value is None
    if expected == "array":
        return isinstance(value, (list, tuple))
    if expected == "object":
        return isinstance(value, dict)
    return True


def _type_name(value: Any) -> str:
    return type(value).__name__


def _unrenderable(answer_type: Any) -> dict[str, Any]:
    """The stored contract for a type this renderer cannot express."""
    return {UNRENDERABLE_KEY: type_display_name(answer_type) or repr(answer_type)}


def _is_named_tuple(answer_type: Any) -> bool:
    """A ``NamedTuple`` class — JSON-encodes as an array, like any tuple."""
    return isinstance(answer_type, type) and issubclass(answer_type, tuple) and hasattr(answer_type, "_fields")


def _render(answer_type: Any) -> dict[str, Any]:
    if answer_type is None or answer_type is Any:
        return dict(UNCONSTRAINED)
    primitive = _PRIMITIVE_SCHEMAS.get(answer_type)
    if primitive is not None:
        return dict(primitive)
    if is_typeddict(answer_type):
        # A TypedDict IS a dict at runtime; its keys are not claimed here
        # because this module does not check ``properties``.
        return {"type": "object"}
    if isinstance(answer_type, type) and issubclass(answer_type, Enum):
        values = [member.value for member in answer_type]
        if any(_json_issue(item) for item in values):
            return _unrenderable(answer_type)
        return {"enum": values}
    if _is_named_tuple(answer_type):
        return {"type": "array"}
    if dataclasses.is_dataclass(answer_type) and isinstance(answer_type, type):
        # The JSON form of a dataclass is an object. ``properties`` is
        # deliberately NOT claimed: this module would not check it, and the
        # slot never stores a constraint it cannot enforce.
        return {"type": "object"}

    origin = get_origin(answer_type)
    if origin is Literal:
        members = list(get_args(answer_type))
        if any(_json_issue(item) for item in members):
            return _unrenderable(answer_type)
        return {"enum": members}
    if origin in _UNION_ORIGINS:
        branches = [_render(arg) for arg in get_args(answer_type)]
        if any(is_unconstrained(branch) for branch in branches):
            # One unrenderable member makes the whole union unconstrainable:
            # a partial anyOf would reject values the declared type accepts.
            return _unrenderable(answer_type)
        return {"anyOf": branches}
    container = _CONTAINER_ORIGINS.get(origin)
    if container is not None:
        return dict(container)
    return _unrenderable(answer_type)
