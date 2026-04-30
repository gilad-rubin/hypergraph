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
    var showInputs = opts.showInputs !== false;

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
      var hidden = !showInputs || inputHidden(ext.deepest_owner, parentMap, expansionState);
      sceneNodes.push({
        id: 'input_' + ext.name,
        type: 'custom',
        position: { x: 0, y: 0 },
        data: {
          nodeType: 'INPUT',
          label: ext.name,
          typeHint: ext.type_hint,
          isBound: !!ext.is_bound,
          deepestOwnerContainer: ext.deepest_owner,
          actualTargets: (ext.consumers || []).slice(),
        },
        sourcePosition: 'bottom',
        targetPosition: 'top',
        hidden: hidden,
      });
    }

    if (separateOutputs) {
      // Materialize DATA scene nodes — one per (producer, output_name).
      for (var dn = 0; dn < ir.nodes.length; dn++) {
        var producer = ir.nodes[dn];
        if (producer.node_type !== 'FUNCTION' && producer.node_type !== 'GRAPH') continue;
        var producerOutputs = producer.outputs || [];
        for (var dn2 = 0; dn2 < producerOutputs.length; dn2++) {
          var pout = producerOutputs[dn2];
          var dataId = 'data_' + producer.id + '_' + pout.name;
          var ancestorHidden = ancestorCollapsed(producer.id, parentMap, expansionState);
          var dataNode = {
            id: dataId,
            type: 'custom',
            position: { x: 0, y: 0 },
            data: {
              nodeType: 'DATA',
              label: pout.name,
              typeHint: pout.type,
              sourceId: producer.id,
            },
            sourcePosition: 'bottom',
            targetPosition: 'top',
            hidden: ancestorHidden,
          };
          if (producer.parent) {
            dataNode.parentNode = producer.parent;
            dataNode.extent = 'parent';
          }
          sceneNodes.push(dataNode);
        }
      }
    }

    var visibleIds = {};
    for (var m = 0; m < sceneNodes.length; m++) {
      if (!sceneNodes[m].hidden) visibleIds[sceneNodes[m].id] = true;
    }

    var sceneEdges = [];

    for (var p = 0; p < ir.edges.length; p++) {
      var irEdge = ir.edges[p];
      var src = irEdge.source;
      if (expansionState[src] && irEdge.source_when_expanded) src = irEdge.source_when_expanded;
      var tgt = irEdge.target;
      if (expansionState[tgt] && irEdge.target_when_expanded) tgt = irEdge.target_when_expanded;

      // separate_outputs mode reroutes data edges through DATA nodes:
      // producer -> data_<producer>_<value> -> consumer
      var valueNames = irEdge.value_names || [];
      if (separateOutputs && irEdge.edge_type === 'data' && valueNames.length > 0) {
        src = 'data_' + src + '_' + valueNames[0];
      }

      sceneEdges.push({
        id: src + '__' + tgt,
        source: src,
        target: tgt,
        data: { edgeType: irEdge.edge_type },
        hidden: !visibleIds[src] || !visibleIds[tgt],
      });
    }

    if (separateOutputs) {
      for (var oe = 0; oe < ir.nodes.length; oe++) {
        var oeNode = ir.nodes[oe];
        if (oeNode.node_type !== 'FUNCTION' && oeNode.node_type !== 'GRAPH') continue;
        var oeOutputs = oeNode.outputs || [];
        for (var oe2 = 0; oe2 < oeOutputs.length; oe2++) {
          var oeOut = oeOutputs[oe2];
          var oeData = 'data_' + oeNode.id + '_' + oeOut.name;
          sceneEdges.push({
            id: oeNode.id + '__' + oeData,
            source: oeNode.id,
            target: oeData,
            data: { edgeType: 'output' },
            hidden: !visibleIds[oeNode.id] || !visibleIds[oeData],
          });
        }
      }
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
