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
    has_section: bool = False


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
        has_section=True,
    )


def resolve_db_path(explicit: str | None = None) -> str:
    """Resolve the database path using a priority chain.

    Resolution order (highest priority first):
    1. Explicit argument (--db flag or direct parameter)
    2. HYPERGRAPH_DB environment variable
    3. [tool.hypergraph] db key in pyproject.toml
    4. Convention: .hypergraph/runs.db (only if [tool.hypergraph] section exists)

    Raises:
        SystemExit: If no database path can be resolved
    """
    import os

    if explicit:
        return explicit

    env_db = os.environ.get("HYPERGRAPH_DB")
    if env_db:
        return env_db

    config = load_config()
    if config.db:
        return config.db

    # Convention path — only if [tool.hypergraph] exists (signal of intent)
    if config.has_section:
        return ".hypergraph/runs.db"

    raise SystemExit("No database found. Set --db, HYPERGRAPH_DB env var, or add [tool.hypergraph] to pyproject.toml. See docs for details.")
