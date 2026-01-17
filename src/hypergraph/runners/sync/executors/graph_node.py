"""Sync executor for GraphNode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
        map_config = node.map_config

        if map_config:
            params, mode = map_config
            results = self.runner.map(node.graph, inputs, map_over=params, map_mode=mode)
            return self._collect_as_lists(results, node.outputs)

        result = self.runner.run(node.graph, inputs)
        if result.status == RunStatus.FAILED:
            raise result.error or RuntimeError("Nested graph execution failed")
        return result.values

    def _collect_as_lists(
        self,
        results: list[RunResult],
        outputs: tuple[str, ...],
    ) -> dict[str, list]:
        """Collect multiple RunResults into lists per output.

        Args:
            results: List of RunResult from runner.map()
            outputs: Output names to collect

        Returns:
            Dict mapping output names to lists of values
        """
        collected: dict[str, list] = {name: [] for name in outputs}
        for result in results:
            if result.status == RunStatus.FAILED:
                raise result.error or RuntimeError("Nested graph execution failed")
            for name in outputs:
                if name in result.values:
                    collected[name].append(result.values[name])
        return collected
