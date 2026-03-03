#!/usr/bin/env python3
"""Debug script for nested graph visualization with scope-aware visibility.

This creates a mock "generation" graph with a nested "prompt_building" container
to test INPUT/OUTPUT visibility behavior:

Test Scenarios:
1. `query` - consumed by BOTH outer (format_response) AND inner (build_prompt)
   → Should stay at ROOT level (visible when container is collapsed)

2. `selected_pages` - consumed ONLY by outer (filter_document_pages)
   → Should stay at ROOT level

3. `system_instructions` - consumed ONLY by inner (get_system_prompt)
   → Should appear INSIDE the container when expanded
   → Should be HIDDEN when container is collapsed

4. `context_text` - produced by inner (build_context), consumed by inner (build_prompt)
   → DATA node should be HIDDEN when container is collapsed (internal-only)

5. `chat_messages` - produced by inner (build_prompt), consumed by outer (generate_answer)
   → DATA node should be VISIBLE when collapsed (has external consumer)

Graph Structure:
```
generation (outer)
├── filter_document_pages(selected_pages, document) → filtered_document
├── prompt_building (nested container)
│   ├── get_system_prompt(system_instructions) → system_prompt
│   ├── build_context(filtered_document) → context_text
│   └── build_prompt(system_prompt, context_text, query, images) → chat_messages
├── generate_answer(chat_messages) → raw_answer
└── format_response(raw_answer, query) → response
```
"""

from __future__ import annotations

from hypergraph import Graph, node

# =============================================================================
# Outer graph nodes (at root level)
# =============================================================================


@node(output_name="filtered_document")
def filter_document_pages(document: str, selected_pages: list[int]) -> str:
    """Filter document to selected pages only."""
    return f"filtered({document}, pages={selected_pages})"


@node(output_name="raw_answer")
def generate_answer(chat_messages: list[dict]) -> str:
    """Generate answer from chat messages using LLM."""
    return f"generated({chat_messages})"


@node(output_name="response")
def format_response(raw_answer: str, query: str) -> dict:
    """Format the raw answer into structured response."""
    return {"answer": raw_answer, "query": query}


# =============================================================================
# Inner graph nodes (inside prompt_building container)
# =============================================================================


@node(output_name="system_prompt")
def get_system_prompt(system_instructions: str) -> str:
    """Build system prompt from instructions."""
    return f"system: {system_instructions}"


@node(output_name="context_text")
def build_context(filtered_document: str) -> str:
    """Build context from filtered document."""
    return f"context: {filtered_document}"


@node(output_name="chat_messages")
def build_prompt(
    system_prompt: str,
    context_text: str,
    query: str,
    images: list[bytes] | None = None,
) -> list[dict]:
    """Build complete chat messages for LLM."""
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"{context_text}\n\nQ: {query}"},
    ]


# =============================================================================
# Graph Construction
# =============================================================================


def make_prompt_building_graph() -> Graph:
    """Create the inner prompt_building graph."""
    return Graph(
        nodes=[get_system_prompt, build_context, build_prompt],
        name="prompt_building",
    )


def make_generation_graph() -> Graph:
    """Create the outer generation graph with nested prompt_building."""
    prompt_building = make_prompt_building_graph()
    return Graph(
        nodes=[
            filter_document_pages,
            prompt_building.as_node(),
            generate_answer,
            format_response,
        ],
        name="generation",
    )


# =============================================================================
# Visualization and Testing
# =============================================================================


def visualize_graph(depth: int = 1, separate_outputs: bool = False) -> None:
    """Visualize the graph at a given depth.

    Args:
        depth: How many levels to expand (0=collapsed, 1=prompt_building expanded)
        separate_outputs: Whether to show DATA nodes separately
    """
    from hypergraph.viz import visualize

    graph = make_generation_graph()
    print(f"\nVisualizing generation graph at depth={depth}")
    print(f"separate_outputs={separate_outputs}")
    print("-" * 60)

    # Print expected INPUT visibility
    print("\nExpected INPUT visibility (depth=1, prompt_building expanded):")
    print("  - query: ROOT (consumed by format_response AND build_prompt)")
    print("  - document: ROOT (consumed by filter_document_pages)")
    print("  - selected_pages: ROOT (consumed by filter_document_pages)")
    print("  - system_instructions: INSIDE prompt_building (only consumed by get_system_prompt)")
    print("  - images: INSIDE prompt_building (only consumed by build_prompt)")
    print()

    visualize(
        graph,
        depth=depth,
        separate_outputs=separate_outputs,
    )


