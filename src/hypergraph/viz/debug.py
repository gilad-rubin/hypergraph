"""Debug utilities for hypergraph visualization.

Provides programmatic tools to spot visualization issues before rendering.
Inspired by hypernodes UIHandler debug system.

Usage:
    from hypergraph.viz.debug import VizDebugger

    debugger = VizDebugger(graph)

    # Quick validation
    result = debugger.validate()
    if not result.valid:
        print("Issues found:", result.errors)

    # Trace node connections ("points from" / "points to")
    info = debugger.trace_node("my_node")
    print(f"Incoming: {info.incoming_edges}")
    print(f"Outgoing: {info.outgoing_edges}")

    # Trace edge (even missing ones)
    edge_info = debugger.trace_edge("source", "target")

    # Full diagnostics
    issues = debugger.find_issues()

    # Complete state snapshot
    dump = debugger.debug_dump()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

import networkx as nx

if TYPE_CHECKING:
    from hypergraph.graph.core import Graph


@dataclass
class ValidationResult:
    """Result of graph validation."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class NodeTrace:
    """Trace information for a single node."""

    status: str  # "FOUND" or "NOT_FOUND"
    node_id: str
    node_type: Optional[str] = None
    parent: Optional[str] = None
    incoming_edges: list[dict[str, str]] = field(default_factory=list)
    outgoing_edges: list[dict[str, str]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    partial_matches: list[str] = field(default_factory=list)


@dataclass
class EdgeTrace:
    """Trace information for an edge (or missing edge)."""

    edge_query: str
    edge_found: bool
    source_info: dict[str, Any] = field(default_factory=dict)
    target_info: dict[str, Any] = field(default_factory=dict)
    analysis: dict[str, Any] = field(default_factory=dict)


@dataclass
class IssueReport:
    """Comprehensive issue report."""

    validation_errors: list[str] = field(default_factory=list)
    orphan_edges: list[str] = field(default_factory=list)
    disconnected_nodes: list[str] = field(default_factory=list)
    missing_parents: list[str] = field(default_factory=list)
    self_loops: list[str] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        """True if any issues were found."""
        return bool(
            self.validation_errors
            or self.orphan_edges
            or self.disconnected_nodes
            or self.missing_parents
            or self.self_loops
        )


class VizDebugger:
    """Debug helper for hypergraph visualization.

    Works with the flattened NetworkX graph produced by Graph.to_flat_graph().
    Provides validation, tracing, and issue discovery for debugging visualization
    problems before rendering.

    Example:
        >>> debugger = graph.debug_viz()
        >>> debugger.visualize(depth=1)  # Shows viz with debug overlays
    """

    def __init__(self, graph: "Graph"):
        """Create debugger for a graph.

        Args:
            graph: The hypergraph Graph to debug
        """
        self.graph = graph
        self._flat_graph: Optional[nx.DiGraph] = None

    @property
    def flat_graph(self) -> nx.DiGraph:
        """Lazily compute and cache the flattened graph."""
        if self._flat_graph is None:
            self._flat_graph = self.graph.to_flat_graph()
        return self._flat_graph

    def invalidate_cache(self) -> None:
        """Clear cached flat graph (call if graph changed)."""
        self._flat_graph = None

    def validate(self) -> ValidationResult:
        """Validate the visualization graph and return results.

        Checks for:
        - Orphan edges (edges referencing non-existent nodes)
        - Missing parent nodes
        - Self-loops (node -> same node)

        Returns:
            ValidationResult with errors and warnings
        """
        errors: list[str] = []
        warnings: list[str] = []

        G = self.flat_graph
        node_ids = set(G.nodes())

        for source, target, _ in G.edges(data=True):
            if source not in node_ids:
                errors.append(f"Edge source '{source}' not found (target: '{target}')")
            if target not in node_ids:
                errors.append(f"Edge target '{target}' not found (source: '{source}')")
            if source == target:
                errors.append(f"Self-loop detected: '{source}'")

        for node_id, attrs in G.nodes(data=True):
            parent = attrs.get("parent")
            if parent is not None and parent not in node_ids:
                errors.append(f"Node '{node_id}' has missing parent '{parent}'")

        return ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    def trace_node(self, node_id: str) -> NodeTrace:
        """Trace detailed information about a specific node.

        Shows "points from" (incoming edges) and "points to" (outgoing edges).

        Args:
            node_id: Node to trace

        Returns:
            NodeTrace with node details, connections, and diagnostics
        """
        G = self.flat_graph

        if node_id not in G.nodes:
            matches = [nid for nid in G.nodes if node_id in nid][:5]
            return NodeTrace(
                status="NOT_FOUND",
                node_id=node_id,
                partial_matches=matches,
            )

        attrs = dict(G.nodes[node_id])

        incoming = [
            {
                "from": src,
                "value": data.get("value_name", ""),
                "type": data.get("edge_type", "data"),
            }
            for src, tgt, data in G.edges(data=True)
            if tgt == node_id
        ]
        outgoing = [
            {
                "to": tgt,
                "value": data.get("value_name", ""),
                "type": data.get("edge_type", "data"),
            }
            for src, tgt, data in G.edges(data=True)
            if src == node_id
        ]

        node_type = attrs.get("node_type", "FUNCTION")
        details: dict[str, Any] = {
            "label": attrs.get("label", node_id),
            "inputs": list(attrs.get("inputs", ())),
            "outputs": list(attrs.get("outputs", ())),
        }

        if node_type == "GRAPH":
            children = [
                nid for nid, a in G.nodes(data=True) if a.get("parent") == node_id
            ]
            details["children"] = children

        return NodeTrace(
            status="FOUND",
            node_id=node_id,
            node_type=node_type,
            parent=attrs.get("parent"),
            incoming_edges=incoming,
            outgoing_edges=outgoing,
            details=details,
        )

    def trace_edge(self, source: str, target: str) -> EdgeTrace:
        """Trace information about a specific edge (or missing edge).

        Useful for debugging why an edge is missing or pointing wrong.

        Args:
            source: Source node ID
            target: Target node ID

        Returns:
            EdgeTrace with edge details and analysis
        """
        G = self.flat_graph

        edge_found = G.has_edge(source, target)

        result = EdgeTrace(
            edge_query=f"{source} -> {target}",
            edge_found=edge_found,
        )

        result.source_info = self._analyze_node(source)
        result.target_info = self._analyze_node(target)

        if not edge_found:
            result.analysis = self._analyze_missing_edge(source, target)

        return result

    def _analyze_node(self, node_id: str) -> dict[str, Any]:
        """Analyze a node for edge tracing."""
        G = self.flat_graph

        if node_id not in G.nodes:
            matches = [nid for nid in G.nodes if node_id in nid][:3]
            return {"found": False, "similar_ids": matches}

        attrs = G.nodes[node_id]
        return {
            "found": True,
            "type": attrs.get("node_type"),
            "parent": attrs.get("parent"),
            "inputs": list(attrs.get("inputs", ())),
            "outputs": list(attrs.get("outputs", ())),
        }

    def _analyze_missing_edge(self, source: str, target: str) -> dict[str, Any]:
        """Analyze why an edge might be missing."""
        G = self.flat_graph
        analysis: dict[str, Any] = {}

        if source in G.nodes:
            from_source = [
                {"to": tgt, "value": G.edges[source, tgt].get("value_name")}
                for _, tgt in G.out_edges(source)
            ]
            analysis["edges_from_source"] = from_source

        if target in G.nodes:
            to_target = [
                {"from": src, "value": G.edges[src, target].get("value_name")}
                for src, _ in G.in_edges(target)
            ]
            analysis["edges_to_target"] = to_target

        if source in G.nodes and target in G.nodes:
            source_outputs = set(G.nodes[source].get("outputs", ()))
            target_inputs = set(G.nodes[target].get("inputs", ()))
            matching = source_outputs & target_inputs
            if matching:
                analysis["suggestion"] = (
                    f"Matching params exist: {matching}. Edge should exist."
                )
            else:
                analysis["suggestion"] = (
                    f"No matching params. "
                    f"Source outputs: {source_outputs}, target inputs: {target_inputs}"
                )

        return analysis

    def find_issues(self) -> IssueReport:
        """Run comprehensive diagnostics and return all found issues.

        This is the main debugging entry point - finds all potential problems.

        Returns:
            IssueReport with categorized issues
        """
        G = self.flat_graph
        report = IssueReport()

        validation = self.validate()
        report.validation_errors = validation.errors

        edge_nodes: set[str] = set()
        for src, tgt in G.edges():
            edge_nodes.add(src)
            edge_nodes.add(tgt)

        parent_nodes = {
            attrs.get("parent")
            for _, attrs in G.nodes(data=True)
            if attrs.get("parent")
        }

        for node_id in G.nodes():
            if node_id not in edge_nodes and node_id not in parent_nodes:
                report.disconnected_nodes.append(node_id)

        node_ids = set(G.nodes())
        for src, tgt, _ in G.edges(data=True):
            if src not in node_ids:
                report.orphan_edges.append(f"{src} -> {tgt} (source missing)")
            if tgt not in node_ids:
                report.orphan_edges.append(f"{src} -> {tgt} (target missing)")

        for node_id, attrs in G.nodes(data=True):
            parent = attrs.get("parent")
            if parent and parent not in node_ids:
                report.missing_parents.append(f"{node_id} (parent: {parent})")

        for src, tgt in G.edges():
            if src == tgt:
                report.self_loops.append(src)

        return report

    def debug_dump(self) -> dict[str, Any]:
        """Return a complete state snapshot for debugging.

        Returns a dictionary with:
        - nodes: List of all nodes with their properties
        - edges: List of all edges
        - metadata: edges_by_source/target maps, input_spec
        - validation: Results of validate()
        - stats: Summary statistics
        """
        G = self.flat_graph

        nodes = []
        for node_id, attrs in G.nodes(data=True):
            nodes.append(
                {
                    "id": node_id,
                    "type": attrs.get("node_type", "FUNCTION"),
                    "parent": attrs.get("parent"),
                    "label": attrs.get("label", node_id),
                    "inputs": list(attrs.get("inputs", ())),
                    "outputs": list(attrs.get("outputs", ())),
                }
            )

        edges = []
        for src, tgt, data in G.edges(data=True):
            edges.append(
                {
                    "source": src,
                    "target": tgt,
                    "edge_type": data.get("edge_type", "data"),
                    "value_name": data.get("value_name", ""),
                }
            )

        edges_by_source: dict[str, list[str]] = {}
        edges_by_target: dict[str, list[str]] = {}
        for src, tgt in G.edges():
            edges_by_source.setdefault(src, []).append(tgt)
            edges_by_target.setdefault(tgt, []).append(src)

        node_types: dict[str, int] = {}
        for _, attrs in G.nodes(data=True):
            t = attrs.get("node_type", "FUNCTION")
            node_types[t] = node_types.get(t, 0) + 1

        return {
            "nodes": nodes,
            "edges": edges,
            "metadata": {
                "edges_by_source": edges_by_source,
                "edges_by_target": edges_by_target,
                "input_spec": G.graph.get("input_spec", {}),
            },
            "validation": self.validate(),
            "stats": {
                "total_nodes": len(nodes),
                "total_edges": len(edges),
                "node_types": node_types,
                "has_cycles": self.graph.has_cycles,
            },
        }

    def visualize(
        self,
        *,
        width: Optional[int] = None,
        height: Optional[int] = None,
        depth: int = 1,
        theme: str = "auto",
        show_types: bool = False,
        output: Optional[str] = None,
    ) -> Any:
        """Visualize the graph with debug overlays enabled.

        Prints issues and stats before rendering, then shows the visualization
        with debug overlays (BOUNDS/WIDTHS/TEXTS tabs, edge debug points).

        Args:
            width: Widget width in pixels (default: auto-calculated)
            height: Widget height in pixels (default: auto-calculated)
            depth: How many levels of nested graphs to expand (default: 1)
            theme: "dark", "light", or "auto" (default: "auto")
            show_types: Whether to show type annotations (default: False)
            output: Path to save HTML file (default: None, display in notebook)

        Returns:
            ScrollablePipelineWidget if output is None, otherwise None

        Example:
            >>> debugger = graph.debug_viz()
            >>> debugger.visualize(depth=1)
        """
        from hypergraph.viz.widget import visualize as viz_func

        # Print debug info
        issues = self.find_issues()
        stats = self.debug_dump()["stats"]

        print("=== Debug Visualization ===")
        print(f"Nodes: {stats['total_nodes']} | Edges: {stats['total_edges']} | "
              f"Types: {stats['node_types']} | Cycles: {stats['has_cycles']}")

        if issues.has_issues:
            print("\n--- Issues Found ---")
            if issues.validation_errors:
                print(f"  Validation errors: {issues.validation_errors}")
            if issues.orphan_edges:
                print(f"  Orphan edges: {issues.orphan_edges}")
            if issues.disconnected_nodes:
                print(f"  Disconnected nodes: {issues.disconnected_nodes}")
            if issues.missing_parents:
                print(f"  Missing parents: {issues.missing_parents}")
            if issues.self_loops:
                print(f"  Self-loops: {issues.self_loops}")
        else:
            print("No issues found.")

        print("\nDebug overlays enabled. Use tabs: BOUNDS | WIDTHS | TEXTS")
        print("=" * 28)

        return viz_func(
            self.graph,
            width=width,
            height=height,
            depth=depth,
            theme=theme,
            show_types=show_types,
            output=output,
            _debug_overlays=True,
        )


def validate_graph(graph: "Graph") -> ValidationResult:
    """Quick validation of a graph.

    Args:
        graph: Graph to validate

    Returns:
        ValidationResult with any errors

    Example:
        >>> result = validate_graph(my_graph)
        >>> if not result.valid:
        ...     print("Errors:", result.errors)
    """
    return VizDebugger(graph).validate()


def find_issues(graph: "Graph") -> IssueReport:
    """Quick issue discovery for a graph.

    Args:
        graph: Graph to check

    Returns:
        IssueReport with all found issues

    Example:
        >>> issues = find_issues(my_graph)
        >>> if issues.has_issues:
        ...     print("Orphan edges:", issues.orphan_edges)
    """
    return VizDebugger(graph).find_issues()


@dataclass
class RenderedEdge:
    """Edge validation result from rendered visualization."""

    source: str
    target: str
    source_label: Optional[str] = None
    target_label: Optional[str] = None
    src_bottom: Optional[float] = None
    tgt_top: Optional[float] = None
    vert_dist: Optional[float] = None
    horiz_dist: Optional[float] = None
    status: str = "OK"
    issue: Optional[str] = None


@dataclass
class RenderedDebugData:
    """Debug data extracted from rendered visualization."""

    version: int
    timestamp: int
    nodes: list[dict[str, Any]]
    edges: list[RenderedEdge]
    summary: dict[str, int]

    @property
    def edge_issues(self) -> list[RenderedEdge]:
        """Return edges with issues."""
        return [e for e in self.edges if e.status != "OK"]

    def print_report(self) -> None:
        """Print a human-readable report."""
        print(f"=== Rendered Debug Data ===")
        print(f"Nodes: {self.summary.get('totalNodes', 0)}")
        print(f"Edges: {self.summary.get('totalEdges', 0)}")
        print(f"Edge Issues: {self.summary.get('edgeIssues', 0)}")

        if self.edge_issues:
            print("\n--- Edge Issues ---")
            for e in self.edge_issues:
                print(f"  {e.source} -> {e.target}: {e.issue}")
                print(f"    srcBottom={e.src_bottom}, tgtTop={e.tgt_top}, "
                      f"vDist={e.vert_dist}, hDist={e.horiz_dist}")
        else:
            print("\nNo edge issues found.")


def extract_debug_data(
    graph: "Graph",
    *,
    depth: int = 1,
    theme: str = "auto",
    headless: bool = True,
    timeout: int = 5000,
) -> RenderedDebugData:
    """Extract debug data from rendered visualization using Playwright.

    Renders the graph in a headless browser and extracts the edge validation
    data computed by the JavaScript layout engine.

    Args:
        graph: Graph to visualize and debug
        depth: How many levels of nested graphs to expand (default: 1)
        theme: "dark", "light", or "auto" (default: "auto")
        headless: Run browser in headless mode (default: True)
        timeout: Max time to wait for layout in ms (default: 5000)

    Returns:
        RenderedDebugData with nodes, edges, and validation results

    Raises:
        ImportError: If playwright is not installed
        TimeoutError: If layout doesn't complete in time

    Example:
        >>> data = extract_debug_data(graph)
        >>> data.print_report()
        >>> for edge in data.edge_issues:
        ...     print(f"{edge.source} -> {edge.target}: {edge.issue}")
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ImportError(
            "playwright is required for extract_debug_data. "
            "Install with: pip install playwright && playwright install chromium"
        )

    import tempfile
    from hypergraph.viz.widget import visualize

    # Render to temp HTML file
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        temp_path = f.name

    visualize(graph, depth=depth, theme=theme, output=temp_path, _debug_overlays=True)

    # Open in headless browser and extract debug data
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.goto(f"file://{temp_path}")

        # Wait for layout to complete (check for __hypergraphVizDebug)
        page.wait_for_function(
            "window.__hypergraphVizDebug && window.__hypergraphVizDebug.version > 0",
            timeout=timeout,
        )

        # Extract debug data
        debug_data = page.evaluate("window.__hypergraphVizDebug")
        browser.close()

    # Clean up temp file
    import os
    os.unlink(temp_path)

    # Parse into dataclass
    edges = [
        RenderedEdge(
            source=e.get("source", ""),
            target=e.get("target", ""),
            source_label=e.get("sourceLabel"),
            target_label=e.get("targetLabel"),
            src_bottom=e.get("srcBottom"),
            tgt_top=e.get("tgtTop"),
            vert_dist=e.get("vertDist"),
            horiz_dist=e.get("horizDist"),
            status=e.get("status", "OK"),
            issue=e.get("issue"),
        )
        for e in debug_data.get("edges", [])
    ]

    return RenderedDebugData(
        version=debug_data.get("version", 0),
        timestamp=debug_data.get("timestamp", 0),
        nodes=debug_data.get("nodes", []),
        edges=edges,
        summary=debug_data.get("summary", {}),
    )
