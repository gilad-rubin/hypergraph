"""Node types for hypergraph."""

from hypergraph.nodes._rename import RenameEntry, RenameError
from hypergraph.nodes.base import HyperNode
from hypergraph.nodes.function import FunctionNode, node
from hypergraph.nodes.gate import END, GateNode, IfElseNode, RouteNode, ifelse, route
from hypergraph.nodes.graph_node import GraphNode
from hypergraph.nodes.interrupt import InterruptNode

__all__ = [
    "HyperNode",
    "RenameEntry",
    "RenameError",
    "FunctionNode",
    "GraphNode",
    "GateNode",
    "IfElseNode",
    "RouteNode",
    "InterruptNode",
    "node",
    "ifelse",
    "route",
    "END",
]
