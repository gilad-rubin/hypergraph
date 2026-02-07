/**
 * Constraint-based Layout Engine for hypergraph visualization
 *
 * A two-phase layout algorithm using constraint solving (kiwi.js/Cassowary):
 * 1. Node positioning via constraint relaxation
 * 2. Edge routing with collision avoidance
 *
 * Produces clean, readable DAG layouts with proper edge routing.
 */
(function() {
  'use strict';

  // Get kiwi.js from global scope (loaded before this script)
  const { Solver, Variable, Constraint, Operator, Strength } = window.kiwi;

  // ============================================================================
  // COMMON.JS - Utility functions
  // ============================================================================

  const HALF_PI = Math.PI * 0.5;

  const clamp = (value, min, max) =>
    value < min ? min : value > max ? max : value;

  const snap = (value, unit) => Math.round(value / unit) * unit;

  const distance1d = (a, b) => Math.abs(a - b);

  const angle = (a, b, orientation) => {
    if (orientation === 'vertical') {
      return Math.atan2(a.y - b.y, a.x - b.x);
    } else {
      return Math.atan2(a.x - b.x, a.y - b.y);
    }
  };

  const nodeLeft = (node) => node.x - node.width * 0.5;
  const nodeRight = (node) => node.x + node.width * 0.5;
  const nodeTop = (node) => node.y - node.height * 0.5;
  const nodeBottom = (node) => node.y + node.height * 0.5;

  // Node type to wrapper offset mapping
  // Different node types have different gaps between wrapper and visible content
  // Measured empirically: wrapper.bottom - innerElement.bottom for each type
  const VizConstants = window.HypergraphVizConstants || {};
  const NODE_TYPE_OFFSETS = VizConstants.NODE_TYPE_OFFSETS || {
    'PIPELINE': 26,
    'GRAPH': 26,
    'FUNCTION': 14,
    'DATA': 6,
    'INPUT': 6,
    'INPUT_GROUP': 6,
    'BRANCH': 10,
  };
  const DEFAULT_OFFSET = VizConstants.DEFAULT_OFFSET ?? 10;
  const VERTICAL_GAP = VizConstants.VERTICAL_GAP ?? 60;

  // Get visible bottom of node (accounts for wrapper/content offset)
  const nodeVisibleBottom = (node) => {
    let nodeType = node.data?.nodeType || 'FUNCTION';
    if (nodeType === 'PIPELINE' && !node.data?.isExpanded) {
      nodeType = 'FUNCTION';
    }
    const offset = NODE_TYPE_OFFSETS[nodeType] ?? DEFAULT_OFFSET;
    return nodeBottom(node) - offset;
  };

  const groupByRow = (nodes, orientation, rowSnap = null) => {
    const rows = {};
    const primaryCoord = orientation === 'vertical' ? 'y' : 'x';
    const secondaryCoord = orientation === 'vertical' ? 'x' : 'y';

    for (const node of nodes) {
      const key = rowSnap
        ? Math.round(node[primaryCoord] / rowSnap) * rowSnap
        : node[primaryCoord];
      rows[key] = rows[key] || [];
      rows[key].push(node);
    }

    const rowNumbers = Object.keys(rows).map((row) => parseFloat(row));
    rowNumbers.sort((a, b) => a - b);

    const sortedRows = rowNumbers.map((row) => rows[row]);
    for (let i = 0; i < sortedRows.length; i += 1) {
      sortedRows[i].sort((a, b) =>
        compare(a[secondaryCoord], b[secondaryCoord], a.id, b.id)
      );

      for (const node of sortedRows[i]) {
        node.row = i;
      }
    }

    return sortedRows;
  };

  const compare = (a, b, ...values) => {
    const delta = typeof a === 'string' ? a.localeCompare(b) : a - b;
    return delta !== 0 || values.length === 0 ? delta : compare(...values);
  };

  const offsetNode = (node, offset) => {
    node.x = node.x - offset.x;
    node.y = node.y - offset.y;
    node.order = node.x + node.y * 9999;
    return node;
  };

  const offsetEdge = (edge, offset) => {
    edge.points.forEach((point) => {
      point.x = point.x - offset.x;
      point.y = point.y - offset.y;
    });
    return edge;
  };

  const nearestOnLine = (x, y, ax, ay, bx, by) => {
    const dx = bx - ax;
    const dy = by - ay;
    const position = ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy || 1);
    const positionClamped = clamp(position, 0, 1);

    return {
      x: ax + dx * positionClamped,
      y: ay + dy * positionClamped,
      ax,
      ay,
      bx,
      by,
    };
  };

  // ============================================================================
  // CONSTRAINTS.JS - Constraint definitions
  // ============================================================================

  const rowConstraint = {
    strict: (constraint, layoutConfig, variableA, variableB) =>
      new Constraint(
        variableA.minus(variableB),
        Operator.Ge,
        constraint.separation,
        Strength.required
      ),
  };

  const layerConstraint = {
    strict: (constraint, layoutConfig, variableA, variableB) =>
      new Constraint(
        variableA.minus(variableB),
        Operator.Ge,
        layoutConfig.layerSpace,
        Strength.required
      ),
  };

  const parallelConstraint = {
    solve: (constraint, layoutConfig) => {
      const { a, b, strength } = constraint;
      const resolve =
        strength * (a[constraint.property] - b[constraint.property]);
      a[constraint.property] -= resolve;
      b[constraint.property] += resolve;
    },

    strict: (constraint, layoutConfig, variableA, variableB) =>
      new Constraint(
        variableA.minus(variableB),
        Operator.Eq,
        0,
        Strength.create(1, 0, 0, constraint.strength)
      ),
  };

  const alignConstraint = {
    solve: (constraint, layoutConfig) => {
      const { a, b, strength } = constraint;
      if (!a || !b || !strength) return;
      const delta = b[constraint.property] - a[constraint.property];
      a[constraint.property] += delta * strength;
    },
    strict: (constraint, layoutConfig, variableA, variableB) =>
      new Constraint(
        variableA.minus(variableB),
        Operator.Eq,
        0,
        Strength.create(1, 0, 0, constraint.strength || 1)
      ),
  };

  const crossingConstraint = {
    solve: (constraint, layoutConfig) => {
      const { edgeA, edgeB, separationA, separationB, strength } = constraint;

      const resolveSource =
        strength *
        ((edgeA.sourceNode[constraint.property] -
          edgeB.sourceNode[constraint.property] -
          separationA) /
          separationA);

      const resolveTarget =
        strength *
        ((edgeA.targetNode[constraint.property] -
          edgeB.targetNode[constraint.property] -
          separationB) /
          separationB);

      edgeA.sourceNode[constraint.property] -= resolveSource;
      edgeB.sourceNode[constraint.property] += resolveSource;
      edgeA.targetNode[constraint.property] -= resolveTarget;
      edgeB.targetNode[constraint.property] += resolveTarget;
    },
  };

  const separationConstraint = {
    strict: (constraint, layoutConfig, variableA, variableB) =>
      new Constraint(
        variableB.minus(variableA),
        Operator.Ge,
        constraint.separation,
        Strength.required
      ),
  };


  // ============================================================================
  // SOLVER.JS - Constraint solving
  // ============================================================================

  const solveLoose = (constraints, iterations, layoutConfig) => {
    for (let i = 0; i < iterations; i += 1) {
      for (const constraint of constraints) {
        constraint.base.solve(constraint, layoutConfig);
      }
    }
  };

  const solveStrict = (constraints, layoutConfig) => {
    const solver = new Solver();
    const variables = {};

    const variableId = (obj, property) => `${obj.id}_${property}`;

    const addVariable = (obj, property) => {
      const id = variableId(obj, property);

      if (!variables[id]) {
        const variable = (variables[id] = new Variable());
        variable.property = property;
        variable.obj = obj;
      }
    };

    for (const constraint of constraints) {
      const property = constraint.property;
      addVariable(constraint.a, property);
      addVariable(constraint.b, property);
    }

    let unsolvableCount = 0;

    for (const constraint of constraints) {
      const property = constraint.property;
      try {
        solver.addConstraint(
          constraint.base.strict(
            constraint,
            layoutConfig,
            variables[variableId(constraint.a, property)],
            variables[variableId(constraint.b, property)]
          )
        );
      } catch (err) {
        unsolvableCount += 1;
      }
    }

    if (unsolvableCount > 0) {
      console.warn(`Skipped ${unsolvableCount} unsolvable constraints`);
    }

    solver.updateVariables();

    const variablesList = Object.values(variables);

    for (const variable of variablesList) {
      variable.obj[variable.property] = variable.value();
    }
  };

  // ============================================================================
  // LAYOUT.JS - Main layout algorithm
  // ============================================================================

  /**
   * Initialize node positions using barycenter heuristic.
   * First spreads bottom-row nodes, then positions upper-row nodes
   * at the barycenter (average x) of their targets.
   */
  const initializeBarycenterPositions = (nodes, edges, coordPrimary) => {
    // Build target lists (source -> targets)
    const nodeById = {};
    for (const node of nodes) {
      nodeById[node.id] = node;
      node._targets = [];
      node._sources = [];
    }

    for (const edge of edges) {
      const source = nodeById[edge.source];
      const target = nodeById[edge.target];
      if (source && target) {
        source._targets.push(target);
        target._sources.push(source);
      }
    }

    // Find leaf nodes (no outgoing edges = bottom of DAG)
    const leafNodes = nodes.filter(n => n._targets.length === 0);
    const nonLeafNodes = nodes.filter(n => n._targets.length > 0);

    // Spread leaf nodes evenly
    const spacing = 200;
    leafNodes.forEach((node, i) => {
      node[coordPrimary] = i * spacing;
    });

    // Position non-leaf nodes at barycenter of their targets
    // Multiple passes for nodes with dependencies on each other
    for (let pass = 0; pass < 5; pass++) {
      for (const node of nonLeafNodes) {
        if (node._targets.length > 0) {
          let sum = 0;
          for (const target of node._targets) {
            sum += target[coordPrimary];
          }
          node[coordPrimary] = sum / node._targets.length;
        }
      }
    }

    // Cleanup
    for (const node of nodes) {
      delete node._targets;
      delete node._sources;
    }
  };

  /**
   * Reorder nodes within each row based on barycenter of their connections.
   * Uses a weighted average of both upstream (sources) and downstream (targets)
   * positions to minimize total edge length.
   *
   * Algorithm:
   * 1. Initialize bottom row evenly
   * 2. Bottom-to-top sweep: position based on targets
   * 3. Top-to-bottom sweep: use weighted average of sources AND targets
   * 4. Optional refinement passes to converge on optimal positions
   */
  const reorderRowsByBarycenter = (rows, edges, coordPrimary) => {
    if (rows.length === 0) return;

    const spacing = 200;

    // Build bidirectional adjacency: targets (downstream) and sources (upstream)
    const nodeById = {};
    for (const row of rows) {
      for (const node of row) {
        nodeById[node.id] = node;
        node._targets = [];
        node._sources = [];
      }
    }

    for (const edge of edges) {
      const source = nodeById[edge.source];
      const target = nodeById[edge.target];
      if (source && target) {
        source._targets.push(target);
        target._sources.push(source);
      }
    }

    // Helper: compute weighted barycenter from both sources and targets
    const computeWeightedBarycenter = (node, sourceWeight, targetWeight) => {
      let sum = 0;
      let count = 0;

      // Add source contributions
      for (const src of node._sources) {
        sum += src[coordPrimary] * sourceWeight;
        count += sourceWeight;
      }

      // Add target contributions
      for (const tgt of node._targets) {
        sum += tgt[coordPrimary] * targetWeight;
        count += targetWeight;
      }

      return count > 0 ? sum / count : null;
    };

    // === SWEEP 1: Bottom-to-top using targets only ===
    // Establishes initial order based on downstream connections

    const bottomRow = rows[rows.length - 1];
    bottomRow.forEach((node, i) => {
      node[coordPrimary] = i * spacing;
    });

    for (let rowIdx = rows.length - 2; rowIdx >= 0; rowIdx--) {
      const row = rows[rowIdx];

      for (const node of row) {
        if (node._targets.length > 0) {
          const sum = node._targets.reduce((acc, t) => acc + t[coordPrimary], 0);
          node._barycenter = sum / node._targets.length;
        } else {
          node._barycenter = row.indexOf(node) * spacing;
        }
      }

      row.sort((a, b) => compare(a._barycenter, b._barycenter, a.id, b.id));
      row.forEach((node, i) => {
        node[coordPrimary] = i * spacing;
      });
    }

    // === SWEEP 2: Top-to-bottom using weighted average of sources AND targets ===
    // This ensures nodes consider both their inputs and outputs for positioning

    for (let rowIdx = 1; rowIdx < rows.length; rowIdx++) {
      const row = rows[rowIdx];

      for (const node of row) {
        // Weight sources and targets equally to balance both directions
        const weighted = computeWeightedBarycenter(node, 1, 1);
        if (weighted !== null) {
          node._barycenter = weighted;
        } else {
          node._barycenter = node[coordPrimary];
        }
      }

      row.sort((a, b) => compare(a._barycenter, b._barycenter, a.id, b.id));
      row.forEach((node, i) => {
        node[coordPrimary] = i * spacing;
      });
    }

    // === SWEEP 3: Bottom-to-top refinement with weighted barycenter ===
    // Propagate the improved positions back up

    for (let rowIdx = rows.length - 2; rowIdx >= 0; rowIdx--) {
      const row = rows[rowIdx];

      for (const node of row) {
        const weighted = computeWeightedBarycenter(node, 1, 1);
        if (weighted !== null) {
          node._barycenter = weighted;
        } else {
          node._barycenter = node[coordPrimary];
        }
      }

      row.sort((a, b) => compare(a._barycenter, b._barycenter, a.id, b.id));
      row.forEach((node, i) => {
        node[coordPrimary] = i * spacing;
      });
    }

    // Cleanup
    for (const row of rows) {
      for (const node of row) {
        delete node._targets;
        delete node._sources;
        delete node._barycenter;
      }
    }
  };

  const layout = ({
    nodes,
    edges,
    layers,
    spaceX,
    spaceY,
    spreadX,
    layerSpaceY,
    iterations,
    orientation,
  }) => {
    let coordPrimary = 'x';
    let coordSecondary = 'y';

    if (orientation === 'horizontal') {
      coordPrimary = 'y';
      coordSecondary = 'x';
    }

    for (const node of nodes) {
      node[coordPrimary] = 0;
      node[coordSecondary] = 0;
    }

    const layoutConfig = {
      orientation,
      spaceX,
      spaceY,
      spreadX,
      layerSpace: (spaceY + layerSpaceY) * 0.5,
      coordPrimary,
      coordSecondary,
      extraVerticalGap: 0,
    };

    const rowConstraints = createRowConstraints(edges, layoutConfig);
    const layerConstraints = createLayerConstraints(nodes, layers, layoutConfig);

    solveStrict(
      [...rowConstraints, ...layerConstraints],
      layoutConfig,
      1
    );

    // Initialize x positions using barycenter heuristic AFTER y positions are set
    // This gives better initial ordering before separation constraints lock things in
    initializeBarycenterPositions(nodes, edges, coordPrimary);

    const rows = groupByRow(nodes, orientation, layoutConfig.spaceY);

    const crossingConstraints = createCrossingConstraints(edges, layoutConfig);
    const parallelConstraints = createParallelConstraints(edges, layoutConfig);
    const dataAlignmentConstraints = createDataAlignmentConstraints(nodes, edges, layoutConfig);

    for (let i = 0; i < iterations; i += 1) {
      solveLoose(crossingConstraints, 1, layoutConfig);
      solveLoose(parallelConstraints, 50, layoutConfig);
    }

    // Reorder nodes within each row by barycenter of their connected nodes
    // This minimizes edge crossings before separation constraints lock in order
    reorderRowsByBarycenter(rows, edges, coordPrimary);

    const separationConstraints = createSeparationConstraints(rows, layoutConfig);
    const sharedTargetConstraints = createSharedTargetConstraints(edges, layoutConfig);

    solveStrict(
      [
        ...separationConstraints,
        ...sharedTargetConstraints,
        ...parallelConstraints,
        ...dataAlignmentConstraints,
      ],
      layoutConfig,
      1
    );

    // Keep edge-to-edge spacing uniform by disabling density-based row expansion.
    expandDenseRows(edges, rows, coordSecondary, spaceY, orientation, 0);

    // After layout settles, nudge DATA nodes toward their producer X (within row constraints).
    alignDataNodesToSources(rows, edges, layoutConfig);
  };

  const createRowConstraints = (edges, layoutConfig) =>
    edges.map((edge) => {
      let separation = layoutConfig.spaceY;

      if (layoutConfig.orientation === 'vertical') {
        const source = edge.sourceNode;
        const target = edge.targetNode;
        const sourceOffset = nodeBottom(source) - nodeVisibleBottom(source);
        const sourceVisibleHalf = Math.max(0, source.height * 0.5 - sourceOffset);
        const targetVisibleHalf = target.height * 0.5;
        separation = VERTICAL_GAP + sourceVisibleHalf + targetVisibleHalf;
      }

      return {
        base: rowConstraint,
        property: layoutConfig.coordSecondary,
        a: edge.targetNode,
        b: edge.sourceNode,
        separation,
      };
    });

  const createLayerConstraints = (nodes, layers, layoutConfig) => {
    const layerConstraints = [];

    if (!layers) {
      return layerConstraints;
    }

    const layerGroups = layers.map((name) =>
      nodes.filter((node) => node.nearestLayer === name)
    );

    for (let i = 0; i < layerGroups.length - 1; i += 1) {
      const layerNodes = layerGroups[i];
      const nextLayerNodes = layerGroups[i + 1];

      const intermediary = { id: `layer-${i}`, x: 0, y: 0 };

      for (const node of layerNodes) {
        layerConstraints.push({
          base: layerConstraint,
          property: layoutConfig.coordSecondary,
          a: intermediary,
          b: node,
        });
      }

      for (const node of nextLayerNodes) {
        layerConstraints.push({
          base: layerConstraint,
          property: layoutConfig.coordSecondary,
          a: node,
          b: intermediary,
        });
      }
    }

    return layerConstraints;
  };

  const createCrossingConstraints = (edges, layoutConfig) => {
    const { spaceX, coordPrimary } = layoutConfig;
    const crossingConstraints = [];

    for (let i = 0; i < edges.length; i += 1) {
      const edgeA = edges[i];
      const { sourceNode: sourceA, targetNode: targetA } = edgeA;

      const edgeADegree =
        sourceA.sources.length +
        sourceA.targets.length +
        targetA.sources.length +
        targetA.targets.length;

      for (let j = i + 1; j < edges.length; j += 1) {
        const edgeB = edges[j];
        const { sourceNode: sourceB, targetNode: targetB } = edgeB;

        if (sourceA.row >= targetB.row || targetA.row <= sourceB.row) {
          continue;
        }

        const edgeBDegree =
          sourceB.sources.length +
          sourceB.targets.length +
          targetB.sources.length +
          targetB.targets.length;

        crossingConstraints.push({
          base: crossingConstraint,
          property: coordPrimary,
          edgeA: edgeA,
          edgeB: edgeB,
          separationA: sourceA.width * 0.5 + spaceX + sourceB.width * 0.5,
          separationB: targetA.width * 0.5 + spaceX + targetB.width * 0.5,
          strength: 1 / Math.max(1, (edgeADegree + edgeBDegree) / 4),
        });
      }
    }

    return crossingConstraints;
  };

  const isDataNode = (node) =>
    (node && (node.data?.nodeType || node.nodeType)) === 'DATA';

  const createParallelConstraints = (edges, layoutConfig) =>
    edges.map(({ sourceNode, targetNode }) => ({
      base: parallelConstraint,
      property: layoutConfig.coordPrimary,
      a: sourceNode,
      b: targetNode,
      // Increased base strength from 0.6 to 1.5 to better align nodes vertically
      // Still scales down with node degree to allow flexibility for highly connected nodes
      strength:
        1.5 /
        Math.max(1, sourceNode.targets.length + targetNode.sources.length - 2),
    }));

  const buildDataSourceMap = (nodes, edges) => {
    const byId = new Map(nodes.map((node) => [node.id, node]));
    const dataSources = new Map();

    for (const edge of edges) {
      const targetNode = byId.get(edge.target);
      if (!targetNode || !isDataNode(targetNode)) continue;
      const sourceNode = byId.get(edge.source);
      if (!sourceNode) continue;
      dataSources.set(targetNode.id, sourceNode);
    }

    return dataSources;
  };

  const createDataAlignmentConstraints = (nodes, edges, layoutConfig) => {
    if (!DATA_NODE_ALIGN_WEIGHT) return [];
    const byId = new Map(nodes.map((node) => [node.id, node]));
    const dataSources = buildDataSourceMap(nodes, edges);
    const constraints = [];
    nodes.forEach((node) => {
      if (!isDataNode(node)) return;
      const sourceId = node.data?.sourceId;
      const sourceNode = sourceId ? byId.get(sourceId) : dataSources.get(node.id);
      if (!sourceNode) return;
      constraints.push({
        base: alignConstraint,
        property: layoutConfig.coordPrimary,
        a: node,
        b: sourceNode,
        strength: DATA_NODE_ALIGN_WEIGHT,
      });
    });
    return constraints;
  };

  const createSeparationConstraints = (rows, layoutConfig) => {
    const { spaceX, coordPrimary, spreadX, orientation } = layoutConfig;
    const separationConstraints = [];

    for (let i = 0; i < rows.length; i += 1) {
      const rowNodes = rows[i];

      rowNodes.sort((a, b) =>
        compare(a[coordPrimary], b[coordPrimary], a.id, b.id)
      );

      for (let j = 0; j < rowNodes.length - 1; j += 1) {
        const nodeA = rowNodes[j];
        const nodeB = rowNodes[j + 1];

        const degreeA = Math.max(
          1,
          nodeA.targets.length + nodeA.sources.length - 2
        );
        const degreeB = Math.max(
          1,
          nodeB.targets.length + nodeB.sources.length - 2
        );

        const spread = Math.min(10, degreeA * degreeB * spreadX);
        const space = snap(spread * spaceX, spaceX);

        const separation =
          orientation === 'horizontal'
            ? nodeA.height + nodeB.height
            : nodeA.width * 0.5 + space + nodeB.width * 0.5;

        separationConstraints.push({
          base: separationConstraint,
          property: coordPrimary,
          a: nodeA,
          b: nodeB,
          separation,
        });
      }
    }

    return separationConstraints;
  };

  const alignDataNodesToSources = (rows, edges, layoutConfig) => {
    if (!DATA_NODE_ALIGN_WEIGHT) return;
    const alignWeight = Math.max(0, Math.min(1, DATA_NODE_ALIGN_WEIGHT));
    const coordPrimary = layoutConfig.coordPrimary;
    const nodeById = new Map();
    rows.forEach((row) => {
      row.forEach((node) => {
        nodeById.set(node.id, node);
      });
    });
    const dataSources = buildDataSourceMap(Array.from(nodeById.values()), edges);

    const degreeOf = (node) =>
      Math.max(1, node.targets.length + node.sources.length - 2);

    const computeSpace = (nodeA, nodeB) => {
      const degreeA = degreeOf(nodeA);
      const degreeB = degreeOf(nodeB);
      const spread = Math.min(10, degreeA * degreeB * layoutConfig.spreadX);
      return snap(spread * layoutConfig.spaceX, layoutConfig.spaceX);
    };

    rows.forEach((row) => {
      row.sort((a, b) =>
        compare(a[coordPrimary], b[coordPrimary], a.id, b.id)
      );

      for (let i = 0; i < row.length; i += 1) {
        const node = row[i];
        if (!isDataNode(node)) continue;
        const sourceId = node.data?.sourceId;
        const sourceNode = sourceId ? nodeById.get(sourceId) : dataSources.get(node.id);
        if (!sourceNode) continue;

        let minX = -Infinity;
        let maxX = Infinity;
        if (i > 0) {
          const left = row[i - 1];
          const space = computeSpace(left, node);
          minX =
            left[coordPrimary] +
            left.width * 0.5 +
            space +
            node.width * 0.5;
        }
        if (i < row.length - 1) {
          const right = row[i + 1];
          const space = computeSpace(node, right);
          maxX =
            right[coordPrimary] -
            (node.width * 0.5 + space + right.width * 0.5);
        }

        if (minX > maxX) continue;
        const desiredX = sourceNode[coordPrimary];
        const clampedX = clamp(desiredX, minX, maxX);
        node[coordPrimary] =
          node[coordPrimary] +
          (clampedX - node[coordPrimary]) * alignWeight;
      }
    });
  };

  const createSharedTargetConstraints = (edges, layoutConfig) => {
    const { spaceX, coordPrimary } = layoutConfig;
    const sourcesByTarget = {};

    for (const edge of edges) {
      const targetId = edge.targetNode.id;
      if (!sourcesByTarget[targetId]) sourcesByTarget[targetId] = [];
      if (!sourcesByTarget[targetId].includes(edge.sourceNode)) {
        sourcesByTarget[targetId].push(edge.sourceNode);
      }
    }

    const constraints = [];
    for (const targetId in sourcesByTarget) {
      const sources = sourcesByTarget[targetId];
      if (sources.length < 2) continue;

      sources.sort((a, b) =>
        compare(a[coordPrimary], b[coordPrimary], a.id, b.id)
      );

      for (let i = 0; i < sources.length - 1; i += 1) {
        const nodeA = sources[i];
        const nodeB = sources[i + 1];
        const separation = (
          nodeA.width * 0.5 + spaceX * 0.5 + nodeB.width * 0.5
        ) * EDGE_SHARED_TARGET_SPACING_SCALE;
        constraints.push({
          base: separationConstraint,
          property: coordPrimary,
          a: nodeA,
          b: nodeB,
          separation,
        });
      }
    }

    return constraints;
  };

  // Configuration constants for edge routing
  const EDGE_CONVERGENCE_OFFSET = VizConstants.EDGE_CONVERGENCE_OFFSET ?? 20;  // Y offset from target for convergence point
  const EDGE_STRAIGHTEN_MAX_SHIFT = VizConstants.EDGE_STRAIGHTEN_MAX_SHIFT ?? 140;
  const EDGE_MICRO_X_SNAP = VizConstants.EDGE_MICRO_X_SNAP ?? 6;
  const EDGE_ANGLE_WEIGHT = VizConstants.EDGE_ANGLE_WEIGHT ?? 1;
  const EDGE_CURVE_WEIGHT = VizConstants.EDGE_CURVE_WEIGHT ?? 0.75;
  const EDGE_TURN_WEIGHT = VizConstants.EDGE_TURN_WEIGHT ?? 0.25;
  const EDGE_LATERAL_WEIGHT = VizConstants.EDGE_LATERAL_WEIGHT ?? 0.001;
  const EDGE_NODE_PENALTY = VizConstants.EDGE_NODE_PENALTY ?? 0;
  const EDGE_NODE_CLEARANCE = VizConstants.EDGE_NODE_CLEARANCE ?? 0;
  const EDGE_NONSTRAIGHT_WEIGHT = VizConstants.EDGE_NONSTRAIGHT_WEIGHT ?? 0;
  const EDGE_EDGE_PENALTY = VizConstants.EDGE_EDGE_PENALTY ?? 0;
  const EDGE_EDGE_CLEARANCE = VizConstants.EDGE_EDGE_CLEARANCE ?? 0;
  const EDGE_SHARED_TARGET_SPACING_SCALE = VizConstants.EDGE_SHARED_TARGET_SPACING_SCALE ?? 1;
  const DATA_NODE_ALIGN_WEIGHT = VizConstants.DATA_NODE_ALIGN_WEIGHT ?? 0;
  const MIN_HORIZONTAL_DIST_FOR_WAYPOINT = 20;
  const MIN_VERTICAL_DIST_FOR_WAYPOINT = 50;
  const SHOULDER_VERTICAL_RATIO = 0.4;
  const SHOULDER_HORIZONTAL_RATIO = 0.5;

  /**
   * Calculate the convergence point where edges should merge before entering target stem.
   * @param {Object} target - Target node
   * @param {number} stemMinTarget - Minimum stem length at target
   * @returns {number} Y coordinate for convergence point
   */
  const calculateConvergenceY = (target, stemMinTarget) => {
    return nodeTop(target) - stemMinTarget - EDGE_CONVERGENCE_OFFSET;
  };

  const expandDenseRows = (
    edges,
    rows,
    coordSecondary,
    spaceY,
    orientation,
    scale = 1.25,
    unit = 0.25
  ) => {
    const densities = rowDensity(edges, orientation);
    const spaceYUnit = Math.round(spaceY * unit);
    let currentOffset = 0;

    for (let i = 0; i < rows.length - 1; i += 1) {
      const density = densities[i] || 0;

      const offset = snap(density * scale * spaceY, spaceYUnit);

      if (orientation === 'horizontal') {
        const maxWidthInCurrentRow = Math.max(
          ...rows[i].map((node) => node.width)
        );
        const maxWidthInNextRow = Math.max(
          ...rows[i + 1].map((node) => node.width)
        );
        currentOffset +=
          offset + maxWidthInCurrentRow * 0.5 + maxWidthInNextRow * 0.5;
      } else {
        currentOffset += offset;
      }

      for (const node of rows[i + 1]) {
        node[coordSecondary] += currentOffset;
      }
    }
  };

  const rowDensity = (edges, orientation) => {
    const rows = {};

    for (const edge of edges) {
      const edgeAngle =
        Math.abs(angle(edge.targetNode, edge.sourceNode, orientation) - HALF_PI) /
        HALF_PI;

      const sourceRow = edge.sourceNode.row;
      const targetRow = edge.targetNode.row - 1;

      rows[sourceRow] = rows[sourceRow] || [0, 0];
      rows[sourceRow][0] += edgeAngle;
      rows[sourceRow][1] += 1;

      if (targetRow !== sourceRow) {
        rows[targetRow] = rows[targetRow] || [0, 0];
        rows[targetRow][0] += edgeAngle;
        rows[targetRow][1] += 1;
      }
    }

    for (const row in rows) {
      rows[row] = rows[row][0] / (rows[row][1] || 1);
    }

    return Object.values(rows);
  };

  // ============================================================================
  // ROUTING.JS - Edge routing
  // ============================================================================

  const routing = ({
    nodes,
    edges,
    spaceX,
    spaceY,
    minPassageGap,
    stemUnit,
    stemMinSource,
    stemMinTarget,
    stemMax,
    stemSpaceSource,
    stemSpaceTarget,
    orientation,
  }) => {
    const rows = groupByRow(nodes, orientation, spaceY);
    const routedSegments = [];
    const getRowCorridors = (row) => {
      if (!row || row.length === 0) return [];
      const firstNode = row[0];
      const rowExtended = [
        { ...firstNode, x: Number.MIN_SAFE_INTEGER },
        ...row,
        { ...firstNode, x: Number.MAX_SAFE_INTEGER },
      ];
      const corridors = [];
      for (let j = 0; j < rowExtended.length - 1; j += 1) {
        const node = rowExtended[j];
        const nextNode = rowExtended[j + 1];
        const nodeGap = nodeLeft(nextNode) - nodeRight(node);

        if (nodeGap < minPassageGap) {
          continue;
        }

        const offsetX = Math.min(spaceX, nodeGap * 0.5);
        const min = nodeRight(node) + offsetX;
        const max = nodeLeft(nextNode) - offsetX;
        if (min <= max) {
          corridors.push({ min, max });
        }
      }
      return corridors;
    };

    const intersectIntervals = (leftIntervals, rightIntervals) => {
      const result = [];
      for (const left of leftIntervals) {
        for (const right of rightIntervals) {
          const min = Math.max(left.min, right.min);
          const max = Math.min(left.max, right.max);
          if (min <= max) {
            result.push({ min, max });
          }
        }
      }
      return result;
    };

    const clampToIntervals = (intervals, x) => {
      let best = null;
      let bestDist = Infinity;
      for (const interval of intervals) {
        const clamped = clamp(x, interval.min, interval.max);
        const dist = Math.abs(clamped - x);
        if (dist < bestDist) {
          bestDist = dist;
          best = clamped;
        }
      }
      return { x: best, dist: bestDist };
    };

    const distanceSq = (ax, ay, bx, by) => {
      const dx = ax - bx;
      const dy = ay - by;
      return dx * dx + dy * dy;
    };

    const segmentsIntersect = (ax, ay, bx, by, cx, cy, dx, dy) => {
      const eps = 1e-6;
      const cross = (x1, y1, x2, y2, x3, y3) =>
        (x2 - x1) * (y3 - y1) - (y2 - y1) * (x3 - x1);
      const onSegment = (x1, y1, x2, y2, x3, y3) => {
        return (
          Math.min(x1, x2) - eps <= x3 && x3 <= Math.max(x1, x2) + eps &&
          Math.min(y1, y2) - eps <= y3 && y3 <= Math.max(y1, y2) + eps
        );
      };

      const d1 = cross(ax, ay, bx, by, cx, cy);
      const d2 = cross(ax, ay, bx, by, dx, dy);
      const d3 = cross(cx, cy, dx, dy, ax, ay);
      const d4 = cross(cx, cy, dx, dy, bx, by);

      if ((d1 > eps && d2 < -eps || d1 < -eps && d2 > eps) &&
          (d3 > eps && d4 < -eps || d3 < -eps && d4 > eps)) {
        return true;
      }

      if (Math.abs(d1) <= eps && onSegment(ax, ay, bx, by, cx, cy)) return true;
      if (Math.abs(d2) <= eps && onSegment(ax, ay, bx, by, dx, dy)) return true;
      if (Math.abs(d3) <= eps && onSegment(cx, cy, dx, dy, ax, ay)) return true;
      if (Math.abs(d4) <= eps && onSegment(cx, cy, dx, dy, bx, by)) return true;
      if (
        Math.abs(d1) <= eps &&
        Math.abs(d2) <= eps &&
        Math.abs(d3) <= eps &&
        Math.abs(d4) <= eps
      ) {
        const abXOverlap =
          Math.max(ax, bx) + eps >= Math.min(cx, dx) &&
          Math.max(cx, dx) + eps >= Math.min(ax, bx);
        const abYOverlap =
          Math.max(ay, by) + eps >= Math.min(cy, dy) &&
          Math.max(cy, dy) + eps >= Math.min(ay, by);
        if (abXOverlap && abYOverlap) return true;
      }
      return false;
    };

    const shouldSkipEdgeSegment = (segment, sourceNode, targetNode, ax, ay, bx, by) => {
      if (!segment) return true;
      if (sourceNode && (segment.sourceId === sourceNode.id || segment.targetId === sourceNode.id)) {
        return true;
      }
      if (targetNode && (segment.sourceId === targetNode.id || segment.targetId === targetNode.id)) {
        return true;
      }
      if (EDGE_EDGE_CLEARANCE > 0) {
        const clearanceSq = EDGE_EDGE_CLEARANCE * EDGE_EDGE_CLEARANCE;
        const near =
          distanceSq(ax, ay, segment.x1, segment.y1) <= clearanceSq ||
          distanceSq(ax, ay, segment.x2, segment.y2) <= clearanceSq ||
          distanceSq(bx, by, segment.x1, segment.y1) <= clearanceSq ||
          distanceSq(bx, by, segment.x2, segment.y2) <= clearanceSq;
        if (near) return true;
      }
      return false;
    };

    const segmentEdgeHits = (ax, ay, bx, by, sourceNode, targetNode) => {
      if (EDGE_EDGE_PENALTY <= 0 || routedSegments.length === 0) return 0;
      let hits = 0;
      for (const segment of routedSegments) {
        if (shouldSkipEdgeSegment(segment, sourceNode, targetNode, ax, ay, bx, by)) {
          continue;
        }
        if (segmentsIntersect(ax, ay, bx, by, segment.x1, segment.y1, segment.x2, segment.y2)) {
          hits += 1;
        }
      }
      return hits;
    };

    const buildSmoothCorridorPath = (rowIndices, preferredX, sourceY, targetX, sourceNode, targetNode) => {
      if (!rowIndices.length) return null;
      const corridorRows = rowIndices.map((idx) => getRowCorridors(rows[idx]));
      if (corridorRows.some((row) => row.length === 0)) return null;

      const uniqueNumbers = (values) => {
        const seen = new Map();
        values.forEach((v) => {
          if (!Number.isFinite(v)) return;
          const key = Math.round(v * 2) / 2;
          if (!seen.has(key)) seen.set(key, key);
        });
        return Array.from(seen.values());
      };

      const buildRowCandidates = (intervals, nextIntervals, fallbackX, targetXValue) => {
        const candidates = [];
        intervals.forEach((interval) => {
          const intervalCandidates = [];
          if (Number.isFinite(interval.min)) intervalCandidates.push(interval.min);
          if (Number.isFinite(interval.max)) intervalCandidates.push(interval.max);
          if (Number.isFinite(fallbackX)) {
            intervalCandidates.push(clamp(fallbackX, interval.min, interval.max));
          }
          if (Number.isFinite(targetXValue)) {
            intervalCandidates.push(clamp(targetXValue, interval.min, interval.max));
          }
          if (nextIntervals && nextIntervals.length) {
            nextIntervals.forEach((nextInterval) => {
              if (Number.isFinite(nextInterval.min)) {
                intervalCandidates.push(clamp(nextInterval.min, interval.min, interval.max));
              }
              if (Number.isFinite(nextInterval.max)) {
                intervalCandidates.push(clamp(nextInterval.max, interval.min, interval.max));
              }
            });
          }
          uniqueNumbers(intervalCandidates).forEach((value) => {
            candidates.push(value);
          });
        });
        return uniqueNumbers(candidates);
      };

      const expandCandidatesWithCarry = (candidates, intervals, prevLayer) => {
        if (!prevLayer || prevLayer.length === 0) return candidates;

        const expanded = candidates.slice();
        const canUseX = (x) => intervals.some((interval) => x >= interval.min && x <= interval.max);

        prevLayer.forEach((bucket) => {
          Object.values(bucket.bySign).forEach((state) => {
            if (canUseX(state.x)) {
              expanded.push(state.x);
            }
          });
        });

        return uniqueNumbers(expanded);
      };

      const rowTopYs = rowIndices.map((idx) => {
        const firstNode = rows[idx][0];
        return nodeTop(firstNode) - spaceY;
      });
      const rowBottomYs = rowIndices.map((idx) => {
        const firstNode = rows[idx][0];
        return nodeTop(firstNode) - spaceY + firstNode.height + spaceY;
      });

      const isExpandedContainer = (node) => {
        const nodeType = node.data?.nodeType;
        const isContainer = nodeType === 'PIPELINE' || nodeType === 'GRAPH';
        return isContainer && node.data?.isExpanded;
      };

      const buildRect = (node) => {
        const clearance = EDGE_NODE_CLEARANCE;
        return {
          left: nodeLeft(node) - clearance,
          right: nodeRight(node) + clearance,
          top: nodeTop(node) - clearance,
          bottom: nodeBottom(node) + clearance,
        };
      };

      const segmentIntersectsRect = (ax, ay, bx, by, rect) => {
        if (ax === bx && ay === by) {
          return ax >= rect.left && ax <= rect.right && ay >= rect.top && ay <= rect.bottom;
        }

        let t0 = 0;
        let t1 = 1;
        const dx = bx - ax;
        const dy = by - ay;
        const p = [-dx, dx, -dy, dy];
        const q = [ax - rect.left, rect.right - ax, ay - rect.top, rect.bottom - ay];

        for (let i = 0; i < 4; i += 1) {
          const pi = p[i];
          const qi = q[i];
          if (pi === 0) {
            if (qi < 0) return false;
            continue;
          }
          const r = qi / pi;
          if (pi < 0) {
            if (r > t1) return false;
            if (r > t0) t0 = r;
          } else {
            if (r < t0) return false;
            if (r < t1) t1 = r;
          }
        }
        return true;
      };

      const rectsByRow = new Map();
      const getRowRects = (rowIndex) => {
        if (rectsByRow.has(rowIndex)) return rectsByRow.get(rowIndex);
        const rowNodes = rows[rowIndex] || [];
        const rects = [];
        rowNodes.forEach((node) => {
          if (node.id === sourceNode?.id || node.id === targetNode?.id) return;
          if (isExpandedContainer(node)) return;
          rects.push(buildRect(node));
        });
        rectsByRow.set(rowIndex, rects);
        return rects;
      };

      const segmentNodeHits = (ax, ay, bx, by, rowA, rowB) => {
        if (EDGE_NODE_PENALTY <= 0) return 0;
        const rects = [];
        rects.push(...getRowRects(rowA));
        if (rowB !== rowA) {
          rects.push(...getRowRects(rowB));
        }
        let hits = 0;
        for (const rect of rects) {
          if (segmentIntersectsRect(ax, ay, bx, by, rect)) {
            hits += 1;
          }
        }
        return hits;
      };

      const makeState = (x, maxAngle, nonStraight, curves, turns, lateral, nodeHits, edgeHits, cost, prevIndex, prevLastSign, lastSign) => {
        return { x, maxAngle, nonStraight, curves, turns, lateral, nodeHits, edgeHits, cost, prevIndex, prevLastSign, lastSign };
      };

      const compareState = (a, b) => {
        if (!b) return -1;
        if (a.cost !== b.cost) return a.cost - b.cost;
        return 0;
      };

      const rowCandidates = corridorRows.map((intervals, r) => {
        const nextIntervals = corridorRows[r + 1] || [];
        return buildRowCandidates(intervals, nextIntervals, preferredX, targetX);
      });
      if (rowCandidates.some((row) => row.length === 0)) return null;

      const initialLayer = rowCandidates[0].map((x) => {
        const dx = x - preferredX;
        const dy = Math.max(1, rowTopYs[0] - sourceY);
        const angle = Math.atan2(Math.abs(dx), dy);
        const sign = Math.abs(dx) < 0.5 ? 0 : (dx > 0 ? 1 : -1);
        const curves = Math.abs(dx) > 0.5 ? 1 : 0;
        const turns = 0;
        const lateral = Math.abs(dx);
        const nodeHits = segmentNodeHits(preferredX, sourceY, x, rowTopYs[0], sourceNode?.row, rowIndices[0]);
        const edgeHits = segmentEdgeHits(preferredX, sourceY, x, rowTopYs[0], sourceNode, targetNode);
        const nonStraight = angle;
        const cost = angle * EDGE_ANGLE_WEIGHT +
          nonStraight * EDGE_NONSTRAIGHT_WEIGHT +
          curves * EDGE_CURVE_WEIGHT +
          turns * EDGE_TURN_WEIGHT +
          lateral * EDGE_LATERAL_WEIGHT +
          nodeHits * EDGE_NODE_PENALTY +
          edgeHits * EDGE_EDGE_PENALTY;
        return {
          bySign: {
            [sign]: makeState(x, angle, nonStraight, curves, turns, lateral, nodeHits, edgeHits, cost, null, null, sign),
          },
        };
      });
      const layers = [initialLayer];

      for (let r = 1; r < rowCandidates.length; r += 1) {
        const candidates = expandCandidatesWithCarry(
          rowCandidates[r],
          corridorRows[r],
          layers[r - 1]
        );
        const next = candidates.map(() => ({ bySign: {} }));
        for (let j = 0; j < candidates.length; j += 1) {
          const x = candidates[j];
          for (let i = 0; i < layers[r - 1].length; i += 1) {
            const prevBucket = layers[r - 1][i].bySign;
            Object.keys(prevBucket).forEach((signKey) => {
              const prevState = prevBucket[signKey];
              const prevX = prevState.x;
              const dx = x - prevX;
              const dy = Math.max(1, rowTopYs[r] - rowBottomYs[r - 1]);
              const angle = Math.atan2(Math.abs(dx), dy);
              const sign = Math.abs(dx) < 0.5 ? 0 : (dx > 0 ? 1 : -1);
              const lastSign = prevState.lastSign || 0;
              const turn = (lastSign !== 0 && sign !== 0 && sign !== lastSign) ? 1 : 0;
              const nextSign = sign === 0 ? lastSign : sign;
              const maxAngle = Math.max(prevState.maxAngle, angle);
              const nonStraight = prevState.nonStraight + angle;
              const curves = prevState.curves + (Math.abs(dx) > 0.5 ? 1 : 0);
              const turns = prevState.turns + turn;
              const lateral = prevState.lateral + Math.abs(dx);
              const hits = segmentNodeHits(prevX, rowBottomYs[r - 1], x, rowTopYs[r], rowIndices[r - 1], rowIndices[r]);
              const nodeHits = prevState.nodeHits + hits;
              const edgeCrosses = segmentEdgeHits(prevX, rowBottomYs[r - 1], x, rowTopYs[r], sourceNode, targetNode);
              const edgeHits = prevState.edgeHits + edgeCrosses;
              const cost = maxAngle * EDGE_ANGLE_WEIGHT +
                nonStraight * EDGE_NONSTRAIGHT_WEIGHT +
                curves * EDGE_CURVE_WEIGHT +
                turns * EDGE_TURN_WEIGHT +
                lateral * EDGE_LATERAL_WEIGHT +
                nodeHits * EDGE_NODE_PENALTY +
                edgeHits * EDGE_EDGE_PENALTY;
              const candidate = makeState(x, maxAngle, nonStraight, curves, turns, lateral, nodeHits, edgeHits, cost, i, lastSign, nextSign);
              const existing = next[j].bySign[nextSign];
              if (!existing || compareState(candidate, existing) < 0) {
                next[j].bySign[nextSign] = candidate;
              }
            });
          }
        }
        layers.push(next);
      }

      let best = null;
      let bestIndex = 0;
      let bestSign = 0;
      const lastLayer = layers[layers.length - 1];
      for (let i = 0; i < lastLayer.length; i += 1) {
        const bucket = lastLayer[i].bySign;
        Object.keys(bucket).forEach((signKey) => {
          const candidate = bucket[signKey];
          if (!best || compareState(candidate, best) < 0) {
            best = candidate;
            bestIndex = i;
            bestSign = parseInt(signKey, 10);
          }
        });
      }

      if (!best) {
        return null;
      }

      const path = new Array(corridorRows.length);
      let currentIndex = bestIndex;
      let currentSign = bestSign;
      for (let r = layers.length - 1; r >= 0; r -= 1) {
        const state = layers[r][currentIndex].bySign[currentSign];
        if (!state) break;
        path[r] = state.x;
        currentIndex = state.prevIndex;
        currentSign = state.prevLastSign || 0;
        if (currentIndex === null || currentIndex === undefined) {
          break;
        }
      }

      const snapMicroMoves = (xs, corridorRowsLocal, threshold) => {
        if (xs.length === 0) return xs;
        const epsilon = threshold;
        const snapped = [xs[0]];
        for (let r = 1; r < xs.length; r += 1) {
          const prevX = snapped[r - 1];
          const currentX = xs[r];
          if (Math.abs(currentX - prevX) <= threshold) {
            const intervals = corridorRowsLocal[r];
            const canUsePrev = intervals.some((interval) =>
              prevX >= (interval.min - epsilon) && prevX <= (interval.max + epsilon)
            );
            if (canUsePrev) {
              snapped.push(prevX);
              continue;
            }
          }
          snapped.push(currentX);
        }
        return snapped;
      };

      return snapMicroMoves(path, corridorRows, EDGE_MICRO_X_SNAP);
    };

    // Sort edges by angle for each node (standard algorithm)
    // Tiebreaker: target node x position for deterministic ordering
    for (const node of nodes) {
      node.targets.sort((a, b) =>
        compare(
          angle(b.sourceNode, b.targetNode, orientation),
          angle(a.sourceNode, a.targetNode, orientation),
          a.targetNode.x,
          b.targetNode.x
        )
      );
    }

    for (const edge of edges) {
      const source = edge.sourceNode;
      const target = edge.targetNode;

      edge.points = [];

      if (orientation === 'vertical') {
        const sourceSeparation = Math.min(
          (source.width - stemSpaceSource) / source.targets.length,
          stemSpaceSource
        );
        const sourceEdgeDistance =
          source.targets.indexOf(edge) - (source.targets.length - 1) * 0.5;
        const sourceOffsetX = sourceSeparation * sourceEdgeDistance;

        const startPoint = { x: source.x, y: source.y };
        let currentPoint = startPoint;
        let smoothXs = null;
        let rowIndices = [];
        let rowIndexByRow = null;

        if (target.row - source.row > 1) {
          const preferredBaseX = source.x + sourceOffsetX;
          rowIndices = [];
          for (let i = source.row + 1; i < target.row; i += 1) {
            rowIndices.push(i);
          }
          rowIndexByRow = new Map(rowIndices.map((row, idx) => [row, idx]));

          let allowed = [{ min: -Infinity, max: Infinity }];
          for (let i = 0; i < rowIndices.length; i += 1) {
            const corridors = getRowCorridors(rows[rowIndices[i]]);
            if (!corridors.length) {
              allowed = [];
              break;
            }
            allowed = intersectIntervals(allowed, corridors);
            if (!allowed.length) break;
          }

          if (allowed.length) {
            const candidate = clampToIntervals(allowed, preferredBaseX);
            if (candidate.x !== null && candidate.dist <= EDGE_STRAIGHTEN_MAX_SHIFT) {
              smoothXs = rowIndices.map(() => candidate.x);
            }
          }

          if (!smoothXs) {
            smoothXs = buildSmoothCorridorPath(rowIndices, preferredBaseX, source.y, target.x, source, target);
          }
        }

        for (let i = source.row + 1; i < target.row; i += 1) {
          const firstNode = rows[i][0];
          const rowY = nodeTop(firstNode) - spaceY;
          const offsetY = firstNode.height + spaceY;

          if (smoothXs) {
            const rowIndex = rowIndexByRow ? rowIndexByRow.get(i) : null;
            const baseX = (rowIndex !== null && rowIndex !== undefined) ? smoothXs[rowIndex] : currentPoint.x;
            edge.points.push({
              x: baseX,
              y: rowY,
            });
            edge.points.push({
              x: baseX,
              y: rowY + offsetY,
            });
            currentPoint = {
              x: baseX,
              y: rowY + offsetY,
            };
            continue;
          }

          let nearestPoint = { x: nodeLeft(firstNode) - spaceX, y: firstNode.y };
          let nearestDistance = Infinity;
          let lockedToCurrent = false;

          const rowExtended = [
            { ...firstNode, x: Number.MIN_SAFE_INTEGER },
            ...rows[i],
            { ...firstNode, x: Number.MAX_SAFE_INTEGER },
          ];

          for (let j = 0; j < rowExtended.length - 1; j += 1) {
            const node = rowExtended[j];
            const nextNode = rowExtended[j + 1];
            const nodeGap = nodeLeft(nextNode) - nodeRight(node);

            if (nodeGap < minPassageGap) {
              continue;
            }

            const offsetX = Math.min(spaceX, nodeGap * 0.5);
            const sourceX = nodeRight(node) + offsetX;
            const sourceY = nodeTop(node) - spaceY;
            const targetX = nodeLeft(nextNode) - offsetX;
            const targetY = nodeTop(nextNode) - spaceY;

            if (!lockedToCurrent &&
                currentPoint.x >= sourceX &&
                currentPoint.x <= targetX) {
              nearestPoint = { x: currentPoint.x, y: sourceY };
              nearestDistance = 0;
              lockedToCurrent = true;
              break;
            }

            const candidatePoint = nearestOnLine(
              currentPoint.x,
              currentPoint.y,
              sourceX,
              sourceY,
              targetX,
              targetY
            );

            const distance = distance1d(currentPoint.x, candidatePoint.x);
            if (distance > nearestDistance) {
              break;
            }

            if (distance < nearestDistance) {
              nearestDistance = distance;
              nearestPoint = candidatePoint;
            }
          }

          edge.points.push({
            x: nearestPoint.x + sourceOffsetX,
            y: nearestPoint.y,
          });
          edge.points.push({
            x: nearestPoint.x + sourceOffsetX,
            y: nearestPoint.y + offsetY,
          });

          currentPoint = {
            x: nearestPoint.x,
            y: nearestPoint.y + offsetY,
          };
        }
      } else {
        const horizontalDist = Math.abs(target.x - source.x);
        const verticalDist = nodeTop(target) - nodeBottom(source);

        const needsIntermediatePoint = horizontalDist > 100 && verticalDist > MIN_VERTICAL_DIST_FOR_WAYPOINT;
        if (needsIntermediatePoint) {
          const shoulderY = nodeBottom(source) + verticalDist * 0.3;
          const shoulderX = source.x + (target.x - source.x) * 0.7;
          edge.points.push({ x: shoulderX, y: shoulderY });
        }

        const needsConvergence = target.sources.length > 1 || horizontalDist > 80;
        if (needsConvergence && horizontalDist > 5) {
          const convergeY = calculateConvergenceY(target, stemMinTarget);
          edge.points.push({ x: target.x, y: convergeY });
        }
      }
    }

    for (const node of nodes) {
      node.targets.sort((a, b) =>
        compare(
          angle(b.sourceNode, b.points[0] || b.targetNode, orientation),
          angle(a.sourceNode, a.points[0] || a.targetNode, orientation),
          a.targetNode.x,
          b.targetNode.x
        )
      );
      node.sources.sort((a, b) =>
        compare(
          angle(
            a.points[a.points.length - 1] || a.sourceNode,
            a.targetNode,
            orientation
          ),
          angle(
            b.points[b.points.length - 1] || b.sourceNode,
            b.targetNode,
            orientation
          ),
          a.sourceNode.x,
          b.sourceNode.x
        )
      );
    }

    for (const edge of edges) {
      const source = edge.sourceNode;
      const target = edge.targetNode;

      const sourceEdgeDistance =
        source.targets.indexOf(edge) - (source.targets.length - 1) * 0.5;
      const targetEdgeDistance =
        target.sources.indexOf(edge) - (target.sources.length - 1) * 0.5;

      const sourceOffsetY =
        stemUnit *
        source.targets.length *
        (1 - Math.abs(sourceEdgeDistance) / source.targets.length);

      const targetOffsetY =
        stemUnit *
        target.sources.length *
        (1 - Math.abs(targetEdgeDistance) / target.sources.length);

      let sourceStem, targetStem;

      if (orientation === 'vertical') {
        // Simplified to 2 points to reduce vertical segment
        // Use visible bottom (excluding shadow) for edge start point
        sourceStem = [
          {
            x: source.x,
            y: nodeVisibleBottom(source),
          },
          {
            x: source.x,
            y: nodeVisibleBottom(source) + 4,  // Minimal vertical offset
          },
        ];
        targetStem = [
          {
            x: target.x,
            y: nodeTop(target) - stemMinTarget - Math.min(targetOffsetY, stemMax),
          },
          {
            x: target.x,
            y: nodeTop(target) - stemMinTarget,
          },
          {
            x: target.x,
            y: nodeTop(target),
          },
        ];
      } else {
        sourceStem = [
          {
            x: nodeRight(source),
            y: source.y,
          },
          {
            y: source.y,
            x: nodeRight(source) + stemMinSource,
          },
          {
            y: source.y,
            x:
              nodeRight(source) +
              stemMinSource +
              Math.min(sourceOffsetY, stemMax),
          },
        ];
        targetStem = [
          {
            y: target.y,
            x:
              nodeLeft(target) - stemMinTarget - Math.min(targetOffsetY, stemMax),
          },
          {
            y: target.y,
            x: nodeLeft(target) - stemMinTarget,
          },
          {
            y: target.y,
            x: nodeLeft(target),
          },
        ];
      }

      const points = [...sourceStem, ...edge.points, ...targetStem];
      for (let i = 1; i < points.length; i += 1) {
        if (Math.abs(points[i].x - points[i - 1].x) <= EDGE_MICRO_X_SNAP) {
          points[i].x = points[i - 1].x;
        }
      }
      const simplifyCollinear = (pts) => {
        if (pts.length < 3) return pts;
        const simplified = [pts[0]];
        for (let i = 1; i < pts.length - 1; i += 1) {
          const prev = simplified[simplified.length - 1];
          const curr = pts[i];
          const next = pts[i + 1];
          const sameX = prev.x === curr.x && curr.x === next.x;
          const sameY = prev.y === curr.y && curr.y === next.y;
          if (sameX || sameY) {
            continue;
          }
          simplified.push(curr);
        }
        simplified.push(pts[pts.length - 1]);
        return simplified;
      };
      const simplifiedPoints = simplifyCollinear(points);

      const coordPrimary = orientation === 'vertical' ? 'y' : 'x';

      let pointMax = points[0][coordPrimary];

      for (const point of simplifiedPoints) {
        if (point[coordPrimary] < pointMax) {
          point[coordPrimary] = pointMax;
        } else {
          pointMax = point[coordPrimary];
        }
      }

      edge.points = simplifiedPoints;
      if (EDGE_EDGE_PENALTY > 0) {
        for (let i = 1; i < simplifiedPoints.length; i += 1) {
          const prev = simplifiedPoints[i - 1];
          const curr = simplifiedPoints[i];
          routedSegments.push({
            x1: prev.x,
            y1: prev.y,
            x2: curr.x,
            y2: curr.y,
            edgeId: edge.id,
            sourceId: edge.sourceNode.id,
            targetId: edge.targetNode.id,
          });
        }
      }
    }
  };

  // ============================================================================
  // GRAPH.JS - Entry point
  // ============================================================================

  // Configuration matching standard defaults
  const defaultOptions = {
    layout: {
      spaceX: 30,       // Increased to match Kedro-viz horizontal spacing
      spaceY: 140,      // Vertical spacing between nodes (matches Kedro-viz)
      layerSpaceY: 120, // Vertical spacing between layers (matches Kedro-viz)
      spreadX: 2.0,     // Reduced to tighten horizontal spacing
      padding: 70,      // Increased padding for more breathing room
      iterations: 30,   // More iterations for better convergence
    },
    routing: {
      spaceX: 50,       // Increased for more generous edge clearance
      spaceY: 40,       // Increased for smoother edge routing
      minPassageGap: 70, // Wider passages between nodes
      stemUnit: 5,      // Moderate stem spread
      stemMinSource: 0,
      stemMinTarget: 15, // Better arrowhead visibility
      stemMax: 6,       // Moderate gap at source
      stemSpaceSource: 8,
      stemSpaceTarget: 3,
    },
  };

  const addEdgeLinks = (nodes, edges) => {
    const nodeById = {};

    for (const node of nodes) {
      nodeById[node.id] = node;
      node.targets = [];
      node.sources = [];
    }

    for (const edge of edges) {
      edge.sourceNode = nodeById[edge.source];
      edge.targetNode = nodeById[edge.target];
      if (edge.sourceNode) {
        edge.sourceNode.targets.push(edge);
      }
      if (edge.targetNode) {
        edge.targetNode.sources.push(edge);
      }
    }
  };

  const bounds = (nodes, padding) => {
    const size = {
      min: { x: Infinity, y: Infinity },
      max: { x: -Infinity, y: -Infinity },
    };

    for (const node of nodes) {
      // Use node EDGES, not centers
      const left = nodeLeft(node);
      const right = nodeRight(node);
      const top = nodeTop(node);
      const bottom = nodeBottom(node);

      if (left < size.min.x) size.min.x = left;
      if (right > size.max.x) size.max.x = right;
      if (top < size.min.y) size.min.y = top;
      if (bottom > size.max.y) size.max.y = bottom;
    }

    size.width = size.max.x - size.min.x + 2 * padding;
    size.height = size.max.y - size.min.y + 2 * padding;
    size.min.x -= padding;
    size.min.y -= padding;

    return size;
  };

  /**
   * Generates a diagram of the given DAG.
   * Input nodes and edges are updated in-place.
   * Results are stored as `x, y` properties on nodes
   * and `points` properties on edges.
   * @param {Array} nodes The input nodes
   * @param {Array} edges The input edges
   * @param {Object=} layers The node layers if specified (unused for hypergraph)
   * @param {String=} orientation 'vertical' or 'horizontal'
   * @param {Object=} options The graph options
   * @returns {Object} The generated graph
   */
  const graph = (
    nodes,
    edges,
    layers = null,
    orientation = 'vertical',
    options = defaultOptions
  ) => {
    addEdgeLinks(nodes, edges);

    layout({
      nodes,
      edges,
      layers,
      orientation,
      ...options.layout,
    });
    routing({ nodes, edges, layers, orientation, ...options.routing });

    const size = bounds(nodes, options.layout.padding);
    nodes.forEach((node) => offsetNode(node, size.min));
    edges.forEach((edge) => offsetEdge(edge, size.min));

    return {
      nodes,
      edges,
      layers,
      size,
    };
  };

  // Export to global scope
  window.ConstraintLayout = {
    graph,
    defaultOptions,
  };

})();
