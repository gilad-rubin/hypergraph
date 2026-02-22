"""Python-side layout estimation for widget sizing.

Estimates the dimensions of the visualization before rendering, so the
iframe can be sized appropriately and avoid double scrolling in notebooks.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hypergraph.graph.core import Graph


class LayoutEstimator:
    """Estimates visualization dimensions to match viz.js layout output."""

    # Constants matching viz.js layout defaults
    LAYOUT_SPACE_Y = 140  # Vertical spacing between nodes (matches Kedro-viz)
    LAYOUT_LAYER_SPACE_Y = 120  # Vertical spacing between layers (matches Kedro-viz)
    LAYOUT_SPACE_X = 30  # Horizontal spacing (matches Kedro-viz)
    # Fixed padding values matching JS fitWithFixedPadding
    PADDING_LEFT = 20
    PADDING_RIGHT = 70  # Extra space for control buttons (matches Kedro-viz)
    PADDING_TOP = 16
    PADDING_BOTTOM = 16

    # Separate outputs mode uses tighter spacing
    SEPARATE_LAYOUT_SPACE_Y = 100
    SEPARATE_LAYOUT_LAYER_SPACE_Y = 90

    # Node sizing constants from html/generator.py
    MAX_NODE_WIDTH = 280
    CHAR_WIDTH_PX = 7
    NODE_BASE_PADDING = 52
    FUNCTION_NODE_BASE_PADDING = 48
    NODE_LABEL_MAX_CHARS = 25
    TYPE_HINT_MAX_CHARS = 25

    # Node heights
    DATA_NODE_HEIGHT = 36
    FUNCTION_NODE_HEIGHT = 56
    OUTPUT_ROW_HEIGHT = 24  # Height per output in function node

    # INPUT_GROUP sizing
    INPUT_GROUP_BASE_HEIGHT = 16
    INPUT_GROUP_ROW_HEIGHT = 20
    INPUT_GROUP_GAP = 4

    def __init__(
        self,
        graph: Graph,
        *,
        separate_outputs: bool = False,
        show_types: bool = False,
        depth: int = 0,
    ):
        self.graph = graph
        self.separate_outputs = separate_outputs
        self.show_types = show_types
        self.depth = depth

        # Use appropriate spacing based on mode
        if separate_outputs:
            self.space_y = self.SEPARATE_LAYOUT_SPACE_Y
            self.layer_space_y = self.SEPARATE_LAYOUT_LAYER_SPACE_Y
        else:
            self.space_y = self.LAYOUT_SPACE_Y
            self.layer_space_y = self.LAYOUT_LAYER_SPACE_Y

    def estimate(self) -> tuple[int, int]:
        """Estimate (width, height) for the graph visualization.

        Returns:
            Tuple of (width, height) in pixels
        """
        nodes = list(self.graph.nodes.values())
        if not nodes:
            return 400, 300

        # Build adjacency from graph edges
        adj: dict[str, list[str]] = defaultdict(list)
        in_degree: dict[str, int] = {name: 0 for name in self.graph.nodes}

        for source, target, _ in self.graph.nx_graph.edges(data=True):
            if source in self.graph.nodes and target in self.graph.nodes:
                adj[source].append(target)
                in_degree[target] += 1

        # Topological layering (longest path)
        levels: dict[int, list[str]] = defaultdict(list)
        node_level: dict[str, int] = {}

        # Start with nodes that have no incoming edges
        queue = [(name, 0) for name, deg in in_degree.items() if deg == 0]

        # Handle nodes with external inputs (they're also sources)
        started = {name for name, _ in queue}
        for name in self.graph.nodes:
            if name not in started and in_degree[name] == 0:
                queue.append((name, 0))

        visited: set[str] = set()
        while queue:
            name, lvl = queue.pop(0)
            if name in visited:
                continue
            visited.add(name)

            # Track max level for this node
            node_level[name] = max(node_level.get(name, 0), lvl)
            levels[node_level[name]].append(name)

            for target in adj[name]:
                queue.append((target, lvl + 1))

        # Handle any unvisited (cycles)
        for name in self.graph.nodes:
            if name not in visited:
                levels[0].append(name)

        if not levels:
            return 400, 300

        # Calculate dimensions per level
        num_levels = max(levels.keys()) + 1

        # Count input groups (they form their own layer at top)
        input_spec = self.graph.inputs
        external_inputs = list(input_spec.required) + list(input_spec.optional)
        has_inputs = bool(external_inputs)

        # Count DATA nodes if separate_outputs
        num_data_nodes = 0
        if self.separate_outputs:
            for hypernode in self.graph.nodes.values():
                num_data_nodes += len(hypernode.outputs)

        # Calculate total layers
        total_layers = num_levels
        if has_inputs:
            total_layers += 1  # Input group layer
        if self.separate_outputs and num_data_nodes > 0:
            total_layers += num_levels  # Each function layer has a DATA layer below

        # Width calculation: max width of any level
        max_level_width = 0
        for lvl in range(num_levels):
            level_nodes = levels.get(lvl, [])
            level_width = 0
            for name in level_nodes:
                node_width = self._estimate_node_width(name)
                level_width += node_width + self.LAYOUT_SPACE_X

            if self.separate_outputs:
                # DATA nodes might make the level wider
                data_width = sum(
                    self._estimate_data_width(out)
                    for name in level_nodes
                    for out in self.graph.nodes[name].outputs
                )
                level_width = max(level_width, data_width)

            max_level_width = max(max_level_width, level_width)

        # Input group width
        if has_inputs:
            input_width = self._estimate_input_group_width(external_inputs)
            max_level_width = max(max_level_width, input_width)

        # Height calculation: sum of layer heights
        total_height = 0

        # Input group height
        if has_inputs:
            total_height += self._estimate_input_group_height(external_inputs)
            total_height += self.layer_space_y

        # Function node layers
        for lvl in range(num_levels):
            level_nodes = levels.get(lvl, [])
            max_node_height = max(
                (self._estimate_node_height(name) for name in level_nodes),
                default=self.FUNCTION_NODE_HEIGHT,
            )
            total_height += max_node_height

            if self.separate_outputs:
                # Add DATA node layer
                total_height += self.space_y + self.DATA_NODE_HEIGHT

            if lvl < num_levels - 1:
                total_height += self.layer_space_y

        # Add fixed padding (matching JS fitWithFixedPadding)
        total_width = max_level_width + self.PADDING_LEFT + self.PADDING_RIGHT
        total_height += self.PADDING_TOP + self.PADDING_BOTTOM

        # Enforce minimums
        total_width = max(300, int(total_width))
        total_height = max(150, int(total_height))

        return total_width, total_height

    def _estimate_node_width(self, name: str) -> int:
        """Estimate width of a function/pipeline node."""
        hypernode = self.graph.nodes[name]
        label_len = min(len(name), self.NODE_LABEL_MAX_CHARS)
        max_content_len = label_len

        if not self.separate_outputs:
            # Outputs shown inside node
            for output_name in hypernode.outputs:
                out_len = min(len(output_name), self.NODE_LABEL_MAX_CHARS)
                type_len = 0
                if self.show_types:
                    out_type = hypernode.get_output_type(output_name)
                    if out_type:
                        type_name = getattr(out_type, "__name__", str(out_type))
                        type_len = min(len(type_name), self.TYPE_HINT_MAX_CHARS) + 2
                total_len = out_len + type_len + 4
                max_content_len = max(max_content_len, total_len)

        width = max_content_len * self.CHAR_WIDTH_PX + self.FUNCTION_NODE_BASE_PADDING
        return min(width, self.MAX_NODE_WIDTH)

    def _estimate_node_height(self, name: str) -> int:
        """Estimate height of a function/pipeline node."""
        if self.separate_outputs:
            return self.FUNCTION_NODE_HEIGHT

        hypernode = self.graph.nodes[name]
        num_outputs = len(hypernode.outputs)
        if num_outputs > 0:
            return self.FUNCTION_NODE_HEIGHT + num_outputs * self.OUTPUT_ROW_HEIGHT
        return self.FUNCTION_NODE_HEIGHT

    def _estimate_data_width(self, output_name: str) -> int:
        """Estimate width of a DATA node."""
        label_len = min(len(output_name), self.NODE_LABEL_MAX_CHARS)
        type_len = 0  # Type info optional
        width = (label_len + type_len) * self.CHAR_WIDTH_PX + self.NODE_BASE_PADDING
        return min(width, self.MAX_NODE_WIDTH)

    def _estimate_input_group_width(self, inputs: list[str]) -> int:
        """Estimate width of an INPUT_GROUP node."""
        max_content_len = 0
        for param in inputs:
            param_len = min(len(param), self.NODE_LABEL_MAX_CHARS)
            type_len = 0
            if self.show_types:
                # Would need to look up type from consuming nodes
                type_len = 10  # Estimate
            # Account for icon (2 chars equivalent) + gap (1 char) + param + type
            total_len = 3 + param_len + type_len
            max_content_len = max(max_content_len, total_len)

        # Minimum width for very short params
        max_content_len = max(max_content_len, 6)
        width = max_content_len * self.CHAR_WIDTH_PX + 32  # Smaller padding for inputs
        return min(width, self.MAX_NODE_WIDTH)

    def _estimate_input_group_height(self, inputs: list[str]) -> int:
        """Estimate height of an INPUT_GROUP node."""
        num_params = max(1, len(inputs))
        return (
            self.INPUT_GROUP_BASE_HEIGHT
            + num_params * self.INPUT_GROUP_ROW_HEIGHT
            + (num_params - 1) * self.INPUT_GROUP_GAP
        )


def estimate_layout(
    graph: Graph,
    *,
    separate_outputs: bool = False,
    show_types: bool = False,
    depth: int = 0,
) -> tuple[int, int]:
    """Convenience function to estimate layout dimensions.

    Args:
        graph: The hypergraph Graph to estimate
        separate_outputs: Whether outputs are rendered as separate DATA nodes
        show_types: Whether type annotations are shown
        depth: Depth of nested graph expansion

    Returns:
        Tuple of (width, height) in pixels
    """
    estimator = LayoutEstimator(
        graph,
        separate_outputs=separate_outputs,
        show_types=show_types,
        depth=depth,
    )
    return estimator.estimate()
