"""Sync executor for GraphNode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hypergraph.runners._shared.helpers import map_inputs_to_func_params
from hypergraph.runners._shared.types import RunResult, RunStatus

if TYPE_CHECKING:
    from hypergraph.nodes.graph_node import GraphNode
    from hypergraph.runners._shared.types import GraphState
    from hypergraph.runners.sync.runner import SyncRunner


class SyncGraphNodeExecutor:
    """Executes GraphNode by delegating to runner.

    Handles:
    - Simple nested graph execution
    - Map-over execution (delegates to runner.map())
    """

    def __init__(self, runner: "SyncRunner"):
        """Initialize with reference to parent runner.

        Args:
            runner: The SyncRunner that owns this executor
        """
        self.runner = runner

    def __call__(
        self,
        node: "GraphNode",
        state: "GraphState",
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a GraphNode by running its inner graph.

        Args:
            node: The GraphNode to execute
            state: Current graph execution state (unused directly)
            inputs: Input values for the nested graph

        Returns:
            Dict mapping output names to their values
        """
        # Translate renamed input keys back to original inner graph names
        inner_inputs = map_inputs_to_func_params(node, inputs)

        map_config = node.map_config

        if map_config:
            _, mode = map_config
            # Use original param names for map_over (inner graph expects these)
            original_params = node._original_map_params()
            results = self.runner.map(
                node.graph, inner_inputs, map_over=original_params, map_mode=mode
            )
            return self._collect_as_lists(results, node)

        result = self.runner.run(node.graph, inner_inputs)
        if result.status == RunStatus.FAILED:
            raise result.error or RuntimeError("Nested graph execution failed")
        return result.values

    def _collect_as_lists(
        self,
        results: list[RunResult],
        node: "GraphNode",
    ) -> dict[str, list]:
        """Collect multiple RunResults into lists per output.

        Handles output name translation: inner graph produces original names,
        but we need to return renamed names to match the GraphNode's interface.

        Args:
            results: List of RunResult from runner.map()
            node: The GraphNode (used for output name translation)

        Returns:
            Dict mapping renamed output names to lists of values
        """
        collected: dict[str, list] = {name: [] for name in node.outputs}
        for result in results:
            if result.status == RunStatus.FAILED:
                raise result.error or RuntimeError("Nested graph execution failed")
            # Translate original output names to renamed names
            renamed_values = node.map_outputs_from_original(result.values)
            for name in node.outputs:
                if name in renamed_values:
                    collected[name].append(renamed_values[name])
        return collected
