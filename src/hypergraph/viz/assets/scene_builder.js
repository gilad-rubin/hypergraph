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
    var showBoundedInputs = !!opts.showBoundedInputs;
    var outputVisibility = ir.graph_output_visibility || {};

    for (var j = 0; j < ir.nodes.length; j++) {
      var irNode = ir.nodes[j];
      var sceneType = scenenodeType(irNode.node_type);
      var isExpanded = irNode.node_type === 'GRAPH' ? !!expansionState[irNode.id] : null;
      var rfType = sceneType === 'PIPELINE' && isExpanded ? 'pipelineGroup' : 'custom';

      var inputs = (irNode.inputs || []).map(function (i) {
        return Object.assign({}, i);
      });

      var data = {
        nodeType: sceneType,
        label: irNode.label || irNode.id,
        separateOutputs: separateOutputs,
        inputs: inputs,
      };
      if (!separateOutputs && (sceneType === 'FUNCTION' || sceneType === 'PIPELINE')) {
        var nodeOutputs = (irNode.outputs || []).slice();
        if (sceneType === 'PIPELINE' && outputVisibility[irNode.id]) {
          var visibleSet = {};
          for (var vi = 0; vi < outputVisibility[irNode.id].length; vi++) {
            visibleSet[outputVisibility[irNode.id][vi]] = true;
          }
          nodeOutputs = nodeOutputs.filter(function (o) { return visibleSet[o.name]; });
        }
        data.outputs = nodeOutputs;
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
      if (ext.is_bound && !showBoundedInputs) continue;
      var hidden = !showInputs || inputHidden(ext.deepest_owner, parentMap, expansionState);
      var params = ext.params || [];
      var typeHints = ext.type_hints || [];
      var isGroup = params.length > 1;
      var inputId = isGroup ? 'input_group_' + params.join('_') : 'input_' + params[0];
      var data = isGroup
        ? {
            nodeType: 'INPUT_GROUP',
            params: params.slice(),
            paramTypes: typeHints.slice(),
            isBound: !!ext.is_bound,
            deepestOwnerContainer: ext.deepest_owner,
            actualTargets: (ext.consumers || []).slice(),
          }
        : {
            nodeType: 'INPUT',
            label: params[0],
            typeHint: typeHints[0] || null,
            isBound: !!ext.is_bound,
            deepestOwnerContainer: ext.deepest_owner,
            actualTargets: (ext.consumers || []).slice(),
          };
      sceneNodes.push({
        id: inputId,
        type: 'custom',
        position: { x: 0, y: 0 },
        data: data,
        sourcePosition: 'bottom',
        targetPosition: 'top',
        hidden: hidden,
      });
    }

    if (separateOutputs) {
      // Materialize DATA scene nodes — one per (producer, output_name).
      for (var dn = 0; dn < ir.nodes.length; dn++) {
        var producer = ir.nodes[dn];
        if (producer.node_type !== 'FUNCTION' && producer.node_type !== 'GRAPH' && producer.node_type !== 'BRANCH') continue;
        var producerOutputs = producer.outputs || [];
        var visibleProducerOutputs = null;
        if (producer.node_type === 'GRAPH' && outputVisibility[producer.id]) {
          visibleProducerOutputs = {};
          for (var po = 0; po < outputVisibility[producer.id].length; po++) {
            visibleProducerOutputs[outputVisibility[producer.id][po]] = true;
          }
        }
        for (var dn2 = 0; dn2 < producerOutputs.length; dn2++) {
          var pout = producerOutputs[dn2];
          if (pout.is_gate_internal) continue;
          if (visibleProducerOutputs && !visibleProducerOutputs[pout.name]) continue;
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
              internalOnly: !!pout.internal_only,
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
        data: {
          edgeType: irEdge.edge_type,
          valueName: valueNames.length > 0 ? valueNames[0] : null,
          label: (irEdge.label === undefined ? null : irEdge.label),
          exclusive: !!irEdge.exclusive,
          forceFeedback: !!irEdge.is_back_edge,
        },
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
      var ext2Params = ext2.params || [];
      var inputNodeId = ext2Params.length > 1
        ? 'input_group_' + ext2Params.join('_')
        : 'input_' + ext2Params[0];
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

    addStartEndNodesAndEdges(ir, sceneNodes, sceneEdges, parentMap, expansionState, visibleIds);

    return { nodes: sceneNodes, edges: sceneEdges };
  }

  function syntheticNode(id, nodeType, label) {
    return {
      id: id,
      type: 'custom',
      position: { x: 0, y: 0 },
      data: { nodeType: nodeType, label: label },
      sourcePosition: 'bottom',
      targetPosition: 'top',
      hidden: false,
    };
  }

  function resolveToVisible(nodeId, parentMap, visibleIds) {
    var current = nodeId;
    while (current !== undefined && current !== null && !visibleIds[current]) {
      current = parentMap[current];
    }
    return (current === undefined || current === null) ? null : current;
  }

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

  function addStartEndNodesAndEdges(ir, sceneNodes, sceneEdges, parentMap, expansionState, visibleIds) {
    var configured = ir.configured_entrypoints || [];
    var startTargets = [];
    var seenStart = {};
    for (var i = 0; i < configured.length; i++) {
      var resolved = resolveToVisible(configured[i], parentMap, visibleIds);
      if (resolved && !seenStart[resolved]) {
        seenStart[resolved] = true;
        startTargets.push(resolved);
      }
    }
    if (startTargets.length > 0) {
      sceneNodes.push(syntheticNode('__start__', 'START', 'Start'));
      for (var s = 0; s < startTargets.length; s++) {
        var target = startTargets[s];
        sceneEdges.push({
          id: '__start____' + target,
          source: '__start__',
          target: target,
          data: { edgeType: 'start' },
          hidden: false,
        });
      }
    }

    var endSources = [];
    var seenEnd = {};
    for (var j = 0; j < ir.nodes.length; j++) {
      var node = ir.nodes[j];
      if (!routesToEnd(node.branch_data)) continue;
      var resolvedSrc = resolveToVisible(node.id, parentMap, visibleIds);
      if (resolvedSrc && !seenEnd[resolvedSrc]) {
        seenEnd[resolvedSrc] = true;
        endSources.push(resolvedSrc);
      }
    }
    if (endSources.length > 0) {
      sceneNodes.push(syntheticNode('__end__', 'END', 'End'));
      for (var e = 0; e < endSources.length; e++) {
        var source = endSources[e];
        sceneEdges.push({
          id: source + '____end__',
          source: source,
          target: '__end__',
          data: { edgeType: 'end' },
          hidden: false,
        });
      }
    }
  }

  global.HypergraphSceneBuilder = {
    buildInitialScene: buildInitialScene,
  };
})(typeof window !== 'undefined' ? window : globalThis);
