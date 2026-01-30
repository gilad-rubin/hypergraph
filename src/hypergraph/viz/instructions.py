"""Dataclasses for explicit visualization instructions.

These dataclasses define the contract between Python (renderer) and JavaScript (layout).
Python generates complete, unambiguous instructions; JavaScript just positions and renders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from itertools import product
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import networkx as nx


class NodeType(str, Enum):
    """Types of nodes in the visualization."""
    INPUT = "INPUT"           # Individual input parameter
    INPUT_GROUP = "INPUT_GROUP"  # Grouped inputs (optional)
    FUNCTION = "FUNCTION"     # Function node
    DATA = "DATA"             # Output data node
    CONTAINER = "CONTAINER"   # Expanded nested graph container


def _format_type(t: type | None) -> str | None:
    """Format a type annotation for display."""
    if t is None:
        return None
    if hasattr(t, "__name__"):
        return t.__name__
    return str(t).replace("typing.", "")


@dataclass
class VizNode:
    """A node in the visualization.

    Represents any visual element: inputs, functions, outputs, or containers.
    """
    id: str
    type: NodeType
    label: str
    parent_id: str | None = None  # ID of containing CONTAINER, or None for root
    is_expanded: bool = False     # Only relevant for CONTAINER nodes

    # INPUT node fields
    type_hint: str | None = None  # Type annotation (e.g., "str", "int")

    # INPUT_GROUP fields (for grouped inputs)
    params: list[str] | None = None       # Parameter names in group
    param_types: list[str] | None = None  # Type hints for each param

    # FUNCTION node fields
    outputs: list[str] | None = None  # Output names produced by this function

    # DATA node fields
    value_name: str | None = None  # The output value this represents

    # Visual properties
    theme: str = "dark"
    show_types: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to React Flow node format."""
        # Map our types to React Flow types
        rf_type_map = {
            NodeType.INPUT: "INPUT",
            NodeType.INPUT_GROUP: "INPUT_GROUP",
            NodeType.FUNCTION: "FUNCTION",
            NodeType.DATA: "DATA",
            NodeType.CONTAINER: "PIPELINE",  # React Flow uses PIPELINE for containers
        }

        data: dict[str, Any] = {
            "nodeType": rf_type_map[self.type],
            "label": self.label,
            "theme": self.theme,
            "showTypes": self.show_types,
        }

        # Add type-specific fields
        if self.type == NodeType.INPUT:
            if self.type_hint:
                data["typeHint"] = self.type_hint

        elif self.type == NodeType.INPUT_GROUP:
            data["params"] = self.params or []
            data["paramTypes"] = self.param_types or []

        elif self.type == NodeType.FUNCTION:
            data["outputs"] = self.outputs or []

        elif self.type == NodeType.DATA:
            if self.value_name:
                data["valueName"] = self.value_name

        elif self.type == NodeType.CONTAINER:
            data["isExpanded"] = self.is_expanded

        result: dict[str, Any] = {
            "id": self.id,
            "type": rf_type_map[self.type],
            "data": data,
            "position": {"x": 0, "y": 0},  # Layout will compute actual position
        }

        if self.parent_id:
            result["parentNode"] = self.parent_id
            result["extent"] = "parent"

        return result


@dataclass
class VizEdge:
    """An edge in the visualization.

    Edges are explicit: source and target are always the actual nodes
    that should be visually connected. No re-routing in JavaScript.
    """
    source: str  # Source node ID (always explicit - the actual visual source)
    target: str  # Target node ID (always explicit - the actual visual target)
    id: str | None = None  # Optional explicit ID, generated if not provided
    label: str | None = None  # Optional edge label

    def __post_init__(self):
        if self.id is None:
            self.id = f"e_{self.source}_to_{self.target}"

    def to_dict(self) -> dict[str, Any]:
        """Convert to React Flow edge format."""
        result: dict[str, Any] = {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "type": "customEdge",
        }
        if self.label:
            result["label"] = self.label
        return result


