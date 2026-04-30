// Build a React Flow scene from the compact IR. Mirrors
// src/hypergraph/viz/scene_builder.py — both implementations must
// produce semantically equivalent output for the same IR.
//
// IR shape (see hypergraph.viz.ir_schema):
//   { nodes: [{id, node_type, parent}],
//     edges: [{source, target, edge_type}],
//     expandable_nodes: [...],
//     external_inputs: [{name, deepest_owner, consumers}] }
//
// The IR carries pure-graph facts only. Expansion state, separate_outputs,
// and show_inputs are passed in as the second arg and re-derived here on
// each toggle — no Python round-trip needed.

(function (global) {
  'use strict';

  function scenenodeType(irNodeType) {
    if (irNodeType === 'GRAPH') return 'PIPELINE';
    return irNodeType;
  }

  function ancestorCollapsed(nodeId, parentMap, expansionState) {
    var current = nodeId;
    while (true) {
      var parent = parentMap[current];
      if (parent === undefined || parent === null) return false;
      if (!expansionState[parent]) return true;
      current = parent;
    }
  }

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

  function buildInitialScene(ir, opts) {
    opts = opts || {};
    var expansionState = opts.expansionState || {};

    var parentMap = {};
    for (var i = 0; i < ir.nodes.length; i++) {
      var n = ir.nodes[i];
      if (n.parent) parentMap[n.id] = n.parent;
    }

    var sceneNodes = [];

    var separateOutputs = !!opts.separateOutputs;

    for (var j = 0; j < ir.nodes.length; j++) {
      var irNode = ir.nodes[j];
      var sceneType = scenenodeType(irNode.node_type);
      var isExpanded = irNode.node_type === 'GRAPH' ? !!expansionState[irNode.id] : null;
      var rfType = sceneType === 'PIPELINE' && isExpanded ? 'pipelineGroup' : 'custom';

      var inputs = (irNode.inputs || []).map(function (i) {
        return Object.assign({}, i, { is_bound: false });
      });

      var data = {
        nodeType: sceneType,
        label: irNode.label || irNode.id,
        separateOutputs: separateOutputs,
        inputs: inputs,
      };
      if (!separateOutputs && (sceneType === 'FUNCTION' || sceneType === 'PIPELINE')) {
        data.outputs = (irNode.outputs || []).slice();
      }
      if (sceneType === 'PIPELINE') {
        data.isExpanded = !!isExpanded;
      }
      if (irNode.branch_data) {
        if (irNode.branch_data.when_true) {
          data.whenTrueTarget = irNode.branch_data.when_true;
          data.whenFalseTarget = irNode.branch_data.when_false;
        }
        if (irNode.branch_data.targets) {
          data.targets = irNode.branch_data.targets;
        }
      }

      var sceneNode = {
        id: irNode.id,
        type: rfType,
        position: { x: 0, y: 0 },
        data: data,
        sourcePosition: 'bottom',
        targetPosition: 'top',
        hidden: ancestorCollapsed(irNode.id, parentMap, expansionState),
      };
      if (irNode.parent) {
        sceneNode.parentNode = irNode.parent;
        sceneNode.extent = 'parent';
      }
      if (sceneType === 'PIPELINE' && isExpanded) {
        sceneNode.style = { width: 600, height: 400 };
      }
      sceneNodes.push(sceneNode);
    }

    var externalInputs = ir.external_inputs || [];
    for (var k = 0; k < externalInputs.length; k++) {
      var ext = externalInputs[k];
      sceneNodes.push({
        id: 'input_' + ext.name,
        data: {
          nodeType: 'INPUT',
          label: ext.name,
          deepestOwnerContainer: ext.deepest_owner,
        },
        hidden: inputHidden(ext.deepest_owner, parentMap, expansionState),
      });
    }

    var visibleIds = {};
    for (var m = 0; m < sceneNodes.length; m++) {
      if (!sceneNodes[m].hidden) visibleIds[sceneNodes[m].id] = true;
    }

    var sceneEdges = [];

    for (var p = 0; p < ir.edges.length; p++) {
      var irEdge = ir.edges[p];
      sceneEdges.push({
        id: irEdge.source + '__' + irEdge.target,
        source: irEdge.source,
        target: irEdge.target,
        data: { edgeType: irEdge.edge_type },
        hidden: !visibleIds[irEdge.source] || !visibleIds[irEdge.target],
      });
    }

    for (var q = 0; q < externalInputs.length; q++) {
      var ext2 = externalInputs[q];
      var inputNodeId = 'input_' + ext2.name;
      var consumers = ext2.consumers || [];
      for (var r = 0; r < consumers.length; r++) {
        var consumer = consumers[r];
        sceneEdges.push({
          id: inputNodeId + '__' + consumer,
          source: inputNodeId,
          target: consumer,
          data: { edgeType: 'input' },
          hidden: !visibleIds[inputNodeId] || !visibleIds[consumer],
        });
      }
    }

    return { nodes: sceneNodes, edges: sceneEdges };
  }

  global.HypergraphSceneBuilder = {
    buildInitialScene: buildInitialScene,
  };
})(typeof window !== 'undefined' ? window : globalThis);
