"""Static mirror checks for the public ``InputSpec`` contract."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CURRENT_SURFACES = (
    "src/hypergraph/graph/core.py",
    "dev/ARCHITECTURE.md",
    "docs/03-patterns/03-agentic-loops.md",
    "docs/07-design/guiding-principles.md",
    "docs/07-design/roadmap.md",
    "docs/changelog.md",
    "scripts/test_api_docs.py",
    "scripts/test_graph_examples.py",
)


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    next_heading = text.find("\n##", start + len(heading))
    if next_heading == -1:
        return text[start:]
    return text[start:next_heading]


def test_inputspec_mirrors_distinguish_scope_from_input_categories() -> None:
    stale_category_claims = (
        "required/optional/entrypoint",
        "required, optional, and entrypoint parameters",
        "InputSpec docs define `entrypoints`",
        "classified as entrypoint parameters",
        "**entrypoint parameter**",
    )
    stale_attribute_access = re.compile(r"\.inputs\.(?:entry_points|entrypoints)\b")
    invalid_inputspec_category = re.compile(
        r"InputSpec[^\n]*\bcategor(?:y|ies|ization)\b[^\n]*"
        r"\b(?:internal|entry_?points?)\b",
        re.IGNORECASE,
    )
    tuple_bound_comment = re.compile(r"inputs\.bound[^\n]*#\s*\([^\n]*\)")
    violations: list[str] = []

    for path in CURRENT_SURFACES:
        text = _read(path)
        for stale_claim in stale_category_claims:
            if stale_claim in text:
                violations.append(f"{path} still contains {stale_claim!r}")
        if stale_attribute_access.search(text):
            violations.append(f"{path} accesses an entrypoint category that InputSpec does not expose")
        if invalid_inputspec_category.search(text):
            violations.append(f"{path} advertises a nonexistent InputSpec category")
        if tuple_bound_comment.search(text):
            violations.append(f"{path} describes InputSpec.bound as a tuple instead of a dict")

    assert not violations, "\n" + "\n".join(violations)

    inputspec = _read("docs/06-api-reference/inputspec.md")
    assert "there is no separate entrypoints category on InputSpec" in inputspec
    scope_narrowing = _section(inputspec, "### Scope Narrowing (Entrypoint and Select)")
    assert "`with_entrypoint()` and `select()` narrow" in scope_narrowing
    assert "`required` or `optional`" in scope_narrowing
