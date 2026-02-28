"""Project-level configuration from pyproject.toml.

Reads the [tool.hypergraph] section to provide named graph shortcuts
and default settings for the CLI.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class HypergraphConfig:
    """Configuration from [tool.hypergraph] in pyproject.toml."""

    graphs: dict[str, str] = field(default_factory=dict)
    db: str | None = None


def find_pyproject(start: Path | None = None) -> Path | None:
    """Walk up from start directory to find pyproject.toml."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
    return None


def load_config(start: Path | None = None) -> HypergraphConfig:
    """Load [tool.hypergraph] from the nearest pyproject.toml.

    Returns default config if no pyproject.toml or no [tool.hypergraph] section.
    """
    path = find_pyproject(start)
    if path is None:
        return HypergraphConfig()

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomli as tomllib
        except ImportError:
            return HypergraphConfig()

    with open(path, "rb") as f:
        data = tomllib.load(f)

    section = data.get("tool", {}).get("hypergraph", {})
    if not section:
        return HypergraphConfig()

    return HypergraphConfig(
        graphs=section.get("graphs", {}),
        db=section.get("db"),
    )