@dataclass
class VizInstructions:
    """Complete visualization instructions for JavaScript.

    This is the single source of truth that Python sends to JavaScript.
    JavaScript trusts these instructions completely - no interpretation needed.
    """
    nodes: list[VizNode] = field(default_factory=list)
    edges: list[VizEdge] = field(default_factory=list)

    # Pre-computed edges for all expansion state combinations
    # Key format: "node1:0,node2:1" (sorted by node id, 0=collapsed, 1=expanded)
    edges_by_state: dict[str, list[VizEdge]] = field(default_factory=dict)

    # List of node IDs that can be expanded/collapsed (CONTAINER nodes)
    expandable_nodes: list[str] = field(default_factory=list)

    # Metadata for debugging/testing
    depth: int = 0
    theme: str = "dark"
    show_types: bool = False
    debug_overlays: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to format expected by React Flow."""
        # Sort for deterministic ordering
        sorted_nodes = sorted(self.nodes, key=lambda n: n.id)
        sorted_edges = sorted(self.edges, key=lambda e: e.id or "")

        # Convert edges_by_state to dict format
        edges_by_state_dict = {
            key: [e.to_dict() for e in sorted(edges, key=lambda e: e.id or "")]
            for key, edges in self.edges_by_state.items()
        }

        return {
            "nodes": [n.to_dict() for n in sorted_nodes],
            "edges": [e.to_dict() for e in sorted_edges],
            "meta": {
                "depth": self.depth,
                "theme": self.theme,
                "showTypes": self.show_types,
                "debugOverlays": self.debug_overlays,
                "edgesByState": edges_by_state_dict,
                "expandableNodes": sorted(self.expandable_nodes),
            },
        }

    def add_node(self, node: VizNode) -> None:
        """Add a node to the instructions."""
        self.nodes.append(node)

    def add_edge(self, source: str, target: str, **kwargs) -> None:
        """Add an edge to the instructions."""
        self.edges.append(VizEdge(source=source, target=target, **kwargs))

    def get_node(self, node_id: str) -> VizNode | None:
        """Find a node by ID."""
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def get_edges_from(self, source_id: str) -> list[VizEdge]:
        """Get all edges originating from a node."""
        return [e for e in self.edges if e.source == source_id]

    def get_edges_to(self, target_id: str) -> list[VizEdge]:
        """Get all edges going to a node."""
        return [e for e in self.edges if e.target == target_id]


# =============================================================================
# Expansion State Helpers
# =============================================================================


def _get_expandable_nodes(flat_graph: "nx.DiGraph") -> list[str]:
    """Get list of node IDs that can be expanded/collapsed (GRAPH nodes)."""
    return [
        node_id
        for node_id, attrs in flat_graph.nodes(data=True)
        if attrs.get("node_type") == "GRAPH"
    ]


def _expansion_state_to_key(expansion_state: dict[str, bool]) -> str:
    """Convert expansion state dict to a canonical string key.

    Format: "node1:0,node2:1" (sorted alphabetically, 0=collapsed, 1=expanded)
    """
    sorted_items = sorted(expansion_state.items())
    return ",".join(f"{node_id}:{int(expanded)}" for node_id, expanded in sorted_items)


def _enumerate_valid_expansion_states(
    flat_graph: "nx.DiGraph",
    expandable_nodes: list[str],
) -> list[dict[str, bool]]:
    """Enumerate all valid expansion state combinations.

    A state is valid if expanded children only appear when their parent is also expanded.
    This prunes unreachable states (e.g., inner expanded when outer collapsed).

    Returns:
        List of expansion state dicts, each mapping node_id -> is_expanded
    """
    if not expandable_nodes:
        return [{}]

    # Build parent-child relationships among expandable nodes
    node_to_parent = {}
    for node_id in expandable_nodes:
        parent_id = flat_graph.nodes[node_id].get("parent")
        if parent_id in expandable_nodes:
            node_to_parent[node_id] = parent_id

    valid_states = []

    # Generate all 2^n combinations
    for bits in product([False, True], repeat=len(expandable_nodes)):
        state = dict(zip(expandable_nodes, bits))

        # Check validity: if a node is expanded, all its expandable ancestors must also be expanded
        is_valid = True
        for node_id, is_expanded in state.items():
            if is_expanded:
                # Check ancestor chain
                parent = node_to_parent.get(node_id)
                while parent is not None:
                    if not state.get(parent, False):
                        is_valid = False
                        break
                    parent = node_to_parent.get(parent)
                if not is_valid:
                    break

        if is_valid:
            valid_states.append(state)

    return valid_states


def _compute_edges_for_state(
    flat_graph: "nx.DiGraph",
    expansion_state: dict[str, bool],
) -> list[VizEdge]:
    """Compute edges for a specific expansion state.

    This is the core edge routing logic - determines which nodes edges connect to
    based on which containers are expanded/collapsed.
    """
    edges = []

    # Build param_to_consumer map for this expansion state
    param_to_consumer = _build_param_to_consumer_map(flat_graph, expansion_state)

    input_spec = flat_graph.graph.get("input_spec", {})
    required = input_spec.get("required", ())
    optional = input_spec.get("optional", ())
    external_inputs = set(required) | set(optional)

    # 1. Add edges from INPUT nodes to their actual consumers
    for param in external_inputs:
        input_node_id = f"input_{param}"
        actual_target = param_to_consumer.get(param)

        if actual_target:
            edges.append(VizEdge(source=input_node_id, target=actual_target))

    # 2. Add edges between function nodes (from graph structure)
    for source, target, edge_data in flat_graph.edges(data=True):
        # Skip if either node is not visible in this expansion state
        if not _is_node_visible(source, flat_graph, expansion_state):
            continue
        if not _is_node_visible(target, flat_graph, expansion_state):
            continue

        value_name = edge_data.get("value_name", "")

        # Create edge directly between visible nodes
        edge_id = f"e_{source}_to_{target}"
        if value_name:
            edge_id = f"e_{source}_{value_name}_to_{target}"

        edges.append(VizEdge(source=source, target=target, id=edge_id))

    return edges


def _build_output_to_producer_map(
    flat_graph: "nx.DiGraph",
    expansion_state: dict[str, bool],
) -> dict[str, str]:
    """Build map of output_value_name -> actual_producer_node_id.

    When an output is produced by a node inside an expanded container,
    returns the actual internal producer, not the container.
    """
    output_to_producer: dict[str, str] = {}

    for node_id, attrs in flat_graph.nodes(data=True):
        for output in attrs.get("outputs", ()):
            # Check if this producer is visible (parent chain is expanded)
            if _is_node_visible(node_id, flat_graph, expansion_state):
                # Only store if we don't have one yet, or if this is a deeper node
                if output not in output_to_producer:
                    output_to_producer[output] = node_id
                else:
                    # Prefer the deeper (more specific) producer
                    existing = output_to_producer[output]
                    if _get_nesting_depth(node_id, flat_graph) > _get_nesting_depth(existing, flat_graph):
                        output_to_producer[output] = node_id

    return output_to_producer


# =============================================================================
# Builder: Convert NetworkX graph to VizInstructions
# =============================================================================

def build_instructions(
    flat_graph: "nx.DiGraph",
    *,
    depth: int = 1,
    theme: str = "dark",
    show_types: bool = False,
) -> VizInstructions:
    """Build visualization instructions from a flattened NetworkX graph.

    This is the main entry point for generating explicit visualization instructions.
    All edge routing decisions are made here - JavaScript just renders.

    Pre-computes edges for ALL valid expansion state combinations, so JavaScript
    can simply select the right edge set when expansion state changes.

    Args:
        flat_graph: NetworkX DiGraph from Graph.to_flat_graph()
        depth: How many levels of nested graphs to expand (0 = collapsed)
        theme: "dark" or "light"
        show_types: Whether to show type annotations

    Returns:
        VizInstructions with explicit nodes, edges, and pre-computed edges_by_state
    """
    instructions = VizInstructions(depth=depth, theme=theme, show_types=show_types)

    # Get input_spec from graph attributes
    input_spec = flat_graph.graph.get("input_spec", {})
    bound_params = set(input_spec.get("bound", {}).keys())

    # Build initial expansion state based on depth
    initial_expansion_state = _build_expansion_state(flat_graph, depth)

    # Build maps for routing edges to actual visible nodes
    param_to_consumer = _build_param_to_consumer_map(flat_graph, initial_expansion_state)

    # Create INPUT nodes for external inputs
    _add_input_nodes(instructions, flat_graph, input_spec, bound_params, param_to_consumer, theme, show_types)

    # Create FUNCTION and CONTAINER nodes (include ALL nodes - JS handles visibility)
    _add_all_graph_nodes(instructions, flat_graph, initial_expansion_state, bound_params, theme, show_types)

    # Create edges for initial state
    _add_edges(instructions, flat_graph, initial_expansion_state, param_to_consumer)

    # Pre-compute edges for ALL valid expansion state combinations
    expandable_nodes = _get_expandable_nodes(flat_graph)
    instructions.expandable_nodes = expandable_nodes

    if expandable_nodes:
        valid_states = _enumerate_valid_expansion_states(flat_graph, expandable_nodes)
        for state in valid_states:
            key = _expansion_state_to_key(state)
            edges = _compute_edges_for_state(flat_graph, state)
            instructions.edges_by_state[key] = edges

    return instructions


def _build_expansion_state(flat_graph: "nx.DiGraph", depth: int) -> dict[str, bool]:
    """Build map of node_id -> is_expanded for all GRAPH nodes."""
    expansion_state = {}
    for node_id, attrs in flat_graph.nodes(data=True):
        if attrs.get("node_type") == "GRAPH":
            parent_id = attrs.get("parent")
            expansion_state[node_id] = _is_node_expanded(node_id, parent_id, depth, flat_graph)
    return expansion_state


def _is_node_expanded(
    node_id: str,
    parent_id: str | None,
    depth: int,
    flat_graph: "nx.DiGraph",
) -> bool:
    """Determine if a GRAPH node should be expanded based on depth."""
    attrs = flat_graph.nodes[node_id]
    if attrs.get("node_type") != "GRAPH":
        return False

    # Calculate nesting level by counting ancestors
    nesting_level = 0
    current_parent = parent_id
    while current_parent is not None:
        nesting_level += 1
        current_parent = flat_graph.nodes[current_parent].get("parent")

    return depth > nesting_level


def _is_node_visible(node_id: str, flat_graph: "nx.DiGraph", expansion_state: dict[str, bool]) -> bool:
    """Check if a node is visible (all ancestors are expanded)."""
    attrs = flat_graph.nodes[node_id]
    parent_id = attrs.get("parent")

    while parent_id is not None:
        if not expansion_state.get(parent_id, False):
            return False
        parent_attrs = flat_graph.nodes[parent_id]
        parent_id = parent_attrs.get("parent")

    return True


def _get_nesting_depth(node_id: str, flat_graph: "nx.DiGraph") -> int:
    """Get the nesting depth of a node (0 = root level)."""
    depth = 0
    attrs = flat_graph.nodes[node_id]
    parent_id = attrs.get("parent")

    while parent_id is not None:
        depth += 1
        parent_attrs = flat_graph.nodes[parent_id]
        parent_id = parent_attrs.get("parent")

    return depth


def _build_param_to_consumer_map(
    flat_graph: "nx.DiGraph",
    expansion_state: dict[str, bool],
) -> dict[str, str]:
    """Build map of param_name -> actual_consumer_node_id.

    When a param is consumed by a node inside an expanded container,
    returns the actual internal consumer, not the container.
    """
    param_to_consumer: dict[str, str] = {}

    for node_id, attrs in flat_graph.nodes(data=True):
        for param in attrs.get("inputs", ()):
            # Check if this consumer is visible (parent chain is expanded)
            if _is_node_visible(node_id, flat_graph, expansion_state):
                # Only store if we don't have one yet, or if this is a deeper node
                if param not in param_to_consumer:
                    param_to_consumer[param] = node_id
                else:
                    # Prefer the deeper (more specific) consumer
                    existing = param_to_consumer[param]
                    if _get_nesting_depth(node_id, flat_graph) > _get_nesting_depth(existing, flat_graph):
                        param_to_consumer[param] = node_id

    return param_to_consumer


def _get_root_ancestor(node_id: str, flat_graph: "nx.DiGraph") -> str:
    """Get the root-level ancestor of a node (or itself if root-level)."""
    attrs = flat_graph.nodes[node_id]
    parent_id = attrs.get("parent")

    if parent_id is None:
        return node_id

    # Walk up to find root
    while True:
        parent_attrs = flat_graph.nodes[parent_id]
        grandparent = parent_attrs.get("parent")
        if grandparent is None:
            return parent_id
        parent_id = grandparent


def _add_input_nodes(
    instructions: VizInstructions,
    flat_graph: "nx.DiGraph",
    input_spec: dict,
    bound_params: set[str],
    param_to_consumer: dict[str, str],
    theme: str,
    show_types: bool,
) -> None:
    """Add INPUT nodes for each external input parameter."""
    required = input_spec.get("required", ())
    optional = input_spec.get("optional", ())
    external_inputs = list(required) + list(optional)

    for param in external_inputs:
        # Find the type for this parameter
        param_type = None
        for node_id, attrs in flat_graph.nodes(data=True):
            if param in attrs.get("inputs", ()):
                param_type = attrs.get("input_types", {}).get(param)
                if param_type is not None:
                    break

        instructions.add_node(VizNode(
            id=f"input_{param}",
            type=NodeType.INPUT,
            label=param,
            type_hint=_format_type(param_type),
            theme=theme,
            show_types=show_types,
        ))


def _add_graph_nodes(
    instructions: VizInstructions,
    flat_graph: "nx.DiGraph",
    expansion_state: dict[str, bool],
    bound_params: set[str],
    theme: str,
    show_types: bool,
) -> None:
    """Add FUNCTION and CONTAINER nodes from the graph (only visible nodes)."""
    for node_id, attrs in flat_graph.nodes(data=True):
        parent_id = attrs.get("parent")
        node_type = attrs.get("node_type", "FUNCTION")

        # Skip nodes that are not visible (inside collapsed container)
        if not _is_node_visible(node_id, flat_graph, expansion_state):
            continue

        if node_type == "GRAPH":
            is_expanded = expansion_state.get(node_id, False)
            instructions.add_node(VizNode(
                id=node_id,
                type=NodeType.CONTAINER,
                label=attrs.get("label", node_id),
                parent_id=parent_id,
                is_expanded=is_expanded,
                outputs=list(attrs.get("outputs", ())),
                theme=theme,
                show_types=show_types,
            ))
        else:
            # FUNCTION node
            instructions.add_node(VizNode(
                id=node_id,
                type=NodeType.FUNCTION,
                label=attrs.get("label", node_id),
                parent_id=parent_id,
                outputs=list(attrs.get("outputs", ())),
                theme=theme,
                show_types=show_types,
            ))


def _add_all_graph_nodes(
    instructions: VizInstructions,
    flat_graph: "nx.DiGraph",
    initial_expansion_state: dict[str, bool],
    bound_params: set[str],
    theme: str,
    show_types: bool,
) -> None:
    """Add ALL FUNCTION and CONTAINER nodes from the graph.

    Unlike _add_graph_nodes, this includes all nodes regardless of visibility.
    JavaScript handles visibility based on current expansion state.
    """
    for node_id, attrs in flat_graph.nodes(data=True):
        parent_id = attrs.get("parent")
        node_type = attrs.get("node_type", "FUNCTION")

        if node_type == "GRAPH":
            is_expanded = initial_expansion_state.get(node_id, False)
            instructions.add_node(VizNode(
                id=node_id,
                type=NodeType.CONTAINER,
                label=attrs.get("label", node_id),
                parent_id=parent_id,
                is_expanded=is_expanded,
                outputs=list(attrs.get("outputs", ())),
                theme=theme,
                show_types=show_types,
            ))
        else:
            # FUNCTION node
            instructions.add_node(VizNode(
                id=node_id,
                type=NodeType.FUNCTION,
                label=attrs.get("label", node_id),
                parent_id=parent_id,
                outputs=list(attrs.get("outputs", ())),
                theme=theme,
                show_types=show_types,
            ))


def _add_edges(
    instructions: VizInstructions,
    flat_graph: "nx.DiGraph",
    expansion_state: dict[str, bool],
    param_to_consumer: dict[str, str],
) -> None:
    """Add edges with explicit routing based on expansion state.

    Key principle: Edges connect to ACTUAL visible nodes, not containers.
    """
    input_spec = flat_graph.graph.get("input_spec", {})
    required = input_spec.get("required", ())
    optional = input_spec.get("optional", ())
    external_inputs = set(required) | set(optional)

    # 1. Add edges from INPUT nodes to their actual consumers
    for param in external_inputs:
        input_node_id = f"input_{param}"
        actual_target = param_to_consumer.get(param)

        if actual_target:
            instructions.add_edge(input_node_id, actual_target)

    # 2. Add edges between function nodes (from graph structure)
    for source, target, edge_data in flat_graph.edges(data=True):
        # Skip if either node is not visible
        if not _is_node_visible(source, flat_graph, expansion_state):
            continue
        if not _is_node_visible(target, flat_graph, expansion_state):
            continue

        edge_type = edge_data.get("edge_type", "data")
        value_name = edge_data.get("value_name", "")

        # Create edge directly between visible nodes
        edge_id = f"e_{source}_to_{target}"
        if value_name:
            edge_id = f"e_{source}_{value_name}_to_{target}"

        instructions.add_edge(source, target, id=edge_id)
