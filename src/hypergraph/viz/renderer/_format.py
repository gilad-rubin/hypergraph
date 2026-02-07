"""Type formatting utilities for visualization nodes."""

from __future__ import annotations

import re

# Pre-compiled regex for simplifying fully-qualified type names
_DOTTED_NAME_RE = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*\.)+([a-zA-Z_][a-zA-Z0-9_]*)")


def format_type(t: type | None) -> str | None:
    """Format a type annotation for display.

    Converts type objects to clean string representations:
    - list[float] → "list[float]"
    - Dict[str, int] → "Dict[str, int]"
    - mymodule.MyClass → "MyClass"
    """
    if t is None:
        return None

    type_str = str(t)
    type_str = type_str.replace("<class '", "").replace("'>", "")

    return _simplify_type_string(type_str)


def _simplify_type_string(type_str: str) -> str:
    """Simplify type strings by extracting only the final part of dotted names.

    Examples:
        "list[mymodule.Document]" → "list[Document]"
        "Dict[str, foo.bar.Baz]" → "Dict[str, Baz]"
    """
    def replace_with_final(match: re.Match) -> str:
        full_match = match.group(0)
        parts = full_match.split(".")
        return parts[-1]

    return _DOTTED_NAME_RE.sub(replace_with_final, type_str)
