/**
 * Shared state helpers for the interactive viz.
 * Exposed as window.HyperGraphVizState in the browser and CommonJS exports for tests.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (root) {
    root.HyperGraphVizState = api;
  }
})(typeof window !== "undefined" ? window : globalThis, function () {
  const toMap = (expansionState) => {
    if (expansionState instanceof Map) return expansionState;
    if (expansionState && typeof expansionState === "object") {
      return new Map(Object.entries(expansionState));
    }
    return new Map();
  };

  const applyState = (baseNodes, baseEdges, options) => {
    const { expansionState, separateOutputs, showTypes, theme } = options;
    const expMap = toMap(expansionState);

    // Build lookup maps
    const sourceNodeTypes = new Map();
    const sourceIdMap = new Map(); // nodeId -> sourceId
    baseNodes.forEach((n) => {
      sourceNodeTypes.set(n.id, n.data?.nodeType);
      if (n.data?.sourceId) sourceIdMap.set(n.id, n.data.sourceId);
    });

    // Track which pipelines are expanded/collapsed
    const expandedPipelines = new Set(
      baseNodes
        .filter((n) => n.data?.nodeType === "PIPELINE" && expMap.get(n.id))
        .map((n) => n.id)
    );
    const collapsedPipelines = new Set(
      baseNodes
        .filter((n) => n.data?.nodeType === "PIPELINE" && !expMap.get(n.id))
        .map((n) => n.id)
    );

    // Identify boundary outputs: DATA nodes with sourceId pointing to a PIPELINE
    const boundaryOutputs = new Set(
      baseNodes
        .filter((n) => {
          if (!n.data?.sourceId) return false;
          return sourceNodeTypes.get(n.data.sourceId) === "PIPELINE";
        })
        .map((n) => n.id)
    );

    // Boundary outputs of EXPANDED pipelines should be hidden
    const boundaryOutputsToHide = new Set(
      baseNodes
        .filter((n) => boundaryOutputs.has(n.id) && expandedPipelines.has(n.data?.sourceId))
        .map((n) => n.id)
    );

    const applyMeta = (nodeList) =>
      nodeList.map((n) => {
        const isPipeline = n.data?.nodeType === "PIPELINE";
        const expanded = isPipeline ? Boolean(expMap.get(n.id)) : undefined;
        return {
          ...n,
          type: isPipeline && expanded ? "pipelineGroup" : n.type,
          style: isPipeline && !expanded ? undefined : n.style,
          data: {
            ...n.data,
            theme,
            showTypes,
            isExpanded: expanded,
          },
        };
      });

    // Build edge map to find producer chains
    const edgesByTarget = new Map();
    baseEdges.forEach((e) => {
      if (!edgesByTarget.has(e.target)) edgesByTarget.set(e.target, []);
      edgesByTarget.get(e.target).push(e);
    });

    // Find the first visible producer for a node
    // For separate mode: stop at any visible output node (not in boundaryOutputsToHide)
    // For combined mode: trace back to the function that produces the value
    const findVisibleProducer = (nodeId, stopAtVisibleOutput = false) => {
      const node = baseNodes.find((n) => n.id === nodeId);
      if (!node?.data?.sourceId) return nodeId;
      
      // Check incoming edges to find the actual producer node
      const incomingEdges = edgesByTarget.get(nodeId) || [];
      if (incomingEdges.length > 0) {
        const producerNodeId = incomingEdges[0].source;
        const producerNode = baseNodes.find((n) => n.id === producerNodeId);
        
        // If producer is visible (not hidden), return it
        if (producerNode && stopAtVisibleOutput && !boundaryOutputsToHide.has(producerNodeId)) {
          return producerNodeId;
        }
        
        // Otherwise continue tracing
        return findVisibleProducer(producerNodeId, stopAtVisibleOutput);
      }
      
      // Fallback: use sourceId
      const sourceType = sourceNodeTypes.get(node.data.sourceId);
      if (sourceType === "FUNCTION" || sourceType === "DUAL") {
        return node.data.sourceId;
      }
      return node.data.sourceId;
    };

    if (separateOutputs) {
      // Separate outputs mode:
      // - Hide boundary outputs for EXPANDED pipelines (internal is visible)
      // - Show boundary outputs for COLLAPSED pipelines
      const filteredNodes = applyMeta(baseNodes)
        .filter((n) => !boundaryOutputsToHide.has(n.id))
        .map((n) => ({
          ...n,
          data: { ...n.data, separateOutputs: true },
        }));

      // Remap edges FROM hidden boundary outputs to their producer
      const remappedEdges = baseEdges
        .filter((e) => !boundaryOutputsToHide.has(e.target))
        .map((e) => {
          if (boundaryOutputsToHide.has(e.source)) {
            // In separate mode, stop at the first visible output node
            const producer = findVisibleProducer(e.source, true);
            return {
              ...e,
              id: `e_${producer}_${e.target}`,
              source: producer,
            };
          }
          return e;
        });

      return { nodes: filteredNodes, edges: remappedEdges };
    }

    // Combined outputs mode:
    // - Always combine FUNCTION/DUAL outputs
    // - Combine PIPELINE outputs only when pipeline is COLLAPSED
    const outputNodes = new Set(
      baseNodes
        .filter((n) => {
          if (!n.data?.sourceId) return false;
          const sourceType = sourceNodeTypes.get(n.data.sourceId);
          // Combine function/dual outputs
          if (sourceType === "FUNCTION" || sourceType === "DUAL") return true;
          // Combine pipeline outputs only if pipeline is collapsed
          if (sourceType === "PIPELINE" && collapsedPipelines.has(n.data.sourceId)) return true;
          return false;
        })
        .map((n) => n.id)
    );

    // Also hide boundary outputs for expanded pipelines
    const nodesToHide = new Set([...outputNodes, ...boundaryOutputsToHide]);

    const functionOutputs = {};
    baseNodes.forEach((n) => {
      if (n.data?.sourceId && outputNodes.has(n.id)) {
        if (!functionOutputs[n.data.sourceId]) functionOutputs[n.data.sourceId] = [];
        functionOutputs[n.data.sourceId].push({
          name: n.data.label,
          type: n.data.typeHint,
        });
      }
    });

    const nodes = applyMeta(baseNodes)
      .filter((n) => !nodesToHide.has(n.id))
      .map((n) => ({
        ...n,
        data: {
          ...n.data,
          separateOutputs: false,
          outputs: functionOutputs[n.id] || [],
        },
      }));

    // Process edges:
    // 1. Filter edges TO hidden nodes
    // 2. Remap edges FROM output nodes to come from their producer
    // 3. Remap edges FROM hidden boundary outputs to their producer
    const edges = baseEdges
      .filter((e) => !nodesToHide.has(e.target))
      .map((e) => {
        // Remap from function output nodes
        if (outputNodes.has(e.source)) {
          const outputNode = baseNodes.find((n) => n.id === e.source);
          if (outputNode?.data?.sourceId) {
            return {
              ...e,
              id: `e_${outputNode.data.sourceId}_${e.target}`,
              source: outputNode.data.sourceId,
            };
          }
        }
        // Remap from hidden boundary outputs
        if (boundaryOutputsToHide.has(e.source)) {
          // In combined mode, trace back to the function
          const producer = findVisibleProducer(e.source, false);
          return {
            ...e,
            id: `e_${producer}_${e.target}`,
            source: producer,
          };
        }
        return e;
      })
      .filter((e) => {
        // Filter out any remaining edges from hidden nodes
        return !nodesToHide.has(e.source);
      });

    return { nodes, edges };
  };

  const applyVisibility = (nodes, expansionState) => {
    const expMap = toMap(expansionState);
    const parentMap = new Map();
    nodes.forEach((n) => {
      if (n.parentNode) parentMap.set(n.id, n.parentNode);
    });

    const isHidden = (nodeId) => {
      let curr = nodeId;
      while (curr) {
        const parent = parentMap.get(curr);
        if (!parent) return false;
        if (expMap.get(parent) === false) return true;
        curr = parent;
      }
      return false;
    };

    return nodes.map((n) => ({
      ...n,
      hidden: isHidden(n.id),
    }));
  };

  const compressEdges = (nodes, edges, debug = false) => {
    const nodeMap = new Map(nodes.map((n) => [n.id, n]));
    const parentMap = new Map();
    const expansionMap = new Map();
    nodes.forEach((n) => {
      if (n.parentNode) parentMap.set(n.id, n.parentNode);
      if (n.data?.nodeType === "PIPELINE") expansionMap.set(n.id, !!n.data.isExpanded);
    });

    if (debug) {
      console.log("[compressEdges] expansionMap:", Object.fromEntries(expansionMap));
      console.log("[compressEdges] parentMap:", Object.fromEntries(parentMap));
      console.log("[compressEdges] input edges:", edges.length);
    }

    const getVisibleAncestor = (nodeId) => {
      let curr = nodeId;
      let candidate = nodeId;
      while (curr) {
        const parent = parentMap.get(curr);
        if (!parent) break;
        if (expansionMap.get(parent) === false) candidate = parent;
        curr = parent;
      }
      return candidate;
    };

    const newEdges = [];
    const processedEdges = new Set();

    edges.forEach((edge) => {
      const sourceNode = nodeMap.get(edge.source);
      const targetNode = nodeMap.get(edge.target);
      if (!sourceNode || !targetNode) return;

      // Get visible ancestors - handles collapsed pipeline remapping
      const sourceVis = getVisibleAncestor(edge.source);
      const targetVis = getVisibleAncestor(edge.target);

      // If both endpoints are visible as-is (no remapping needed), keep edge unchanged
      if (sourceVis === edge.source && targetVis === edge.target) {
        const edgeId = edge.id || `e_${edge.source}_${edge.target}`;
        if (!processedEdges.has(edgeId)) {
          newEdges.push(edge);
          processedEdges.add(edgeId);
        }
        return;
      }

      // If endpoints need remapping and result in different visible nodes, create compressed edge
      if (sourceVis && targetVis && sourceVis !== targetVis) {
        const edgeId = `e_${sourceVis}_${targetVis}`;
        if (!processedEdges.has(edgeId)) {
          if (debug) {
            console.log(`[compressEdges] remapping edge: ${edge.source} -> ${edge.target} to ${sourceVis} -> ${targetVis}`);
          }
          newEdges.push({ ...edge, id: edgeId, source: sourceVis, target: targetVis });
          processedEdges.add(edgeId);
        }
      } else if (debug && (sourceVis !== edge.source || targetVis !== edge.target)) {
        console.log(`[compressEdges] DROPPED edge: ${edge.source} -> ${edge.target} (sourceVis=${sourceVis}, targetVis=${targetVis})`);
      }
    });

    if (debug) {
      console.log("[compressEdges] output edges:", newEdges.length);
    }

    return newEdges;
  };

  /**
   * Group input nodes that share the same targets and bound state.
   * This runs after edge compression so inputs targeting the same collapsed pipeline get grouped.
   */
  const groupInputs = (nodes, edges) => {
    // Find input nodes: DATA nodes without sourceId, not hidden
    const inputNodes = nodes.filter(
      (n) => n.data?.nodeType === "DATA" && !n.data?.sourceId && !n.hidden
    );
    if (inputNodes.length < 2) return { nodes, edges };

    // Build target map for each input
    const targetMap = new Map();
    inputNodes.forEach((n) => targetMap.set(n.id, new Set()));
    edges.forEach((e) => {
      if (targetMap.has(e.source)) {
        targetMap.get(e.source).add(e.target);
      }
    });

    // Group by (targets, isBound, parentNode)
    const groups = new Map();
    inputNodes.forEach((n) => {
      const targets = Array.from(targetMap.get(n.id) || []).sort().join(",");
      const isBound = Boolean(n.data?.isBound);
      const parent = n.parentNode || "root";
      const key = `${parent}|${targets}|${isBound}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(n);
    });

    // Only group if 2+ nodes share the same key
    const nodesToRemove = new Set();
    const newNodes = [];
    const edgeRewrites = new Map(); // oldSourceId -> newGroupId

    groups.forEach((group, key) => {
      if (group.length < 2) return;

      // Create group node
      const groupId = `group_input_${Math.abs(hashCode(key)) % 100000}`;
      const params = group.map((n) => n.data?.label || n.id);
      const paramTypes = group.map((n) => n.data?.typeHint || null);
      const firstNode = group[0];

      const groupNode = {
        id: groupId,
        position: firstNode.position || { x: 0, y: 0 },
        parentNode: firstNode.parentNode,
        extent: firstNode.extent,
        type: "custom",
        data: {
          nodeType: "INPUT_GROUP",
          label: "Inputs",
          params,
          paramTypes,
          isBound: firstNode.data?.isBound || false,
          theme: firstNode.data?.theme,
          showTypes: firstNode.data?.showTypes,
        },
        sourcePosition: "bottom",
        targetPosition: "top",
      };
      newNodes.push(groupNode);

      // Mark individual nodes for removal and track edge rewrites
      group.forEach((n) => {
        nodesToRemove.add(n.id);
        edgeRewrites.set(n.id, groupId);
      });
    });

    if (newNodes.length === 0) return { nodes, edges };

    // Filter out removed nodes and add group nodes
    const filteredNodes = nodes.filter((n) => !nodesToRemove.has(n.id));
    const finalNodes = [...filteredNodes, ...newNodes];

    // Rewrite edges from removed inputs to group
    const rewrittenEdges = [];
    const seenEdges = new Set();
    edges.forEach((e) => {
      const newSource = edgeRewrites.get(e.source) || e.source;
      const edgeKey = `${newSource}_${e.target}`;
      if (!seenEdges.has(edgeKey)) {
        seenEdges.add(edgeKey);
        rewrittenEdges.push({
          ...e,
          id: `e_${newSource}_${e.target}`,
          source: newSource,
        });
      }
    });

    return { nodes: finalNodes, edges: rewrittenEdges };
  };

  // Simple hash function for consistent group IDs
  const hashCode = (str) => {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
      hash = ((hash << 5) - hash + str.charCodeAt(i)) | 0;
    }
    return hash;
  };

  /**
   * Debug helper to analyze visualization state.
   * Call from browser console: HyperGraphVizState.debug.analyzeState()
   */
  const debug = {
    /**
     * Enable debug mode for compressEdges logging.
     * Usage: HyperGraphVizState.debug.enableDebug()
     */
    enableDebug: () => {
      if (typeof window !== "undefined") {
        window.__hypernodes_debug_viz = true;
        console.log("[debug] Debug mode enabled. Interactions will now log details.");
      }
    },

    /**
     * Disable debug mode.
     */
    disableDebug: () => {
      if (typeof window !== "undefined") {
        window.__hypernodes_debug_viz = false;
        console.log("[debug] Debug mode disabled.");
      }
    },

    /**
     * Get the current expansion state from React Flow.
     */
    getExpansionState: () => {
      if (typeof window !== "undefined" && window.__hypernodesVizExpansionState) {
        return Object.fromEntries(window.__hypernodesVizExpansionState);
      }
      return null;
    },

    /**
     * Analyze the current visualization state.
     * Logs detailed information about nodes, edges, visibility, and edge compression.
     */
    analyzeState: () => {
      if (typeof window === "undefined") {
        console.log("[debug] Only available in browser.");
        return;
      }

      const graphData = document.getElementById("graph-data");
      if (!graphData) {
        console.log("[debug] No graph-data element found.");
        return;
      }

      const data = JSON.parse(graphData.textContent);
      console.group("[debug] Graph Analysis");
      console.log("Total nodes:", data.nodes.length);
      console.log("Total edges:", data.edges.length);
      
      const pipelineNodes = data.nodes.filter(n => n.data?.nodeType === "PIPELINE");
      console.log("Pipeline nodes:", pipelineNodes.map(n => ({ id: n.id, isExpanded: n.data?.isExpanded })));

      const hiddenNodes = data.nodes.filter(n => n.hidden);
      console.log("Hidden nodes:", hiddenNodes.length);

      const edgeTargets = new Set(data.edges.map(e => e.target));
      const edgeSources = new Set(data.edges.map(e => e.source));
      const nodeIds = new Set(data.nodes.map(n => n.id));

      const danglingSourceEdges = data.edges.filter(e => !nodeIds.has(e.source));
      const danglingTargetEdges = data.edges.filter(e => !nodeIds.has(e.target));

      if (danglingSourceEdges.length > 0) {
        console.warn("Edges with missing source nodes:", danglingSourceEdges);
      }
      if (danglingTargetEdges.length > 0) {
        console.warn("Edges with missing target nodes:", danglingTargetEdges);
      }

      console.groupEnd();
      return { nodes: data.nodes, edges: data.edges, pipelineNodes, hiddenNodes, danglingSourceEdges, danglingTargetEdges };
    },

    /**
     * Simulate edge compression for a given expansion state.
     * @param {Object} expansionState - Map of pipeline ID to expanded state
     */
    simulateCompression: (expansionState = {}) => {
      if (typeof window === "undefined") return;

      const graphData = document.getElementById("graph-data");
      if (!graphData) return;

      const data = JSON.parse(graphData.textContent);
      const expMap = new Map(Object.entries(expansionState));

      // Apply expansion state to nodes
      const nodes = data.nodes.map(n => {
        if (n.data?.nodeType === "PIPELINE") {
          return { ...n, data: { ...n.data, isExpanded: expMap.get(n.id) ?? n.data?.isExpanded } };
        }
        return n;
      });

      // Apply visibility
      const withVisibility = applyVisibility(nodes, expMap);
      
      // Compress edges
      const compressed = compressEdges(withVisibility, data.edges, true);

      console.group("[debug] Simulation Results");
      console.log("Expansion state:", expansionState);
      console.log("Visible nodes:", withVisibility.filter(n => !n.hidden).length);
      console.log("Hidden nodes:", withVisibility.filter(n => n.hidden).length);
      console.log("Compressed edges:", compressed.length);
      console.log("Edges:", compressed);
      console.groupEnd();

      return { nodes: withVisibility, edges: compressed };
    },

    /**
     * Show debug overlays (node bounding boxes and edge connection points).
     * Usage: HyperGraphVizState.debug.showOverlays()
     */
    showOverlays: () => {
      if (typeof window !== "undefined") {
        window.__hypernodes_debug_overlays = true;
        console.log("[debug] Debug overlays enabled. Toggle in UI or call hideOverlays() to disable.");
        // Try to trigger React re-render by dispatching a custom event
        window.dispatchEvent(new CustomEvent('hypernodes-debug-toggle', { detail: { overlays: true } }));
      }
    },

    /**
     * Hide debug overlays.
     * Usage: HyperGraphVizState.debug.hideOverlays()
     */
    hideOverlays: () => {
      if (typeof window !== "undefined") {
        window.__hypernodes_debug_overlays = false;
        console.log("[debug] Debug overlays disabled.");
        window.dispatchEvent(new CustomEvent('hypernodes-debug-toggle', { detail: { overlays: false } }));
      }
    },

    /**
     * Inspect the current layout state.
     * Returns detailed position and dimension information for all nodes and edges.
     * Usage: HyperGraphVizState.debug.inspectLayout()
     */
    inspectLayout: () => {
      if (typeof window === "undefined") {
        console.log("[debug] Only available in browser.");
        return null;
      }

      const layoutData = window.__hypernodesVizLayout;
      if (!layoutData) {
        console.log("[debug] No layout data available. Make sure the visualization has rendered.");
        return null;
      }

      // Get rendered edge paths from DOM
      const edgePaths = [];
      document.querySelectorAll('.react-flow__edge path').forEach((path, idx) => {
        const d = path.getAttribute('d');
        if (d) {
          // Parse bezier path: M startX,startY C ... endX,endY
          const coords = d.match(/[\d.-]+/g);
          if (coords && coords.length >= 4) {
            edgePaths.push({
              index: idx,
              pathD: d,
              startX: parseFloat(coords[0]),
              startY: parseFloat(coords[1]),
              endX: parseFloat(coords[coords.length - 2]),
              endY: parseFloat(coords[coords.length - 1]),
            });
          }
        }
      });

      const result = {
        nodes: layoutData.nodes.map(n => ({
          ...n,
          bottom: n.y + (n.height || 68),
          right: n.x + (n.width || 200),
        })),
        edges: layoutData.edges,
        edgePaths,
        layoutVersion: layoutData.version,
      };

      console.group("[debug] Layout Inspection");
      console.log("Layout version:", result.layoutVersion);
      console.log("Nodes:", result.nodes.length);
      console.table(result.nodes.filter(n => !n.hidden).slice(0, 20));
      console.log("Edges:", result.edges.length);
      console.log("Edge paths from DOM:", edgePaths.length);
      if (edgePaths.length > 0) {
        console.table(edgePaths.slice(0, 10));
      }
      console.groupEnd();

      return result;
    },

    /**
     * Validate that edge connection points fall within node boundaries.
     * Returns issues if edges start/end outside their source/target nodes.
     * Usage: HyperGraphVizState.debug.validateConnections()
     */
    validateConnections: () => {
      if (typeof window === "undefined") {
        console.log("[debug] Only available in browser.");
        return null;
      }

      const layoutData = window.__hypernodesVizLayout;
      if (!layoutData) {
        console.log("[debug] No layout data available.");
        return { valid: false, issues: [{ issue: "No layout data available" }] };
      }

      const issues = [];
      const nodeMap = new Map(layoutData.nodes.map(n => [n.id, n]));
      
      // Get edge paths from DOM with their source/target info
      const edgeElements = document.querySelectorAll('.react-flow__edge');
      
      edgeElements.forEach(edgeEl => {
        // Extract edge ID from data-id, id, or data-testid (React Flow uses different attributes)
        let edgeId = edgeEl.getAttribute('data-id') || edgeEl.id || '';
        if (!edgeId) {
          // Fallback: extract from data-testid which has format "rf__edge-{edgeId}"
          const testid = edgeEl.getAttribute('data-testid') || '';
          const match = testid.match(/^rf__edge-(.+)$/);
          if (match) edgeId = match[1];
        }
        
        const path = edgeEl.querySelector('path');
        if (!path) return;
        
        const d = path.getAttribute('d');
        if (!d) return;
        
        const coords = d.match(/[\d.-]+/g);
        if (!coords || coords.length < 4) return;
        
        const startX = parseFloat(coords[0]);
        const startY = parseFloat(coords[1]);
        const endX = parseFloat(coords[coords.length - 2]);
        const endY = parseFloat(coords[coords.length - 1]);
        
        // Find matching edge from layout data
        // Edge IDs include version suffix like "e_source_target_v3"
        const baseEdgeId = edgeId.replace(/_v\d+$/, '');
        const edgeMatch = layoutData.edges.find(e => 
          e.id === baseEdgeId || e.id.startsWith(baseEdgeId.replace(/^e_/, ''))
        );
        
        if (!edgeMatch) return;
        
        const sourceNode = nodeMap.get(edgeMatch.source);
        const targetNode = nodeMap.get(edgeMatch.target);
        
        // Note: Layout data now stores ABSOLUTE positions (parent offsets already applied)
        if (sourceNode && !sourceNode.hidden) {
          const sourceBottom = sourceNode.y + (sourceNode.height || 68);
          const sourceLeft = sourceNode.x;
          const sourceRight = sourceNode.x + (sourceNode.width || 200);
          
          // Check if edge starts from bottom of source node (with tolerance)
          const tolerance = 20; // pixels
          const withinXBounds = startX >= sourceLeft - tolerance && startX <= sourceRight + tolerance;
          const nearSourceBottom = Math.abs(startY - sourceBottom) <= tolerance;
          
          if (!withinXBounds || !nearSourceBottom) {
            issues.push({
              edge: edgeMatch.id,
              type: 'source_mismatch',
              issue: `Edge starts at (${Math.round(startX)}, ${Math.round(startY)}) but source node "${sourceNode.id}" ends at y=${Math.round(sourceBottom)}`,
              expected: { x: `${Math.round(sourceLeft)}-${Math.round(sourceRight)}`, y: Math.round(sourceBottom) },
              actual: { x: startX, y: startY },
              delta: { y: Math.round(startY - sourceBottom) },
            });
          }
        }
        
        if (targetNode && !targetNode.hidden) {
          const targetTop = targetNode.y;
          const targetLeft = targetNode.x;
          const targetRight = targetNode.x + (targetNode.width || 200);
          
          // Check if edge ends at top of target node (with tolerance)
          const tolerance = 20;
          const withinXBounds = endX >= targetLeft - tolerance && endX <= targetRight + tolerance;
          const nearTargetTop = Math.abs(endY - targetTop) <= tolerance;
          
          if (!withinXBounds || !nearTargetTop) {
            issues.push({
              edge: edgeMatch.id,
              type: 'target_mismatch',
              issue: `Edge ends at (${Math.round(endX)}, ${Math.round(endY)}) but target node "${targetNode.id}" starts at y=${Math.round(targetTop)}`,
              expected: { x: `${Math.round(targetLeft)}-${Math.round(targetRight)}`, y: Math.round(targetTop) },
              actual: { x: endX, y: endY },
              delta: { y: Math.round(endY - targetTop) },
            });
          }
        }
      });

      const result = {
        valid: issues.length === 0,
        issues,
        summary: `${edgeElements.length} edges validated, ${issues.length} issues found`,
      };

      console.group("[debug] Connection Validation");
      console.log(result.summary);
      if (issues.length > 0) {
        console.warn("Issues found:");
        console.table(issues);
      } else {
        console.log("All edges connect properly to their nodes.");
      }
      console.groupEnd();

      return result;
    },

    /**
     * Get text analysis for all nodes including labels, type hints, and truncation status.
     * Shows which nodes have truncated type hints and their calculated widths.
     * Usage: HyperGraphVizState.debug.getTextAnalysis()
     */
    getTextAnalysis: () => {
      if (typeof window === "undefined") {
        console.log("[debug] Only available in browser.");
        return null;
      }

      const layoutData = window.__hypernodesVizLayout;
      const graphData = document.getElementById("graph-data");
      if (!graphData || !layoutData) {
        console.log("[debug] No graph or layout data available.");
        return null;
      }

      const data = JSON.parse(graphData.textContent);
      const TYPE_HINT_MAX_CHARS = 25; // Match the constant in html_generator.py
      
      const analysis = data.nodes.map(n => {
        const label = n.data?.label || '';
        const typeHint = n.data?.typeHint || '';
        const nodeType = n.data?.nodeType || '';
        const params = n.data?.params || [];
        const paramTypes = n.data?.paramTypes || [];
        const outputs = n.data?.outputs || [];
        const showTypes = n.data?.showTypes;
        
        // Find all text content in this node
        const texts = [];
        if (label) texts.push({ kind: 'label', text: label, length: label.length });
        if (typeHint) texts.push({ kind: 'typeHint', text: typeHint, length: typeHint.length, truncated: typeHint.length > TYPE_HINT_MAX_CHARS });
        
        // For INPUT_GROUP, add params and their types
        params.forEach((p, i) => {
          if (p) texts.push({ kind: 'param', text: p, length: p.length });
          const t = paramTypes[i];
          if (t) texts.push({ kind: 'paramType', text: t, length: t.length, truncated: t.length > TYPE_HINT_MAX_CHARS });
        });
        
        // For FUNCTION/PIPELINE with outputs, add output names and types
        outputs.forEach(out => {
          if (out.name) texts.push({ kind: 'outputName', text: out.name, length: out.name.length });
          if (out.type) texts.push({ kind: 'outputType', text: out.type, length: out.type.length, truncated: out.type.length > TYPE_HINT_MAX_CHARS });
        });
        
        // Find longest text
        const longestText = texts.reduce((max, t) => t.length > max.length ? t : max, { text: '', length: 0 });
        
        // Get width from layout data
        const layoutNode = layoutData.nodes.find(ln => ln.id === n.id);
        const width = layoutNode?.width || n.style?.width || 200;
        
        // Check if any type hint is truncated
        const hasTruncatedType = texts.some(t => t.truncated);
        
        return {
          id: n.id,
          nodeType,
          label,
          typeHint,
          typeHintLength: typeHint.length,
          isTruncated: hasTruncatedType,
          truncatedAt: TYPE_HINT_MAX_CHARS,
          longestText: longestText.text,
          longestTextLength: longestText.length,
          width,
          allTexts: texts,
        };
      });
      
      // Filter to nodes with type hints or multiple texts
      const nodesWithTypes = analysis.filter(n => n.typeHint || n.allTexts.length > 1);
      const truncatedNodes = analysis.filter(n => n.isTruncated);
      
      console.group("[debug] Text Analysis");
      console.log(`TYPE_HINT_MAX_CHARS: ${TYPE_HINT_MAX_CHARS}`);
      console.log(`Total nodes: ${analysis.length}`);
      console.log(`Nodes with type hints: ${nodesWithTypes.length}`);
      console.log(`Nodes with truncated types: ${truncatedNodes.length}`);
      
      if (truncatedNodes.length > 0) {
        console.log("\nTruncated type hints:");
        console.table(truncatedNodes.map(n => ({
          id: n.id.slice(-20),
          nodeType: n.nodeType,
          typeHint: n.typeHint.slice(0, 30) + (n.typeHint.length > 30 ? '...' : ''),
          length: n.typeHintLength,
          width: n.width,
        })));
      }
      
      console.log("\nAll nodes with types:");
      console.table(nodesWithTypes.slice(0, 20).map(n => ({
        id: n.id.slice(-20),
        type: n.nodeType,
        label: n.label.slice(0, 15),
        typeHint: n.typeHint ? n.typeHint.slice(0, 20) + (n.typeHint.length > 20 ? '..' : '') : '-',
        longest: n.longestTextLength,
        width: n.width,
        truncated: n.isTruncated ? 'Y' : '-',
      })));
      console.groupEnd();
      
      return {
        allNodes: analysis,
        nodesWithTypes,
        truncatedNodes,
        typeHintMaxChars: TYPE_HINT_MAX_CHARS,
        summary: `${truncatedNodes.length}/${nodesWithTypes.length} nodes have truncated type hints`,
      };
    },

    /**
     * Get a comprehensive debug report combining all analysis.
     * Usage: HyperGraphVizState.debug.fullReport()
     */
    fullReport: () => {
      console.group("[debug] Full Debug Report");
      
      const stateAnalysis = debug.analyzeState();
      const layoutInspection = debug.inspectLayout();
      const connectionValidation = debug.validateConnections();
      const expansionState = debug.getExpansionState();
      const textAnalysis = debug.getTextAnalysis();
      
      console.log("\n=== Summary ===");
      console.log("Expansion state:", expansionState);
      if (connectionValidation) {
        console.log("Connection validation:", connectionValidation.valid ? "PASS" : "FAIL");
        if (!connectionValidation.valid) {
          console.warn("Connection issues need attention!");
        }
      }
      if (textAnalysis) {
        console.log("Text analysis:", textAnalysis.summary);
      }
      
      console.groupEnd();
      
      return {
        stateAnalysis,
        layoutInspection,
        connectionValidation,
        expansionState,
        textAnalysis,
      };
    }
  };

  return { applyState, applyVisibility, compressEdges, groupInputs, debug };
});
