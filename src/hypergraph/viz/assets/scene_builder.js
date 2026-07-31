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
  // v3: tuple target_when_expanded (multi field-pill fan-out) + map_fed inputs.
  // v4: canonical container_entrypoints field (D14, #211) — this scene
  // builder no longer derives entrypoints, so a v3 payload without the
  // field must banner instead of silently mis-routing START/control edges.
  var SUPPORTED_SCHEMA_VERSION = '5';

  // Mirror of scene_builder.py:DATA_FLOW_EDGE_TYPES — the edge types that
  // carry a value, and so the only ones the `simplify` path graph walks.
  var DATA_FLOW_EDGE_TYPES = Object.assign(Object.create(null), { data: true, output: true, input: true });

  function isSchemaSupported(ir) {
    return !!ir && ir.schema_version === SUPPORTED_SCHEMA_VERSION;
  }

  var ancestorCollapsed = D.ancestorCollapsed;
  var inputHidden = D.inputHidden;
  var resolveToVisible = D.resolveToVisible;
  var resolveExpandedEntrypoints = D.resolveExpandedEntrypoints;
  var visibleOwner = D.visibleOwner;
  var expandedContainerEntrypoints = D.expandedContainerEntrypoints;
  var routesToEnd = D.routesToEnd;
  var sceneNodeType = D.sceneNodeType;

  // Twin of Python `_merge_inputs_for_state` (viz/scene_builder.py).
  //
  // The IR groups inputs by their DEEPEST consumers — the only
  // state-independent fact one IR can carry for every expansion state. But two
  // inputs entering different nodes inside a COLLAPSED container visibly feed
  // the identical set of boxes, so at that state they are one pill; expand the
  // container and they are genuinely two. Grouping is therefore a per-state
  // projection, exactly like ownerContainer (per-render) beside
  // deepestOwnerContainer (the state-independent fact).
  function mergeInputsForState(ir, parentMap, expansionState, visibleIds, showBoundedInputs, entrypointOverrides) {
    var externalInputs = ir.external_inputs || [];
    var buckets = {};
    var order = [];
    for (var i = 0; i < externalInputs.length; i++) {
      var ext = externalInputs[i];
      if (ext.is_bound && !showBoundedInputs) continue;
      var consumers = ext.consumers || [];
      var targets = [];
      for (var c = 0; c < consumers.length; c++) {
        var target = resolveToVisible(consumers[c], parentMap, visibleIds);
        if (!target) continue;
        // An input whose only consumer IS the container — a HyperTable's
        // identity column, say — resolves to the container itself. Once that
        // container is EXPANDED it is a compound node, and dagre cannot route
        // an edge to a node that has children ("Cannot set properties of
        // undefined (setting 'rank')"). Route into the entrypoint instead,
        // exactly as container-bound data edges do.
        var entered = resolveExpandedEntrypoints(target, entrypointOverrides);
        for (var ei = 0; ei < entered.length; ei++) {
          if (targets.indexOf(entered[ei]) === -1) targets.push(entered[ei]);
        }
      }
      var ownerContainer = visibleOwner(ext.deepest_owner, parentMap, expansionState);
      var params = ext.params || [];
      var segments = (ext.id_segments && ext.id_segments.length === params.length) ? ext.id_segments : params;
      // A real external input is part of the graph's contract, so collapsing
      // the container that owns it must not erase it: the pill hoists to its
      // deepest VISIBLE ancestor (ownerContainer) and its edges aggregate to
      // the collapsed boundary. Only map-fed pills disappear with their
      // container — they project a fan-out edge that re-attaches to the
      // container hull while it is collapsed, so keeping the pill would draw
      // the same value twice.
      var hidden = ext.map_fed ? inputHidden(ext.deepest_owner, parentMap, expansionState) : false;
      // map_fed and is_bound style the pill; ownerContainer decides where it
      // nests. Merging across any of them would change what the pill means.
      var key = JSON.stringify([
        targets.slice().sort(),
        !!ext.is_bound,
        !!ext.map_fed,
        ownerContainer === undefined ? null : ownerContainer,
      ]);
      var bucket = buckets[key];
      if (!bucket) {
        order.push(key);
        buckets[key] = {
          params: params.slice(),
          segments: segments.slice(),
          typeHints: (ext.type_hints || []).slice(),
          isBound: !!ext.is_bound,
          mapFed: !!ext.map_fed,
          ownerContainer: ownerContainer,
          deepestOwner: ext.deepest_owner,
          targets: targets,
          hidden: hidden,
        };
        continue;
      }
      bucket.params = bucket.params.concat(params);
      bucket.segments = bucket.segments.concat(segments);
      bucket.typeHints = bucket.typeHints.concat(ext.type_hints || []);
      // A merged pill is hidden only when every constituent is.
      bucket.hidden = bucket.hidden && hidden;
    }

    var merged = [];
    for (var o = 0; o < order.length; o++) {
      var b = buckets[order[o]];
      // Sort params together with their segments and hints so a merged pill
      // reads in the same stable order the IR uses for a native group.
      var paired = [];
      for (var x = 0; x < b.params.length; x++) {
        paired.push([b.params[x], b.segments[x], b.typeHints[x] === undefined ? null : b.typeHints[x]]);
      }
      paired.sort(function (l, r) {
        if (l[0] < r[0]) return -1;
        if (l[0] > r[0]) return 1;
        return 0;
      });
      b.params = paired.map(function (t) { return t[0]; });
      b.segments = paired.map(function (t) { return t[1]; });
      b.typeHints = paired.map(function (t) { return t[2]; });
      b.id = b.segments.length === 1 ? 'input_' + b.segments[0] : 'input_group_' + b.segments.join('_');
      merged.push(b);
    }
    return merged;
  }

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

    // Only real graph nodes exist in sceneNodes here, which is exactly the
    // set a consumer can resolve to — INPUT and DATA nodes are never a graph
    // node's ancestor. Twin of the Python `node_visible_ids`.
    var nodeVisibleIds = {};
    for (var nv = 0; nv < sceneNodes.length; nv++) {
      if (!sceneNodes[nv].hidden) nodeVisibleIds[sceneNodes[nv].id] = true;
    }
    // Mirror Python: when show_inputs is off, INPUT nodes (and their edges)
    // are skipped entirely, not just hidden.
    var inputBuckets = showInputs
      ? mergeInputsForState(ir, parentMap, expansionState, nodeVisibleIds, showBoundedInputs, expandedContainerEntrypoints(ir, expansionState))
      : [];
    for (var k = 0; k < inputBuckets.length; k++) {
      var ext = inputBuckets[k];
      var hidden = ext.hidden;
      var ownerContainer = ext.ownerContainer;
      var params = ext.params;
      var typeHints = ext.typeHints;
      var isGroup = params.length > 1;
      var inputId = ext.id;
      var data = isGroup
        ? {
            nodeType: 'INPUT_GROUP',
            params: params.slice(),
            paramTypes: typeHints.slice(),
            isBound: !!ext.isBound,
            mapFed: !!ext.mapFed,
            ownerContainer: ownerContainer,
            deepestOwnerContainer: ext.deepestOwner,
            actualTargets: ext.targets.slice(),
          }
        : {
            nodeType: 'INPUT',
            label: params[0],
            typeHint: typeHints[0] || null,
            isBound: !!ext.isBound,
            mapFed: !!ext.mapFed,
            ownerContainer: ownerContainer,
            deepestOwnerContainer: ext.deepestOwner,
            actualTargets: ext.targets.slice(),
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
    var edgeEntrypointOverrides = expandedContainerEntrypoints(ir, expansionState);
    // scene-edge id -> {entry, exit} when the edge touches a *collapsed*
    // container. Kept out of the scene payload: it exists only so `simplify`
    // can tell a real pass-through from an assumed one. Twin of
    // scene_builder.py's edge_ports.
    var edgePorts = Object.create(null);
    var parentOf = Object.create(null);
    for (var pm = 0; pm < ir.nodes.length; pm++) {
      if (ir.nodes[pm].parent) parentOf[ir.nodes[pm].id] = ir.nodes[pm].parent;
    }
    var collapsedContainers = Object.create(null);
    for (var cc = 0; cc < ir.nodes.length; cc++) {
      if (ir.nodes[cc].node_type === 'GRAPH' && !expansionState[ir.nodes[cc].id]) {
        collapsedContainers[ir.nodes[cc].id] = true;
      }
    }

    // Twin of scene_builder.py:_resolve_rewritten_endpoints: a rewritten
    // endpoint may sit inside a still-collapsed INNER container, so it is
    // walked up to its deepest visible ancestor — the edge aggregates to that
    // boundary instead of vanishing with the hidden node. Several endpoints
    // resolving to one ancestor collapse to one edge. Only collapse-hiding
    // aggregates: a node hidden by hide=True walks up to an EXPANDED ancestor,
    // and an edge must never target an expanded container (dagre cannot rank
    // it) — that resolution is rejected and the edge stays hidden.
    function resolveRewrittenEndpoints(endpoints) {
      var resolved = [];
      for (var re = 0; re < endpoints.length; re++) {
        var visible = resolveToVisible(endpoints[re], parentMap, visibleIds);
        var candidate = visible === null ? endpoints[re] : visible;
        if (candidate !== endpoints[re] && expansionState[candidate]) candidate = endpoints[re];
        if (resolved.indexOf(candidate) === -1) resolved.push(candidate);
      }
      return resolved;
    }

    for (var p = 0; p < ir.edges.length; p++) {
      var irEdge = ir.edges[p];
      var baseSources = [irEdge.source];
      if (expansionState[irEdge.source] && irEdge.source_when_expanded) {
        baseSources = Array.isArray(irEdge.source_when_expanded)
          ? irEdge.source_when_expanded.slice()
          : [irEdge.source_when_expanded];
        baseSources = resolveRewrittenEndpoints(baseSources);
      }
      // An edge into an expanded container may re-route to several internal
      // consumers (one value entering a container at more than one node) or,
      // for identity-mode fan-out, to several item-field INPUT pills — so
      // target_when_expanded can be an array; emit one edge per target
      // (mirrors the multi-source fan-out below).
      var targets = [irEdge.target];
      if (expansionState[irEdge.target]) {
        if (irEdge.target_when_expanded) {
          targets = Array.isArray(irEdge.target_when_expanded)
            ? irEdge.target_when_expanded.slice()
            : [irEdge.target_when_expanded];
        }
        var resolvedTargets = [];
        for (var rt = 0; rt < targets.length; rt++) {
          resolvedTargets = resolvedTargets.concat(
            resolveExpandedEntrypoints(targets[rt], edgeEntrypointOverrides)
          );
        }
        targets = resolveRewrittenEndpoints(resolvedTargets);
      }

      // A data edge can carry multiple value_names (one NetworkX edge per
      // (src,tgt) merges them). Merged-output mode should still render one
      // visible edge per node pair; separateOutputs mode fans out through
      // one DATA node per value.
      var valueNames = irEdge.value_names || [];
      var valuesToEmit = (separateOutputs && irEdge.edge_type === 'data' && valueNames.length > 0) ? valueNames : [null];

      for (var ti = 0; ti < targets.length; ti++) {
        var tgt = targets[ti];
        for (var bs = 0; bs < baseSources.length; bs++) {
          var baseSrc = baseSources[bs];
          for (var v = 0; v < valuesToEmit.length; v++) {
            var valueName = valuesToEmit[v];
            var src = baseSrc;
            if (separateOutputs && irEdge.edge_type === 'data' && valueName !== null) {
              src = 'data_' + src + '_' + valueName;
            }
            var edgeId = valueName === null ? src + '__' + tgt : src + '__' + tgt + '__' + valueName;
            var ports = collapsedPorts(irEdge, expansionState, parentOf);
            if (ports) edgePorts[edgeId] = ports;
            sceneEdges.push({
              id: edgeId,
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

    // Same per-state buckets the pills were built from, so an edge can never
    // point at a pill id the node loop did not emit.
    for (var q = 0; q < inputBuckets.length; q++) {
      var ext2 = inputBuckets[q];
      var inputNodeId = ext2.id;
      for (var r = 0; r < ext2.targets.length; r++) {
        var target = ext2.targets[r];
        sceneEdges.push({
          id: inputNodeId + '__' + target,
          source: inputNodeId,
          target: target,
          data: { edgeType: 'input' },
          hidden: !visibleIds[inputNodeId] || !visibleIds[target],
        });
      }
    }

    addStartEndNodesAndEdges(ir, sceneNodes, sceneEdges, parentMap, expansionState, visibleIds);

    if (opts.simplify !== false) {
      sceneEdges = simplifyTransitiveEdges(sceneEdges, {
        containerTransits: ir.container_transits,
        edgePorts: edgePorts,
        collapsedContainers: collapsedContainers,
      });
    }

    return { nodes: sceneNodes, edges: sceneEdges };
  }

  // Drop data edges that a longer visible path already implies —
  // `A ──▶ B ──▶ C` plus a direct `A ──▶ C` renders the shortcut as noise.
  // Twin of scene_builder.py:simplify_transitive_edges + _simplify.py; see
  // those docstrings for why only data edges are dropped and why the path
  // graph is restricted to the data-flow spine.
  function simplifyTransitiveEdges(sceneEdges, options) {
    var opts = options || {};
    // containerTransits arrives straight from JSON.parse, so it still inherits
    // from Object.prototype — and every prototype member name is a legal Python
    // identifier, so any of them can arrive as a container id. A container named
    // `constructor` with no recorded transit reads the `Object` function off the
    // prototype; its `length` is 1, so the loop below indexes `pairs[0][0]` on
    // undefined and blanks the canvas. Re-key into a null-prototype map first.
    // (`__proto__` itself survives JSON.parse as an own property, so it is the
    // one prototype name a naive lookup happens to get right.)
    var transits = Object.create(null);
    var rawTransits = opts.containerTransits;
    if (rawTransits) {
      var transitKeys = Object.keys(rawTransits);
      for (var tk = 0; tk < transitKeys.length; tk++) {
        transits[transitKeys[tk]] = rawTransits[transitKeys[tk]];
      }
    }
    var edgePorts = opts.edgePorts || Object.create(null);
    var collapsed = opts.collapsedContainers || Object.create(null);

    // Path-graph identity for one end of an edge. Plain nodes keep their id; a
    // collapsed container becomes an in- or out-port so the walk can only
    // cross it where a real transit exists. An unresolvable port becomes one
    // unique to this edge, so nothing joins through it — unverified means
    // "do not hide".
    function port(nodeId, edgeId, side) {
      if (!collapsed[nodeId]) return nodeId;
      var recorded = edgePorts[edgeId];
      var resolved = recorded ? recorded[side === 'in' ? 'entry' : 'exit'] : undefined;
      var suffix = (resolved === undefined || resolved === null) ? '?' + edgeId : resolved;
      return nodeId + '\u0000' + side + '\u0000' + suffix;
    }

    // Object.create(null) throughout: node ids come from user-authored Python
    // names, and `__proto__` is a legal Python identifier. On a normal object
    // literal `adjacency['__proto__']` resolves to Object.prototype, so the
    // assignment below throws and blanks the canvas. The Python twin uses
    // dicts and has never had this failure mode.
    var adjacency = Object.create(null);
    function link(from, to) {
      if (!adjacency[from]) adjacency[from] = Object.create(null);
      adjacency[from][to] = true;
    }

    // An INPUT pill edge targets the collapsed box AS A BOX, never an
    // in-port. The drawn line ends at the hull, so the reader's question is
    // only "does this value reach the box?" — answered by the phantom
    // in-port → box links below — and, ending at the bare box id (a sink in
    // the path graph), the edge can never stand in for a transit.
    function pathTarget(e) {
      var eData = e.data || {};
      return eData.edgeType === 'input' ? e.target : port(e.target, e.id, 'in');
    }

    for (var i = 0; i < sceneEdges.length; i++) {
      var e = sceneEdges[i];
      if (e.hidden) continue;
      var eData = e.data || {};
      if (!DATA_FLOW_EDGE_TYPES[eData.edgeType]) continue;
      // Exclusive arms are excluded from the path graph too, not just from the
      // candidates below: an arm only carries its value when its branch is
      // taken, so it must not imply away an unconditional edge.
      if (eData.forceFeedback || eData.exclusive) continue;
      link(port(e.source, e.id, 'out'), pathTarget(e));
    }

    // Join each collapsed container's in-ports to the out-ports it genuinely
    // reaches. Without these the box is impassable; with all-to-all it would
    // be assumed transparent. Neither guess — use what the container does.
    for (var containerId in collapsed) {
      if (!Object.prototype.hasOwnProperty.call(collapsed, containerId)) continue;
      var pairs = transits[containerId] || [];
      for (var t = 0; t < pairs.length; t++) {
        link(
          containerId + '\u0000in\u0000' + pairs[t][0],
          containerId + '\u0000out\u0000' + pairs[t][1]
        );
      }
    }

    // Path-only links from every delivered-to in-port to its box, so "reaches
    // the box" means exactly "some visible edge delivers into the box". The
    // bare box id has no outgoing path links, so these can never manufacture
    // a pass-through — they only answer input-edge candidacy.
    var inMarker = '\u0000in\u0000';
    for (var pi = 0; pi < sceneEdges.length; pi++) {
      var pe = sceneEdges[pi];
      if (pe.hidden) continue;
      var pePort = pathTarget(pe);
      var markerAt = pePort.indexOf(inMarker);
      if (markerAt === -1) continue;
      link(pePort, pePort.slice(0, markerAt));
    }

    var dropped = Object.create(null);
    for (var j = 0; j < sceneEdges.length; j++) {
      var edge = sceneEdges[j];
      var data = edge.data || {};
      // Input edges are candidates too: one input feeding a chain keeps only
      // its EARLIEST consumer(s) — every later edge is a shortcut past a
      // route the diagram already draws.
      if (edge.hidden || (data.edgeType !== 'data' && data.edgeType !== 'input')) continue;
      if (data.exclusive || data.forceFeedback) continue;
      if (edge.source === edge.target) continue;
      var from = port(edge.source, edge.id, 'out');
      var to = pathTarget(edge);
      if (from === to) continue;
      if (hasIndirectPath(adjacency, from, to)) dropped[edge.id] = true;
    }

    return sceneEdges.filter(function (e) { return !dropped[e.id]; });
  }

  // Twin of scene_builder.py:_collapsed_ports. The `*_when_expanded` fields
  // name the DEEPEST internal producer/consumer, but transits are recorded
  // between a container's direct children, so each port is walked back up to
  // the child it belongs to. A multi-producer source array stays unresolved —
  // no single child definitely emits the value. A multi-consumer target array
  // still resolves when every consumer sits under the SAME direct child (the
  // value provably arrives there); consumers spread across children stay
  // unresolved.
  function collapsedPorts(irEdge, expansionState, parentOf) {
    var ports = null;
    if (!expansionState[irEdge.source] && typeof irEdge.source_when_expanded === 'string') {
      var exitChild = directChildOf(irEdge.source, irEdge.source_when_expanded, parentOf);
      if (exitChild !== null) { ports = ports || Object.create(null); ports.exit = exitChild; }
    }
    if (!expansionState[irEdge.target] && irEdge.target_when_expanded) {
      var expandedTargets = Array.isArray(irEdge.target_when_expanded)
        ? irEdge.target_when_expanded
        : [irEdge.target_when_expanded];
      var entryChild = null;
      var consistent = expandedTargets.length > 0;
      for (var et = 0; et < expandedTargets.length; et++) {
        var child = directChildOf(irEdge.target, expandedTargets[et], parentOf);
        if (child === null || (entryChild !== null && child !== entryChild)) { consistent = false; break; }
        entryChild = child;
      }
      if (consistent && entryChild !== null) { ports = ports || Object.create(null); ports.entry = entryChild; }
    }
    return ports;
  }

  function directChildOf(containerId, descendant, parentOf) {
    var current = descendant;
    var guard = 0;
    while (current !== undefined && current !== null && guard <= 1000) {
      var parent = parentOf[current];
      if (parent === containerId) return current;
      current = parent;
      guard++;
    }
    return null;
  }

  // True if `target` is reachable from `source` without ever taking the
  // direct `source -> target` edge.
  function hasIndirectPath(adjacency, source, target) {
    var stack = [];
    var seen = Object.create(null);
    var direct = adjacency[source] || Object.create(null);
    for (var first in direct) {
      if (!Object.prototype.hasOwnProperty.call(direct, first)) continue;
      if (first === target) continue;
      stack.push(first);
      seen[first] = true;
    }
    while (stack.length > 0) {
      var current = stack.pop();
      if (current === target) return true;
      var successors = adjacency[current] || {};
      for (var next in successors) {
        if (!Object.prototype.hasOwnProperty.call(successors, next)) continue;
        if (current === source && next === target) continue; // edge under test
        if (!seen[next]) {
          seen[next] = true;
          stack.push(next);
        }
      }
    }
    return false;
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
      var targets = resolveExpandedEntrypoints(entry, overrides);
      for (var t = 0; t < targets.length; t++) {
        var target = targets[t];
        var resolved = resolveToVisible(target, parentMap, visibleIds);
        if (resolved && !seenStart[resolved]) {
          seenStart[resolved] = true;
          startTargets.push(resolved);
        }
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
    simplifyTransitiveEdges: simplifyTransitiveEdges,
    isSchemaSupported: isSchemaSupported,
    SUPPORTED_SCHEMA_VERSION: SUPPORTED_SCHEMA_VERSION,
  };
})(typeof window !== 'undefined' ? window : globalThis);
