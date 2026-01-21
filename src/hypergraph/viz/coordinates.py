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

    Represents a position in 2D space with optional offset from parent.

    Attributes:
        x: X coordinate
        y: Y coordinate
        offset: Optional offset from parent (another Point)

    Example:
        >>> p1 = Point(10, 20)
        >>> p2 = Point(5, 5)
        >>> p3 = p1 + p2
        >>> p3.x, p3.y
        (15, 25)
    """

    x: float
    y: float
    offset: Point | None = None

    def __add__(self, other: Point) -> Point:
        """Add two points coordinate-wise.

        Args:
            other: Point to add

        Returns:
            New Point with summed coordinates

        Example:
            >>> Point(1, 2) + Point(3, 4)
            Point(x=4, y=6, offset=None)
        """
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Point) -> Point:
        """Subtract two points coordinate-wise.

        Args:
            other: Point to subtract

        Returns:
            New Point with difference

        Example:
            >>> Point(5, 10) - Point(2, 3)
            Point(x=3, y=7, offset=None)
        """
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
        Point(x=110, y=70, offset=None)
    """

    x: float
    y: float
    space: str
    parent: CoordinateSpace | None = None

    def to_parent(self, point: Point) -> Point:
        """Transform point from this space to parent space.

        Args:
            point: Point in this coordinate space

        Returns:
            Point in parent coordinate space

        Example:
            >>> child = CoordinateSpace(100, 50, "child")
            >>> child.to_parent(Point(10, 20))
            Point(x=110, y=70, offset=None)
        """
        return Point(self.x + point.x, self.y + point.y)

    def to_absolute(self, point: Point) -> Point:
        """Transform point from this space to absolute (root) space.

        Walks up the parent chain, accumulating offsets.

        Args:
            point: Point in this coordinate space

        Returns:
            Point in absolute coordinate space

        Example:
            >>> root = CoordinateSpace(0, 0, "root")
            >>> child = CoordinateSpace(100, 50, "child", parent=root)
            >>> grandchild = CoordinateSpace(20, 10, "grandchild", parent=child)
            >>> grandchild.to_absolute(Point(5, 3))
            Point(x=125, y=63, offset=None)
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

        Args:
            point: Point in this coordinate space
            viewport_offset: Offset of viewport from absolute origin

        Returns:
            Point in viewport coordinate space

        Example:
            >>> space = CoordinateSpace(100, 50, "space")
            >>> space.to_viewport(Point(10, 20), viewport_offset=Point(5, 5))
            Point(x=105, y=65, offset=None)
        """
        absolute = self.to_absolute(point)
        return absolute - viewport_offset


def layout_to_absolute(
    layout: dict[str, Any],
    space: CoordinateSpace,
) -> dict[str, Point]:
    """Convert layout positions to absolute coordinates.

    Takes a layout dictionary with node positions in a coordinate space
    and transforms all positions to absolute coordinates.

    Args:
        layout: Dict mapping node IDs to position dicts with 'x' and 'y' keys
        space: Coordinate space of the layout

    Returns:
        Dict mapping node IDs to absolute Point coordinates

    Example:
        >>> layout = {'a': {'x': 10, 'y': 20}, 'b': {'x': 30, 'y': 40}}
        >>> space = CoordinateSpace(100, 50, "space")
        >>> result = layout_to_absolute(layout, space)
        >>> result['a']
        Point(x=110, y=70, offset=None)
        >>> result['b']
        Point(x=130, y=90, offset=None)
    """
    result = {}
    for node_id, pos in layout.items():
        local_point = Point(pos["x"], pos["y"])
        result[node_id] = space.to_absolute(local_point)
    return result
