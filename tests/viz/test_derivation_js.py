"""Headless Node tests for ``assets/derivation.js``.

Loads the JS module via Node, calls each primitive against synthetic
fixtures, and asserts the output matches what ``scene_builder.py`` would
produce against the same IR shape. This is the JS-side unit gate that
catches drift between the two derivation libraries before the full
parity harness runs (Stage 3).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

DERIVATION_JS = Path(__file__).resolve().parents[2] / "src" / "hypergraph" / "viz" / "assets" / "derivation.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js not installed")


def _run(snippet: str) -> dict:
    """Run a derivation.js primitive in Node and return the JSON result."""
    script = f"""
        const fs = require('fs');
        eval(fs.readFileSync({json.dumps(str(DERIVATION_JS))}, 'utf-8'));
        const D = globalThis.HypergraphDerivation;
        const result = (function () {{ {snippet} }})();
        process.stdout.write(JSON.stringify(result));
    """
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        raise RuntimeError(f"node failed: {proc.stderr}")
    return json.loads(proc.stdout)


def test_module_attaches_to_globalthis():
    keys = _run("return Object.keys(D).sort();")
    assert keys == sorted(
        [
            "ancestorCollapsed",
            "buildParentMap",
            "expandedContainerEntrypoints",
            "inputHidden",
            "resolveToVisible",
            "routesToEnd",
            "sceneNodeType",
            "visibleOwner",
        ]
    )


def test_ancestor_collapsed_walks_to_root():
    result = _run("""
        const parentMap = {child: 'mid', mid: 'root'};
        return {
            allExpanded: D.ancestorCollapsed('child', parentMap, {mid: true, root: true}),
            midCollapsed: D.ancestorCollapsed('child', parentMap, {root: true}),
            rootCollapsed: D.ancestorCollapsed('child', parentMap, {mid: true}),
            noParent: D.ancestorCollapsed('orphan', {}, {}),
        };
    """)
    assert result == {
        "allExpanded": False,
        "midCollapsed": True,
        "rootCollapsed": True,
        "noParent": False,
    }


def test_input_hidden_root_scope_always_visible():
    result = _run("""
        return {
            rootScope: D.inputHidden(null, {}, {}),
            collapsedOwner: D.inputHidden('inner', {}, {}),
            expandedOwner: D.inputHidden('inner', {}, {inner: true}),
            ancestorCollapsed: D.inputHidden('inner', {inner: 'outer'}, {inner: true}),
            allExpanded: D.inputHidden('inner', {inner: 'outer'}, {inner: true, outer: true}),
        };
    """)
    assert result == {
        "rootScope": False,
        "collapsedOwner": True,
        # Owner expanded, no further ancestor → visible (not hidden).
        "expandedOwner": False,
        "ancestorCollapsed": True,
        "allExpanded": False,
    }


def test_resolve_to_visible_walks_up_until_visible():
    result = _run("""
        const parentMap = {leaf: 'mid', mid: 'root'};
        return {
            leafVisible: D.resolveToVisible('leaf', parentMap, {leaf: true, mid: true, root: true}),
            walkToMid: D.resolveToVisible('leaf', parentMap, {mid: true, root: true}),
            walkToRoot: D.resolveToVisible('leaf', parentMap, {root: true}),
            allHidden: D.resolveToVisible('leaf', parentMap, {}),
        };
    """)
    assert result == {
        "leafVisible": "leaf",
        "walkToMid": "mid",
        "walkToRoot": "root",
        "allHidden": None,
    }


def test_visible_owner_picks_first_expanded_ancestor():
    result = _run("""
        const parentMap = {inner: 'middle', middle: 'outer'};
        return {
            innerExpanded: D.visibleOwner('inner', parentMap, {inner: true}),
            middleExpanded: D.visibleOwner('inner', parentMap, {middle: true}),
            outerExpanded: D.visibleOwner('inner', parentMap, {outer: true}),
            allCollapsed: D.visibleOwner('inner', parentMap, {}),
            rootScope: D.visibleOwner(null, {}, {}),
        };
    """)
    assert result == {
        "innerExpanded": "inner",
        "middleExpanded": "middle",
        "outerExpanded": "outer",
        "allCollapsed": None,
        "rootScope": None,
    }


def test_expanded_container_entrypoints_picks_first_child():
    result = _run("""
        const ir = {nodes: [
            {id: 'outer', node_type: 'GRAPH', parent: null},
            {id: 'outer/first', node_type: 'FUNCTION', parent: 'outer'},
            {id: 'outer/second', node_type: 'FUNCTION', parent: 'outer'},
            {id: 'collapsed', node_type: 'GRAPH', parent: null},
            {id: 'collapsed/x', node_type: 'FUNCTION', parent: 'collapsed'},
        ]};
        return D.expandedContainerEntrypoints(ir, {outer: true});
    """)
    assert result == {"outer": "outer/first"}


def test_routes_to_end_handles_all_branch_shapes():
    result = _run("""
        return {
            none: D.routesToEnd(null),
            empty: D.routesToEnd({}),
            whenTrueEnd: D.routesToEnd({when_true: 'END', when_false: 'other'}),
            whenFalseEnd: D.routesToEnd({when_true: 'other', when_false: 'END'}),
            targetsDictEnd: D.routesToEnd({targets: {accept: 'next', reject: 'END'}}),
            targetsListEnd: D.routesToEnd({targets: ['next', 'END']}),
            targetsNoEnd: D.routesToEnd({targets: ['a', 'b']}),
        };
    """)
    assert result == {
        "none": False,
        "empty": False,
        "whenTrueEnd": True,
        "whenFalseEnd": True,
        "targetsDictEnd": True,
        "targetsListEnd": True,
        "targetsNoEnd": False,
    }


def test_scene_node_type_maps_graph_to_pipeline():
    result = _run("""
        return {
            graph: D.sceneNodeType('GRAPH'),
            function: D.sceneNodeType('FUNCTION'),
            branch: D.sceneNodeType('BRANCH'),
        };
    """)
    assert result == {"graph": "PIPELINE", "function": "FUNCTION", "branch": "BRANCH"}


def test_build_parent_map_filters_root_nodes():
    result = _run("""
        const ir = {nodes: [
            {id: 'a', parent: null},
            {id: 'b', parent: 'a'},
            {id: 'c', parent: 'b'},
            {id: 'd'},
        ]};
        return D.buildParentMap(ir);
    """)
    assert result == {"b": "a", "c": "b"}
