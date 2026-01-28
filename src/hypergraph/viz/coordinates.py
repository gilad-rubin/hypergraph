"""Coordinate space transformations for hierarchical graph layout.

Provides data structures and utilities for managing coordinates across
different coordinate spaces in nested graph visualizations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Point:
    """Immutable 2D point with offset support.

    Attributes:
        x: X coordinate
        y: Y coordinate

    Example:
        >>> p1 = Point(10, 20)
        >>> p2 = Point(5, 5)
        >>> p3 = p1 + p2
        >>> p3.x, p3.y
        (15, 25)
    """

    x: float
    y: float

    def __add__(self, other: Point) -> Point:
        """Add two points coordinate-wise."""
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Point) -> Point:
        """Subtract two points coordinate-wise."""
        return Point(self.x - other.x, self.y - other.y)


@dataclass(frozen=True)
class CoordinateSpace:
    """Coordinate space with transformation methods.

    Represents a coordinate system that can be transformed to parent,
    absolute (root), or viewport coordinates.

    Attributes:
        x: X position in parent space
        y: Y position in parent space
        space: Name identifier for this coordinate space
        parent: Parent coordinate space (None for root)

    Example:
        >>> root = CoordinateSpace(0, 0, "root")
        >>> child = CoordinateSpace(100, 50, "child", parent=root)
        >>> point = Point(10, 20)
        >>> child.to_absolute(point)
        Point(x=110, y=70)
    """

    x: float
    y: float
    space: str
    parent: CoordinateSpace | None = None

    def to_parent(self, point: Point) -> Point:
        """Transform point from this space to parent space."""
        return Point(self.x + point.x, self.y + point.y)

    def to_absolute(self, point: Point) -> Point:
        """Transform point from this space to absolute (root) space.

        Walks up the parent chain, accumulating offsets.
        """
        current_point = point
        current_space: CoordinateSpace | None = self

        while current_space is not None:
            current_point = current_space.to_parent(current_point)
            current_space = current_space.parent

        return current_point

    def to_viewport(self, point: Point, viewport_offset: Point) -> Point:
        """Transform point from this space to viewport coordinates.

        First transforms to absolute space, then applies viewport offset.
        """
        absolute = self.to_absolute(point)
        return absolute - viewport_offset


def layout_to_absolute(
    layout: dict[str, Any],
    space: CoordinateSpace,
) -> dict[str, Point]:
    """Convert layout positions to absolute coordinates.

    Args:
        layout: Dict mapping node IDs to position dicts with 'x' and 'y' keys
        space: Coordinate space of the layout

    Returns:
        Dict mapping node IDs to absolute Point coordinates
    """
    result = {}
    for node_id, pos in layout.items():
        local_point = Point(pos["x"], pos["y"])
        result[node_id] = space.to_absolute(local_point)
    return result
