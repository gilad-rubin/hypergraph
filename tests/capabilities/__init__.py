"""Capability matrix for systematic testing of hypergraph features."""

from .matrix import (
    Capability,
    Runner,
    NodeType,
    Topology,
    MapMode,
    NestingDepth,
    Concurrency,
    TypeValidation,
    all_valid_combinations,
    combinations_for,
)
from .builders import build_graph_for_capability

__all__ = [
    "Capability",
    "Runner",
    "NodeType",
    "Topology",
    "MapMode",
    "NestingDepth",
    "Concurrency",
    "TypeValidation",
    "all_valid_combinations",
    "combinations_for",
    "build_graph_for_capability",
]
