"""Headless smoke tests for the split no-build visualization modules."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hypergraph.viz.assets import FIRST_PARTY_ASSET_NAMES

ASSETS = Path(__file__).resolve().parents[2] / "src" / "hypergraph" / "viz" / "assets"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js not installed")


def test_split_viz_modules_attach_expected_globals() -> None:
    """All first-party viz modules load in order and expose the app init hook."""
    files = list(FIRST_PARTY_ASSET_NAMES)
    script = f"""
        const fs = require('fs');
        const vm = require('vm');
        const path = require('path');
        const assets = {json.dumps(str(ASSETS))};
        const root = {{
          console,
          React: {{
            createElement: () => ({{}}),
            Fragment: 'Fragment',
            useState: () => [undefined, () => {{}}],
            useEffect: () => {{}},
            useMemo: (fn) => fn(),
            useCallback: (fn) => fn,
            useRef: (value) => ({{ current: value }}),
          }},
          ReactDOM: {{ createRoot: () => ({{ render: () => {{}} }}) }},
          ReactFlow: {{
            ReactFlow: 'ReactFlow',
            Background: 'Background',
            Panel: 'Panel',
            Position: {{ Top: 'top', Bottom: 'bottom' }},
            MarkerType: {{ ArrowClosed: 'arrowclosed' }},
            ReactFlowProvider: 'ReactFlowProvider',
            Handle: 'Handle',
            BaseEdge: 'BaseEdge',
            EdgeLabelRenderer: 'EdgeLabelRenderer',
            useNodesState: () => [[], () => {{}}, () => {{}}],
            useEdgesState: () => [[], () => {{}}, () => {{}}],
            useReactFlow: () => ({{
              zoomIn() {{}},
              zoomOut() {{}},
              setViewport() {{}},
              getViewport: () => ({{ x: 0, y: 0, zoom: 1 }}),
            }}),
            useUpdateNodeInternals: () => () => {{}},
            getBezierPath: () => ['M0 0L1 1'],
          }},
          htm: function () {{}},
          dagre: {{ graphlib: {{ Graph: function () {{}} }}, layout() {{}} }},
        }};
        root.window = root;
        root.self = root;
        root.globalThis = root;
        root.htm.bind = () => function () {{ return {{}}; }};
        const context = vm.createContext(root);
        for (const file of {json.dumps(files)}) {{
          vm.runInContext(fs.readFileSync(path.join(assets, file), 'utf8'), context, {{ filename: file }});
        }}
        const globals = [
          'HypergraphDerivation',
          'HypergraphSceneBuilder',
          'HypergraphVizRuntime',
          'HypergraphVizLayout',
          'HypergraphVizEdges',
          'HypergraphVizNodes',
          'HypergraphVizControls',
          'HypergraphVizDebug',
          'HypergraphViz',
        ];
        const missing = globals.filter((name) => !context[name]);
        if (missing.length) throw new Error('Missing globals: ' + missing.join(', '));
        if (typeof context.HypergraphViz.init !== 'function') throw new Error('HypergraphViz.init missing');
    """
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=10)

    assert proc.returncode == 0, proc.stderr


def test_tooltip_button_exposes_accessible_name_and_keyboard_handlers() -> None:
    controls_source = (ASSETS / "viz_controls.js").read_text()

    assert "aria-label" in controls_source
    assert 'role="tooltip"' in controls_source
    assert "onFocus" in controls_source
    assert "onBlur" in controls_source
    assert "onKeyDown" in controls_source
    assert "Escape" in controls_source
