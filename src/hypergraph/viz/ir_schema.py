"""Schema for the compact graph IR.

The IR is the single source of truth for all viz frontends (live cell,
standalone HTML, future Mermaid integration). It contains pure-graph
facts plus initial state — no 2^N expansion-state precomputation.

Fields are added as tests demand them. Keep this minimal.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class IRNode:
    id: str
    node_type: str
    parent: str | None = None


@dataclass(frozen=True)
class IREdge:
    source: str
    target: str
    edge_type: str = "data"


@dataclass(frozen=True)
class IRExternalInput:
    name: str
    deepest_owner: str | None = None
    consumers: tuple[str, ...] = ()


@dataclass(frozen=True)
class GraphIR:
    nodes: list[IRNode] = field(default_factory=list)
    edges: list[IREdge] = field(default_factory=list)
    expandable_nodes: list[str] = field(default_factory=list)
    external_inputs: list[IRExternalInput] = field(default_factory=list)