def analyze_input_scopes() -> None:
    """Analyze and print expected INPUT scopes for the graph."""

    graph = make_generation_graph()
    flat_graph = graph.to_flat_graph()

    print("\nAnalyzing INPUT scopes for generation graph:")
    print("=" * 60)

    # Get input_spec
    input_spec = flat_graph.graph.get("input_spec", {})
    required = input_spec.get("required", ())
    optional = input_spec.get("optional", ())
    all_inputs = list(required) + list(optional)

    # Build consumer mapping: param -> list of consuming node IDs
    param_consumers: dict[str, list[str]] = {}
    for node_id, attrs in flat_graph.nodes(data=True):
        for param in attrs.get("inputs", ()):
            if param not in param_consumers:
                param_consumers[param] = []
            param_consumers[param].append(node_id)

    # Get containers (GRAPH nodes)
    containers = {node_id for node_id, attrs in flat_graph.nodes(data=True) if attrs.get("node_type") == "GRAPH"}

    print(f"\nContainers: {containers}")
    print(f"\nExternal inputs: {all_inputs}")
    print()

    for param in all_inputs:
        consumers = param_consumers.get(param, [])
        consumer_info = []
        for consumer in consumers:
            parent = flat_graph.nodes[consumer].get("parent")
            consumer_info.append(f"{consumer} (parent={parent})")

        print(f"  {param}:")
        print(f"    consumers: {consumer_info}")

        # Determine expected scope
        consumer_parents = [flat_graph.nodes[c].get("parent") for c in consumers]
        if any(p is None for p in consumer_parents):
            expected_scope = "ROOT (has root-level consumer)"
        elif len(set(consumer_parents)) == 1:
            expected_scope = f"INSIDE {consumer_parents[0]} (all consumers in same container)"
        else:
            expected_scope = "ROOT (consumers in multiple containers)"

        print(f"    expected scope: {expected_scope}")
        print()


def print_flat_graph_structure() -> None:
    """Print the flat graph structure for debugging."""
    graph = make_generation_graph()
    flat_graph = graph.to_flat_graph()

    print("\nFlat graph structure:")
    print("=" * 60)

    print("\nNodes:")
    for node_id, attrs in flat_graph.nodes(data=True):
        node_type = attrs.get("node_type", "?")
        parent = attrs.get("parent")
        inputs = list(attrs.get("inputs", ()))
        outputs = list(attrs.get("outputs", ()))
        print(f"  {node_id}:")
        print(f"    type: {node_type}")
        print(f"    parent: {parent}")
        print(f"    inputs: {inputs}")
        print(f"    outputs: {outputs}")

    print("\nEdges:")
    for source, target, data in flat_graph.edges(data=True):
        edge_type = data.get("edge_type", "?")
        value_name = data.get("value_name", "")
        print(f"  {source} -> {target} ({edge_type}: {value_name})")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Debug nested graph visualization")
    parser.add_argument("--depth", type=int, default=1, help="Expansion depth (0-2)")
    parser.add_argument("--separate-outputs", action="store_true", help="Show separate DATA nodes")
    parser.add_argument("--analyze", action="store_true", help="Analyze input scopes only")
    parser.add_argument("--structure", action="store_true", help="Print flat graph structure")

    args = parser.parse_args()

    if args.structure:
        print_flat_graph_structure()
    elif args.analyze:
        analyze_input_scopes()
    else:
        visualize_graph(depth=args.depth, separate_outputs=args.separate_outputs)
