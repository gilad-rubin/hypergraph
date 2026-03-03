"""Graph CLI commands: inspect, ls."""

from __future__ import annotations

import importlib
import sys
from typing import Annotated

import typer

from hypergraph.cli._config import load_config
from hypergraph.cli._format import print_json, print_lines, print_table

app = typer.Typer(help="Inspect graph structure.")


def _import_graph(module_path: str):
    """Import a Graph object from 'module:attribute' path.

    Examples:
        my_module:graph
        my_package.pipeline:rag_graph
    """
    module_name, attr_name = module_path.rsplit(":", 1)

    try:
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


def _load_from_registry(name: str):
    """Look up a graph name in [tool.hypergraph.graphs] and import it."""
    config = load_config()
    module_path = config.graphs.get(name)
    if module_path is None:
        print(f"Error: '{name}' not found in [tool.hypergraph.graphs]")
        print("Hint: Use 'module:attribute' format or register in pyproject.toml:")
        print(f'  [tool.hypergraph.graphs]\n  {name} = "my_module:graph"')
        raise typer.Exit(1)
    return _import_graph(module_path)


def load_graph(target: str):
    """Load a Graph by module path ('module:attr') or registry name."""
    if ":" in target:
        return _import_graph(target)
    return _load_from_registry(target)


@app.command("ls")
def graph_ls(
    as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    output: Annotated[str | None, typer.Option("--output", help="Write JSON to file")] = None,
):
    """List registered graphs from [tool.hypergraph.graphs]."""
    config = load_config()

    if as_json:
        data = {"graphs": config.graphs}
        print_json("graph.ls", data, output)
        return

    if not config.graphs:
        print("\n  No graphs registered in pyproject.toml.")
        print("  Add entries under [tool.hypergraph.graphs]:")
        print('    [tool.hypergraph.graphs]\n    pipeline = "my_module:graph"')
        return

    headers = ["Name", "Module Path"]
    rows = [[name, path] for name, path in sorted(config.graphs.items())]
    lines = print_table(headers, rows)

    print(f"\n  Registered graphs ({len(config.graphs)}):\n")
    print_lines(lines)


@app.command("inspect")
def graph_inspect(
    target: Annotated[str, typer.Argument(help="Graph as 'module:attribute' or registered name")],
    as_json: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    output: Annotated[str | None, typer.Option("--output", help="Write JSON to file")] = None,
):
    """Show graph structure (nodes, edges, inputs)."""
    graph = load_graph(target)

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

    input_spec = graph.input_spec
    if input_spec.required:
        print(f"\n  Required inputs: {', '.join(input_spec.required)}")
    if input_spec.optional:
        print(f"  Optional inputs: {', '.join(f'{k} (default: {v})' for k, v in input_spec.optional.items())}")
    if input_spec.entrypoints:
        print(f"  Entrypoints: {', '.join(input_spec.entrypoints)}")

    print(f"\n  For JSON: hypergraph graph inspect {target} --json")
