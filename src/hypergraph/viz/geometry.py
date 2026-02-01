"""Geometry types for edge connection validation.

These dataclasses represent the spatial properties of nodes and edges
extracted from the rendered visualization, enabling precise validation
of edge connections to node boundaries.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class NodeGeometry:
    """Node bounding box (excludes shadow/glow).

    Represents the actual visible box element (.group.rounded-lg),
    not visual effects that extend beyond.
    """

    id: str
    x: float  # Left edge
    y: float  # Top edge
    width: float
    height: float

    @property
    def center_x(self) -> float:
        """Horizontal center of the node."""
        return self.x + self.width / 2

    @property
    def bottom(self) -> float:
        """Y coordinate of bottom edge."""
        return self.y + self.height

    @property
    def center_bottom(self) -> tuple[float, float]:
        """(x, y) of center-bottom point - where outgoing edges should start."""
        return (self.center_x, self.bottom)

    @property
    def center_top(self) -> tuple[float, float]:
        """(x, y) of center-top point - where incoming edges should end."""
        return (self.center_x, self.y)


@dataclass(frozen=True)
class EdgeGeometry:
    """Edge path endpoints extracted from SVG.

    The start_point and end_point are extracted from the actual rendered
    SVG path, not calculated from node positions.
    """

    source_id: str
    target_id: str
    start_point: tuple[float, float]  # First point of SVG path (M command)
    end_point: tuple[float, float]  # Last point of SVG path (arrow tip)


@dataclass
class EdgeConnectionValidator:
    """Validates edge connections against node geometry.

    Checks that:
    1. Edge starts at center-bottom of source node
    2. Edge ends at center-top of target node
    3. Source node is positioned above target node
    """

    nodes: dict[str, NodeGeometry]  # id -> geometry
    edges: list[EdgeGeometry]
    tolerance: float = 0.0  # pixels (strict by default)

    def validate_edge(self, edge: EdgeGeometry) -> list[str]:
        """Returns list of issues (empty = valid)."""
        issues = []

        src = self.nodes.get(edge.source_id)
        tgt = self.nodes.get(edge.target_id)

        if not src:
            issues.append(f"Source node '{edge.source_id}' not found")
            return issues
        if not tgt:
            issues.append(f"Target node '{edge.target_id}' not found")
            return issues

        # Check 1: Source above target
        if src.bottom >= tgt.y:
            issues.append(
                f"Source '{src.id}' not above target '{tgt.id}': "
                f"src.bottom={src.bottom:.1f} >= tgt.y={tgt.y:.1f}"
            )

        # Check 2: Edge starts from center-bottom of source
        expected_start = src.center_bottom
        dx_start = abs(edge.start_point[0] - expected_start[0])
        dy_start = abs(edge.start_point[1] - expected_start[1])
        if dx_start > self.tolerance:
            issues.append(
                f"Edge start X offset: {dx_start:.1f}px from center of '{src.id}'"
            )
        if dy_start > self.tolerance:
            issues.append(
                f"Edge start Y offset: {dy_start:.1f}px from bottom of '{src.id}'"
            )

        # Check 3: Edge ends at center-top of target
        expected_end = tgt.center_top
        dx_end = abs(edge.end_point[0] - expected_end[0])
        dy_end = abs(edge.end_point[1] - expected_end[1])
        if dx_end > self.tolerance:
            issues.append(
                f"Edge end X offset: {dx_end:.1f}px from center of '{tgt.id}'"
            )
        if dy_end > self.tolerance:
            issues.append(
                f"Edge end Y offset: {dy_end:.1f}px from top of '{tgt.id}'"
            )

        return issues

    def validate_all(self) -> dict[str, list[str]]:
        """Returns {edge_id: [issues]} for all edges with issues."""
        return {
            f"{e.source_id}->{e.target_id}": issues
            for e in self.edges
            if (issues := self.validate_edge(e))
        }


def format_issues(issues: dict[str, list[str]]) -> str:
    """Format validation issues for display in test failures."""
    lines = []
    for edge_id, edge_issues in issues.items():
        lines.append(f"  {edge_id}:")
        for issue in edge_issues:
            lines.append(f"    - {issue}")
    return "\n".join(lines)
