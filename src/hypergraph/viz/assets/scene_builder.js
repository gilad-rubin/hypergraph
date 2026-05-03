// Build a React Flow scene from the compact IR. Mirrors
// src/hypergraph/viz/scene_builder.py — both implementations must
// produce semantically equivalent output for the same IR.
//
// Pure graph-walk primitives live in derivation.js; this file consumes
// them to assemble React Flow nodes/edges. Load order: derivation.js
// must be evaluated before scene_builder.js.

(function (global) {
  'use strict';

  var D = global.HypergraphDerivation;
  if (!D) throw new Error('HypergraphDerivation not loaded — load derivation.js before scene_builder.js');

  // Pinned by Python via GraphIR.schema_version. Bump in lockstep with
  // ir_schema.py:CURRENT_SCHEMA_VERSION when the IR shape changes.
  var SUPPORTED_SCHEMA_VERSION = '2';

  function isSchemaSupported(ir) {
    if (!ir) return true;
    var v = ir.schema_version;
    return v === undefined || v === null || v === SUPPORTED_SCHEMA_VERSION;
  }

  var ancestorCollapsed = D.ancestorCollapsed;
  var inputHidden = D.inputHidden;
  var resolveToVisible = D.resolveToVisible;
  var visibleOwner = D.visibleOwner;
  var expandedContainerEntrypoints = D.expandedContainerEntrypoints;
  var routesToEnd = D.routesToEnd;
  var sceneNodeType = D.sceneNodeType;

  function buildInitialScene(ir, opts) {
    opts = opts || {};
    if (!isSchemaSupported(ir)) {
      return {
        nodes: [],
        edges: [],
        schemaVersionMismatch: {
          got: (ir && ir.schema_version) || null,
          supported: SUPPORTED_SCHEMA_VERSION,
        },
      };
    }
    var expansionState = opts.expansionState || {};

    var parentMap = D.buildParentMap(ir);

    var sceneNodes = [];

    var separateOutputs = !!opts.separateOutputs;
    var showInputs = opts.showInputs !== false;
    var showBoundedInputs = !!opts.showBoundedInputs;
    var outputVisibility = ir.graph_output_visibility || {};

    for (var j = 0; j < ir.nodes.length; j++) {
      var irNode = ir.nodes[j];
      var sceneType = sceneNodeType(irNode.node_type);
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
      // Mirror Python: when show_inputs is off, INPUT nodes (and their
      // edges) are skipped entirely, not just hidden.
      if (!showInputs) continue;
      var hidden = inputHidden(ext.deepest_owner, parentMap, expansionState);
      var params = ext.params || [];
      var typeHints = ext.type_hints || [];
      var isGroup = params.length > 1;
      // ``id_segments`` falls back to ``params`` (leaf names) and mirrors
      // the Python disambiguator that swaps in the full dot-path when
      // leaf names collide between sibling subgraphs (issue #94).
      var idSegments = (ext.id_segments && ext.id_segments.length === params.length) ? ext.id_segments : params;
      var inputId = isGroup ? 'input_group_' + idSegments.join('_') : 'input_' + idSegments[0];
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
          // When a GRAPH container itself is expanded the data edge is
          // re-routed to the internal producer's DATA node, leaving the
          // container-level DATA node disconnected. Hide it so it doesn't
          // render as an orphan duplicate.
          var selfExpanded = producer.node_type === 'GRAPH' && !!expansionState[producer.id];
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
            hidden: ancestorHidden || selfExpanded,
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
      var baseSources = [irEdge.source];
      if (expansionState[irEdge.source] && irEdge.source_when_expanded) {
        baseSources = Array.isArray(irEdge.source_when_expanded)
          ? irEdge.source_when_expanded.slice()
          : [irEdge.source_when_expanded];
      }
      var tgt = irEdge.target;
      if (expansionState[tgt] && irEdge.target_when_expanded) tgt = irEdge.target_when_expanded;

      // A data edge can carry multiple value_names (one NetworkX edge per
      // (src,tgt) merges them). Emit one scene edge per value to mirror
      // the legacy renderer; in separate_outputs mode each routes through
      // its own data_<producer>_<value> node.
      var valueNames = irEdge.value_names || [];
      var valuesToEmit = (irEdge.edge_type === 'data' && valueNames.length > 0) ? valueNames : [null];

      for (var bs = 0; bs < baseSources.length; bs++) {
        var baseSrc = baseSources[bs];
        for (var v = 0; v < valuesToEmit.length; v++) {
          var valueName = valuesToEmit[v];
          var src = baseSrc;
          if (separateOutputs && irEdge.edge_type === 'data' && valueName !== null) {
            src = 'data_' + src + '_' + valueName;
          }
          sceneEdges.push({
            id: valueName === null ? src + '__' + tgt : src + '__' + tgt + '__' + valueName,
            source: src,
            target: tgt,
            data: {
              edgeType: irEdge.edge_type,
              valueName: valueName,
              label: (irEdge.label === undefined ? null : irEdge.label),
              exclusive: !!irEdge.exclusive,
              forceFeedback: !!irEdge.is_back_edge,
            },
            hidden: !visibleIds[src] || !visibleIds[tgt],
          });
        }
      }
    }

    if (separateOutputs) {
      // Mirror the DATA-node-creation loop above: BRANCH gates with
      // emitted outputs need producer→DATA edges too, and gate-internal
      // outputs are filtered so we don't connect to a non-existent
      // DATA node.
      for (var oe = 0; oe < ir.nodes.length; oe++) {
        var oeNode = ir.nodes[oe];
        if (oeNode.node_type !== 'FUNCTION' && oeNode.node_type !== 'GRAPH' && oeNode.node_type !== 'BRANCH') continue;
        var oeVisibleSet = null;
        if (oeNode.node_type === 'GRAPH' && outputVisibility[oeNode.id]) {
          oeVisibleSet = {};
          for (var oeVi = 0; oeVi < outputVisibility[oeNode.id].length; oeVi++) {
            oeVisibleSet[outputVisibility[oeNode.id][oeVi]] = true;
          }
        }
        var oeOutputs = oeNode.outputs || [];
        for (var oe2 = 0; oe2 < oeOutputs.length; oe2++) {
          var oeOut = oeOutputs[oe2];
          if (oeOut.is_gate_internal) continue;
          if (oeVisibleSet !== null && !oeVisibleSet[oeOut.name]) continue;
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
      if (ext2.is_bound && !showBoundedInputs) continue;
      if (!showInputs) continue;
      var ext2Params = ext2.params || [];
      var ext2Ids = (ext2.id_segments && ext2.id_segments.length === ext2Params.length) ? ext2.id_segments : ext2Params;
      var inputNodeId = ext2Params.length > 1
        ? 'input_group_' + ext2Ids.join('_')
        : 'input_' + ext2Ids[0];
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

  function addStartEndNodesAndEdges(ir, sceneNodes, sceneEdges, parentMap, expansionState, visibleIds) {
    var configured = ir.configured_entrypoints || [];
    var overrides = expandedContainerEntrypoints(ir, expansionState);
    var startTargets = [];
    var seenStart = {};
    for (var i = 0; i < configured.length; i++) {
      var entry = configured[i];
      var target = overrides[entry] || entry;
      var resolved = resolveToVisible(target, parentMap, visibleIds);
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
        endSources.push({ source: resolvedSrc, label: endBranchLabel(node.branch_data) });
      }
    }
    if (endSources.length > 0) {
      sceneNodes.push(syntheticNode('__end__', 'END', 'End'));
      for (var e = 0; e < endSources.length; e++) {
        var entry = endSources[e];
        sceneEdges.push({
          id: entry.source + '____end__',
          source: entry.source,
          target: '__end__',
          data: { edgeType: 'end', label: entry.label },
          hidden: false,
        });
      }
    }
  }

  function endBranchLabel(branchData) {
    if (!branchData) return null;
    if (branchData.when_true === 'END') return 'True';
    if (branchData.when_false === 'END') return 'False';
    var targets = branchData.targets;
    if (targets && typeof targets === 'object' && !Array.isArray(targets)) {
      var keys = Object.keys(targets);
      for (var i = 0; i < keys.length; i++) {
        if (targets[keys[i]] === 'END') return String(keys[i]);
      }
    }
    return null;
  }

  global.HypergraphSceneBuilder = {
    buildInitialScene: buildInitialScene,
    isSchemaSupported: isSchemaSupported,
    SUPPORTED_SCHEMA_VERSION: SUPPORTED_SCHEMA_VERSION,
  };
})(typeof window !== 'undefined' ? window : globalThis);
