// Pure graph-walk primitives over a GraphIR + expansion state.
//
// This module exposes only state-derivable facts: visibility,
// expansion-aware routing, container entrypoint resolution. It does NOT
// know about React Flow, layout, or styling — that's scene_builder's
// job.
//
// Two consumers:
//   1. assets/scene_builder.js — assembles a React Flow scene from these
//      primitives.
//   2. Node-side unit tests (tests/viz/test_derivation_js.py drives
//      `node` to import this file directly).
//
// Both browser and Node load this file by side-effect; the API attaches
// to `global.HypergraphDerivation` either way (window in the browser,
// globalThis in Node).

(function (global) {
  'use strict';

  // ── Visibility ──────────────────────────────────────────────────────────

  // True if any ancestor of nodeId in parentMap is currently collapsed
  // (or has no expansion entry, which means collapsed by default).
  function ancestorCollapsed(nodeId, parentMap, expansionState) {
    var current = nodeId;
    while (true) {
      var parent = parentMap[current];
      if (parent === undefined || parent === null) return false;
      if (!expansionState[parent]) return true;
      current = parent;
    }
  }

  // True if an external INPUT scoped to `deepestOwner` should be hidden:
  // its deepest container, or any of that container's ancestors, must be
  // collapsed. A null deepestOwner means root-scoped — always visible.
  function inputHidden(deepestOwner, parentMap, expansionState) {
    if (deepestOwner === null || deepestOwner === undefined) return false;
    if (!expansionState[deepestOwner]) return true;
    var current = parentMap[deepestOwner];
    while (current !== undefined && current !== null) {
      if (!expansionState[current]) return true;
      current = parentMap[current];
    }
    return false;
  }

  // Walk up from nodeId to the first ancestor whose id is in visibleIds.
  // Returns null if every ancestor (including nodeId) is hidden.
  function resolveToVisible(nodeId, parentMap, visibleIds) {
    var current = nodeId;
    while (current !== undefined && current !== null && !visibleIds[current]) {
      current = parentMap[current];
    }
    return (current === undefined || current === null) ? null : current;
  }

  // Walk up from deepestOwner to the first ancestor that is currently
  // expanded — i.e. the container an INPUT visually nests inside at the
  // current state. Used for `data.ownerContainer`.
  function visibleOwner(deepestOwner, parentMap, expansionState) {
    if (deepestOwner === null || deepestOwner === undefined) return null;
    var current = deepestOwner;
    while (current !== undefined && current !== null && !expansionState[current]) {
      current = parentMap[current];
    }
    return (current === undefined || current === null) ? null : current;
  }

  // ── Expansion-aware routing ────────────────────────────────────────────

  // For each currently-expanded GRAPH, return the inner child(ren) that
  // should receive START / control edges that would otherwise attach to
  // the container hull. Consumes the canonical `ir.container_entrypoints`
  // field computed once by the Python IR builder (locked decision D14,
  // #211) — the JS side never re-derives entrypoints from node
  // inputs/outputs.
  function expandedContainerEntrypoints(ir, expansionState) {
    var canonical = (ir && ir.container_entrypoints) || {};
    var overrides = {};
    for (var containerId in canonical) {
      if (!Object.prototype.hasOwnProperty.call(canonical, containerId)) continue;
      if (!expansionState[containerId]) continue;
      var entrypoints = canonical[containerId] || [];
      overrides[containerId] = entrypoints.slice();
    }
    return overrides;
  }

  // Recursively replace expanded containers with their canonical descendants
  // while preserving every plain sibling in declaration order.
  function resolveExpandedEntrypoints(entry, overrides) {
    var resolved = [];
    var pending = [entry];

    while (pending.length > 0) {
      var target = pending.pop();
      if (Object.prototype.hasOwnProperty.call(overrides, target)) {
        var replacement = overrides[target];
        if (replacement.length > 0) {
          for (var r = replacement.length - 1; r >= 0; r--) {
            pending.push(replacement[r]);
          }
        }
      } else if (resolved.indexOf(target) === -1) {
        resolved.push(target);
      }
    }

    return resolved;
  }

  // ── Branch / END routing ────────────────────────────────────────────────

  // True if a gate's branch_data routes to the END sentinel via any
  // when_true / when_false / targets entry.
  function routesToEnd(branchData) {
    if (!branchData) return false;
    if (branchData.when_true === 'END' || branchData.when_false === 'END') return true;
    var targets = branchData.targets;
    if (targets && typeof targets === 'object' && !Array.isArray(targets)) {
      for (var k in targets) if (targets[k] === 'END') return true;
    }
    if (Array.isArray(targets)) {
      for (var i = 0; i < targets.length; i++) if (targets[i] === 'END') return true;
    }
    return false;
  }

  // ── Helpers ─────────────────────────────────────────────────────────────

  function buildParentMap(ir) {
    var parentMap = {};
    for (var i = 0; i < ir.nodes.length; i++) {
      var n = ir.nodes[i];
      if (n.parent) parentMap[n.id] = n.parent;
    }
    return parentMap;
  }

  function sceneNodeType(irNodeType) {
    if (irNodeType === 'GRAPH') return 'PIPELINE';
    return irNodeType;
  }

  // ── Export ──────────────────────────────────────────────────────────────

  global.HypergraphDerivation = {
    ancestorCollapsed: ancestorCollapsed,
    inputHidden: inputHidden,
    resolveToVisible: resolveToVisible,
    visibleOwner: visibleOwner,
    expandedContainerEntrypoints: expandedContainerEntrypoints,
    resolveExpandedEntrypoints: resolveExpandedEntrypoints,
    routesToEnd: routesToEnd,
    buildParentMap: buildParentMap,
    sceneNodeType: sceneNodeType,
  };
})(typeof window !== 'undefined' ? window : globalThis);
