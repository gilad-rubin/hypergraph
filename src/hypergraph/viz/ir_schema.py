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
    label: str | None = None
    outputs: tuple[dict, ...] = ()
    inputs: tuple[dict, ...] = ()
    branch_data: dict | None = None


@dataclass(frozen=True)
class IREdge:
    source: str
    target: str
    edge_type: str = "data"
    # When the source is a container, source_when_expanded is the deepest
    # internal producer of the value flowing along this edge. scene_builder
    # rewrites the edge's source to this when the container is expanded.
    source_when_expanded: str | None = None
    # Symmetric: deepest internal consumer when the target container is expanded.
    target_when_expanded: str | None = None
    # Output-value names this edge carries (for separate_outputs mode, where
    # the data flow goes producer -> data_node -> consumer instead of
    # producer -> consumer directly).
    value_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class IRExternalInput:
    name: str
    deepest_owner: str | None = None
    consumers: tuple[str, ...] = ()
    type_hint: str | None = None
    is_bound: bool = False


@dataclass(frozen=True)
class GraphIR:
    nodes: list[IRNode] = field(default_factory=list)
    edges: list[IREdge] = field(default_factory=list)
    expandable_nodes: list[str] = field(default_factory=list)
    external_inputs: list[IRExternalInput] = field(default_factory=list)
