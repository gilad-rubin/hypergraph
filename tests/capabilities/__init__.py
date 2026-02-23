"""Capability matrix for systematic testing of hypergraph features."""

from .builders import build_graph_for_capability
from .matrix import (
    Caching,
    Capability,
    Concurrency,
    MapMode,
    NestingDepth,
    NodeType,
    Runner,
    Topology,
    TypeValidation,
    all_valid_combinations,
    combinations_for,
)

__all__ = [
    "Caching",
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
