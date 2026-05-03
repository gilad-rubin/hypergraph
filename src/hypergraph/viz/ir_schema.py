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
    # Branch label for control edges originating from a gate (e.g. "True"
    # / "False" for an ifelse, or the route key for a route).
    label: str | None = None
    # True when this data edge belongs to an exclusive (mutex) gate branch.
    # Two data edges that feed the same value into one consumer from
    # different branches of an exclusive gate are both flagged.
    exclusive: bool = False
    # DFS back-edge — an edge that closes a cycle in the execution
    # subgraph. Frontends route these as feedback loops so they don't
    # disappear when an enclosing container expands.
    is_back_edge: bool = False


@dataclass(frozen=True)
class IRExternalInput:
    """An external-input group. Single-param groups render as INPUT;
    multi-param groups (e.g. one consumer takes both `alpha` and
    `beta`) render as INPUT_GROUP with a stable id like
    `input_group_alpha_beta`.

    ``params`` stores the user-visible leaf names (the lexical-scope leaf
    of dot-pathed external inputs from issue #94). ``id_segments`` are
    the corresponding identifier-safe segments used to construct the
    synthetic ``input_*`` node id; they are equal to ``params`` except
    when colliding leaf names force a fallback to the full dot-path.
    """

    params: tuple[str, ...]  # display labels (leaf segments)
    deepest_owner: str | None = None
    consumers: tuple[str, ...] = ()
    type_hints: tuple[str | None, ...] = ()  # one per param
    is_bound: bool = False
    id_segments: tuple[str, ...] = ()  # one per param; falls back to params if empty

    @property
    def name(self) -> str:
        """Backward-compat single-name accessor for single-param groups."""
        return self.params[0] if len(self.params) == 1 else "_".join(self.params)

    @property
    def is_group(self) -> bool:
        return len(self.params) > 1

    @property
    def synthetic_id(self) -> str:
        """Stable scene-graph id of the INPUT/INPUT_GROUP node.

        Uses ``id_segments`` only when its length matches ``params`` -- mirrors
        the JS twin (assets/scene_builder.js) which guards on the same length
        check. Without this guard, a Python/JS payload with ``id_segments`` of
        a different length would diverge: Python would use ``id_segments``
        while JS falls back to ``params``, producing mismatched scene IDs.
        """
        segs = self.id_segments if self.id_segments and len(self.id_segments) == len(self.params) else self.params
        if len(segs) == 1:
            return f"input_{segs[0]}"
        return f"input_group_{'_'.join(segs)}"


# Bump when the IR shape changes in a way old scene_builders can't read.
# Both Python and JS scene_builders pin to this value; mismatches trigger
# the static-fallback banner instead of a runtime exception.
CURRENT_SCHEMA_VERSION = "1"


class IRSchemaError(ValueError):
    """Raised when a scene_builder is asked to consume an IR whose
    ``schema_version`` it doesn't know how to interpret."""


@dataclass(frozen=True)
class GraphIR:
    nodes: list[IRNode] = field(default_factory=list)
    edges: list[IREdge] = field(default_factory=list)
    expandable_nodes: list[str] = field(default_factory=list)
    external_inputs: list[IRExternalInput] = field(default_factory=list)
    # Top-level entrypoints declared via ``Graph.with_entrypoint(...)``.
    # scene_builder synthesizes a __start__ node + edges to these (after
    # resolving any ancestor that is currently collapsed).
    configured_entrypoints: tuple[str, ...] = ()
    # GRAPH-id -> visible output names when collapsed. Inner-only outputs
    # (consumed exclusively by descendants) are absent so they don't leak
    # to the container surface.
    graph_output_visibility: dict[str, tuple[str, ...]] = field(default_factory=dict)
    schema_version: str = CURRENT_SCHEMA_VERSION
