"""Test script to verify the visualization renderer output."""

from hypergraph import Graph, node


@node(output_name="y")
def double(x: int) -> int:
    return x * 2


@node(output_name="z")
def square(y: int) -> int:
    return y ** 2


graph = Graph(nodes=[double, square])

# Import and use renderer directly to see the JSON structure
from hypergraph.viz.renderer import render_graph

result = render_graph(graph, separate_outputs=False, show_types=True)

print("=== NODES ===")
for n in result["nodes"]:
    print(f"  {n['id']}: nodeType={n['data'].get('nodeType')}")
    if n["data"].get("params"):
        print(f"    params={n['data']['params']}")
    if n["data"].get("sourceId"):
        print(f"    sourceId={n['data']['sourceId']}")

print()
print("=== EDGES ===")
for e in result["edges"]:
    print(
        f"  {e['source']} -> {e['target']} (type={e.get('data', {}).get('edgeType', '?')})"
    )
