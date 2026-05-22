"""Core Mermaid result and ID helpers."""

from __future__ import annotations

import re
from typing import Any

# Characters unsafe in Mermaid IDs (anything not alphanumeric or underscore)
_UNSAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_]")

# Mermaid reserved words that cannot be used as bare node IDs
_RESERVED_WORDS = frozenset(
    {
        "end",
        "subgraph",
        "direction",
        "click",
        "style",
        "classDef",
        "class",
        "linkStyle",
        "graph",
        "flowchart",
    }
)


class MermaidDiagram:
    """A Mermaid diagram that renders in Jupyter notebooks.

    Rendering:

    - **JupyterLab 4.1+ / Notebook 7.1+**: native ``text/vnd.mermaid`` MIME
      type — fully local, zero network requests.
    - **Terminal / plain**: raw Mermaid source via ``text/plain``.

    Example:
        >>> diagram = graph.to_mermaid()
        >>> diagram                  # renders in notebook
        >>> print(diagram)           # prints raw Mermaid source
        >>> diagram.source           # raw string
    """

    def __init__(self, source: str) -> None:
        self.source = source

    def __str__(self) -> str:
        return self.source

    def __repr__(self) -> str:
        lines = self.source.split("\n")
        preview = lines[0] if lines else ""
        return f"MermaidDiagram({preview!r}, {len(lines)} lines)"

    def __contains__(self, item: str) -> bool:
        return item in self.source

    def startswith(self, prefix: str) -> bool:
        """Delegate to source string."""
        return self.source.startswith(prefix)

    def _repr_mimebundle_(self, **kwargs: Any) -> dict[str, str]:
        """Provide MIME types for notebook rendering.

        JupyterLab 4.1+ uses text/vnd.mermaid for native rendering.
        """
        return {
            "text/vnd.mermaid": self.source,
            "text/plain": str(self),
        }


def _sanitize_id(node_id: str) -> str:
    """Convert a node ID to a Mermaid-safe identifier.

    Replaces '/' with '__', strips unsafe chars, and prefixes with 'n_'
    to avoid collisions with Mermaid reserved words or digit-leading IDs.
    """
    safe = node_id.replace("/", "__")
    safe = _UNSAFE_ID_RE.sub("_", safe)
    if safe and (safe.lower() in _RESERVED_WORDS or safe[0:1].isdigit()):
        safe = f"n_{safe}"
    return safe or "n_empty"


class _MermaidIdAllocator:
    """Allocate deterministic Mermaid IDs after sanitization."""

    def __init__(self) -> None:
        self._by_raw: dict[str, str] = {}
        self._used: set[str] = set()
        self._next_suffix: dict[str, int] = {}

    def get(self, raw_id: str) -> str:
        if raw_id in self._by_raw:
            return self._by_raw[raw_id]

        base = _sanitize_id(raw_id)
        candidate = base
        suffix = self._next_suffix.get(base, 1)
        while candidate in self._used:
            suffix += 1
            candidate = f"{base}_{suffix}"

        self._next_suffix[base] = suffix
        self._used.add(candidate)
        self._by_raw[raw_id] = candidate
        return candidate
