"""Graph-internal helpers.

Private to ``hypergraph.graph``: the only callers are ``core.py``
(``Graph._sources_of``) and ``input_spec.py`` (cycle-seed detection). Anything
imported from outside this package belongs in ``graph/addressing.py``, which
carries a cross-package contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hypergraph.nodes.base import HyperNode


def sources_of(output: str, nodes: dict[str, HyperNode]) -> list[str]:
    """Get all node names that produce the given output."""
    return [node.name for node in nodes.values() if output in node.outputs]
