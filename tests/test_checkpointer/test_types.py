"""Tests for checkpointer types and CheckpointPolicy validation."""

import pytest

from hypergraph.checkpointers import CheckpointPolicy, Run, StepRecord, StepStatus, WorkflowStatus


class TestCheckpointPolicy:
    def test_default_policy(self):
        policy = CheckpointPolicy()
        assert policy.durability == "async"
        assert policy.retention == "full"
        assert policy.window is None
        assert policy.ttl is None

    def test_sync_full(self):
        policy = CheckpointPolicy(durability="sync", retention="full")
        assert policy.durability == "sync"

    def test_exit_latest(self):
        policy = CheckpointPolicy(durability="exit", retention="latest")
        assert policy.durability == "exit"

    def test_exit_full_raises(self):
        with pytest.raises(ValueError, match='durability="exit" requires retention="latest"'):
            CheckpointPolicy(durability="exit", retention="full")

    def test_exit_windowed_raises(self):
        with pytest.raises(ValueError, match='durability="exit" requires retention="latest"'):
            CheckpointPolicy(durability="exit", retention="windowed", window=10)

    def test_windowed_requires_window(self):
        with pytest.raises(ValueError, match="requires window parameter"):
            CheckpointPolicy(retention="windowed")

    def test_window_only_valid_with_windowed(self):
        with pytest.raises(ValueError, match="window parameter only valid"):
            CheckpointPolicy(retention="full", window=10)

    def test_windowed_with_window(self):
        policy = CheckpointPolicy(retention="windowed", window=50)
        assert policy.window == 50


class TestStepRecord:
    def test_frozen(self):
        record = StepRecord(
            run_id="wf-1",
            superstep=0,
            node_name="embed",
            index=0,
            status=StepStatus.COMPLETED,
            input_versions={"x": 1},
        )
        with pytest.raises(AttributeError):
            record.node_name = "other"  # type: ignore[misc]

    def test_to_dict(self):
        record = StepRecord(
            run_id="wf-1",
            superstep=0,
            node_name="embed",
            index=0,
            status=StepStatus.COMPLETED,
            input_versions={"x": 1},
            values={"embedding": [1, 2, 3]},
            duration_ms=42.5,
            cached=True,
        )
        d = record.to_dict()
        assert d["run_id"] == "wf-1"
        assert d["status"] == "completed"
        assert d["values"] == {"embedding": [1, 2, 3]}
        assert d["cached"] is True

    def test_defaults(self):
        record = StepRecord(
            run_id="wf-1",
            superstep=0,
            node_name="embed",
            index=0,
            status=StepStatus.COMPLETED,
            input_versions={},
        )
        assert record.values is None
        assert record.duration_ms == 0.0
        assert record.cached is False
        assert record.decision is None
        assert record.error is None
        assert record.child_run_id is None
        assert record.node_type is None

    def test_node_type_field(self):
        record = StepRecord(
            run_id="wf-1",
            superstep=0,
            node_name="embed",
            index=0,
            status=StepStatus.COMPLETED,
            input_versions={},
            node_type="FunctionNode",
        )
        assert record.node_type == "FunctionNode"
        assert record.to_dict()["node_type"] == "FunctionNode"


class TestRun:
    def test_to_dict(self):
        r = Run(id="wf-1", status=WorkflowStatus.ACTIVE, graph_name="my_graph")
        d = r.to_dict()
        assert d["id"] == "wf-1"
        assert d["status"] == "active"
        assert d["graph_name"] == "my_graph"

    def test_new_fields(self):
        r = Run(
            id="wf-1",
            status=WorkflowStatus.COMPLETED,
            duration_ms=150.5,
            node_count=3,
            error_count=1,
            parent_run_id="wf-parent",
        )
        d = r.to_dict()
        assert d["duration_ms"] == 150.5
        assert d["node_count"] == 3
        assert d["error_count"] == 1
        assert d["parent_run_id"] == "wf-parent"
