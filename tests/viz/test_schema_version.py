"""Schema-version forward-compat contract.

The IR carries a ``schema_version`` field. Both Python and JS scene
builders pin to a single supported version; encountering anything else
must surface as a typed error / mismatch sentinel so frontends can fall
back to a static view rather than crash silently. This is the one rule
the save+reopen contract relies on (PR #88, Stage 4).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from hypergraph.viz.ir_schema import CURRENT_SCHEMA_VERSION, GraphIR, IRSchemaError
from hypergraph.viz.renderer.ir_builder import build_graph_ir
from hypergraph.viz.scene_builder import build_initial_scene
from tests.viz.conftest import make_simple_graph

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = Path(__file__).resolve().parent / "_parity_runner.js"
NODE = shutil.which("node")


def test_current_schema_version_pinned() -> None:
    """A bump must be deliberate — pin both ends so the hard-coded JS
    constant in scene_builder.js stays in lockstep with Python."""
    assert CURRENT_SCHEMA_VERSION == "2"


def test_build_graph_ir_emits_current_schema_version() -> None:
    flat = make_simple_graph().to_flat_graph()
    ir = build_graph_ir(flat)
    assert ir.schema_version == CURRENT_SCHEMA_VERSION


def test_default_graph_ir_has_current_schema_version() -> None:
    """An empty IR (no nodes/edges) still ships a schema_version so
    frontends can decide whether to render or fall back."""
    assert GraphIR().schema_version == CURRENT_SCHEMA_VERSION


def test_python_scene_builder_rejects_unsupported_version() -> None:
    flat = make_simple_graph().to_flat_graph()
    ir = build_graph_ir(flat)
    future_ir = replace(ir, schema_version="999")
    with pytest.raises(IRSchemaError, match="schema_version"):
        build_initial_scene(future_ir)


@pytest.mark.skipif(NODE is None, reason="Node.js not installed")
def test_js_scene_builder_returns_mismatch_sentinel_for_future_version() -> None:
    flat = make_simple_graph().to_flat_graph()
    ir = build_graph_ir(flat)
    ir_dict = asdict(ir)
    ir_dict["schema_version"] = "999"
    payload = json.dumps({"ir": ir_dict, "opts": {}})
    proc = subprocess.run(
        [NODE, str(RUNNER), str(REPO_ROOT)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    scene = json.loads(proc.stdout)
    assert scene.get("schemaVersionMismatch") == {"got": "999", "supported": "2"}
    # The mismatch-sentinel must short-circuit derivation entirely so the
    # frontend can render the static fallback without dragging in any
    # potentially-stale interpretation of the IR.
    assert scene["nodes"] == []
    assert scene["edges"] == []


@pytest.mark.skipif(NODE is None, reason="Node.js not installed")
def test_js_scene_builder_accepts_current_version() -> None:
    flat = make_simple_graph().to_flat_graph()
    ir = build_graph_ir(flat)
    ir_dict = asdict(ir)
    payload = json.dumps({"ir": ir_dict, "opts": {}})
    proc = subprocess.run(
        [NODE, str(RUNNER), str(REPO_ROOT)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, proc.stderr
    scene = json.loads(proc.stdout)
    assert "schemaVersionMismatch" not in scene
    assert scene["nodes"]
