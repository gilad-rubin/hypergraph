"""Daft-inspired Hypergraph examples."""

from examples.daft.document_processing import build_document_processing_graph
from examples.daft.quickstart_orders import build_quickstart_orders_graph
from examples.daft.scenario_sweeps import build_scenario_sweep_graph

__all__ = [
    "build_quickstart_orders_graph",
    "build_document_processing_graph",
    "build_scenario_sweep_graph",
]
