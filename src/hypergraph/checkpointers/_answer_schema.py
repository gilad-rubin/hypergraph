"""The graph-derived answer contract: ``answer_type`` rendered as JSON Schema.

PRD 0010 / ADR 0008 (A8): a durable pause slot persists the answer contract as
**data**, never as a callable. The interrupt handler's return annotation
already declares ``answer_type`` (enforced at graph build time in
``graph/validation.py``); this module renders that declared type into a JSON
Schema the slot can store, and checks one settled value against a stored
schema.

Two rules keep the stored contract honest:

- **A type the renderer cannot express becomes the empty schema** ``{}``.
  The empty JSON Schema constrains nothing, so settlement then only requires
  a JSON-safe value. The slot never invents a constraint it cannot check, and
  never rejects an answer on the strength of a type it did not understand.
- **Occurrence options narrow the contract only when the declared type
  accepts them.** ``question.options`` is ``tuple[str, ...]`` — human-facing
  choices. They become an ``enum`` only when every option already validates
  against the rendered base schema (``answer_type=str`` does; a
  ``answer_type=bool`` question whose options are ``("yes", "no")`` does
  not). Options that the declared type cannot accept stay display data on
  ``PauseSlot.options`` and constrain nothing.

Both the pause write path (runners) and the settlement path (checkpointers)
import from here, so the contract is rendered and checked by one definition.
"""

from __future__ import annotations

import json
import types
from collections.abc import Sequence
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin

#: The schema for a type the renderer cannot express: constrains nothing.
UNCONSTRAINED: dict[str, Any] = {}

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

    Returns :data:`UNCONSTRAINED` for a type this renderer cannot express —
    see the module docstring; that is a deliberate absence of constraint, not
    a failure.
    """
    schema = _render(answer_type)
    if not options:
        return schema
    candidates = list(options)
    if not schema or any(validate_answer(schema, option) for option in candidates):
        # Either nothing to narrow, or the options are display labels the
        # declared type cannot accept (``validate_answer`` also rejects a
        # non-JSON-safe option). Both leave the base schema alone.
        return schema
    return {**schema, "enum": candidates}


def validate_answer(schema: dict[str, Any], value: Any) -> tuple[str, ...]:
    """Check one value against a stored answer schema; ``()`` means valid.

    A value that cannot be JSON-serialized always fails: the settled answer
    is durable resume input, so it must survive the journal.
    """
    json_issue = _json_issue(value)
    if json_issue is not None:
        return (json_issue,)
    return tuple(_schema_issues(schema, value))


def _json_issue(value: Any) -> str | None:
    try:
        json.dumps(value)
    except (TypeError, ValueError) as error:
        return f"value is not JSON-serializable ({type(value).__name__}): {error}"
    return None


def _schema_issues(schema: dict[str, Any], value: Any) -> list[str]:
    if not schema:
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


def _render(answer_type: Any) -> dict[str, Any]:
    if answer_type is None or answer_type is Any:
        return dict(UNCONSTRAINED)
    primitive = _PRIMITIVE_SCHEMAS.get(answer_type)
    if primitive is not None:
        return dict(primitive)
    if isinstance(answer_type, type) and issubclass(answer_type, Enum):
        values = [member.value for member in answer_type]
        if any(_json_issue(item) for item in values):
            return dict(UNCONSTRAINED)
        return {"enum": values}

    origin = get_origin(answer_type)
    if origin is Literal:
        members = list(get_args(answer_type))
        if any(_json_issue(item) for item in members):
            return dict(UNCONSTRAINED)
        return {"enum": members}
    if origin in _UNION_ORIGINS:
        branches = [_render(arg) for arg in get_args(answer_type)]
        if any(not branch for branch in branches):
            # One unrenderable member makes the whole union unconstrainable:
            # a partial anyOf would reject values the declared type accepts.
            return dict(UNCONSTRAINED)
        return {"anyOf": branches}
    container = _CONTAINER_ORIGINS.get(origin)
    if container is not None:
        return dict(container)
    return dict(UNCONSTRAINED)
