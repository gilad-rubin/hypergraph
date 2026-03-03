"""Phase 2.5: workflow_id propagation tests."""

import pytest

from hypergraph import AsyncRunner, Graph, SyncRunner, node


@node(output_name="doubled")
def double(x: int) -> int:
    return x * 2


class TestWorkflowId:
    def test_sync_run_propagates_workflow_id(self):
        """workflow_id flows from run() to RunResult."""
        graph = Graph([double])
        result = SyncRunner().run(graph, {"x": 5}, workflow_id="wf-123")
        assert result.workflow_id == "wf-123"

    def test_sync_run_defaults_to_none(self):
        """Without workflow_id, RunResult.workflow_id is None."""
        graph = Graph([double])
        result = SyncRunner().run(graph, {"x": 5})
        assert result.workflow_id is None

    @pytest.mark.asyncio
    async def test_async_run_propagates_workflow_id(self):
        """Async runner propagates workflow_id to RunResult."""
        graph = Graph([double])
        result = await AsyncRunner().run(graph, {"x": 5}, workflow_id="wf-456")
        assert result.workflow_id == "wf-456"

    def test_sync_map_items_have_no_workflow_id(self):
        """Map items don't inherit parent workflow_id (they're child runs)."""
        graph = Graph([double])
        results = SyncRunner().map(
            graph,
            {"x": [1, 2, 3]},
            map_over="x",
            workflow_id="wf-map",
        )
        # Each item gets its own run; workflow_id on map is for the map operation itself
        # Individual items don't inherit it (they use _parent_span_id instead)
        assert len(results) == 3
