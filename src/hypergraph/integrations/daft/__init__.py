"""Compatibility re-export for the public Daft runner.

Prefer:

    from hypergraph import DaftRunner

This module remains as a stable import path for older examples and notebooks.
"""

from hypergraph.runners.daft import DaftRunner

__all__ = ["DaftRunner"]
