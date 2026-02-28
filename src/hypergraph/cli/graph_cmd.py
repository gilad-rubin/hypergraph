"""Graph CLI commands: inspect."""

from __future__ import annotations

import importlib
import sys
from typing import Annotated

import typer

from hypergraph.cli._format import print_json, print_lines, print_table

app = typer.Typer(help="Inspect graph structure.")


def _load_graph(module_path: str):
    """Load a Graph object from 'module:attribute' path.

    Examples:
        my_module:graph
        my_package.pipeline:rag_graph
    """
    if ":" not in module_path:
        print(f"Error: Expected 'module:attribute' format, got '{module_path}'")
        print("Example: my_module:graph or my_package.pipeline:rag_graph")
        raise typer.Exit(1)

    module_name, attr_name = module_path.rsplit(":", 1)

    try:
        # Add cwd to path so local modules can be found
        if "." not in sys.path:
            sys.path.insert(0, ".")
        module = importlib.import_module(module_name)
    except ImportError as e:
        print(f"Error: Could not import module '{module_name}': {e}")
        raise typer.Exit(1) from e

    graph = getattr(module, attr_name, None)
    if graph is None:
        print(f"Error: Module '{module_name}' has no attribute '{attr_name}'")
        raise typer.Exit(1)

    from hypergraph.graph import Graph

    if not isinstance(graph, Graph):
        print(f"Error: '{module_path}' is not a Graph instance (got {type(graph).__name__})")
        raise typer.Exit(1)

    return graph


@app.command("inspect")
def graph_inspect(
    module_path: Annotated[str, typer.Argument(help="Graph location as 'module:attribute'")],
    as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    output: Annotated[str | None, typer.Option("--output", help="Write JSON to file")] = None,
):
    """Show graph structure (nodes, edges, inputs)."""
    graph = _load_graph(module_path)

    if as_json:
        nodes_data = []
        for node in graph.iter_nodes():
            nodes_data.append(
                {
                    "name": node.name,
                    "type": type(node).__name__,
                    "inputs": list(node.inputs),
                    "outputs": list(node.outputs),
                }
            )

        input_spec = graph.input_spec
        data = {
            "name": graph.name,
            "nodes": nodes_data,
            "node_count": len(list(graph.iter_nodes())),
            "required_inputs": list(input_spec.required),
            "optional_inputs": list(input_spec.optional),
        }
        print_json("graph.inspect", data, output)
        return

    # Human-readable output
    nodes = list(graph.iter_nodes())
    edge_count = graph._nx_graph.number_of_edges()
    print(f"\nGraph: {graph.name} | {len(nodes)} nodes | {edge_count} edges\n")

    headers = ["Node", "Type", "Inputs", "Outputs"]
    rows = []
    for node in nodes:
        inputs_str = ", ".join(node.inputs) if node.inputs else "—"
        outputs_str = ", ".join(node.outputs) if node.outputs else "—"
        # Truncate long input/output lists
        if len(inputs_str) > 40:
            inputs_str = inputs_str[:37] + "…"
        if len(outputs_str) > 30:
            outputs_str = outputs_str[:27] + "…"

        rows.append(
            [
                node.name,
                type(node).__name__,
                inputs_str,
                outputs_str,
            ]
        )

    lines = print_table(headers, rows)
    print_lines(lines)

    # Input spec
    input_spec = graph.input_spec
    if input_spec.required:
        print(f"\n  Required inputs: {', '.join(input_spec.required)}")
    if input_spec.optional:
        print(f"  Optional inputs: {', '.join(f'{k} (default: {v})' for k, v in input_spec.optional.items())}")
    if input_spec.entrypoints:
        print(f"  Entrypoints: {', '.join(input_spec.entrypoints)}")

    print(f"\n  For JSON: hypergraph graph inspect {module_path} --json")
